"""
naina_agent_avataar.py — Avataar Skincare sales script (from the dev's
naina_agent_sarvam.py) MERGED with our latest agent-code improvements.

Kept from the dev file (the SCRIPT + its machinery): the full Avataar prompt, the
language-mode strategy, multi-turn date+time capture, resolve_datetime, 6 disposition
outcomes, CSV log, usage tracking, scheduling/handover/pricing/guardrail logic, tools.

Layered in from our code (see DEVELOPER_GUIDE.md):
  • STT auto-detect (language="unknown") — needed by the language-mode strategy.
  • Number → spoken-ENGLISH TTS filter (tts_node + num2words; needs: pip install num2words).
  • TTS streaming tuning (min_buffer_size=30, max_chunk_length=60).
  • VAD noise robustness (min_speech_duration; activation_threshold kept at the dev's 0.8).
  • Count-aware silence nudge (1st = presence check, then re-engage).
  • Disposition delivered ONCE and reliably on BOTH hangup paths (fires on
    participant_disconnect, awaited in shutdown — fixes bot-hangup webhook loss).

NOTE: shares agent_name "naina-bot" with the other agent files — run only ONE at a time.

Indian-language voice bot on LiveKit Agents (no Pipecat), for Avataar Skincare's
outbound lead-qualification calls (skin/hair/weight-loss treatment consultations).
Runs as an agent-dispatch worker; LiveKit spawns it per outbound SIP call placed by
your dialer. Handles the full call: greet → learn/confirm concern → pitch → push the
free doctor consultation → dispose → hang up, with voicemail detection, silence
nudges, a max-call-duration cap, and a disposition webhook.

>>> Full docs: see DEVELOPER_GUIDE.md (config, prompt reasons, tools, scaling). <<<

Run:
  ./.venv/bin/python naina_agent_sarvam.py download-files   # Silero VAD (once)
  ./.venv/bin/python naina_agent_sarvam.py start            # production (dev = verbose)

Install:
  pip install "livekit-agents[google,silero]" livekit-plugins-sarvam python-dotenv aiohttp

Env (.env in this dir):
  # ── required ──
  LIVEKIT_URL=ws://127.0.0.1:7880
  LIVEKIT_API_KEY=mykey
  LIVEKIT_API_SECRET=<secret>
  SARVAM_API_KEY=...                 # Sarvam STT (Saaras v3) + TTS (Bulbul v3)
  GOOGLE_API_KEY=...                 # Gemini text LLM (NOT a realtime model)
  # ── optional ──
  NAINA_LANG=hi-IN                   # STT/TTS language (hi-IN, en-IN, ta-IN, ...)
  MAX_CALL_SECONDS=300               # hard cap on call length (billing safety)
  DISPOSITION_WEBHOOK_URL=...        # POST the outcome here on call end (else just logged)
  DISPOSITION_WEBHOOK_TOKEN=...      # optional Bearer token for the webhook
  OMP_NUM_THREADS=1                  # optional: cap Silero VAD threads on small boxes

Dialer/SIP participant attributes (set by your dialer per call):
  dialer.prospect_name, dialer.phone, dialer.campaign, dialer.call_id  # existing
  dialer.concern       # optional, e.g. "hair_fall" — skips the generic concern ask
  dialer.answered_by   # optional AMD result: machine/voicemail/amd/fax → skips pitch
  dialer.dnd_flag      # optional: true/1/yes/dnc/dnd → skips pitch, disposition=DNC
                       # (real TRAI NDNC/consent scrubbing must still happen upstream
                       # in your dialer/CRM before dialing — this is just a backstop)
"""

import os
import re
import csv
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiohttp
from google import genai   # one-off Gemini calls for datetime resolution, separate from the agent's LLM
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
from livekit.agents.metrics import LLMModelUsage, STTModelUsage, TTSModelUsage
from livekit.plugins import sarvam, google, silero

log = logging.getLogger("naina")


# ── Number → spoken ENGLISH words (for TTS) ───────────────────────────────────
# The prompt tells the LLM to emit numbers as plain digits ("499", "10,000"); this
# filter converts each digit-run to English words ("four hundred ninety-nine", "ten
# thousand") in the TTS text stream, so numbers are always spoken in English whatever
# the sentence language (Hinglish included). Runs on the LLM OUTPUT (after generation),
# so it adds no turn latency — a local string transform, no network. Requires num2words.
try:
    from num2words import num2words as _num2words
except ImportError:            # fail soft — speak raw digits rather than crash
    _num2words = None
    log.warning("[naina] num2words not installed — numbers spoken as digits. "
                "Run: pip install num2words")

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")     # 499   10,000   4.6
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
        if len(clean) > 6 or (len(clean) > 1 and clean[0] == "0"):
            return " ".join(_num2words(int(d)) for d in clean)
        return _num2words(int(clean))
    except Exception:
        return num_str


def _replace_numbers(text: str) -> str:
    return _NUMBER_RE.sub(lambda m: " " + _number_to_words(m.group()) + " ", text)


async def _convert_numbers_in_stream(text_stream):
    """Streaming filter: digit runs → English words as text flows to TTS. Holds back a
    trailing digit run (it may continue next chunk) so a split number isn't half-converted."""
    buf = ""
    async for chunk in text_stream:
        buf += chunk.translate(_DEVANAGARI_DIGITS)
        m = _TRAILING_NUMBER_RE.search(buf)
        if m and m.start() > 0:
            emit, buf = buf[:m.start()], buf[m.start():]
        elif m:
            emit, buf = "", buf
        else:
            emit, buf = buf, ""
        if emit:
            yield _replace_numbers(emit)
    if buf:
        yield _replace_numbers(buf)


# Language for this deployment. Sarvam supports hi-IN, en-IN, ta-IN, te-IN,
# bn-IN, gu-IN, kn-IN, ml-IN, mr-IN, od-IN, pa-IN. Set per campaign.
LANG = os.getenv("NAINA_LANG", "hi-IN")

# Hard cap on call length (billing safety). A stuck/looping call is force-hung-up
# after this many seconds so it can't rack up STT/LLM/TTS + telephony charges.
MAX_CALL_SECONDS = float(os.getenv("MAX_CALL_SECONDS", "300"))   # default 5 minutes

# How long before the hard MAX_CALL_SECONDS cutoff to inject a soft "wrap up" nudge into
# the live system prompt (not a hangup — just urgency). Skipped if MAX_CALL_SECONDS is too
# small for this window to make sense.
SOFT_NUDGE_BEFORE_END = 300.0   # 5 minutes

# EDIT ME — appended verbatim to the live instructions once the soft-nudge timer fires.
SOFT_NUDGE_TEXT = (
    " TIME CHECK: this call has been going on a while now. Stop opening new topics or "
    "re-explaining things already covered — move decisively toward a close. If a day and "
    "time haven't been captured yet, ask for them directly on your next turn and drive "
    "straight to set_disposition."
)

# Local CSV disposition log — append-only, one row per call: number,disposition,detail.
# Runs alongside (not instead of) the webhook in post_disposition(), so nothing else
# needs to change if you stop/start using either one.
DISPOSITION_CSV_PATH = os.getenv("DISPOSITION_CSV_PATH", "disposition.csv")

# India Standard Time. All caller-stated times are IST; calls are India-only.
IST = timezone(timedelta(hours=5, minutes=30))

# Consultation window — EDIT ME to your real hours. Single source of truth: used
# both for resolve_datetime()'s plausibility check and the prompt's OUR HOURS line,
# so the two can never drift apart.
BOOKING_HOUR_START = 10   # 10 AM IST
BOOKING_HOUR_END = 20     # 8 PM IST

# ── Disposition mapping (inlined) ─────────────────────────────────────────────
# Field1 in the webhook payload — the 4 backend-specified values, plus a 5th
# ("Do Not Call") carved out for DNC/opt-out so it can never collapse into
# "Connected- Not interested" and lose the do-not-redial signal.
FLOW_OUTCOME_TO_CODE = {
    "CONSULTATION_BOOKED": "Connected- Consultation required",
    "CALLBACK_REQUESTED":  "Connected - Call back required",
    "HUMAN_REQUESTED":     "Connected - Call back required",
    "FRUSTRATED":          "Connected- Not interested",
    "NOT_INTERESTED":      "Connected- Not interested",
    "DNC":                 "Do Not Call",
    "VOICEMAIL":           "Not connected",
    "DROPPED":             "Not connected",
    "IDLE_DROPPED":        "Not connected",
    "NO_BOOKING":          "Connected - Call back required",
}


