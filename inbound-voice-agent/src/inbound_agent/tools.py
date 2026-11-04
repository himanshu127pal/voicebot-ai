"""Function tools the LLM calls during the conversation.

Docstrings are the LLM's instructions for WHEN to call each tool — edit them to change
behaviour. Return strings tell the LLM what to say/do next. State lives in the session
userdata (`CallState`), so tools are plain functions rather than agent methods.
"""

# No `from __future__ import annotations` here: the tool decorator inspects the real
# RunContext annotation to tell the context argument apart from LLM-supplied ones.

import logging

from livekit.agents import RunContext, function_tool

from . import telephony
from .call_state import CallState, Outcome, usable_phone
from .redaction import has_sensitive_number
from .settings import get_settings

log = logging.getLogger(__name__)


def _quiet_if_asked(state: CallState, guidance: str) -> str | None:
    """A non-None tool result makes the LLM generate a second reply — after it has already
    asked something, that means two questions back to back. Returning None skips that
    reply, but only once the agent has asked: after a bare "Perfect." the follow-up reply
    is what asks the next question, and skipping it would leave the line silent."""
    return None if "?" in state.reply_text else guidance


@function_tool
async def share_details(context: RunContext[CallState], channel: str, contact: str = "") -> str:
    """The caller agreed to receive the details on WhatsApp or email instead of a phone call.

    Args:
        channel: "whatsapp" or "email".
        contact: The WhatsApp number or email address they gave. Leave empty for WhatsApp
            if they are happy with the number they are calling from.
    """
    state = context.userdata
    ch = (channel or "").strip().lower()

    if ch == "whatsapp":
        if not state.campaign.whatsapp_enabled:
            return "WhatsApp isn't available for this campaign — offer email or a callback."
        number = usable_phone(contact) or state.caller
        if not number:
            return "NOT SAVED — the caller ID is withheld. Ask for their WhatsApp number."
        state.whatsapp_number = number
        log.info("WhatsApp opt-in → …%s", number[-4:])
        return (f"Saved. Tell them the details will come on WhatsApp to the number ending "
                f"in {number[-4:]} shortly, then ask if there's anything else.")

    if ch == "email":
        if not state.campaign.email_enabled:
            return "Email isn't available for this campaign — offer WhatsApp or a callback."
        if "@" not in (contact or ""):
            return ("NOT SAVED — no valid email yet. Ask them to spell it slowly, then call "
                    "share_details again.")
        state.email = contact.strip()
        log.info("email opt-in recorded")
        return "Saved. Read the email back once to confirm, then ask if there's anything else."

    return "Unknown channel — use 'whatsapp' or 'email'."


@function_tool
async def opt_out(context: RunContext[CallState]) -> str:
    """The caller does not want any more calls from us — not interested, annoyed, wrong
    number, or 'stop calling me'. Puts their number on the do-not-call list."""
    context.userdata.do_not_call = True
    log.info("opt-out (do-not-call)")
    return ("Recorded. Reassure them their number is being removed and nobody will call "
            "them again about this unless they ask. Then say goodbye and call end_call.")


@function_tool
async def book_callback(context: RunContext[CallState], when: str) -> str:
    """The caller wants a call back at a particular time. Never guess the time.

    Args:
        when: The day and time in the caller's own words, e.g. "tomorrow after 5pm".
    """
    if not (when or "").strip():
        return "NOT SAVED — ask what day and time suits them, then call book_callback again."
    context.userdata.callback_when = when.strip()
    log.info("callback requested: %r", when)
    return "Saved. Confirm the time back to them in one short line."


@function_tool
async def mark_interested(context: RunContext[CallState], notes: str = "") -> str | None:
    """The caller is interested in what we called about.

    Args:
        notes: One short line for the specialist — what the caller cares about.
    """
    state = context.userdata
    state.interested = True
    state.add_note(notes)
    log.info("interested: %r", notes)
    if state.campaign.questions:
        return _quiet_if_asked(state, "Recorded. Now the QUESTIONS: first save_answer anything "
                               "they've already told you, then ask the first one still "
                               "unanswered.")
    return _quiet_if_asked(state, "Recorded. Now offer the next step: a specialist callback, "
                           "or a transfer right now.")


@function_tool
async def save_answer(context: RunContext[CallState], field: str, answer: str) -> str | None:
    """Save the caller's answer to one of the QUESTIONS, in their own words, as soon as
    they give it. One call per question. Never guess an answer.

    Args:
        field: The question's field name exactly as listed under QUESTIONS, e.g. "move_date".
        answer: What the caller said, in their own words.
    """
    state = context.userdata
    fields = [q.field for q in state.campaign.questions]
    key = (field or "").strip().lower()
    if key not in fields:
        return (f"NOT SAVED — unknown field. Use one of: {', '.join(fields)}."
                if fields else "This campaign has no questions — carry on.")
    if not (answer or "").strip():
        return "NOT SAVED — no answer yet. Ask again, or move on if they'd rather not say."
    if has_sensitive_number(answer):
        return ("NOT SAVED — that looks like a card or account number. Do not repeat it. "
                "Say: 'Please don't read that out — we never take card or bank details "
                "over the phone.'")

    state.answers[key] = answer.strip()
    log.info("answer %s = %r", key, answer)
    pending = [q.field for q in state.campaign.questions if q.field not in state.answers]
    if pending:
        return _quiet_if_asked(state, f"Saved. Not yet saved: {', '.join(pending)} — anything "
                               "the caller already told you, save it instead of asking; "
                               "then ask the next one.")
    return _quiet_if_asked(state, "Saved. All questions answered — move on to the next step.")


@function_tool
async def transfer_to_human(context: RunContext[CallState]) -> str:
    """Connect the caller to a real person. Say one short line like "Sure, let me put you
    through" BEFORE calling this."""
    state = context.userdata
    uri = state.campaign.human_transfer_uri or get_settings().human_transfer_uri
    if not uri:
        return ("No live team is available right now. Apologise briefly, offer a callback and "
                "ask what time suits them (book_callback).")

    await context.wait_for_playout()
    try:
        await telephony.transfer_caller(uri)
    except Exception as e:
        log.error("transfer to %s failed: %s", uri, e)
        return "The transfer didn't connect. Apologise briefly and offer a callback instead."
    state.transferred = True
    log.info("transferred → %s", uri)
    return "Transferred. Say nothing more."


@function_tool
async def set_disposition(context: RunContext[CallState], outcome: str, reason: str = "") -> str:
    """Record the outcome when none of the other tools fits.

    Args:
        outcome: One of NOT_INTERESTED, WRONG_NUMBER, INFO_ONLY.
        reason: One short line in the caller's words, e.g. "no longer moving".
    """
    value = (outcome or "").strip().upper()
    if value not in Outcome.MANUAL:
        return f"Use one of {', '.join(Outcome.MANUAL)} — or the matching tool."
    state = context.userdata
    state.outcome = value
    state.add_note(reason)
    log.info("disposition → %s (%s)", value, reason)
    return "Recorded."


@function_tool
async def end_call(context: RunContext[CallState]) -> None:
    """Hang up. Call ONLY after a short goodbye, when the conversation is finished."""
    # RunContext.wait_for_playout(), never current_speech.wait_for_playout(): this tool
    # owns the current speech handle, so waiting on it deadlocks.
    await context.wait_for_playout()
    log.info("ending call (agent-initiated)")
    await telephony.hang_up()


ALL_TOOLS = [
    share_details,
    opt_out,
    book_callback,
    mark_interested,
    save_answer,
    transfer_to_human,
    set_disposition,
    end_call,
]
