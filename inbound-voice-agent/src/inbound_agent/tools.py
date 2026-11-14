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
from .call_state import CallState, Outcome
from .campaigns import Campaign
from .leads import Lead
from .phones import (
    PhoneInfo,
    check_phone,
    gives_digits,
    heard_digits,
    matches_what_was_said,
    means_caller_line,
    means_calling_number,
)
from .postcodes import BAD_FORMAT, FOUND, NOT_FOUND, lookup_postcode, spoken_postcode
from .redaction import has_sensitive_number
from .settings import get_settings
from .validation import is_full_name, normalise_email, spell_out_email

log = logging.getLogger(__name__)

# How many times a detail is rejected before the agent takes what it has and moves on.
# Pushing harder than this annoys callers more than a missing detail costs.
MAX_NAME_REJECTIONS = 2
MAX_EMAIL_REJECTIONS = 2
MAX_POSTCODE_CHECKS = 3


def _a_mobile(c: Campaign) -> str:
    return "a UK mobile, starting with 07" if c.country == "GB" else "a mobile number"


def _number_the_caller_said(state: CallState, raw: str) -> tuple[PhoneInfo, str]:
    """(number, "") if `raw` is a complete, valid number the caller really said — or
    (number, refusal) telling the model what to do instead. The model must never pad or
    'fix' a number: what's saved is exactly what was said."""
    phone = check_phone(raw, state.campaign.country)
    turns = state.recent_caller_turns()
    if phone.valid and matches_what_was_said(phone, turns):
        return phone, ""
    said = next((d for d in map(heard_digits, reversed(turns)) if d), "")
    heard = f" The caller said {len(said)} digits: {' '.join(said)}." if said else ""
    if phone.valid:
        return phone, ("NOT SAVED — that isn't the number the caller said. Never add, drop or "
                       f"change digits.{heard} Read back exactly what they said and ask for "
                       "any missing digits.")
    full = (" A UK mobile is 11 digits starting with 07; a landline is 10 or 11 digits with "
            "its area code." if state.campaign.country == "GB" else "")
    return phone, (f"NOT SAVED — that isn't a complete phone number.{heard}{full} Ask them to "
                   "say the whole number again. Never add digits yourself.")


