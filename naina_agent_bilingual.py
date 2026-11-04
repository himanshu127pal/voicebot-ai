"""
naina_agent_bilingual.py — BILINGUAL, production variant of naina_agent_sarvam.py.

Same agent as naina_agent_sarvam.py, with THREE changes so the bot answers in the
CALLER's language and always speaks numbers in English:

  1. STT language AUTO-DETECT (Saaras v3 `language="unknown"`) instead of a
     pinned hi-IN. A pinned language transliterates spoken English into
     Devanagari, so the LLM thinks the caller spoke Hindi and replies in Hindi.
     Auto-detect preserves the real language → the "reply in caller's language"
     prompt actually works.

  2. SANDWICHED STATIC language rule (no per-turn hook). The system prompt states
     the language rule as HARD RULE 1 AND repeats it as the LAST block (with few-shot
     examples), so the model matches the caller's MOST RECENT message and doesn't
     drift to the conversation's earlier language. Static = keeps LiveKit's
     preemptive-generation head-start intact (no per-turn context mutation → lower
     latency). Can drift more than a per-turn hook; the deterministic fallback (keyed
     off Sarvam's STT `language` field) is documented in DEVELOPER_GUIDE §6.1-detail.

  3. NUMBERS → ENGLISH WORDS. The LLM writes numbers as plain digits (prompt RULE 3);
     `tts_node` converts digit runs to spoken English words (num2words) just before TTS
     — always English regardless of sentence language. Runs on the LLM OUTPUT, so it
     adds no latency (no preemptive-gen hit). Requires: pip install num2words.
     Also tuned TTS streaming (min_buffer_size=30, max_chunk_length=60) for faster
     time-to-first-word on natural-length replies.

Everything else (disposition, voicemail, end_call, silence nudge, max-duration,
prewarm, webhook) is identical to naina_agent_sarvam.py.

NOTE: run EITHER this OR naina_agent_sarvam.py, not both — they share the same
agent_name ("naina-bot"), so LiveKit would load-balance between them.

>>> Full docs: see DEVELOPER_GUIDE.md (§6 = prompt guide, §6.1 = language,
    §6.2 = numbers). <<<

Install: pip install "livekit-agents[google,silero]" livekit-plugins-sarvam \
         python-dotenv num2words
Run:     ./.venv/bin/python naina_agent_bilingual.py download-files   # VAD model
         ./.venv/bin/python naina_agent_bilingual.py start
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
load_dotenv()   # load .env at import so the per-job subprocess gets the keys too

from livekit import api
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
)
from livekit.plugins import sarvam, google, silero

log = logging.getLogger("naina")


# ── Number → spoken ENGLISH words (for TTS) ───────────────────────────────────
# The LLM is told to emit numbers as plain digits ("499"); we convert them to
# English words ("four hundred ninety-nine") in the TTS text stream so numbers are
# ALWAYS spoken in English, whatever language the sentence is in. This runs on the
# LLM OUTPUT (after generation), so it does NOT touch LiveKit's preemptive-generation
# head-start — it's a local string transform (microseconds), no network, no added
# turn latency. Requires: pip install num2words
try:
    from num2words import num2words as _num2words
except ImportError:            # fail soft — speak raw digits rather than crash
    _num2words = None
    log.warning("[naina] num2words not installed — numbers will be spoken as digits. "
                "Run: pip install num2words")

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")     # 499   1,000   3.5
_TRAILING_NUMBER_RE = re.compile(r"[\d,\.]*\d$")   # incomplete number at a chunk's end


def _number_to_words(num_str: str) -> str:
    """'499' -> 'four hundred ninety-nine'. Long runs, leading-zero, or decimals are
    read digit-by-digit (phone numbers, OTPs, IDs). English only, by design."""
    if _num2words is None:
        return num_str
    clean = num_str.replace(",", "")
    try:
        if "." in clean:
            int_part, frac = clean.split(".", 1)
            left = _number_to_words(int_part) if int_part else "zero"
            return left + " point " + " ".join(_num2words(int(d)) for d in frac)
        # >6 digits or a leading zero → not a quantity (phone/OTP/ID); speak each digit
        if len(clean) > 6 or (len(clean) > 1 and clean[0] == "0"):
            return " ".join(_num2words(int(d)) for d in clean)
        return _num2words(int(clean))
    except Exception:
        return num_str


def _replace_numbers(text: str) -> str:
    # pad with spaces so a number glued to Hindi text detaches cleanly for the voice
    return _NUMBER_RE.sub(lambda m: " " + _number_to_words(m.group()) + " ", text)


async def _convert_numbers_in_stream(text_stream):
    """Streaming filter: convert digit runs → English words as text flows to TTS.
    A trailing digit run is held back (it may continue in the next chunk) so a number
    split across chunk boundaries isn't converted in halves. Near-zero latency."""
    buf = ""
    async for chunk in text_stream:
        buf += chunk.translate(_DEVANAGARI_DIGITS)
        m = _TRAILING_NUMBER_RE.search(buf)
        if m and m.start() > 0:
            emit, buf = buf[:m.start()], buf[m.start():]
        elif m:                       # whole buffer is an in-progress number → hold
            emit, buf = "", buf
        else:
            emit, buf = buf, ""
        if emit:
            yield _replace_numbers(emit)
    if buf:
        yield _replace_numbers(buf)