def map_outcome_to_code(outcome: str) -> str:
    return FLOW_OUTCOME_TO_CODE.get(outcome, "Not connected")


# ── Disposition webhook ───────────────────────────────────────────────────────
# DEVELOPERS: edit build_disposition_payload() freely to match what your dialer /
# CRM expects — add, remove, or rename fields. Nothing else needs to change.
# The endpoint is DISPOSITION_WEBHOOK_URL in .env (optional bearer token via
# DISPOSITION_WEBHOOK_TOKEN). If the URL is unset, we just log the payload.

def summarize_usage(session: AgentSession) -> dict:
    """Sum this call's STT audio-seconds, LLM total tokens, and TTS character count
    from AgentSession.usage (LiveKit's built-in per-session ModelUsageCollector) —
    one call's worth of the 3 real cost drivers for our stack (Sarvam STT is
    duration-billed, Gemini is token-billed, Sarvam TTS is character-billed).
    Does NOT include the separate one-off Gemini call in resolve_datetime() below,
    which uses its own genai.Client outside this AgentSession and isn't tracked here."""
    stt_audio_duration = 0.0
    llm_total_tokens = 0
    tts_characters_count = 0
    for usage in session.usage.model_usage:
        if isinstance(usage, STTModelUsage):
            stt_audio_duration += usage.audio_duration
        elif isinstance(usage, LLMModelUsage):
            llm_total_tokens += usage.input_tokens + usage.output_tokens
        elif isinstance(usage, TTSModelUsage):
            tts_characters_count += usage.characters_count
    return {
        "stt_audio_duration": round(stt_audio_duration, 2),
        "llm_total_tokens": llm_total_tokens,
        "tts_characters_count": tts_characters_count,
    }


def build_disposition_payload(prospect: dict, outcome: str, code: str,
                              started_at: datetime, ended_at: datetime,
                              remarks: str = "", consultation_datetime: str = "",
                              followup_datetime: str = "",
                              scheduled_time_iso: str = "",
                              time_status: str = "",
                              time_field: str = "",
                              decline_reason: str = "",
                              usage: dict | None = None) -> dict:
    """The JSON body POSTed to your backend at end of call. EDIT ME.

    disposition_code (Field1) is one of: "Not connected", "Connected- Not interested",
    "Connected- Consultation required", "Connected - Call back required", "Do Not Call"
    — see FLOW_OUTCOME_TO_CODE above. remarks (Field2) is deliberately a nested object,
    not a flat string, so consultation/follow-up timing survives structurally instead of
    being buried in free text — split it further here if your backend wants more.
    scheduled_time_iso is consultation_datetime/followup_datetime resolved to a real
    ISO 8601 timestamp by resolve_datetime() below — "" if there was nothing to resolve
    or the model couldn't confidently parse it; the raw caller-words fields are always
    kept too, so nothing is lost if the resolved value is wrong or missing.
    usage is summarize_usage()'s output — stt_audio_duration/llm_total_tokens/
    tts_characters_count, defaulted to 0 if not passed. decline_reason is the caller's
    stated reason for a NOT_INTERESTED outcome (set_disposition requires it for that
    outcome) — "" for every other outcome."""
    usage = usage or {}
    return {
        "call_id":          prospect.get("call_id"),
        "agent":            "naina-bot",
        "prospect": {
            "name":     prospect.get("name"),
            "phone":    prospect.get("phone"),
            "campaign": prospect.get("campaign"),
        },
        "outcome":          outcome,   # raw flow outcome, e.g. "CONSULTATION_BOOKED"
        "disposition_code": code,      # Field1 — backend-facing 5-value disposition
        "remarks": {                   # Field2 — text + structured follow-up timing
            "text":                  remarks,
            "decline_reason":        decline_reason,   # only set for NOT_INTERESTED
            "consultation_datetime": consultation_datetime,
            "followup_datetime":     followup_datetime,
            "scheduled_time_iso":    scheduled_time_iso,
            "scheduled_time_source": time_field,      # "consultation" | "followup" | ""
            "scheduled_time_status": time_status,
            "time_capture_complete": time_status == "resolved",
        },
        "started_at":       started_at.astimezone(IST).isoformat(),
        "ended_at":         ended_at.astimezone(IST).isoformat(),
        "duration_seconds": round((ended_at - started_at).total_seconds(), 1),
        "usage": {
            "stt_audio_duration":  usage.get("stt_audio_duration", 0.0),
            "llm_total_tokens":    usage.get("llm_total_tokens", 0),
            "tts_characters_count": usage.get("tts_characters_count", 0),
        },
    }

def resolve_instant_slot(reference_time: datetime) -> str:
    """Next bookable slot for an 'ASAP'/'right now' request — computed deterministically,
    no LLM parsing involved (unlike resolve_datetime() below, which has no rule for
    interpreting 'ASAP' and would likely fail to resolve it). Rounds reference_time up to
    the next 10-minute mark in IST, then clamps into BOOKING_HOUR_START/_END, rolling over
    to the next day's opening time if that lands at or after closing."""
    local = reference_time.astimezone(IST).replace(second=0, microsecond=0)
    minute = (local.minute // 10 + 1) * 10
    local = local.replace(minute=0) + timedelta(minutes=minute)
    if local.hour < BOOKING_HOUR_START:
        local = local.replace(hour=BOOKING_HOUR_START, minute=0)
    elif local.hour >= BOOKING_HOUR_END:
        local = (local + timedelta(days=1)).replace(hour=BOOKING_HOUR_START, minute=0)
    return local.isoformat()


async def resolve_datetime(text: str, reference_time: datetime) -> tuple[str, str]:
    """Resolve a caller's free-text preferred time ('Monday 5(Five PM or Five Evening),'kal shaam 5 baje')
    to a real ISO 8601 datetime via one small Gemini call. reference_time anchors
    relative words and is converted to IST before being sent.

    Returns (iso, status). status is one of:
      "resolved"     — iso is a trustworthy timestamp
      "empty"        — nothing to resolve
      "date_only"    — caller gave a day but no time; NEVER guessed
      "time_only"    — caller gave a time but no day; NEVER guessed
      "implausible"  — resolved to the past or outside booking hours
      "unresolvable" — API failure, or the model couldn't parse it
    Only "resolved" returns a non-empty iso. Never raises. The raw caller-words
    fields are always kept upstream, so nothing is lost when iso is ""."""
    if not text.strip():
        return "", "empty"
    try:
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        prompt = (
            f"Current date/time: {reference_time.astimezone(IST).isoformat()} "
            f"(India, IST, UTC+5:30).\n"
            f"A phone caller said this about a preferred day and time, possibly in "
            f"Hindi/Hinglish: \"{text}\"\n"
            "Resolve it to a single ISO 8601 datetime with the +05:30 offset "
            "(e.g. 2026-07-29T18:00:00+05:30). Always resolve to the NEXT FUTURE "
            "occurrence, never a past date.\n"
            "If a date AND a period of day are given, use morning=10:00, "
            "afternoon=14:00, evening=18:00, night=20:00.\n"
            "If a date is given but NO time and NO period of day at all, reply with "
            "exactly: DATE_ONLY\n"
            "If a time is given but no date at all, reply with exactly: TIME_ONLY\n"
            "If nothing can be resolved, reply with exactly: NONE\n"
            "Otherwise reply with ONLY the ISO datetime and nothing else."
        )
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
        )
        result = (resp.text or "").strip()
        if result == "DATE_ONLY":
            return "", "date_only"
        if result == "TIME_ONLY":
            return "", "time_only"
        if not result or result == "NONE":
            return "", "unresolvable"

        resolved = datetime.fromisoformat(result)
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=IST)
        local = resolved.astimezone(IST)
        if (resolved < reference_time - timedelta(minutes=5)
                or not (BOOKING_HOUR_START <= local.hour < BOOKING_HOUR_END)):
            log.warning(f"[naina] implausible resolved time {result} from {text!r}")
            return "", "implausible"
        return result, "resolved"
    except Exception as e:
        log.warning(f"[naina] resolve_datetime failed for {text!r}: {e}")
        return "", "unresolvable"