def _number_they_mean(state: CallState, contact: str) -> PhoneInfo:
    """No digits — 'yes', 'same number', 'I already told you': the best number they
    confirmed earlier, unless they mean the line they're calling from."""
    if means_caller_line(contact) or not state.contact_phone.valid:
        return state.caller_phone
    return state.contact_phone


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
        contact: The WhatsApp number or email address they gave, exactly as said. For
            WhatsApp, leave it empty when they mean a number we already have ("yes",
            "same number", "I already told you", "the one I'm calling from").
    """
    state = context.userdata
    ch = (channel or "").strip().lower()
    carry_on = ("then carry on with the next step of the script, or if there is none, ask "
                "if there's anything else.")

    if ch == "whatsapp":
        if not state.campaign.whatsapp_enabled:
            return "WhatsApp isn't available for this campaign — offer email or a callback."
        if gives_digits(contact):
            phone, refusal = _number_the_caller_said(state, contact)
            if refusal:
                return refusal
        else:
            phone = _number_they_mean(state, contact)
            if not phone.valid:
                return ("NOT SAVED — we don't have their number (caller ID withheld). Ask for "
                        f"{_a_mobile(state.campaign)} for WhatsApp.")
        if not phone.can_whatsapp:
            return (f"NOT SAVED — the number ending in {phone.last4} is {phone.describe()}, "
                    "and WhatsApp only works on a mobile. Ask: \"Do you have a mobile we "
                    "could send them to on WhatsApp?\" If not, offer email once.")
        state.whatsapp_number = phone.e164
        log.info("WhatsApp opt-in → …%s (%s)", phone.last4, phone.kind)
        return (f"Saved. Tell them the details will come on WhatsApp to the number ending "
                f"in {phone.last4}, {carry_on}")

    if ch == "email":
        if not state.campaign.email_enabled:
            return "Email isn't available for this campaign — offer WhatsApp or a callback."
        if state.whatsapp_number:
            return ("NOT SAVED — they've already agreed to WhatsApp, so don't ask for an "
                    "email too. Carry on with the next step.")
        email = normalise_email(contact)
        if not email:
            state.email_rejections += 1
            if state.email_rejections > MAX_EMAIL_REJECTIONS:
                state.add_note(f"email unclear: {contact}")
                return ("NOT SAVED — the email still isn't clear. Don't ask again and don't "
                        "offer post or any other way: say \"No problem\", then carry on.")
            return ("NOT SAVED — that isn't a complete email address. Ask them to spell it "
                    "slowly, like \"j dot smith at gmail dot com\", then call share_details "
                    "again with what they said.")
        state.email = email
        log.info("email opt-in recorded")
        return (f"Saved. Read it back once as \"{spell_out_email(email)}\" — if they correct "
                f"it, call share_details again — {carry_on}")

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

    answer = answer.strip()
    if key == "full_name" and not is_full_name(answer):
        state.name_rejections += 1
        if state.name_rejections <= MAX_NAME_REJECTIONS:
            return ("NOT SAVED — that's only part of their name; we need their first name AND "
                    "surname. Ask politely, e.g. \"Thank you — and your surname, please?\", "
                    "then save the full name.")
        answer += " (surname not given)"
    if key == "address" and state.campaign.checks_uk_address:
        on_file = (" If they said the address ON FILE is still right, call confirm_address "
                   "instead." if state.lead and state.lead.has_address else "")
        return ("NOT SAVED — for the address, call check_postcode with the postcode, then "
                "save_address with the first line. (Lives outside the UK → follow UK ONLY; "
                f"no need to save an address.){on_file}")
    if key == "contact_number":
        return _save_contact_number(state, answer)

    state.answers[key] = answer
    log.info("answer %s = %r", key, answer)
    return _quiet_if_asked(state, _next_question(state, "Saved."))


def _next_question(state: CallState, done: str) -> str:
    pending = [q.field for q in state.campaign.questions if q.field not in state.answers]
    if pending:
        return (f"{done} Not yet saved: {', '.join(pending)} — anything the caller already "
                "told you, save it instead of asking; then ask the next one.")
    return f"{done} All questions answered — move on to the next step."


def _save_contact_number(state: CallState, answer: str) -> str | None:
    """Saves the number AND its type (mobile / landline), worked out from the number
    itself — the agent never has to ask "is that a mobile or a landline?"."""
    c = state.campaign
    said_digits = gives_digits(answer)
    if said_digits:
        phone, refusal = _number_the_caller_said(state, answer)
        if refusal:
            return refusal
    elif means_calling_number(answer):
        phone = state.caller_phone
        if not phone.valid:
            return "NOT SAVED — their number is withheld. Ask for the best number to reach them."
    else:
        return ("NOT SAVED — no number yet. If they said the number they're calling from is "
                "fine, save \"calling number\"; otherwise ask for the best number.")
    state.contact_phone = phone
    state.answers["contact_number"] = phone.national or phone.e164
    state.answers["number_type"] = phone.kind
    log.info("contact number …%s (%s)", phone.last4, phone.kind)
    whatsapp = (" It's a mobile, so WhatsApp can go to it — later, \"yes\", \"same number\" "
                "or \"I already told you\" means this number." if phone.can_whatsapp else
                f" It's NOT a mobile, so WhatsApp can't go to it — for WhatsApp, ask for "
                f"{_a_mobile(c)}.")
    read_back = ("" if not said_digits else
                 f" If you haven't yet, read it back once as \"{phone.spoken}\".")
    return _quiet_if_asked(state, _next_question(
        state, f"Saved — it's {phone.describe()}; don't ask whether it's a mobile or a "
               f"landline.{whatsapp}{read_back}"))


@function_tool
async def check_postcode(context: RunContext[CallState], postcode: str) -> str:
    """Check the caller's UK postcode is real and find its area. Call it as soon as they
    say their postcode — before asking for the first line of the address. Before calling,
    say only a short "Thank you" — the result tells you what to read back.

    Args:
        postcode: The postcode exactly as they said it, e.g. "SW1A 1AA" or
            "S W one A, one A A".
    """
    state = context.userdata
    state.postcode_checks += 1
    result = await lookup_postcode(postcode, get_settings().postcode_api_url)
    log.info("postcode check %r → %s %s", postcode, result.status, result.postcode)
    next_step = "ask for the first line of their address (house number or name, and street)"

    if result.status == FOUND:
        if not result.in_uk:
            return (f"That postcode is in {result.country}, which is outside the UK — follow "
                    "UK ONLY.")
        state.postcode, state.postcode_area = result.postcode, result.place
        state.postcode_verified = True
        return (f"Real postcode: {result.postcode}, {result.place}. Read it back once as "
                f"\"{spoken_postcode(result.postcode)}, in {result.area or result.place}\" and "
                f"ask if that's right. Yes → {next_step}. No → ask them to say it again and "
                "call check_postcode again.")

    if state.postcode_checks >= MAX_POSTCODE_CHECKS or result.status not in (NOT_FOUND,
                                                                               BAD_FORMAT):
        # Out of tries, or the checker is down: take what they said and keep going.
        state.postcode = result.postcode or postcode.strip().upper()
        state.postcode_area = ""
        state.postcode_verified = False
        return (f"Couldn't verify it — don't ask again. Read it back once as "
                f"\"{spoken_postcode(state.postcode)}\", then {next_step}.")

    if result.status == NOT_FOUND:
        area_hint = ""
        if result.outcode_area:
            outward = result.postcode.split()[0]
            area_hint = (f" The first half, {spoken_postcode(outward)}, is in "
                         f"{result.outcode_area} — you can ask if that's their area.")
        return ("NOT FOUND — that postcode doesn't exist, so a letter was probably misheard. "
                "Apologise and ask them to say it again slowly, one letter at a time (\"S for "
                f"Sugar\" helps).{area_hint} Then call check_postcode again.")
    return ("NOT A POSTCODE — UK postcodes look like \"SW1A 1AA\". Ask them to say it again, "
            "one letter at a time, then call check_postcode again.")


@function_tool
async def save_address(context: RunContext[CallState], first_line: str) -> str | None:
    """Save the first line of the address once check_postcode has dealt with the postcode.
    Save it as they said it — no need to read the street back.

    Args:
        first_line: House number or name, and street, e.g. "10 Downing Street".
    """
    state = context.userdata
    if not state.postcode:
        return "NOT SAVED — check the postcode first with check_postcode."
    line = " ".join((first_line or "").split())
    if not line:
        return "NOT SAVED — ask for the first line of their address (house number and street)."
    state.answers["address"] = ", ".join(
        p for p in (line, state.postcode_area, state.postcode) if p)
    log.info("address saved (postcode %s, verified=%s)", state.postcode,
             state.postcode_verified)
    return _quiet_if_asked(state, _next_question(state, "Address saved."))


@function_tool
async def confirm_address(context: RunContext[CallState]) -> str | None:
    """The caller said the address we have ON FILE is still right. Call it as soon as they
    say yes — never ask for their postcode after that."""
    state = context.userdata
    lead = state.lead
    if not (lead and lead.has_address):
        return "NOT SAVED — there's no address on file. Ask for their postcode instead."
    state.postcode, state.postcode_area = lead.postcode, lead.town
    state.address_on_file = True
    state.answers["address"] = lead.address
    log.info("address on file confirmed (postcode %s)", lead.postcode)
    return _quiet_if_asked(state, _next_question(state, "Address confirmed."))


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
ADDRESS_TOOLS = [check_postcode, save_address]


def asks_address(campaign: Campaign) -> bool:
    return any(q.field == "address" for q in campaign.questions)


def tools_for(campaign: Campaign, lead: Lead | None = None) -> list:
    tools = ALL_TOOLS + ADDRESS_TOOLS if campaign.checks_uk_address else list(ALL_TOOLS)
    if lead and lead.has_address and asks_address(campaign):
        tools.append(confirm_address)
    return tools