# Default Hindi; auto-detect handles the rest. Used as the Hindi TTS voice lang.
LANG = os.getenv("NAINA_LANG", "hi-IN")

# Hard cap on call length (billing safety). A stuck/looping call is force-hung-up
# after this many seconds so it can't rack up STT/LLM/TTS + telephony charges.
MAX_CALL_SECONDS = float(os.getenv("MAX_CALL_SECONDS", "300"))   # default 5 minutes


# ── Disposition mapping (inlined) ─────────────────────────────────────────────
FLOW_OUTCOME_TO_CODE = {
    "BOOKED": "Sale",
    "CALLBACK": "Callback Requested",
    "NOT_INTERESTED": "Not Interested",
    "DNC": "DND",
    "OUT_OF_ZONE": "Not Interested",
    "VOICEMAIL": "Voicemail",
    "DROPPED": "Follow-up",
    "IDLE_DROPPED": "Follow-up",
}


def map_outcome_to_code(outcome: str) -> str:
    return FLOW_OUTCOME_TO_CODE.get(outcome, "Follow-up")


# ── Disposition webhook ───────────────────────────────────────────────────────
# DEVELOPERS: edit build_disposition_payload() freely to match what your dialer /
# CRM expects — add, remove, or rename fields. Nothing else needs to change.
# The endpoint is DISPOSITION_WEBHOOK_URL in .env (optional bearer token via
# DISPOSITION_WEBHOOK_TOKEN). If the URL is unset, we just log the payload.

def build_disposition_payload(prospect: dict, outcome: str, code: str,
                              started_at: datetime, ended_at: datetime) -> dict:
    """The JSON body POSTed to your backend at end of call. EDIT ME."""
    return {
        "call_id":          prospect.get("call_id"),
        "agent":            "naina-bot",
        "prospect": {
            "name":     prospect.get("name"),
            "phone":    prospect.get("phone"),
            "campaign": prospect.get("campaign"),
        },
        "outcome":          outcome,   # raw flow outcome, e.g. "BOOKED"
        "disposition_code": code,      # mapped backend code, e.g. "Sale"
        "started_at":       started_at.isoformat(),
        "ended_at":         ended_at.isoformat(),
        "duration_seconds": round((ended_at - started_at).total_seconds(), 1),
    }