async def handle_disposition(agent: "Naina", prospect: dict, started_at: datetime,
                             session: AgentSession) -> None:
    """Single entry point for end-of-call disposition handling — maps the outcome to
    its backend code, resolves any preferred-time text to a real timestamp, builds the
    payload, logs it, and sends it (webhook if DISPOSITION_WEBHOOK_URL is set, else just
    logged by post_disposition). Called once from entrypoint()'s shutdown callback. If
    you need to change what happens at end of call, edit this function (and/or
    build_disposition_payload / post_disposition above) — nothing else in this file
    needs to change."""
    ended_at = datetime.now(timezone.utc)
    code = map_outcome_to_code(agent.outcome)
    raw_time_text = agent.consultation_datetime or agent.followup_datetime
    time_field = ("consultation" if agent.consultation_datetime
                    else "followup" if agent.followup_datetime else "")
    if raw_time_text:
        if agent._instant_slot_requested:
            # 'ASAP'/'right now' — computed deterministically, not guessed by the LLM-based
            # resolver below (which has no rule for interpreting "ASAP").
            scheduled_time_iso, time_status = resolve_instant_slot(ended_at), "resolved"
        else:
            scheduled_time_iso, time_status = await resolve_datetime(raw_time_text, ended_at)
    else:
        scheduled_time_iso, time_status = "", "empty"

    # A booking or callback with no trustworthy timestamp is a defect, not a nuance —
    # surface it loudly so it can be re-called rather than sitting silently in the CRM.
    if agent.outcome in ("CONSULTATION_BOOKED", "CALLBACK_REQUESTED") and time_status != "resolved":
        log.error(
            f"[naina] INCOMPLETE TIME CAPTURE — outcome={agent.outcome} "
            f"status={time_status} raw={raw_time_text!r} "
            f"call_id={prospect.get('call_id')}"
        )
    usage = summarize_usage(session)
    payload = build_disposition_payload(
        prospect=prospect,
        outcome=agent.outcome,
        code=code,
        started_at=started_at,
        ended_at=ended_at,
        remarks=agent.remarks,
        consultation_datetime=agent.consultation_datetime,
        followup_datetime=agent.followup_datetime,
        scheduled_time_iso=scheduled_time_iso,
        time_status=time_status,
        time_field=time_field,
        decline_reason=agent.decline_reason,
        usage=usage,
    )
    log.info(f"[naina] call ended → {agent.outcome} → {code}")
    write_disposition_csv(payload)
    await post_disposition(payload)


def write_disposition_csv(payload: dict) -> None:
    """Append this call's disposition to DISPOSITION_CSV_PATH — columns: number,name,
    disposition,detail,decline_reason,time,logged_at,stt_audio_duration,
    llm_total_tokens,tts_characters_count. Creates the file with a header row if it
    doesn't exist yet. EDIT ME if the columns need to change; this is separate from
    build_disposition_payload so the webhook's JSON shape and this CSV's columns can
    evolve independently.

    time is the resolve_datetime()-parsed ISO 8601 timestamp (blank if there was no
    preferred time given, or it couldn't be confidently resolved) — parse THIS column,
    not detail's free text, if you're consuming the CSV programmatically. decline_reason
    is its own column (rather than folded into detail) precisely so "why did they say
    no" is queryable/groupable without parsing free text — blank for every outcome but
    NOT_INTERESTED. logged_at is the wall-clock moment this row was actually written,
    for auditing when calls landed regardless of whether a preferred time was ever
    discussed. The 3 usage columns are this call's real cost drivers from
    summarize_usage() — STT seconds, Gemini total tokens, TTS characters — NOT
    including the separate resolve_datetime Gemini call, which isn't tracked."""
    number = payload["prospect"].get("phone") or ""
    name = payload["prospect"].get("name") or ""
    disposition = payload["disposition_code"]
    remarks = payload["remarks"]
    detail_parts = [remarks["text"]] if remarks["text"] else []
    if remarks["consultation_datetime"]:
        detail_parts.append(f"consultation: {remarks['consultation_datetime']}")
    if remarks["followup_datetime"]:
        detail_parts.append(f"followup: {remarks['followup_datetime']}")
    detail = "; ".join(detail_parts)
    decline_reason = remarks.get("decline_reason", "")
    time_iso = remarks["scheduled_time_iso"]
    time_status = remarks.get("scheduled_time_status", "")
    time_complete = remarks.get("time_capture_complete", False)
    logged_at = datetime.now(timezone.utc).astimezone(IST).isoformat()
    usage = payload["usage"]
    stt_audio_duration = usage["stt_audio_duration"]
    llm_total_tokens = usage["llm_total_tokens"]
    tts_characters_count = usage["tts_characters_count"]

    file_exists = os.path.exists(DISPOSITION_CSV_PATH)
    try:
        with open(DISPOSITION_CSV_PATH, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["number", "name", "disposition", "detail",
                    "decline_reason", "time", "time_status", "time_complete",
                    "logged_at", "stt_audio_duration", "llm_total_tokens",
                    "tts_characters_count"])
            writer.writerow([number, name, disposition, detail, decline_reason,
                time_iso, time_status, time_complete, logged_at,
                stt_audio_duration, llm_total_tokens, tts_characters_count])
    except Exception as e:
        log.error(f"[naina] failed to write {DISPOSITION_CSV_PATH}: {e}")