async def post_disposition(payload: dict) -> None:
    """POST the disposition to your backend. Fails soft — never raises, so a
    webhook outage can't break call teardown."""
    url = os.getenv("DISPOSITION_WEBHOOK_URL")
    if not url:
        log.info(f"[naina] DISPOSITION_WEBHOOK_URL unset — would POST: {payload}")
        return
    headers = {}
    token = os.getenv("DISPOSITION_WEBHOOK_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = await r.text()
                log.info(f"[naina] disposition POST {url} -> {r.status} {body[:200]}")
    except Exception as e:
        log.warning(f"[naina] disposition POST failed: {e}")


class Naina(Agent):
    def __init__(self, prospect: dict):
        name = prospect.get("name")
        # Name guard: only give the model a name if we actually have one,
        # otherwise it invents one (e.g. "राम").
        name_clause = (
            f"The prospect's name is {name}; address them by it."
            if name else
            "You do NOT know the caller's name — do not use or invent one."
        )
        super().__init__(
            # TODO: refine with your real sales script / persona.
            # Prompt is SANDWICHED for language adherence: the language rule is stated
            # at the TOP (rule #1) AND repeated as the very LAST block with few-shot
            # examples. LLMs weight the start (primacy) and end (recency) of the prompt
            # most; after several same-language turns the conversation history pulls the
            # model to keep replying in that language, so the rule must WIN the recency
            # fight — hence it is the last thing the model reads every turn. Sections are
            # kept short so the whole prompt stays well under the "attention collapse"
            # threshold. (See DEVELOPER_GUIDE.md → language handling.)
            instructions=(
                # >>> DEVELOPER: replace "GrowEasy" and the one-line offer below with your
                # real company + product pitch (DEVELOPER_GUIDE §6.3). It appears here AND
                # in HARD RULES 5–6 — change all of them. Keep it to one line. <<<
                "You are Naina, a warm, confident sales agent for GrowEasy — an "
                "easy-to-use marketing platform that helps small businesses reach more "
                "customers online.\n\n"
                "## HARD RULES (never break):\n"
                "1. LANGUAGE: reply in the SAME language as the caller's most recent REAL "
                "message — Latin letters → English; Devanagari → Hindi. Match them every "
                "turn; never carry over an earlier turn's language. EXCEPTION: a short "
                "acknowledgment on its own (ok, okay, yeah, haan, hmm, achha, अच्छा, ठीक है) "
                "is NOT a language switch — ignore it and keep the language of the caller's "
                "last full sentence, whatever script it was typed in.\n"
                "2. Speak naturally and warmly — usually one or two short sentences per "
                "turn. Never clipped, pushy, or a long monologue.\n"
                "3. NUMBERS: write every number as plain digits (499, 2025) — never "
                "spell them out, never use Hindi/Devanagari numerals. The system speaks "
                "them aloud in English automatically.\n"
                "4. YOU lead: end every turn with your next point or a question — never dead air.\n"
                "5. STAY ON TOPIC: you represent GrowEasy and ONLY discuss GrowEasy and its "
                "offer. If the caller asks anything unrelated (general knowledge, news, "
                "politics, other companies, personal questions, math, jokes) politely decline "
                "— e.g. \"I'm only able to help with GrowEasy, but…\" — and steer back. Real "
                "questions about GrowEasy or the offer ARE on topic — answer those normally.\n"
                "6. NEVER INVENT FACTS: state only things about GrowEasy that are written in "
                "these instructions. If you don't know a specific detail (exact price, a "
                "feature, a policy), say you'll have someone follow up — never make it up.\n\n"
                "## STYLE:\n"
                "Pitch ONE benefit per turn, in small steps. If the caller asks a side "
                "question, answer briefly then return to your point. If they're silent or "
                "just say 'ok'/'haan', continue yourself.\n\n"
                "## AGENDA:\n"
                "greet → confirm the right person → check they're a business owner → pitch "
                "GrowEasy → handle questions briefly → ask if interested.\n\n"
                "## TOOLS:\n"
                "- Outcome reached → set_disposition (BOOKED, CALLBACK, NOT_INTERESTED, "
                "DNC, OUT_OF_ZONE).\n"
                "- Answering machine / voicemail (recorded greeting, 'leave a message after "
                "the tone', a monologue that ignores you) → call voicemail_detected immediately.\n"
                "- Finished (outcome reached or caller wants to go) → short one-sentence "
                "goodbye, then end_call. Always set_disposition first.\n\n"
                f"{name_clause}\n\n"
                # ── FINAL BLOCK: language rule repeated last (recency) + few-shot ──
                "## ⚠️ MOST IMPORTANT — CHECK EVERY SINGLE TURN:\n"
                "Reply in the caller's CURRENT language, NOT the language you just used. "
                "Look ONLY at their latest message:\n"
                "- Caller: \"Yes, I run a small shop.\"  → You reply in ENGLISH.\n"
                "- Caller: \"हां जी, बताइए।\"  → You reply in HINDI.\n"
                "- Caller was speaking Hindi but now writes in English → you switch to "
                "ENGLISH this same turn (and vice-versa).\n"
                "- BUT a lone short acknowledgment (\"achha\", \"ok\", \"haan\", \"hmm\", "
                "\"ठीक है\") is NOT a switch — keep the language of the last full sentence.\n"
                "Never continue in the previous turn's language out of habit.\n\n"
                "NUMBERS — write every number as plain digits, in ANY language "
                "(the system voices them in English for you):\n"
                "- Hindi sentence: \"यह प्लान सिर्फ 499 रुपये का है।\"\n"
                "- English sentence: \"It's just 499 rupees.\"\n"
                "Never spell numbers out and never use Devanagari numerals (४९९)."
            )
        )
        self._prospect = prospect
        self.outcome: str = "DROPPED"

    @function_tool
    async def set_disposition(self, context: RunContext, outcome: str):
        """Record the final call outcome.
        outcome: one of BOOKED, CALLBACK, NOT_INTERESTED, DNC, OUT_OF_ZONE."""
        self.outcome = outcome
        log.info(f"[naina] disposition set → {outcome}")
        return "recorded"

    @function_tool
    async def end_call(self, context: RunContext):
        """Hang up the call. Call this ONLY after you have said a short goodbye and the
        conversation is genuinely finished (an outcome was reached, or the caller wants
        to go). Always set_disposition first if you haven't."""
        # Wait for the goodbye to finish playing before dropping the line.
        # Use RunContext.wait_for_playout() — NOT current_speech.wait_for_playout(),
        # which deadlocks: this tool owns that speech handle (circular wait).
        await context.wait_for_playout()
        log.info("[naina] ending call (agent-initiated)")
        jctx = get_job_context()
        await jctx.api.room.delete_room(api.DeleteRoomRequest(room=jctx.room.name))

    @function_tool
    async def voicemail_detected(self, context: RunContext):
        """Call this the MOMENT you realize you've reached an answering machine or
        voicemail, NOT a live person — e.g. a recorded greeting, "please leave a message
        after the tone", "the number you are calling is not available", or a monologue
        that never actually responds to you. Do NOT use this for a real person who is
        just talking a lot."""
        self.outcome = "VOICEMAIL"
        log.info("[naina] answering machine / voicemail detected — hanging up")
        jctx = get_job_context()
        await jctx.api.room.delete_room(api.DeleteRoomRequest(room=jctx.room.name))

    async def tts_node(self, text, model_settings):
        """Rewrite digit runs in the reply to spoken ENGLISH words just before TTS,
        so numbers are always voiced in English regardless of the sentence language.
        Delegates to the default TTS node with a transformed text stream."""
        async for frame in Agent.default.tts_node(
            self, _convert_numbers_in_stream(text), model_settings
        ):
            yield frame


async def entrypoint(ctx: JobContext):
    await ctx.connect()

    participant = await ctx.wait_for_participant()
    attrs = participant.attributes
    prospect = {
        "name":     attrs.get("dialer.prospect_name"),
        "phone":    attrs.get("dialer.phone"),
        "campaign": attrs.get("dialer.campaign"),
        "call_id":  attrs.get("dialer.call_id"),
    }
    log.info(f"[naina] call room={ctx.room.name} prospect={prospect}")
    call_started = datetime.now(timezone.utc)

    agent = Naina(prospect)

    session = AgentSession(
        # STT: Saaras v3 with AUTO-DETECT so the caller's real language (and script)
        # is preserved — English stays English, so the LLM replies in English.
        stt=sarvam.STT(model="saaras:v3", mode="transcribe", language="unknown"),
        llm=google.LLM(model="gemini-2.5-flash"),   # text LLM (NOT realtime)
        # Single TTS voice. Streaming latency tuning: lower min_buffer_size and
        # max_chunk_length so Sarvam starts speaking sooner and streams long replies
        # fast — lets us keep natural (1-2 sentence) replies without a latency hit.
        tts=sarvam.TTS(target_language_code=LANG, output_audio_codec="linear16",
                       min_buffer_size=30, max_chunk_length=60),
        vad=ctx.proc.userdata["vad"],   # preloaded in prewarm() — not per-call
        # ── Noise robustness (Layer 1, DEVELOPER_GUIDE §11.1) ──────────────────
        # Background noise is rejected at the VAD level (activation_threshold raised in
        # prewarm), NOT via a word-count interruption gate: Sarvam STT only emits its
        # transcript at END-of-utterance (no interim words), so min_interruption_words>0
        # can't fire until the caller finishes their sentence. We therefore keep LiveKit's
        # DEFAULT interruption behaviour (immediate on VAD speech) — the old
        # min_interruption_words / min_interruption_duration kwargs were left at their
        # defaults and are omitted here (they're deprecated in favour of TurnHandlingOptions;
        # since we only wanted the defaults, dropping them is a no-op that silences the
        # deprecation warning). See DEVELOPER_GUIDE §11.1.
    )

    # ── Disposition: POST the outcome to your backend, exactly once ─────────────
    # Timing matters and the two hangup paths tear down very differently:
    #   • Caller hangup  → the worker lingers ~20s before the process exits.
    #   • Bot hangup     → end_call deletes the room, so teardown is near-INSTANT (~30ms).
    # We start the POST as soon as anyone leaves the room (participant_disconnected) so it
    # fires immediately. But a bare fire-and-forget task gets KILLED on the fast bot-hangup
    # path before the ~700ms HTTP call finishes (confirmed: webhook never arrived). So the
    # shutdown callback AWAITS the same task — the framework waits for shutdown callbacks,
    # which keeps the process alive until the POST actually completes. Started once, awaited
    # to completion, delivered on both paths.
    _disposition = {"task": None}

    async def _run_disposition():
        code = map_outcome_to_code(agent.outcome)
        payload = build_disposition_payload(
            prospect=prospect,
            outcome=agent.outcome,
            code=code,
            started_at=call_started,
            ended_at=datetime.now(timezone.utc),
        )
        log.info(f"[naina] call ended → {agent.outcome} → {code}")
        await post_disposition(payload)

    def _start_disposition_once():
        # Kick off the POST exactly once; return the task so shutdown can await it.
        if _disposition["task"] is None:
            _disposition["task"] = asyncio.create_task(_run_disposition())
        return _disposition["task"]

    async def _on_shutdown():
        # Await the POST so the process cannot exit before the webhook is delivered
        # (critical for the fast bot-hangup teardown).
        try:
            await _start_disposition_once()
        except Exception as e:
            log.warning(f"[naina] disposition await failed: {e}")

    ctx.add_shutdown_callback(_on_shutdown)

    @ctx.room.on("participant_disconnected")
    def _on_participant_left(participant):
        # Caller hung up (or bot deleted the room) → start the POST now.
        _start_disposition_once()

    await session.start(room=ctx.room, agent=agent)

    # ── Max call duration guard (billing safety) ───────────────────────────────
    # Force-hang up any call that runs past MAX_CALL_SECONDS so a stuck/looping
    # call can't rack up charges. Outcome stays whatever it was (default DROPPED →
    # Follow-up, i.e. retryable).
    async def _max_duration_guard():
        try:
            await asyncio.sleep(MAX_CALL_SECONDS)
            log.warning(f"[naina] max call duration {MAX_CALL_SECONDS:.0f}s reached — force hangup")
            try:
                await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            except Exception as e:
                log.warning(f"[naina] max-duration hangup failed: {e}")
        except asyncio.CancelledError:
            pass
    asyncio.create_task(_max_duration_guard())

    # ── Silence-timeout nudge ──────────────────────────────────────────────────
    # If the caller goes quiet after the bot finishes speaking, don't sit in dead
    # air — prompt the bot to continue its agenda itself. Armed when the agent
    # starts listening, cancelled when the user speaks or the agent speaks.
    NUDGE_AFTER = 10.0     # seconds of silence before nudging (gentle — allow natural pauses)
    MAX_NUDGES = 3         # don't nag forever on a truly dead line
    _idle = {"task": None, "nudges": 0}

    def _cancel_idle():
        t = _idle["task"]
        if t and not t.done():
            t.cancel()
        _idle["task"] = None

    async def _idle_timer():
        try:
            await asyncio.sleep(NUDGE_AFTER)
            if _idle["nudges"] >= MAX_NUDGES:
                # Dead line — no response after repeated nudges. Hang up.
                log.info("[naina] no response after nudges — hanging up dead line")
                try:
                    await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
                except Exception as e:
                    log.warning(f"[naina] idle hangup failed: {e}")
                return
            _idle["nudges"] += 1
            # Count-aware nudges: the FIRST is a presence check ("can you hear me?") so
            # we don't pitch into a void; later ones re-engage. Both stay in the caller's
            # language and never repeat. After MAX_NUDGES unanswered → hang up (above).
            if _idle["nudges"] == 1:
                nudge_instr = (
                    "The caller has gone quiet. In the SAME language you were speaking "
                    "(never switch), gently CHECK IF THEY CAN STILL HEAR YOU in ONE short "
                    "line — e.g. \"Hello, are you still there? Can you hear me okay?\""
                )
            else:
                nudge_instr = (
                    "The caller is still quiet. In the SAME language you were speaking "
                    "(never switch), try ONCE MORE to re-engage with ONE fresh, short line "
                    "— a new soft question or point. Say something NEW; never repeat a "
                    "line you've already used."
                )
            await session.generate_reply(instructions=nudge_instr)
        except asyncio.CancelledError:
            pass

    def _arm_idle():
        _cancel_idle()
        _idle["task"] = asyncio.create_task(_idle_timer())

    @session.on("user_state_changed")
    def _on_user_state(ev):
        if str(getattr(ev, "new_state", "")) == "speaking":
            _idle["nudges"] = 0        # real user activity → reset the nag counter
            _cancel_idle()

    @session.on("agent_state_changed")
    def _on_agent_state(ev):
        st = str(getattr(ev, "new_state", ""))
        if st == "speaking":
            _cancel_idle()
        elif st == "listening":
            _arm_idle()                # bot finished; start the silence clock

    # Dialer-side AMD hook: if your dialer/FreeSWITCH already detected a machine and
    # passed it as a SIP header (mapped to dialer.answered_by), skip the pitch entirely.
    if (attrs.get("dialer.answered_by") or "").lower() in ("machine", "voicemail", "amd", "fax"):
        agent.outcome = "VOICEMAIL"
        log.info("[naina] dialer flagged answering machine — VOICEMAIL, hanging up")
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        return

    await session.generate_reply(
        instructions="Greet the caller and open the conversation."
    )


def prewarm(proc):
    """Runs once per worker subprocess BEFORE it takes a call. Loading the Silero
    VAD here (instead of inside entrypoint) means warm processes are call-ready,
    eliminating the per-call model-load and the 'no warmed process' cold start."""
    # Noise robustness (Layer 1, DEVELOPER_GUIDE §11.1): raise the speech threshold
    # so quiet background chatter falls below "speech", and require a slightly longer
    # burst to start a turn — both cut phantom turns from a noisy caller-side room.
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=0.6,     # default 0.5 — higher = less noise-triggered
        min_speech_duration=0.10,     # default 0.05 — ignore very short blips
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,        # preload VAD into each warm process
            agent_name="naina-bot",     # MUST match agentName in the SIP dispatch rule
            num_idle_processes=3,       # keep 3 warm processes ready (8-core box)
        )
    )