async def post_disposition(payload: dict) -> None:
    """POST the disposition to your backend. Never raises, so a webhook outage can't
    break call teardown — but a hot lead's outcome (e.g. CONSULTATION_BOOKED) riding on
    a single unconfirmed POST right before hangup is a real loss, so this retries once
    before giving up, and giving up is logged loudly (error, not warning)."""
    url = os.getenv("DISPOSITION_WEBHOOK_URL")
    if not url:
        log.info(f"[naina] DISPOSITION_WEBHOOK_URL unset — would POST: {payload}")
        return
    headers = {}
    token = os.getenv("DISPOSITION_WEBHOOK_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in (1, 2):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    body = await r.text()
                    log.info(f"[naina] disposition POST {url} -> {r.status} {body[:200]}")
                    return
        except Exception as e:
            if attempt == 1:
                log.warning(f"[naina] disposition POST failed (retrying once): {e}")
            else:
                log.error(f"[naina] disposition POST failed after retry — outcome may be "
                          f"LOST: {payload}: {e}")


# ── Example service catalog (inlined placeholder — EDIT ME) ───────────────────
# DEVELOPERS: replace with a real lookup against Avataar's actual service catalog.
# Shape (priority-ranked list, "info" field) matches what get_service_suggestions_for_concern
# must return so the agent's grounding/pricing logic keeps working unchanged.
# EXAMPLE_SERVICE_CATALOG = {
#     "acne":          [{"name": "ClearSkin Acne Therapy", "priority": 1,
#                         "info": "A combination laser + medi-facial protocol that targets active acne and reduces future breakouts."}],
#     "acne_scars":    [{"name": "Renew Acne Scar Therapy", "priority": 1,
#                         "info": "A PRP (platelet-rich plasma) growth-factor protocol using the client's own blood plasma to smooth and rebuild acne-scarred skin texture over a course of sessions."}],
#     "pigmentation":  [{"name": "Radiance Pigmentation Correction", "priority": 1,
#                         "info": "A peel + laser combination that targets dark spots and evens out skin tone over a course of sessions."}],
#     "anti_aging":    [{"name": "Youth Renew Anti-Ageing Therapy", "priority": 1,
#                         "info": "A collagen-stimulating skin-tightening protocol that reduces fine lines and improves elasticity."}],
#     "skin_glow":     [{"name": "Radiance Pigmentation Correction", "priority": 1,
#                         "info": "A peel + laser combination that brightens overall skin tone and texture."}],
#     "oily_skin":     [{"name": "Balance Oil-Control Facial", "priority": 1,
#                         "info": "A pore-refining facial protocol designed to balance excess oil and improve skin texture over a course of sessions."}],
#     "dark_circles":  [{"name": "Radiant Eye Peel Therapy", "priority": 1,
#                         "info": "A targeted mandelic peel protocol designed to gradually reduce the appearance of under-eye dark circles."}],
#     "body_skin":     [{"name": "SmoothGlow Body Polish & De-Tan", "priority": 1,
#                         "info": "A body polishing and de-tanning protocol designed to brighten tanned or dull body skin over a course of sessions."}],
#     "hair_fall":     [{"name": "HairMax Regrowth Therapy", "priority": 1,
#                         "info": "A scalp-stimulation and growth-factor protocol designed to reduce hair fall and support regrowth."}],
#     "hair_thinning": [{"name": "HairMax Regrowth Therapy", "priority": 1,
#                         "info": "A scalp-stimulation and growth-factor protocol designed to reduce hair fall and support regrowth."}],
#     "weight_loss":   [{"name": "ContourFit Slimming Program", "priority": 1,
#                         "info": "A doctor-guided, non-surgical body contouring and slimming program combining diet guidance with fat-reduction sessions."}],
#     "wellness_iv":   [{"name": "Glow Boost IV Therapy", "priority": 1,
#                         "info": "A doctor-administered glutathione IV drip protocol designed for overall skin brightening and wellness."}],
# }

# NOTE: no pricing catalog. Pricing is deliberately never revealed on this call —
# see the PRICING sub-section in Naina's instructions below. If that policy ever
# changes, reintroduce an EXAMPLE_PRICING dict + get_service_pricing tool here.

# Values that mean "no real answer given". The model is told to leave a field empty
# rather than fill it with a placeholder, but check anyway.
_NO_ANSWER = {"", "unspecified", "unknown", "none", "n/a", "na", "tbd", "any",
              "anytime", "any time", "whenever", "flexible", "asap", "not sure",
              "kabhi bhi", "jaldi", "koi bhi", "-"}

# Explicit immediate-intent phrases for an INSTANT BOOKING (see set_disposition below).
# "asap" is deliberately also in _NO_ANSWER above — it's still meaningless as a DAY answer
# (there's no such day as "asap"), the exemption below only ever applies to the TIME field.
_INSTANT_TIME_VALUES = {"asap", "as soon as possible", "right now", "rightnow",
                         "abhi", "abhi hi", "turant", "immediately"}


def _missing(value: str) -> bool:
    """True if this day/time field carries no usable answer."""
    return value.strip().lower() in _NO_ANSWER
class Naina(Agent):
    def __init__(self, prospect: dict):
        name = prospect.get("name")
        concern = prospect.get("concern")
        # Name guard: only give the model a name if we actually have one,
        # otherwise it invents one (e.g. "राम").
        name_clause = (
            f"The prospect's name is {name}; address them by it."
            if name else
            "You do NOT know the caller's name — do not use or invent one."
        )
        # Concern guard, same pattern as name_clause: if the dialer/CRM already knows
        # why this lead was raised, open by referencing it directly instead of asking
        # generically (and the pitch must also work fully concern-agnostic when it's
        # unknown — most outbound leads won't have this populated).
        concern_clause = (
            f"The prospect's stated concern is {concern} — open by referencing it and pitching doctor consult for the same "
            if concern else
            "You do NOT know the caller's concern. Introduce them our brand and start pitching for doctor consult."
        )
        # Current-time snapshot, taken once at call start (same UTC→IST convention used
        # everywhere else in this file, e.g. ended_at/call_started) and rounded to the
        # nearest 10 minutes — a rough anchor, not a live clock. It drifts as the call goes
        # on; that's fine, this is only ever used for coarse judgment calls (see
        # CURRENT TIME AWARENESS below), never read aloud as an exact time.
        _now_ist = datetime.now(timezone.utc).astimezone(IST)
        _rounded_minute = round(_now_ist.minute / 10) * 10
        _now_rounded = _now_ist.replace(second=0, microsecond=0, minute=0) + timedelta(minutes=_rounded_minute)
        current_time_str = _now_rounded.strftime("%I:%M %p").lstrip("0")
        current_time_clause = (
            f"CURRENT TIME AWARENESS: it is currently around {current_time_str} IST (India "
            "time) — approximate, and it drifts later as the call goes on, so treat it only "
            "as a rough anchor, never an exact clock, and never state it aloud unless the "
            "caller explicitly asks what time it is right now. Use it only for two judgment "
            "calls: (1) whether a day+time the caller names falls inside OUR HOURS, and "
            "(2) whether it has already passed, or is within about 30 minutes of right now "
            "— see the TIME SANITY CHECK in SCHEDULING."
        )
        super().__init__(
            instructions=(
                # ── HARD RULES banner (primacy). These non-negotiables are detailed in
                #    the sections below; stated here + repeated at the very END (recency)
                #    so they survive a long call. Content unchanged — just anchored. ──
                "## HARD RULES — never break (details follow):\n"
                "1. LANGUAGE: lock to the caller's first-message mode (English vs Hinglish) "
                "per LANGUAGE STRATEGY; never drift mid-call.\n"
                "2. STAY GROUNDED — do NOT invent: say only what these instructions contain. "
                "Never claim or make up a treatment detail, medical fact, result, timeline, "
                "or price. Anything diagnostic/medical → route to the consultation "
                "(GROUNDING SCOPE + GUARDRAILS).\n"
                "3. NEVER STATE A PRICE — deflect every time (PRICING).\n"
                "4. NUMBERS: plain digits only (499, 10,000) — never spelled out, never "
                "Devanagari numerals.\n"
                "5. YOU lead: short 1-2 sentence turns; always drive toward the free "
                "doctor consultation.\n\n"
                # ── Identity (self-name, self-intro, bot questions) ──
                "IDENTITY: If asked your name again later, you are Naina, "
                "calling from Avataar Skincare. If asked whether you're a bot, an AI,"
                " or a recording, stay natural and warm — say you're calling from "
                "Avataar Skincare's team, never confirm or deny it directly. "
                # ── Language & Script ──
                # "LANGUAGE: reply in the SAME language as the caller's most recent REAL "
                # "message — Latin letters → English; Devanagari → Hindi. Match them every "
                # "turn; never carry over an earlier turn's language. EXCEPTION: a short "
                # "acknowledgment on its own (ok, okay, yeah, haan, hmm, achha, अच्छा, ठीक है) "
                # "is NOT a language switch — ignore it and keep the language of the caller's "
                # "last full sentence.\n"
                "LANGUAGE STRATEGY — Two-Mode System \n"
                "This call ALWAYS has three possible language modes based on caller's first "
                "real message (after your opening):\n"
                "MODE 1 — ENGLISH (Caller responds in Latin letters)"
                "If their first message is in English letters (even if brief like 'yes' or "
                "'ok'), the ENTIRE CALL is in English. Never switch to Hindi. Examples: "
                "'yes', 'great', 'tell me more', 'what's the price', etc. \n"
                "MODE 2 — HINGLISH (Caller responds in Devanagari OR code-switches heavily)"
                "If their first message is in Devanagari script (Hindi), OR if they mix both"
                "scripts significantly (e.g., 'mere hair fall bohot problem hai'), switch to"
                "HINGLISH for the entire rest of the call.\n"
                "HINGLISH means: 50% Hindi words (colloquial, emotion, structure) + 50%"
                "English words (business, medical, premium terms). Example sentence:"
                "'Bilkul, aapka hair fall issue ke liye hamara dermatologist aapko ek free"
                "consultation mein best treatment recommend kar sakte hain.\n"
                "KEY RULE for Hinglish: Use English for business/medical/premium terms"
                "(see ENGLISH-ONLY TERMS below), Hindi for colloquial/emotional words."
                # --- Numbers ---
                "NUMBERS — write as plain digits with commas for clarity (4+ digits):\n"
                "- Single/double: 10, 99, 100, 499 (no comma).\n"
                "- Thousands+: 10,000 (not 10000), 8,000+ (not 8000+).\n"
                "In Hindi: \"यह प्लान सिर्फ 499 रुपये का है।\" or \"हमारे 10,000+ reviews हैं।\"\n"
                "In English: \"It's just 499 rupees\" or \"We have 10,000+ verified reviews.\"\n"
                "Never spell numbers out, never use Devanagari numerals (४९९)."
                # --- Opening ---
                "OPENING  Your first turn is verbatim between <<<SCRIPT>>> "
                "and <<<END SCRIPT>>> — do NOT paraphrase, shorten, or reorder. This block "
                "OVERRIDES PACING for this turn only.\n"
                "<<<SCRIPT>>> "
                "Hi, this is Naina from Avataar Skincare. We provide at-home laser and "
                "GLP-1 weight loss treatments for skin, body, and hair concerns. We received "
                "your enquiry on our portal. Is this a good time to talk? "
                "<<<END SCRIPT>>> "
                "Handle the response:\n"
                "— If 'no' / 'call me back' / not free now: treat as CALLBACK_REQUESTED, "
                "skip the pitch, ask for preferred date+time (see SCHEDULING below).\n"
                "— If 'not interested' / 'no thanks': go to NOT_INTERESTED decline path "
                "(see HANDOVER).\n"
                "— If 'yes' / positive: continue to CREDIBILITY + DOCTOR CONSULTATION below. "
                "On your VERY NEXT turn (the one right after they say yes), say: "
                "'Avataar Skincare is a Shark Tank–funded company, present in 18 cities "
                "and rated 4.6 on Google with over 10,000 verified reviews. I'd like to "
                "connect you with one of our certified dermatologists for a free virtual "
                "consultationcan you share your preferred time?' That completes the "
                "three-element DOCTOR CONSULTATION push (assert + explain + ask for time). "
                "Never say the credibility line unless they've said yes to availability. "
                "The DOCTOR CONSULTATION rule applies from that turn onward, not the opening. "
                # ── is a spoken call, not a chat message) ──
                "PACING: speak in short, natural turns of 1-2 sentences, ONE point per "
                "turn — never a long paragraph. Always move forward: \n"
                "END EVERY TURN with one of: (a) a question that moves the agenda forward "
                "(what time works?), (b) a statement that sets the next expected action "
                "(we'll send a confirmation), or (c) empathetic silence (just listen). "
                "Never end on ambiguity or a dead statement."
                "If the caller asks a side question, "
                "Drive every conversation toward the free doctor consultation with empathy and "
                "information on their concern. Do Not directly claim anything on concern"
                "show empathy and drive towards consulation answer briefly THEN "
                "immediately return to what you were saying — do NOT wait for them to acknowledge."
                " If the caller is silent or just says 'ok'/'haan', continue to your next point"
                " yourself. If the caller is resistant, lead "
                "with empathy and push for doctor consult as a marker for trust, not sales. "
                # ── Strict grounding ──
                "GROUNDING SCOPE: You may describe what each service IS (from SERVICE CATEGORY "
                "REFERENCE). You may NOT describe HOW it works, WHAT side effects exist, "
                "WHICH skin types it suits, or WHY it works — those are medical assessments"
                "that belong in the doctor consultation. If a question requires any of those "
                "categories, answer: 'That's exactly what our dermatologist will assess "
                "let me schedule your free consultation.'"
                # ── Service category reference (tool-free baseline facts) ──
                "SERVICE CATEGORY REFERENCE — background knowledge of what we do and "
                "do not offer. Never recite it unprompted, never read it aloud as a list."
                "Short Summary : We offer a range of "
                "treatments including laser hair reduction, weight loss programs"
                " for 20 kgs and more, facials for acne, pigmentation, and dull skin,"
                "along with hair treatments. \n Detail Summary : "
                " We offer: skin treatments like laser hair reduction; weight loss programs"
                "via Body Slimming and GLP1 treatments "
                "; facials and peels for acne, pigmentation, dull skin, oily "
                "skin and dark circles; growth-factor and PRP treatments for acne "
                "scars, hair problems like hair fall and hair thinning;"
                "collagen-boosting treatments for early ageing; body polishing "
                "and de-tanning. Never attach a specific product name or any "
                "price to any of it. When the caller asks about "
                "treatments, answer per FOLLOW-UP QUESTIONS A below. "

                # ── Specialist term ──
                "SPECIALIST TERM: Use 'dermatologist' for SKIN or HAIR concerns (acne, hair fall, "
                "pigmentation, anti-aging, etc.). Use 'doctor' for WEIGHT LOSS and WELLNESS concerns."
                "If unsure which bucket a concern falls into, default to 'dermatologist' (skin/hair "
                "specialists are the primary offering). EXCEPTION: the OPENING SCRIPT always says "
                " 'dermatologist' verbatim, regardless of the caller's concern, so don't change it."
                # ── Doctor consultation rules ──
                "DOCTOR CONSULTATION PUSH — must contain ALL THREE:"
                "(1) Assert: 'Given [their concern], the best next step is a free consultation...'"
                "(2) Explain: 'One of our dermatologists will assess your specific situation and"
                "recommend the right treatment plan for you — it's completely free.'"
                "(2b) IF they hesitate on time/price: Add 'It only takes 10-15 minutes and there's "
                "no obligation.'"
                "(3) Ask for BOTH parts: 'What day and time work best for you?'"

                "If interrupted mid-push, acknowledge their question briefly (1 sentence), then"
                "continue with whichever element you didn't finish yet before moving on."
                "SPECIALIST TERM. It will only take a short amount of time "
                "If interrupted before the 'ask for a time' part lands, "
                "return to asking for the time on your very next turn before moving to "
                "anything else. Once you've pushed the consultation 3 or more times in "
                "this call, call mark_consultation_pushed and ease off — let the caller "
                "lead the rest of the conversation rather than initiating another push "
                "yourself. "
                # ── Pricing (deflect always — never reveal a number) ──
                "PRICING DEFLECTIONS — Use ONE per asking (pick the variant that best fits the "
                "conversation context):"
                "1. 'Pricing depends on your specific plan — the dermatologist will recommend "
                   "what's right for you, then we'll discuss cost.'"
                "2. 'We customize packages per person, so there's no set number I can quote now. "
                   "That's settled after your consultation.'"
                "3. 'That's part of what you'll discuss with the dermatologist — they'll tailor "
                   "a plan and the pricing to match.'"
                "4. 'Each person's needs are different, so cost varies. The consultation is "
                   "completely free, and pricing details come after.'"
                "5. 'I don't have access to pricing, but I can promise you'll get a clear,"
                  " transparent quote from our team after your consultation.'"

                "After the 3rd ask: Add one of these reassurances, then STOP deflecting and"
                "redirect firmly:"
                "- 'Our plans are designed to be accessible. Let's just book your free"
                   "consultation first, then all your questions get answered.'"
                "- 'I promise the conversation will be worth your time. Can we lock in that"
                   "consultation date?'"
                # ── Scheduling: multi-turn date+time capture ───────────────────
                "SCHEDULING — DATE AND TIME CAPTURE. This section OVERRIDES the "
                "HANDOVER rules below for every booking and callback case. Booking is "
                "a multi-turn capture, not an immediate hand-off. When the outcome is "
                "CONSULTATION_BOOKED or CALLBACK_REQUESTED, NEVER call set_disposition "
                "and NEVER call end_call until you have BOTH a day AND a time and you "
                "have read them back. This restriction applies ONLY to those two "
                "outcomes: DNC, FRUSTRATED, NOT_INTERESTED and HUMAN_REQUESTED are "
                "disposed immediately with no time required. "

                "THE ASK: 'Can I schedule this consultation now if you are available? "
                "Otherwise, please let me know what time works best for you.' "

                "IF THEY GIVE A DAY BUT NO TIME: ask 'Great, and what time would you "
                "prefer on [day]?' — that question is your entire turn. Do not "
                "dispose, do not end the call, do not add anything else. "

                "IF THEY GIVE A TIME BUT NO DAY: ask which day suits them, same rule — "
                "that question is your entire turn. "

                "IF THEY GIVE SOMETHING VAGUE ('evening', 'after work', 'kabhi bhi', "
                "'jaldi'): treat it as no time given and ask once for a specific time. "
                "If they still won't narrow it down, offer two concrete options in our "
                "hours and let them pick. "
                # complementary word
                "INSTANT BOOKING — triggered by an explicit immediate-intent phrase "
                "('abhi', 'right now', 'asap', 'turant') OR by the caller accepting the "
                "TIME SANITY CHECK offer below. This is DIFFERENT from vague answers "
                "('evening', 'kabhi bhi') above — those still get narrowed down, never "
                "treated as instant. On trigger: do NOT ask for a specific time — close "
                "immediately with this exact line — 'I understand, I'll arrange a "
                "consultation for you as soon as possible, thank you.' That single line "
                "is both your close and your goodbye — do not add a separate one. THEN "
                "call set_disposition with CONSULTATION_BOOKED, consultation_day='today' "
                "and consultation_time='as soon as possible', plus a short remarks note, "
                "THEN end_call. This does NOT mean connecting them to the doctor on this "
                "same call — 'as soon as possible' means our team arranges the earliest "
                "slot; never imply the consult happens right now on this line. "

                "IF THEY GIVE DAY AND TIME TOGETHER: close with this exact line — "
                "'Perfect, you're booked for [day] at [time] for your free "
                "[specialist] consultation. You'll receive a confirmation shortly. "
                "Thank you for your time, have a great day!' That single line is both "
                "your read-back and your goodbye — do not add a separate one. THEN call "
                "set_disposition with CONSULTATION_BOOKED, passing their day as "
                "consultation_day and their time as consultation_time, both in their "
                "own words, plus a short remarks note, THEN end_call. "

                "IF THEY ARE HESITANT OR SAY 'NOT NOW' / 'let me think' / 'I'll see': "
                "this is NOT a refusal and NOT an outcome. Do NOT dispose and do NOT "
                "end the call. Handle the objection in one short empathetic turn, then "
                "re-attempt the ask. A re-attempt that follows a specific objection "
                "does NOT count toward the 5-push consultation limit. "

                "IF THEY ASK US TO CALL THEM BACK LATER: ask 'No problem, when should "
                "we call you back?' Capture BOTH day and time using exactly the same "
                "rules above — never log just one. Read both back: 'Sure, we'll call "
                "you back on [day] at [time].' THEN call set_disposition with "
                "CALLBACK_REQUESTED, passing their day as followup_day and their time "
                "as followup_time, both in their own words, plus a short remarks note. "
                "THEN goodbye and end_call. "

                "IF THEY DECLINE ('no', 'not interested', 'don't want it'): a decline "
                "is an OBJECTION to work through, not an immediate outcome — do NOT "
                "set_disposition or end_call on the first or second decline. "
                "ON THE 1ST DECLINE: do not close the call. Ask one warm, brief "
                "question to understand what's holding them back (e.g. 'No problem — "
                "just so I understand, is it the timing, or something else?'). "
                "Whatever reason they give — or if they give none — respond to that "
                "specific reason in one short empathetic line, then re-offer the free "
                "consultation and the day/time ask once. "
                "ON THE 2ND DECLINE: give one more brief, differently-worded response "
                "— never repeat your 1st rebuttal verbatim — and re-offer the ask once "
                "more. This is your last rebuttal. "
                "ON THE 3RD DECLINE (or if they explicitly say they won't change "
                "their mind / ask you to stop asking): stop, do not push a 3rd time. "
                "Close with this exact line — 'No problem at all. Whenever you're "
                "ready, we're here to help with a free consultation. Have a great "
                "day!' THEN call set_disposition with NOT_INTERESTED, passing "
                "decline_reason (their stated reason, in their own words — "
                "'declined without giving a reason' if they never gave one across all "
                "declines) and a remarks note, THEN end_call. "
                "This rebuttal cap is separate from HANDOVER's genuine frustration "
                "rule below and does not override it — if their tone escalates into "
                "real anger or a fraud accusation at any point during this sequence, "
                "hand over as FRUSTRATED immediately instead of continuing to rebut. "

                "OUR HOURS: consultations run [10 AM to 8 PM, every day]. "  # EDIT ME
                "If they name a slot outside that, don't just reject it — negotiate: say "
                "our [specialist]s are available between those hours and ask if a nearby "
                "time within that window would work for them instead. Never accept or "
                "read back a slot you know we can't honour. "

                "TIME SANITY CHECK: using your rough sense of the current time (see "
                "CURRENT TIME AWARENESS), if the day+time the caller names has already "
                "passed, or falls within about 30 minutes of right now, don't accept it "
                "silently — flag it warmly: say that time's really close to now (or has "
                "already passed), and ask if they'd rather have us arrange the "
                "consultation right away instead. If they agree, treat it as an INSTANT "
                "BOOKING (below). If they insist on the original time anyway, capture it "
                "as given — do not argue further. "

                "RECORD THEIR WORDS: pass the day and time exactly as the caller said "
                "them — 'kal shaam 5 baje if and only if they you are speaking hindi "
                "otherwise explicity english time ', 'Monday 5pm', 'tomorrow evening'. Do NOT "
                "convert to a calendar date, do not compute what day tomorrow is, do "
                "not reformat. Our system resolves that afterwards. "

                "INTERRUPTED MID-CAPTURE: if the caller sidetracks you before both "
                "halves are captured, answer briefly and return to the missing half on "
                "your very next turn, before anything else. "

                # ── Follow-up questions (reactive only; always route back) ──────
                "FOLLOW-UP QUESTIONS — REACTIVE ONLY. These three answers fire only "
                "when the caller asks. Never volunteer them. Keep each one light: one "
                "short answer, then return to the SCHEDULING ask for a day and time in "
                "the same turn. You are routing them to the doctor, not advising them. "
                "C. 'IS IT REALLY FREE?': say 'Yes, the consultation itself is "
                "completely free. Only if you choose to go ahead with a treatment plan "
                "afterward would there be any cost, and that would be discussed with "
                "you clearly at that stage.' This is the ONE place you may describe when "
                "a cost would arise — still never state, estimate, or hint at any "
                "amount. Then return to the ask. "
                "GUARDRAILS: (1) MEDICAL ADVICE — if they ask anything diagnostic or "
                "medical ('is my hair fall hormonal', 'which cream should I use', 'will "
                "this work on me', 'is it safe with my medication'), never answer it, "
                "not even partially. Say that is exactly what the [specialist] will "
                "assess, then return to the ask. (2) OFF-TOPIC — one short sentence at "
                "most, or politely say you're not the right person for that, then "
                "return to the ask. (3) HARD PRICING PUSH — give the "
                "pricing-after-consult answer once; if they push again, acknowledge it "
                "briefly and go straight back to the ask. Do not keep re-explaining. "
                # ── Handover to human ──
                "HANDOVER — action request (hand over immediately): they ask for a "
                "phone call, ask for our number, say they tried calling us,or they want "
                "to place an order or pay. Reply warmly and briefly that our team will "
                "connect with them shortly (never mention 'agent' or 'AI'), call "
                "set_disposition with HUMAN_REQUESTED, plus a short remarks note — "
                "THEN end_call. If they mention any preferred day or time, this is a "
                "booking or callback instead: go to SCHEDULING above and capture both "
                "halves before disposing. "
                "HANDOVER — opt-out (hand over immediately, regardless of tone): they "
                "ask us to stop calling/messaging or to remove their number — this is "
                "a consent withdrawal, not necessarily anger, and is handled the same "
                "way whether said calmly or angrily. Reply with ONE brief, polite "
                "acknowledgment, call set_disposition with DNC, THEN end_call. "
                "HANDOVER — genuine frustration (hand over when continuing would "
                "clearly make things worse): they accuse us of fraud/scam, or their "
                "irritation is escalating (a second consecutive angry message even "
                "after you responded with empathy) — distinct from a calm opt-out "
                "above, which is always DNC regardless of tone. Reply with ONE brief, "
                "apologetic, zero-sales message — no pitch, no consultation push — "
                "call set_disposition with FRUSTRATED, THEN end_call. Do NOT hand "
                "over for minor friction — skepticism, a price "
                "objection, a tough question, or a single mildly negative message are "
                "yours to handle with empathy. When unsure whether frustration is "
                "genuine, respond once with pure empathy and no push, and only hand over "
                "if their next message confirms it. "
                # ── Agenda ──
                "AGENDA: greet (introduce yourself by name and establish company image, per IDENTITY)"
                "slightly hint on what we offer for it -> immidiately push heavily for "
                "doctor consultation -> handle questions -> when an outcome is reached, call "
                "set_disposition with one of: CONSULTATION_BOOKED, HUMAN_REQUESTED, "
                "FRUSTRATED, NOT_INTERESTED, DNC, CALLBACK_REQUESTED. "
                # ── Answering machine ──
                "If you reach an answering machine or voicemail — a recorded greeting, "
                "'leave a message after the tone', or a monologue that ignores you — "
                "call voicemail_detected IMMEDIATELY instead of pitching. "
                # ── Ending the call ──
                "When the conversation is finished (an outcome is reached, or the "
                "caller wants to hang up), say a short one-sentence goodbye, THEN call "
                "end_call to hang up. Always set_disposition before end_call, with a "
                "brief remarks note on how the call went regardless of outcome. "
                f"{name_clause} "
                f"{concern_clause} "
                f"{current_time_clause}"
                # ── FINAL BLOCK: the fragile rules repeated LAST (recency anchor). The
                #    model weights the end of the prompt most, so the last thing it reads
                #    every turn is "stay grounded / don't invent / hold language / no price"
                #    — not the time clause. Restates existing policy; adds nothing new. ──
                "\n\n## ⚠️ MOST IMPORTANT — RE-READ EVERY TURN:\n"
                "• STAY GROUNDED: say ONLY what these instructions contain. Do NOT invent "
                "or claim any treatment detail, medical fact, result, timeline, or price. "
                "Anything diagnostic, medical, or 'will it work for me' → 'that's exactly "
                "what our dermatologist/doctor will assess' and return to booking.\n"
                "• NEVER state, estimate, or hint a price — deflect and redirect to the "
                "free consultation.\n"
                "• LANGUAGE: stay in the caller's established mode (English or Hinglish); a "
                "lone 'ok'/'haan'/'hmm' is NOT a language switch.\n"
                "• NUMBERS: plain digits only.\n"
                "• Keep turns short (1-2 sentences) and always drive toward booking the "
                "free consultation."
            )
        )
        self._prospect = prospect
        self.outcome: str = "DROPPED"
        self.remarks: str = ""
        self.decline_reason: str = ""
        self.consultation_datetime: str = ""
        self.followup_datetime: str = ""
        self._consult_pushes = 0
        self._price_asks = 0
        self._disposition_accepted = False
        self._rejected_timed_outcome = ""
        self._instant_slot_requested = False

    @function_tool
    async def set_disposition(self, context: RunContext, outcome: str, remarks: str = "",
                                consultation_day: str = "", consultation_time: str = "",
                                followup_day: str = "", followup_time: str = "",
                                decline_reason: str = ""):
        """Record the final call outcome.
        outcome: one of CONSULTATION_BOOKED, CALLBACK_REQUESTED, HUMAN_REQUESTED,
        FRUSTRATED, NOT_INTERESTED, DNC.
        CONSULTATION_BOOKED — the caller agreed to a consultation AND gave both a day
        and a time. Requires consultation_day AND consultation_time.
        CALLBACK_REQUESTED — the caller wants a callback later and gave both a day and
        a time for it. Requires followup_day AND followup_time.
        HUMAN_REQUESTED — any other action-request handover (wants our number, tried
        calling us, wants to pay) where no day or time was given.
        NOT_INTERESTED — only after the 2-rebuttal decline sequence in SCHEDULING.
        Requires decline_reason.
        remarks: a brief note on how the call went — what they asked, anything our
        team should know before following up.
        decline_reason: REQUIRED for NOT_INTERESTED — the caller's stated reason for
        declining, in their own words (e.g. 'says it's too expensive', 'no time these
        days', 'had a bad experience before'). Never invent a reason they didn't give —
        if they declined across all rebuttals without ever stating one, pass exactly
        'declined without giving a reason'. Leave empty for every other outcome.
        consultation_day: the day, in the caller's own words — 'Tuesday', 'kal',
        'tomorrow', 'the 15th'. REQUIRED for CONSULTATION_BOOKED.
        consultation_time: the time, in the caller's own words — '4 PM',
        'half past four', 'evening'. REQUIRED for CONSULTATION_BOOKED. Leave EMPTY if
        the caller never gave one — never guess, never fill it with 'anytime' or
        'unspecified'. EXCEPTION — for an INSTANT BOOKING, pass exactly 'as soon as
        possible' here with consultation_day='today'; this is the one deliberately vague
        value that's accepted, not treated as missing.
        followup_day / followup_time: the same two fields for a callback. Both REQUIRED
        for CALLBACK_REQUESTED. Leave empty otherwise."""
        # HARD GATE: a booking or callback needs both halves. The prompt asks for this
        # too, but a refused tool call cannot be ignored.
        if outcome == "NOT_INTERESTED" and not decline_reason.strip():
            log.info("[naina] set_disposition REJECTED (NOT_INTERESTED) — missing decline_reason")
            return ("NOT SAVED — you are missing decline_reason. Pass the caller's "
                    "stated reason for declining, or 'declined without giving a "
                    "reason' if they never gave one, then call set_disposition again.")
        instant = False
        if outcome in ("CONSULTATION_BOOKED", "CALLBACK_REQUESTED"):
            if outcome == "CONSULTATION_BOOKED":
                day, tod = consultation_day, consultation_time
            else:
                day, tod = followup_day, followup_time
            # 'asap'/'right now' is a deliberate answer, not a missing one — it's still
            # meaningless as a DAY though (there's no such day as "asap"), so the
            # exemption only ever applies to the TIME field.
            instant = tod.strip().lower() in _INSTANT_TIME_VALUES
            if _missing(day) or (_missing(tod) and not instant):
                what = ("the time" if not _missing(day)
                        else "the day" if not (_missing(tod) and not instant)
                        else "both the day and the time")
                self._rejected_timed_outcome = outcome
                log.info(f"[naina] set_disposition REJECTED ({outcome}) — missing "
                            f"{what}: day={day!r} time={tod!r}")
                return (f"NOT SAVED — you are still missing {what}. Do not end the "
                        f"call. Ask the caller for {what} now, then call "
                        f"set_disposition again.")

        self.outcome = outcome
        self.remarks = remarks
        self.decline_reason = decline_reason
        self.consultation_datetime = f"{consultation_day} {consultation_time}".strip()
        self.followup_datetime = f"{followup_day} {followup_time}".strip()
        self._instant_slot_requested = instant
        self._disposition_accepted = True
        self._rejected_timed_outcome = ""
        log.info(f"[naina] disposition set → {outcome} remarks={remarks!r} "
                    f"consult_dt={self.consultation_datetime!r} "
                    f"followup_dt={self.followup_datetime!r}")
        return "recorded"

    # @function_tool
    # async def get_service_suggestions_for_concern(self, context: RunContext, concern: str):
    #     """Look up Avataar services for a specific skin, hair or weight-loss concern.
    #     concern: one of acne, acne_scars, pigmentation, anti_aging, skin_glow,
    #     oily_skin, dark_circles, body_skin, hair_fall, hair_thinning, weight_loss,
    #     wellness_iv — matches the SERVICE CATEGORY REFERENCE taxonomy. Returns up to 2
    #     results ranked by priority (1 = lead with this one). EDIT ME: wire to the real
    #     service catalog — this currently returns placeholder example data."""
    #     results = EXAMPLE_SERVICE_CATALOG.get(concern.lower().strip(), [])
    #     log.info(f"[naina] get_service_suggestions_for_concern({concern}) -> {len(results)} result(s)")
    #     return {"concern": concern, "services": results}

    # @function_tool
    # async def note_price_asked(self, context: RunContext):
    #     """Call this every time the caller asks about price or cost, before you
    #     deflect. Pricing is never revealed on this call — this tool only tracks how
    #     many times they've asked, so you can vary and escalate your deflection
    #     wording instead of repeating yourself. Returns the real ask count for this
    #     call so far — use this count, do not tally price-asks yourself from memory."""
    #     self._price_asks += 1
    #     log.info(f"[naina] price asked x{self._price_asks}")
    #     return {"price_ask_count": self._price_asks}

    @function_tool
    async def mark_consultation_pushed(self, context: RunContext):
        """Call once you've pushed the doctor consultation 5 or more times in this
        conversation (check the conversation so far before calling)."""
        self._consult_pushes += 1
        log.info(f"[naina] consultation pushed x{self._consult_pushes}")
        return "recorded"

    @function_tool
    async def end_call(self, context: RunContext):
        """Hang up the call. Call this ONLY after you have said a short goodbye and the
        conversation is genuinely finished (an outcome was reached, or the caller wants
        to go). Always set_disposition first if you haven't."""
        # Refuse to hang up mid-capture: a booking was attempted and rejected for a
        # missing half, and nothing valid has been recorded since. Hanging up here
        # would lose a lead who already agreed to a consultation.
        if self._rejected_timed_outcome and not self._disposition_accepted:
            log.warning(f"[naina] end_call BLOCKED — {self._rejected_timed_outcome} "
                        f"still incomplete")
            return ("Cannot end the call yet — the booking is missing its day or time. "
                    "Ask for the missing half, then call set_disposition, then end_call.")
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
        """Rewrite digit runs in the reply to spoken ENGLISH words just before TTS, so
        numbers are always voiced in English regardless of sentence language. Delegates
        to the default TTS node with a transformed text stream."""
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
        "concern":  attrs.get("dialer.concern"),   # e.g. "hair_fall" — optional, from CRM/campaign data
    }
    log.info(f"[naina] call room={ctx.room.name} prospect={prospect}")
    call_started = datetime.now(timezone.utc)

    agent = Naina(prospect)

    session = AgentSession(
        # ── Production stack: Sarvam STT/TTS + Groq LLM ──
        # STT: Saaras v3 (streaming, sub-150ms TTFT) replaces the slow/deprecated
        # saarika:v2.5 — this is the #1 latency fix.
        # STT: AUTO-DETECT (language="unknown") so the caller's real script is preserved —
        # this is what the prompt's LANGUAGE STRATEGY (English vs Hinglish mode) needs. A
        # pinned language transliterates English into Devanagari and breaks the mode pick.
        stt=sarvam.STT(model="saaras:v3", mode="transcribe", language="unknown"),
        llm=google.LLM(model="gemini-2.5-flash"),             # text LLM (NOT realtime); reads GOOGLE_API_KEY
        # TTS: linear16 (raw PCM) = no decode step. speaker/pace/temperature are the dev's
        # voice tuning; min_buffer_size/max_chunk_length (30/60, below the 50/150 defaults)
        # make Sarvam start speaking sooner and stream long replies fast.
        tts=sarvam.TTS(target_language_code=LANG, speaker="roopa", output_audio_codec="linear16",
                       pace=1.5, temperature=0.8, min_buffer_size=30, max_chunk_length=60),
        vad=ctx.proc.userdata["vad"],   # preloaded in prewarm() — not per-call
    )

    # ── Disposition: run handle_disposition() exactly once, delivered on BOTH paths ──
    # Caller hangup lets the worker linger ~20s; bot hangup (end_call → delete_room) tears
    # down in ~30ms. We START on participant_disconnect so it fires immediately, and the
    # shutdown callback AWAITS the same task so the process can't exit before the webhook
    # POST (and resolve_datetime/CSV) finish — otherwise a bot-hangup booking is lost.
    _disposition = {"task": None}

    def _start_disposition_once():
        if _disposition["task"] is None:
            _disposition["task"] = asyncio.create_task(
                handle_disposition(agent, prospect, call_started, session)
            )
        return _disposition["task"]

    async def _on_shutdown():
        try:
            await _start_disposition_once()
        except Exception as e:
            log.warning(f"[naina] disposition await failed: {e}")
    ctx.add_shutdown_callback(_on_shutdown)

    @ctx.room.on("participant_disconnected")
    def _on_participant_left(participant):
        _start_disposition_once()

    await session.start(room=ctx.room, agent=agent)

    # ── Max call duration guard (billing safety) ───────────────────────────────
    # Force-hang up any call that runs past MAX_CALL_SECONDS so a stuck/looping
    # call can't rack up charges. Outcome stays whatever it was (default DROPPED →
    # "Not connected", i.e. retryable).
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

    # ── Soft time-based nudge (billing/UX safety) ──────────────────────────────
    # Independent of the hard cutoff above: never hangs up, just edits the live system
    # prompt a fixed window before MAX_CALL_SECONDS so the LLM self-corrects toward
    # closing while there's still time left to act on it.
    async def _soft_nudge_guard():
        delay = MAX_CALL_SECONDS - SOFT_NUDGE_BEFORE_END
        if delay <= 0:
            log.info(f"[naina] MAX_CALL_SECONDS={MAX_CALL_SECONDS:.0f}s too short for the "
                     f"soft nudge window ({SOFT_NUDGE_BEFORE_END:.0f}s) — skipping")
            return
        try:
            await asyncio.sleep(delay)
            log.info(f"[naina] soft nudge fired at {delay:.0f}s — injecting urgency directive")
            await agent.update_instructions(agent.instructions + SOFT_NUDGE_TEXT)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning(f"[naina] soft nudge update_instructions failed: {e}")
    asyncio.create_task(_soft_nudge_guard())

    # ── Silence-timeout nudge ──────────────────────────────────────────────────
    # If the caller goes quiet after the bot finishes speaking, don't sit in dead
    # air — prompt the bot to continue its agenda itself. Armed when the agent
    # starts listening, cancelled when the user speaks or the agent speaks.
    NUDGE_AFTER = 4.5      # seconds of silence before nudging
    MAX_NUDGES = 3         # don't nag forever on a truly dead line
    _idle = {"task": None, "nudges": 0 }

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
            # Count-aware: the FIRST nudge is a presence check (don't pitch into a void);
            # later ones re-engage toward the consultation. Both stay in the caller's
            # language and never repeat.
            if _idle["nudges"] == 1:
                nudge_instr = (
                    "The caller has gone quiet. In the SAME language you were speaking "
                    "(never switch), gently CHECK IF THEY CAN STILL HEAR YOU in ONE short "
                    "line — e.g. \"Hello, are you still there? Can you hear me okay?\""
                )
            else:
                nudge_instr = (
                    "The caller is still quiet. In the SAME language you were speaking "
                    "(never switch), continue yourself in ONE short line — move your next "
                    "point or gently re-ask for a day and time. Say something NEW; never "
                    "repeat a line you've already used."
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
            # First time we hear a real human: the call connected. Upgrade the
            # default outcome so a call that ends with no explicit disposition
            # logs as follow-up required, not "Not connected" (retryable dial).
            if agent.outcome == "DROPPED":
                agent.outcome = "NO_BOOKING"
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

    # Dialer-side DND hook: real TRAI NDNC/consent scrubbing must happen upstream in
    # your dialer/CRM before a number is ever dialed — this is just a defensive
    # backstop so a number your dialer already flagged never gets pitched to.
    if (attrs.get("dialer.dnd_flag") or "").lower() in ("true", "1", "yes", "dnc", "dnd"):
        agent.outcome = "DNC"
        log.info("[naina] dialer flagged DND/DNC — skipping pitch, hanging up")
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        return

    await session.generate_reply(
        instructions=(
            "Deliver the opening script VERBATIM between <<<SCRIPT>>> and <<<END SCRIPT>>>. "
            "Wait for their response to 'Is this a good time to talk?' and handle it per "
            "the OPENING rules in your instructions."
        )
    )


def prewarm(proc):
    """Runs once per worker subprocess BEFORE it takes a call. Loading the Silero
    VAD here (instead of inside entrypoint) means warm processes are call-ready,
    eliminating the per-call model-load and the 'no warmed process' cold start."""
    # Noise robustness (our Layer 1): activation_threshold is the dev's 0.8 (aggressive —
    # rejects background chatter, but can miss a very soft caller; our tested middle
    # ground is 0.6, lower it if quiet callers get dropped). min_speech_duration ignores
    # very short noise blips before starting a turn.
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=0.8,
        min_speech_duration=0.10,
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
