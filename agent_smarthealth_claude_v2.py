"""
agent_smarthealth_claude_v2.py — Smart Health (AU) lead qualification on CLAUDE.

Same campaign, script, tools and compliance logic as agent_smarthealth.py (the working
Gemini worker). ONLY the LLM differs. Built from LiveKit's own plugin docs + source
rather than from provider docs — the earlier Claude attempt failed because I configured
it from Anthropic's API reference instead:
  docs.livekit.io/agents/models/llm/anthropic/
  docs.livekit.io/agents/start/prompting/
  github.com/livekit/agents/tree/main/livekit-plugins/livekit-plugins-anthropic

⚠️ VERSION SKEW — READ FIRST. This venv has anthropic SDK 1.0.0 but
livekit-plugins-anthropic 1.7.0 was built against anthropic 0.x. That mismatch has bitten
twice: the SDK moved to httpx2 (client construction), and 1.0.0 REMOVED the sampling
params from messages.create() (so a `temperature` kwarg raises TypeError every turn).
build_llm() works around both, but the clean fix is to run the SDK the plugin expects:
    pip install "anthropic<1.0"      # plugin requires anthropic>=0.41
Do that and the two workarounds below become no-ops.

⚠️ THE ONE THING THAT MATTERED: `_strict_tool_schema` (leading underscore) defaults to
True. Strict schemas are compiled into a sampling grammar; this agent's 9 tools exceed
the grammar size limit, so every request returned 400 "Schema is too complex" and the
bot never spoke. build_llm() sets it False. See the comment there for the full story.

ALL LLM OPTIONS ARE SET IN build_llm() IN THIS FILE, not in .env — one reviewable place,
no per-environment drift. The only env var Claude needs is ANTHROPIC_API_KEY.

Install:  pip install "livekit-agents[anthropic,deepgram,cartesia,silero]~=1.6"                       python-dotenv aiohttp
  # ~=1.6 is what LiveKit's docs specify: PEP 440 for ">=1.6, <2.0". 1.7.x is
  # fine; it blocks an accidental 2.0, which removes min_interruption_words
  # (see the deprecation warnings in the logs) and reshapes turn handling.
Run:      ./.venv/bin/python agent_smarthealth_claude_v2.py download-files   # VAD, once
          ./.venv/bin/python agent_smarthealth_claude_v2.py start

⚠️ Shares agent_name "smarthealth-bot" with agent_smarthealth.py — run ONE at a time, or
LiveKit will load-balance calls between them.

────────────────────────────────────────────────────────────────────────────────
Smart Health (AU) private-health-insurance lead qualification and live transfer,
on LiveKit Agents.

DIFFERENT CAMPAIGN from naina_agent_avataar.py (India / skincare / book-a-consult).
This one is Australia / health insurance / QUALIFY-THEN-HANDOFF. It reuses our proven
infrastructure (disposition reliability, silence nudge, max-duration cap, prewarm,
hard tool gates, sandwiched prompt) but the flow, stack and compliance rules differ.

WHAT THIS CALL DOES
  greet → confirm they hold private health insurance (gate) → rate-rise hook →
  confirm 25+ (gate) → capture 10 qualification fields (premium <$300/mo = gate) →
  get explicit consent for the advisor call → hand off → dispose.

STACK — READ THIS BEFORE DEPLOYING
  Australian callers need an Australian-English stack. Sarvam (Saaras/Bulbul) is built
  for Indian languages and Indian-accented speech; on AU callers STT accuracy drops and
  the TTS sounds Indian-accented while the script claims "we are based in Melbourne".
  Default here is Deepgram (en-AU) + Cartesia. Deepgram also emits INTERIM transcripts,
  which is what makes min_interruption_words actually work (Sarvam only returns a final
  at end-of-utterance, so a word-gate there lags until the caller stops talking).
  Set STT_PROVIDER / TTS_PROVIDER to override.

COMPLIANCE (Australia) — these are wired in, do not remove casually
  • DNCR: your DIALER must wash numbers against the Do Not Call Register before dialling.
    There is no AI exemption. register_dnc() here is an in-call backstop, not a substitute.
  • Calling hours (prospect's LOCAL time): 9am-8pm weekdays, 9am-5pm weekends, none on
    public holidays. Australia spans 5 timezones — callbacks are validated per state.
  • Caller ID: the entity calling, the entity on whose behalf, and a contact number must
    be identifiable. COMPANY_NAME / BROKER_NAME / CONTACT_NUMBER below all get spoken.
  • ACL s18 (misleading/deceptive conduct, penalties to $50M): every factual claim in the
    prompt must be current and true. Hence RATE_RISE_* is config with a staleness alarm,
    and the bot DISCLOSES it is an AI if asked directly (it never claims to be human).
  • No personal financial advice. The bot qualifies and routes; it never advises.

Run:
  ./.venv/bin/python agent_smarthealth.py download-files   # Silero VAD (once)
  ./.venv/bin/python agent_smarthealth.py start

Install:
  pip install "livekit-agents[deepgram,cartesia,silero]~=1.6" python-dotenv aiohttp
  # ~=1.6 is what LiveKit's docs specify: PEP 440 for ">=1.6, <2.0". 1.7.x is
  # fine; it blocks an accidental 2.0, which removes min_interruption_words
  # (see the deprecation warnings in the logs) and reshapes turn handling.
  # optional fallbacks: livekit-plugins-elevenlabs livekit-plugins-sarvam

Env (.env):
  LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
  DEEPGRAM_API_KEY, CARTESIA_API_KEY, ANTHROPIC_API_KEY
  AGENT_NAME=Sarah                 # the voice's name, spoken in the opening
  COMPANY_NAME=Smart Health
  BROKER_NAME=...                  # REQUIRED by caller-ID rules — name the broker
  CONTACT_NUMBER=...               # given on request
  RATE_RISE_PCT / RATE_RISE_EFFECTIVE / RATE_RISE_SOURCE / RATE_RISE_AS_AT
  MIN_MONTHLY_PREMIUM=300
  HANDOFF_MODE=callback            # callback | warm_transfer | cold_transfer
  ADVISOR_SIP_URI=...              # required for *_transfer modes
  MAX_CALL_SECONDS=420
  DISPOSITION_WEBHOOK_URL / DISPOSITION_WEBHOOK_TOKEN
  DISPOSITION_CSV_PATH=disposition_smarthealth.csv

Dialer/SIP participant attributes:
  dialer.prospect_name, dialer.phone, dialer.campaign, dialer.call_id
  dialer.state          # optional AU state (NSW/VIC/QLD/SA/WA/TAS/NT/ACT) for callback tz
  dialer.postcode       # optional, used to infer state if state not given
  dialer.answered_by    # optional AMD result
  dialer.dnd_flag       # optional DNCR backstop
"""

import os
import csv
import time
import asyncio
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

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
from livekit.agents.metrics import LLMModelUsage, STTModelUsage, TTSModelUsage
from livekit.plugins import anthropic, google, silero  # noqa: F401 (google kept for reference)

log = logging.getLogger("smarthealth")


# ══════════════════════════════════════════════════════════════════════════════
# 1. CAMPAIGN CONFIG — EDIT ME. Everything the script says about the business
#    lives here, so no factual claim is hard-coded inside the prompt text.
# ══════════════════════════════════════════════════════════════════════════════

AGENT_NAME     = os.getenv("AGENT_NAME", "Sarah")
COMPANY_NAME   = os.getenv("COMPANY_NAME", "Smart Health")
# Caller-ID rules require naming the entity on whose behalf the call is made.
# "one of Australia's most trusted brokers" is NOT an identification — name them.
BROKER_NAME    = os.getenv("BROKER_NAME", "")
CONTACT_NUMBER = os.getenv("CONTACT_NUMBER", "")
BASED_IN       = os.getenv("BASED_IN", "Melbourne")
DATA_SOURCE    = os.getenv("DATA_SOURCE", "Australian Data Services")

# ── The annual premium rate-rise hook. THIS GOES STALE EVERY YEAR. ───────────
# Australian health premiums rise each April 1. Quoting last year's number as
# current is misleading conduct on a regulated product (ACL s18). Update all four
# every April; _assert_stat_fresh() below screams in the logs if you forget.
RATE_RISE_PCT       = os.getenv("RATE_RISE_PCT", "")            # e.g. "3.73"
RATE_RISE_EFFECTIVE = os.getenv("RATE_RISE_EFFECTIVE", "")      # e.g. "1 April 2026"
RATE_RISE_SOURCE    = os.getenv("RATE_RISE_SOURCE", "")         # e.g. "Finder.com.au"
RATE_RISE_AS_AT     = os.getenv("RATE_RISE_AS_AT", "")          # ISO date, e.g. "2026-02-28"

# ── Qualification gates (from the ORIGINAL script; the newer one drops them) ──
MIN_MONTHLY_PREMIUM = float(os.getenv("MIN_MONTHLY_PREMIUM", "300"))
MIN_AGE = 25

# ── Handoff ──────────────────────────────────────────────────────────────────
#   callback       = advisor rings back shortly (the NEWER script; simplest, safest)
#   cold_transfer  = SIP REFER to the advisor, bot drops (needs REFER on your trunk)
#   warm_transfer  = hold caller, brief advisor privately, merge (most complex)
HANDOFF_MODE    = os.getenv("HANDOFF_MODE", "callback").lower()
ADVISOR_SIP_URI = os.getenv("ADVISOR_SIP_URI", "")

MAX_CALL_SECONDS = float(os.getenv("MAX_CALL_SECONDS", "420"))   # 7 min: this flow is longer
DISPOSITION_CSV_PATH = os.getenv("DISPOSITION_CSV_PATH", "disposition_smarthealth.csv")

# ── Australian calling windows (prospect's LOCAL time) ───────────────────────
# ACMA telemarketing standard. Used to validate a requested CALLBACK slot — the
# bot must never promise an advisor call outside a legal window.
CALL_WINDOW_WEEKDAY = (9, 20)   # 9am - 8pm
CALL_WINDOW_WEEKEND = (9, 17)   # 9am - 5pm
# Public holidays are state-specific and move every year — your CRM/dialer is the
# source of truth. Populate with ISO dates to have the bot refuse those days.
PUBLIC_HOLIDAYS: set[str] = set(
    d.strip() for d in os.getenv("PUBLIC_HOLIDAYS", "").split(",") if d.strip()
)

# AU state → UTC offset. NOTE: DST states (NSW/VIC/SA/TAS/ACT) shift +1 in summer;
# this table is standard time. For exact DST use zoneinfo (Australia/Sydney etc.) —
# swap in ZoneInfo if you need to-the-minute accuracy on callback validation.
AU_STATE_TZ = {
    "NSW": 10.0, "VIC": 10.0, "QLD": 10.0, "ACT": 10.0, "TAS": 10.0,
    "SA": 9.5, "NT": 9.5, "WA": 8.0,
}
# Australian postcode ranges → state (first digit is enough for the common cases).
POSTCODE_STATE = {"1": "NSW", "2": "NSW", "3": "VIC", "4": "QLD",
                  "5": "SA", "6": "WA", "7": "TAS", "0": "NT", "8": "NT", "9": "QLD"}


def _assert_stat_fresh() -> None:
    """The rate-rise claim is the one time-sensitive factual statement in the pitch.
    Australian premiums reset every 1 April, so a stat older than ~12 months is no
    longer 'the current rate rise' and stating it as such is misleading. Logged loudly
    at worker start rather than silently spoken to thousands of prospects."""
    if not (RATE_RISE_PCT and RATE_RISE_AS_AT):
        log.warning("[smarthealth] RATE_RISE_* not configured — the rate-rise hook will "
                    "be OMITTED from the pitch (safe default; set it to enable).")
        return
    try:
        as_at = datetime.fromisoformat(RATE_RISE_AS_AT).replace(tzinfo=timezone.utc)
    except ValueError:
        log.error(f"[smarthealth] RATE_RISE_AS_AT={RATE_RISE_AS_AT!r} is not an ISO date")
        return
    age_days = (datetime.now(timezone.utc) - as_at).days
    if age_days > 365:
        log.error(
            f"[smarthealth] ⚠️ STALE RATE-RISE STAT: {RATE_RISE_PCT}% as at "
            f"{RATE_RISE_AS_AT} is {age_days} days old. Quoting a superseded premium "
            f"rise as current is misleading conduct — UPDATE RATE_RISE_* NOW."
        )


def rate_rise_clause() -> str:
    """The pitch's rate-rise sentence, built from config. Empty (and silently skipped
    in the prompt) if not configured — better to lose the hook than state a stale number."""
    if not (RATE_RISE_PCT and RATE_RISE_EFFECTIVE):
        return ""
    src = f" (source: {RATE_RISE_SOURCE})" if RATE_RISE_SOURCE else ""
    return (f"Premiums went up on average {RATE_RISE_PCT}% from {RATE_RISE_EFFECTIVE}"
            f"{src}, which is a lot with the cost of living right now. ")


# ══════════════════════════════════════════════════════════════════════════════
# 2. QUALIFICATION STATE — the 10 fields the advisor needs before a handoff.
# ══════════════════════════════════════════════════════════════════════════════

# Callers say premiums out loud ("eight hundred dollars"), and the model records them
# in the caller's own words as instructed — so a digits-only parser silently fails and
# the MIN_MONTHLY_PREMIUM gate never fires. That happened on a live call: $800/year
# (~$66/mo) was handed off instead of disqualified. These let the gate read words too.
_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM_SCALES = {"hundred": 100, "thousand": 1000, "k": 1000, "grand": 1000}


def _words_to_number(text: str) -> float | None:
    """'eight hundred dollars' -> 800.0 ; 'twelve hundred' -> 1200.0 ;
    'one thousand two hundred' -> 1200.0. None if no number words are present."""
    import re as _re
    total = current = 0.0
    found = False
    for w in _re.findall(r"[a-z]+", text.lower()):
        if w in _NUM_WORDS:
            current += _NUM_WORDS[w]; found = True
        elif w in _NUM_SCALES:
            scale = _NUM_SCALES[w]
            if scale >= 1000:
                total += (current or 1) * scale; current = 0.0
            else:
                current = (current or 1) * scale
            found = True
        # "and" and any other word (dollars, about, roughly) is ignored
    return (total + current) if found else None


@dataclass
class Qualification:
    """One lead's captured answers. Everything is stored as the caller SAID it —
    normalisation belongs downstream, not in a live call."""
    holds_policy:      str = ""   # yes / no
    over_min_age:      str = ""   # yes / no  (25+)
    insurer:           str = ""   # BUPA / HCF / NIB / Medibank / ...
    cover_type:        str = ""   # single / couple / family / single parent
    premium_amount:    str = ""   # as said: "$180", "two hundred"
    premium_frequency: str = ""   # fortnightly / monthly / quarterly
    hospital_extras:   str = ""   # hospital / extras / both
    permanent_resident: str = ""  # yes / no
    main_interest:     str = ""   # reduce premiums / add benefits   (MANDATORY)
    first_name:        str = ""
    last_name:         str = ""
    email:             str = ""
    postcode:          str = ""
    decision_maker:    str = ""   # yes / no — only asked if a partner was mentioned

    # Fields an advisor cannot work a lead without.
    REQUIRED = ("insurer", "cover_type", "premium_amount", "premium_frequency",
                "main_interest", "first_name", "postcode")

    def missing_required(self) -> list[str]:
        return [f for f in self.REQUIRED if not getattr(self, f).strip()]

    def monthly_premium(self) -> float | None:
        """Normalise the premium to a monthly figure for the MIN_MONTHLY_PREMIUM gate.
        Returns None if it can't be read confidently — the gate then does NOT fire,
        because dropping a real lead on a parse failure is worse than one extra transfer."""
        import re
        amount = None
        m = re.search(r"\d[\d,]*(?:\.\d+)?", self.premium_amount.replace(" ", ""))
        if m:
            try:
                amount = float(m.group().replace(",", ""))
            except ValueError:
                amount = None
        if amount is None:                      # no digits — try spoken words
            amount = _words_to_number(self.premium_amount)
        if amount is None:
            # Make the miss VISIBLE. A silently skipped gate hands off unprofitable
            # leads, which is exactly what it exists to prevent.
            log.warning(f"[smarthealth] premium gate SKIPPED — could not read an amount "
                        f"from {self.premium_amount!r}; lead will not be filtered")
            return None
        freq = self.premium_frequency.strip().lower()
        if "fort" in freq:      return amount * 26 / 12
        if "week" in freq:      return amount * 52 / 12
        if "month" in freq:     return amount
        if "quart" in freq:     return amount / 3
        if "annu" in freq or "year" in freq: return amount / 12
        log.warning(f"[smarthealth] premium gate SKIPPED — unknown frequency "
                    f"{self.premium_frequency!r} (amount={amount}); lead not filtered")
        return None   # unknown frequency → don't guess, don't gate


# ══════════════════════════════════════════════════════════════════════════════
# 3. DISPOSITIONS
# ══════════════════════════════════════════════════════════════════════════════

FLOW_OUTCOME_TO_CODE = {
    "TRANSFERRED":            "Connected - Transferred to advisor",
    "CALLBACK_BOOKED":        "Connected - Call back required",
    "QUALIFIED_PENDING":      "Connected - Qualified, advisor to call",
    "NOT_QUALIFIED_NO_POLICY": "Connected - Not qualified",
    "NOT_QUALIFIED_AGE":      "Connected - Not qualified",
    "NOT_QUALIFIED_PREMIUM":  "Connected - Not qualified",
    "NOT_DECISION_MAKER":     "Connected - Not qualified",
    "NOT_INTERESTED":         "Connected - Not interested",
    "DNC":                    "Do Not Call",
    "VOICEMAIL":              "Not connected",
    "DROPPED":                "Not connected",
    "NO_OUTCOME":             "Connected - Call back required",
}


def map_outcome_to_code(outcome: str) -> str:
    return FLOW_OUTCOME_TO_CODE.get(outcome, "Not connected")


# ══════════════════════════════════════════════════════════════════════════════
# 4. CALLBACK-WINDOW VALIDATION (compliance, not nicety)
# ══════════════════════════════════════════════════════════════════════════════

def state_for(prospect: dict) -> str:
    st = (prospect.get("state") or "").upper().strip()
    if st in AU_STATE_TZ:
        return st
    pc = (prospect.get("postcode") or "").strip()
    if pc and pc[0] in POSTCODE_STATE:
        return POSTCODE_STATE[pc[0]]
    return "NSW"   # most populous; only used for a coarse window check


def callback_window_ok(state: str, when_utc: datetime) -> tuple[bool, str]:
    """Is `when_utc` inside a legal telemarketing window in `state`'s local time?
    Returns (ok, human_reason). Conservative: unknown → treated as OK, because this
    is a guard rail, not the authority. Your dialer must enforce the real rules."""
    offset = AU_STATE_TZ.get(state, 10.0)
    local = when_utc.astimezone(timezone(timedelta(hours=offset)))
    if local.date().isoformat() in PUBLIC_HOLIDAYS:
        return False, "it's a public holiday"
    start, end = (CALL_WINDOW_WEEKEND if local.weekday() >= 5 else CALL_WINDOW_WEEKDAY)
    if not (start <= local.hour < end):
        return False, f"our advisors call between {start} and {end} in {state}"
    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# 5. DISPOSITION OUTPUT — webhook + CSV.  EDIT build_disposition_payload().
# ══════════════════════════════════════════════════════════════════════════════

def summarize_usage(session: AgentSession) -> dict:
    """This call's three cost drivers: STT seconds, LLM tokens, TTS characters."""
    stt_s, llm_tok, tts_chars = 0.0, 0, 0
    for usage in session.usage.model_usage:
        if isinstance(usage, STTModelUsage):
            stt_s += usage.audio_duration
        elif isinstance(usage, LLMModelUsage):
            llm_tok += usage.input_tokens + usage.output_tokens
        elif isinstance(usage, TTSModelUsage):
            tts_chars += usage.characters_count
    return {"stt_audio_duration": round(stt_s, 2),
            "llm_total_tokens": llm_tok,
            "tts_characters_count": tts_chars}


def build_disposition_payload(prospect: dict, outcome: str, code: str,
                              qual: Qualification, consent_given: bool,
                              callback_text: str, remarks: str,
                              decline_reason: str,
                              started_at: datetime, ended_at: datetime,
                              usage: dict | None = None) -> dict:
    """The JSON POSTed to your backend / CRM at end of call. EDIT ME.

    `qualification` is the advisor's brief — it is the whole point of the call.
    `consent_given` records that the prospect explicitly agreed to the advisor
    contacting them; keep it, it is your consent artifact."""
    return {
        "call_id":          prospect.get("call_id"),
        "agent":            "smarthealth-bot",
        "campaign":         prospect.get("campaign"),
        "prospect": {
            "name":     prospect.get("name"),
            "phone":    prospect.get("phone"),
            "state":    prospect.get("state"),
            "postcode": qual.postcode or prospect.get("postcode"),
        },
        "outcome":          outcome,
        "disposition_code": code,
        "qualification":    asdict(qual),
        "consent_to_contact": consent_given,
        "callback_request": callback_text,
        "remarks":          remarks,
        "decline_reason":   decline_reason,
        "started_at":       started_at.isoformat(),
        "ended_at":         ended_at.isoformat(),
        "duration_seconds": round((ended_at - started_at).total_seconds(), 1),
        "usage":            usage or {},
    }


def write_disposition_csv(payload: dict) -> None:
    """Append-only local log, one row per call. Runs alongside the webhook so an
    outage never loses a lead outright."""
    q = payload["qualification"]
    row = {
        "logged_at":  datetime.now(timezone.utc).isoformat(),
        "call_id":    payload["call_id"],
        "phone":      payload["prospect"]["phone"],
        "outcome":    payload["outcome"],
        "disposition": payload["disposition_code"],
        "consent":    payload["consent_to_contact"],
        "first_name": q["first_name"], "last_name": q["last_name"],
        "insurer":    q["insurer"], "cover_type": q["cover_type"],
        "premium":    f'{q["premium_amount"]} {q["premium_frequency"]}'.strip(),
        "hospital_extras": q["hospital_extras"],
        "resident":   q["permanent_resident"],
        "main_interest": q["main_interest"],
        "email":      q["email"], "postcode": q["postcode"],
        "callback":   payload["callback_request"],
        "decline_reason": payload["decline_reason"],
        "remarks":    payload["remarks"],
        "stt_audio_duration":  payload["usage"].get("stt_audio_duration", 0),
        "llm_total_tokens":    payload["usage"].get("llm_total_tokens", 0),
        "tts_characters_count": payload["usage"].get("tts_characters_count", 0),
    }
    exists = os.path.exists(DISPOSITION_CSV_PATH)
    try:
        with open(DISPOSITION_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if not exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.error(f"[smarthealth] failed to write {DISPOSITION_CSV_PATH}: {e}")


async def post_disposition(payload: dict) -> None:
    """POST to your backend. Never raises. Retries once — a qualified lead riding on a
    single unconfirmed POST is a real loss, so giving up is logged as an ERROR."""
    url = os.getenv("DISPOSITION_WEBHOOK_URL")
    if not url:
        log.info(f"[smarthealth] DISPOSITION_WEBHOOK_URL unset — would POST: {payload}")
        return
    headers = {}
    if (token := os.getenv("DISPOSITION_WEBHOOK_TOKEN")):
        headers["Authorization"] = f"Bearer {token}"
    for attempt in (1, 2):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    body = await r.text()
                    log.info(f"[smarthealth] disposition POST -> {r.status} {body[:200]}")
                    return
        except Exception as e:
            if attempt == 1:
                log.warning(f"[smarthealth] disposition POST failed (retrying): {e}")
            else:
                log.error(f"[smarthealth] disposition POST failed after retry — "
                          f"LEAD MAY BE LOST: {payload}: {e}")


async def handle_disposition(agent: "SmartHealthAgent", prospect: dict,
                             started_at: datetime, session: AgentSession) -> None:
    """Single end-of-call entry point. Edit this (and the two functions above) if you
    need to change what happens when a call ends; nothing else needs to change."""
    ended_at = datetime.now(timezone.utc)
    code = map_outcome_to_code(agent.outcome)
    payload = build_disposition_payload(
        prospect=prospect, outcome=agent.outcome, code=code,
        qual=agent.qual, consent_given=agent.consent_given,
        callback_text=agent.callback_request, remarks=agent.remarks,
        decline_reason=agent.decline_reason,
        started_at=started_at, ended_at=ended_at, usage=summarize_usage(session),
    )
    # A handoff with an incomplete brief is a defect, not a nuance — surface it.
    if agent.outcome in ("TRANSFERRED", "QUALIFIED_PENDING", "CALLBACK_BOOKED"):
        if (miss := agent.qual.missing_required()):
            log.error(f"[smarthealth] INCOMPLETE BRIEF — outcome={agent.outcome} "
                      f"missing={miss} call_id={prospect.get('call_id')}")
    log.info(f"[smarthealth] call ended → {agent.outcome} → {code}")
    write_disposition_csv(payload)
    await post_disposition(payload)


# ══════════════════════════════════════════════════════════════════════════════
# 6. THE AGENT
# ══════════════════════════════════════════════════════════════════════════════

_NO_ANSWER = {"", "unknown", "unspecified", "n/a", "na", "none", "-", "not sure", "tbd"}


def _missing(v: str) -> bool:
    return v.strip().lower() in _NO_ANSWER


class SmartHealthAgent(Agent):
    def __init__(self, prospect: dict):
        name = prospect.get("name")
        name_clause = (f"The prospect's name is {name} — greet them by it and confirm "
                       f"you're speaking to them."
                       if name else
                       "You do NOT know the prospect's name — ask who you're speaking "
                       "with; never invent one.")
        broker_clause = (f"The broker you work with is {BROKER_NAME}; name them if asked."
                         if BROKER_NAME else
                         "You do NOT have the broker's name — if asked exactly which "
                         "broker, say your advisor will confirm that on their call.")
        contact_clause = (f"If they ask for a contact number, give {CONTACT_NUMBER}."
                          if CONTACT_NUMBER else
                          "If they ask for a contact number, say the advisor will "
                          "provide it on their call.")
        handoff_clause = (
            "When qualified, an advisor will CALL THEM BACK shortly — ask them to stay "
            "near their phone and get an explicit yes."
            if HANDOFF_MODE == "callback" else
            "When qualified, you will CONNECT THEM to an advisor on this same call — "
            "tell them you're putting them through and get an explicit yes first."
        )

        super().__init__(
            instructions=(
                # ══ HARD RULES (primacy). Repeated verbatim-in-spirit at the very
                #    END of this prompt (recency) so they survive a long call. ══
                "## HARD RULES — never break:\n"
                "1. ENGLISH ONLY, Australian and natural. Never switch language.\n"
                "2. NEVER GIVE ADVICE. You do not compare policies, recommend products, "
                "interpret cover, or comment on whether a policy is good. You gather "
                "information and route to a licensed advisor. Any product question → "
                "'I'm not the right person for that — the advisor will cover it.'\n"
                "3. NEVER INVENT FACTS. Say only what is written in these instructions. "
                "No premium figures, no savings amounts, no percentages, no waiting-period "
                "claims, no insurer comparisons. If you don't know it, the advisor will.\n"
                "4. IF ASKED WHETHER YOU ARE AN AI, A BOT, OR A RECORDING — say yes, "
                "plainly and warmly ('I am — I'm an automated assistant with " + COMPANY_NAME +
                "'), then carry on. NEVER claim to be human, never dodge the question.\n"
                "5. IF THEY ASK TO BE REMOVED / STOP CALLING / 'do not call' — stop "
                "selling immediately, apologise once, call register_dnc, then end_call. "
                "This overrides everything else in this prompt.\n"
                "6. SHORT TURNS: 1-2 sentences, ONE question at a time. This is a phone "
                "call, not a form. Never read a list of questions in one breath.\n"
                "7. PLAIN SPOKEN TEXT ONLY. Everything you write is READ ALOUD by a "
                "text-to-speech voice. Never use markdown, bullet points, numbered "
                "lists, headings, tables, JSON, code, asterisks, emojis or any other "
                "formatting — it is spoken out or mangled. Write only what a person would say.\n\n"

                # ══ SOUNDING HUMAN (per LiveKit's prompting guide) ══
                "## HOW YOU SOUND:\n"
                "Real speech is slightly messy — that is what makes it sound human. Use "
                "light filler and softeners naturally ('okay so', 'sure', 'right', 'no "
                "worries', a brief 'umm' when thinking), and occasionally self-correct or "
                "restart a phrase the way people do. Warm and unhurried; never sound like "
                "a form being read out.\n"
                "VARY YOUR OPENERS — do not begin consecutive turns the same way. Rotate "
                "between a short acknowledgement, the caller's name, and going straight to "
                "the point. Repeating 'Great!' or 'Perfect!' every turn is the fastest "
                "way to sound like a robot.\n\n"

                # ══ IDENTITY ══
                f"## WHO YOU ARE:\nYou are {AGENT_NAME}, calling from {COMPANY_NAME}"
                f"{', based in ' + BASED_IN if BASED_IN else ''}. You are warm, brisk and "
                f"professional. {broker_clause} {contact_clause}\n"
                f"If asked where their number came from: '{DATA_SOURCE}, a data management "
                f"company.'\n\n"

                # ══ OPENING — verbatim ══
                "## OPENING — say this, do not paraphrase:\n"
                "<<<SCRIPT>>>\n"
                f"Hi, is that {name or '[their name]'}? This is {AGENT_NAME}, calling from "
                f"{COMPANY_NAME}. How are you today?\n"
                "<<<END SCRIPT>>>\n"
                "After they answer, say: 'Good to hear! It's just a quick call about your "
                "existing private health insurance — I believe you have one, right?'\n"
                "• If they say NO (no private health cover): they are not eligible. Say "
                "'No worries at all, thanks for your time — have a great day!', call "
                "disqualify with reason 'no_policy', then end_call. Do NOT pitch.\n"
                "• If YES: say 'That's lovely.' then go to THE HOOK.\n\n"

                # ══ HOOK ══
                "## THE HOOK (one turn):\n"
                f"{rate_rise_clause()}"
                f"'The reason I'm calling is we work with one of Australia's most trusted "
                f"health insurance brokers. It's an obligation-free service to see if they "
                f"can save you money on your private health insurance. It's for people over "
                f"{MIN_AGE} — can I just confirm you're over {MIN_AGE}?'\n"
                f"• If UNDER {MIN_AGE}: thank them warmly, call disqualify with reason "
                f"'under_age', end_call.\n"
                "• If over: go to QUALIFYING.\n\n"

                # ══ QUALIFYING ══
                "## QUALIFYING — ask these ONE AT A TIME, conversationally, in this order. "
                "Call save_qualification as each answer comes in (you may batch fields you "
                "just heard). Never ask two in the same turn. Never re-ask something they "
                "already answered.\n"
                "1. Who are you currently with? (BUPA, HCF, NIB, Medibank, Australian "
                "Unity, Defence Health, Teachers Health, or someone else)\n"
                "2. Does that cover just yourself, you and your partner, or the family?\n"
                "3. And roughly how much is the premium — and is that fortnightly, monthly "
                "or quarterly? (you need BOTH the amount AND the frequency; if they give "
                "only one, ask for the other next turn)\n"
                "4. Does it cover hospital only, or hospital and extras?\n"
                "5. And you're a permanent Australian resident?\n"
                "6. MANDATORY — If you had the opportunity, would you be looking to reduce "
                "your premium, or add more benefits? (never skip this; the advisor needs it)\n"
                "7. Can I grab your first and last name?\n"
                "8. And the best postcode for you?\n"
                "EMAIL: only ask once, casually. If they hesitate, decline, or the spelling "
                "gets messy, DROP IT immediately — say 'no problem' and move on. Do not "
                "spell email addresses back and forth. A missing email is fine.\n\n"

                # ══ CLOSE ══
                "## CLOSING THE HANDOFF:\n"
                f"{handoff_clause}\n"
                "Say: 'Now, one of our best advisors will give you all the information on "
                "what options are available. As I said, it's completely obligation-free and "
                "it won't take long — is that okay with you?'\n"
                "WAIT for a clear yes. When you get it, call record_consent, then "
                "request_handoff. Only then say the closing line and end_call.\n"
                "If they will not consent now but are still warm, treat it as a callback: "
                "ask what day and time suits, call book_callback with their words, end_call.\n\n"

                # ══ OBJECTIONS — near-verbatim approved copy ══
                "## OBJECTIONS — use these responses, then return to where you were:\n"
                "• NOT INTERESTED (at the opening): 'That's okay. We're not here to sell you "
                "any products or services — our job is just to see if we can help you save on "
                "your private health insurance and beat the rate rise.'\n"
                "• WON'T SHARE PERSONAL INFO: 'That's fine — we're a legitimate company and "
                "we're not allowed to ask for personal information like a PIN, password or ID. "
                "It's just to see if we can point you at the right information, at no cost.'\n"
                "• HAPPY WITH MY PROVIDER / DON'T WANT TO CHANGE: 'That's fine. The motive "
                "isn't to change your provider — it's an obligation-free check that you're "
                "getting value for what you pay, and whether you could pay less for the same "
                "cover and benefits.'\n"
                "• WANTS A COMPARATIVE QUOTE REASSURANCE: 'Getting a comparative quote "
                "doesn't obligate you to change anything. It simply lets you see what else is "
                "out there and whether you could be saving. Even if you stay exactly where you "
                "are, at least you'll know you're getting good value.'\n"
                "• BUSY / DRIVING / AT WORK: 'No worries, I'll keep it quick — okay?' If they "
                "still can't talk, book a callback (day AND time) and let them go.\n"
                "• SEND ME SOMETHING IN WRITING: 'Happy to send information across, but I "
                "wouldn't be doing my best if I didn't also get your okay for an advisor to "
                "call — it's no-obligation, and if you like what you hear you can take it "
                "further from there.'\n"
                "• MY PARTNER HANDLES THIS: ask 'Are you both mutual decision-makers?' You "
                "need a clear YES to continue. If NO, don't push — call disqualify with "
                "reason 'not_decision_maker' and close warmly.\n"
                "• JUST COMPARED / JUST SWITCHED: 'May I ask when you compared?' If it was "
                "over three months ago: 'Then it's probably a good thing I called — you "
                "haven't had a comparison with us, and our advisors work with a wide variety "
                "of products.' If very recent, thank them and close politely.\n"
                "• ARE YOU FROM BUPA/NIB/HCF?: 'We work with an unbiased broker who has "
                "multiple companies on their panel — Bupa, HCF, NIB and others. If you're "
                "already on a good policy they'll tell you so, and you don't have to change "
                "a thing.'\n"
                "• PRODUCT-SPECIFIC QUESTION: 'I'm honestly not the right person for that — "
                "but that's exactly what the advisor can walk you through.' Then return to "
                "your question.\n"
                "• PRICE / SAVINGS AMOUNT: never quote or estimate a figure. 'That depends "
                "entirely on your policy — that's what the advisor works out with you.'\n"
                "Use each objection response ONCE. If they raise the same objection a third "
                "time, stop rebutting: close warmly, call set_disposition NOT_INTERESTED "
                "with their reason, and end_call.\n\n"

                # ══ TOOLS ══
                "## TOOLS:\n"
                "- save_qualification — call it every time you learn a field. Pass the "
                "caller's own words.\n"
                "- record_consent — the moment they clearly agree to the advisor contact.\n"
                "- request_handoff — after consent, to hand the lead over.\n"
                "- book_callback — when they want to be called another time (needs day AND "
                "time in their words).\n"
                "- disqualify — no policy / under age / not decision-maker.\n"
                "- register_dnc — any stop-calling or remove-my-number request.\n"
                "- set_disposition — NOT_INTERESTED (needs their reason) or other outcomes.\n"
                "- voicemail_detected — recorded greeting, 'leave a message after the tone', "
                "or a monologue that ignores you. Call it immediately, don't pitch.\n"
                "- end_call — after a short goodbye, once an outcome is recorded.\n\n"

                f"{name_clause}\n\n"

                # ══ FINAL BLOCK — recency anchor. Last thing read every turn. ══
                "## ⚠️ MOST IMPORTANT — RE-READ EVERY TURN:\n"
                "• NO ADVICE, NO INVENTED FACTS. No figures, percentages, savings claims, "
                "waiting-period statements or policy comparisons that aren't written above. "
                "Unsure? 'The advisor will cover that.'\n"
                "• If asked whether you're an AI — say yes, warmly. Never claim to be human.\n"
                "• Stop-calling request → apologise, register_dnc, end_call. Always.\n"
                "• ONE short question per turn, in English. Never read a list.\n"
                "• PLAIN SPOKEN TEXT — no markdown, bullets, asterisks, emojis or "
                "formatting of any kind. Every word is read aloud.\n"
                "• Your only goal is a consented, qualified handoff — keep moving toward it."
            )
        )
        self._prospect = prospect
        self.qual = Qualification()
        self.outcome: str = "DROPPED"
        self.remarks: str = ""
        self.decline_reason: str = ""
        self.consent_given: bool = False
        self.callback_request: str = ""
        self._handoff_requested = False

    # ── Slot filling ─────────────────────────────────────────────────────────
    @function_tool
    async def save_qualification(
        self, context: RunContext,
        insurer: str = "", cover_type: str = "", premium_amount: str = "",
        premium_frequency: str = "", hospital_extras: str = "",
        permanent_resident: str = "", main_interest: str = "",
        first_name: str = "", last_name: str = "", email: str = "",
        postcode: str = "", decision_maker: str = "", over_min_age: str = "",
        holds_policy: str = "",
    ):
        """Record qualification answers as the caller gives them. Call this every time
        you learn something new — you can pass several fields at once if they answered
        several, and call it again later for the rest. Pass the caller's OWN WORDS; never
        guess, never fill a field they didn't answer.
        premium_amount: the figure as said ('$180', 'about two hundred').
        premium_frequency: fortnightly / monthly / quarterly.
        cover_type: single / couple / family / single parent.
        hospital_extras: hospital / extras / both.
        main_interest: 'reduce premiums' or 'add benefits' — MANDATORY before handoff.
        decision_maker: 'yes'/'no' — only if a partner or spouse was mentioned."""
        updates = {k: v for k, v in locals().items()
                   if k not in ("self", "context") and isinstance(v, str) and v.strip()}
        for k, v in updates.items():
            setattr(self.qual, k, v.strip())
        if updates:
            log.info(f"[smarthealth] qualification += {updates}")

        # Premium gate: enforced in CODE, because the LLM will happily transfer an
        # unprofitable lead if the caller is friendly. Returning the refusal as tool
        # output makes it un-ignorable.
        monthly = self.qual.monthly_premium()
        if monthly is not None and monthly < MIN_MONTHLY_PREMIUM:
            log.info(f"[smarthealth] premium gate: ~${monthly:.0f}/mo < "
                     f"${MIN_MONTHLY_PREMIUM:.0f} — not qualified")
            self.outcome = "NOT_QUALIFIED_PREMIUM"
            self.remarks = (self.remarks + f" premium ~${monthly:.0f}/mo below threshold").strip()
            return ("saved — NOTE: this premium is below the level our advisors can help "
                    "with. Do NOT hand off. Say warmly that it looks like they're already "
                    "on a very competitively priced policy so there's likely nothing to "
                    "save right now, thank them, then call end_call.")
        return f"saved: {', '.join(updates) if updates else 'nothing new'}"

    # ── Consent + handoff ────────────────────────────────────────────────────
    @function_tool
    async def record_consent(self, context: RunContext):
        """Call the moment the prospect clearly agrees to an advisor contacting them
        ('yes', 'that's fine', 'okay'). Only call this on an explicit yes — never infer
        consent from silence or from them simply continuing to talk."""
        self.consent_given = True
        log.info("[smarthealth] consent to advisor contact = YES")
        return "consent recorded"

    @function_tool
    async def request_handoff(self, context: RunContext, remarks: str = ""):
        """Hand the qualified lead to an advisor. Call ONLY after record_consent.
        remarks: one short line for the advisor — what they care about, anything useful.
        Refused if consent is missing or the brief is incomplete."""
        if not self.consent_given:
            log.info("[smarthealth] request_handoff REFUSED — no consent yet")
            return ("NOT DONE — you don't have their explicit yes yet. Ask if they're "
                    "happy for an advisor to give them a call, then call record_consent.")
        if (miss := self.qual.missing_required()):
            pretty = ", ".join(m.replace("_", " ") for m in miss)
            log.info(f"[smarthealth] request_handoff REFUSED — missing {miss}")
            return (f"NOT DONE — the advisor still needs: {pretty}. Do not end the call. "
                    f"Ask for the next missing item now, save it with save_qualification, "
                    f"then call request_handoff again.")
        self.remarks = remarks or self.remarks
        self._handoff_requested = True
        self.outcome = "QUALIFIED_PENDING" if HANDOFF_MODE == "callback" else "TRANSFERRED"
        log.info(f"[smarthealth] HANDOFF ({HANDOFF_MODE}) — {asdict(self.qual)}")

        if HANDOFF_MODE == "cold_transfer" and ADVISOR_SIP_URI:
            # SIP REFER — the bot drops off and the carrier bridges the two parties.
            # Requires REFER enabled on your trunk; falls back to a callback if it fails.
            try:
                jctx = get_job_context()
                sip_participant = next(
                    (p for p in jctx.room.remote_participants.values()
                     if p.identity.startswith("sip_")), None)
                if sip_participant is None:
                    raise RuntimeError("no SIP participant found to transfer")
                await jctx.api.sip.transfer_sip_participant(
                    api.TransferSIPParticipantRequest(
                        room_name=jctx.room.name,
                        participant_identity=sip_participant.identity,
                        transfer_to=ADVISOR_SIP_URI,
                        play_dialtone=True,
                    )
                )
                log.info(f"[smarthealth] cold transfer → {ADVISOR_SIP_URI}")
                return ("transferred — say a brief 'putting you through now' and nothing "
                        "else; the call is leaving you.")
            except Exception as e:
                # Never strand the prospect on a transfer failure — degrade to callback.
                log.error(f"[smarthealth] cold transfer FAILED, degrading to callback: {e}")
                self.outcome = "QUALIFIED_PENDING"
                return ("Transfer didn't connect. Apologise briefly, tell them an advisor "
                        "will call them back very shortly, then end_call.")

        # callback mode (and warm_transfer until it's wired — see WARM TRANSFER note)
        return ("handoff recorded — tell them the advisor will call shortly and to stay "
                "near their phone, then end_call.")

    @function_tool
    async def book_callback(self, context: RunContext, day: str, time_of_day: str,
                            remarks: str = ""):
        """The prospect wants to be contacted another time. Needs BOTH day and time in
        their own words ('Tuesday', 'tomorrow', '4pm', 'after 6'). Never guess either
        half — if you only have one, ask for the other first."""
        if _missing(day) or _missing(time_of_day):
            what = "the time" if not _missing(day) else "the day" if not _missing(time_of_day) else "both the day and the time"
            log.info(f"[smarthealth] book_callback REJECTED — missing {what}")
            return (f"NOT SAVED — still missing {what}. Ask for it now, then call "
                    f"book_callback again. Do not end the call.")
        self.callback_request = f"{day} {time_of_day}".strip()
        self.outcome = "CALLBACK_BOOKED"
        self.remarks = remarks or self.remarks
        log.info(f"[smarthealth] callback booked: {self.callback_request!r}")

        # Compliance guard: never promise a call outside a legal window. We only warn
        # the model (rather than refusing) because the caller's words are approximate —
        # your dialer does the authoritative scheduling.
        st = state_for(self._prospect)
        note = ""
        low = time_of_day.lower()
        if any(k in low for k in ("9pm", "10pm", "11pm", "midnight", "late night",
                                  "early morning", "6am", "7am", "sunday")):
            note = (f" NOTE: that may fall outside our calling hours in {st} "
                    f"(9am-8pm weekdays, 9am-5pm weekends, not on public holidays). "
                    f"Gently offer a nearby time inside those hours instead.")
        return f"callback recorded for {self.callback_request}.{note}"

    # ── Exits ────────────────────────────────────────────────────────────────
    @function_tool
    async def disqualify(self, context: RunContext, reason: str, remarks: str = ""):
        """End a call that cannot qualify. reason must be one of:
        'no_policy' (no private health cover), 'under_age' (under 25),
        'not_decision_maker' (partner decides and they are not a mutual decision-maker).
        Say a warm one-line goodbye, call this, then end_call. Never pitch after this."""
        mapping = {"no_policy": "NOT_QUALIFIED_NO_POLICY",
                   "under_age": "NOT_QUALIFIED_AGE",
                   "not_decision_maker": "NOT_DECISION_MAKER"}
        self.outcome = mapping.get(reason.strip().lower(), "NOT_QUALIFIED_NO_POLICY")
        self.remarks = remarks or reason
        log.info(f"[smarthealth] disqualified: {reason}")
        return "recorded"

    @function_tool
    async def register_dnc(self, context: RunContext, remarks: str = ""):
        """The prospect asked not to be called again / to be removed from the list.
        Call this IMMEDIATELY, whether they said it calmly or angrily. Say exactly:
        'Thanks for letting me know — I do apologise for the inconvenience. I'll add your
        number to our do-not-call list so you won't get any more calls.' Then end_call.
        Never rebut, never pitch, never ask why."""
        self.outcome = "DNC"
        self.remarks = remarks or "opt-out requested on call"
        log.warning(f"[smarthealth] DNC requested — phone={self._prospect.get('phone')} "
                    f"call_id={self._prospect.get('call_id')}")
        return "recorded — apologise once, confirm removal, then end_call"

    @function_tool
    async def set_disposition(self, context: RunContext, outcome: str,
                              remarks: str = "", decline_reason: str = ""):
        """Record a final outcome that isn't covered by the other tools.
        outcome: NOT_INTERESTED (requires decline_reason, in their own words) or
        NO_OUTCOME (talked, no decision reached).
        Use disqualify / register_dnc / request_handoff / book_callback for those cases."""
        if outcome == "NOT_INTERESTED" and not decline_reason.strip():
            return ("NOT SAVED — pass decline_reason: the caller's stated reason in their "
                    "own words, or 'declined without giving a reason'.")
        self.outcome = outcome
        self.remarks = remarks or self.remarks
        self.decline_reason = decline_reason
        log.info(f"[smarthealth] disposition → {outcome} reason={decline_reason!r}")
        return "recorded"

    @function_tool
    async def voicemail_detected(self, context: RunContext):
        """Call the MOMENT you realise this is an answering machine or voicemail rather
        than a live person — a recorded greeting, 'leave a message after the tone', or a
        monologue that never responds. Not for a real person who simply talks a lot."""
        self.outcome = "VOICEMAIL"
        log.info("[smarthealth] voicemail detected — hanging up")
        jctx = get_job_context()
        await jctx.api.room.delete_room(api.DeleteRoomRequest(room=jctx.room.name))

    @function_tool
    async def end_call(self, context: RunContext):
        """Hang up. Call ONLY after a short goodbye and once an outcome has been
        recorded by one of the other tools."""
        # Don't hang up mid-qualification on a consented lead — that loses a hot lead.
        if self.consent_given and not self._handoff_requested:
            log.warning("[smarthealth] end_call BLOCKED — consented lead not handed off")
            return ("Cannot end yet — they agreed to the advisor call but the handoff "
                    "isn't recorded. Collect anything still missing, then call "
                    "request_handoff, then end_call.")
        # Wait for the goodbye to finish playing before dropping the line. Use
        # RunContext.wait_for_playout() — current_speech.wait_for_playout() deadlocks
        # (this tool owns that speech handle).
        await context.wait_for_playout()
        log.info("[smarthealth] ending call (agent-initiated)")
        jctx = get_job_context()
        await jctx.api.room.delete_room(api.DeleteRoomRequest(room=jctx.room.name))


# ══════════════════════════════════════════════════════════════════════════════
# 7. PROVIDER SELECTION
# ══════════════════════════════════════════════════════════════════════════════

def build_stt():
    """Deepgram for AU English. `en-AU` is a real Deepgram model locale, and Deepgram
    streams INTERIM results — which is what lets min_interruption_words work (a Sarvam
    word-gate can't fire until the caller stops talking, because Sarvam only emits a
    final at end-of-utterance)."""
    provider = os.getenv("STT_PROVIDER", "deepgram").lower()
    if provider == "deepgram":
        from livekit.plugins import deepgram
        return deepgram.STT(model="nova-3", language=os.getenv("STT_LANGUAGE", "en-AU"))
    if provider == "sarvam":   # not recommended for AU — Indian-language model
        from livekit.plugins import sarvam
        return sarvam.STT(model="saaras:v3", mode="transcribe", language="en-IN")
    raise ValueError(f"unknown STT_PROVIDER={provider!r}")


def build_tts():
    """Cartesia by default: sub-100ms time-to-first-audio and a natural English voice.
    Pick an AUSTRALIAN or neutral voice id — an Indian- or American-accented voice
    undercuts a 'based in Melbourne' pitch."""
    provider = os.getenv("TTS_PROVIDER", "cartesia").lower()
    voice = os.getenv("TTS_VOICE", "").strip()
    if provider == "cartesia":
        from livekit.plugins import cartesia
        # Only pass `voice` when set — an empty string is not a valid voice id and
        # would fail at session start rather than falling back to the default.
        return cartesia.TTS(model="sonic-2", **({"voice": voice} if voice else {}))
    if provider == "elevenlabs":
        from livekit.plugins import elevenlabs
        return elevenlabs.TTS(**({"voice_id": voice} if voice else {}))
    raise ValueError(f"unknown TTS_PROVIDER={provider!r}")


# ══════════════════════════════════════════════════════════════════════════════
# 8. ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE LLM — every option is set HERE in code, deliberately not in .env, so the
# configuration is reviewable in one place and cannot drift per-environment.
# Sourced from the plugin's own signature, not from provider docs:
#   github.com/livekit/agents/tree/main/livekit-plugins/livekit-plugins-anthropic
#   docs.livekit.io/agents/models/llm/anthropic/
# ══════════════════════════════════════════════════════════════════════════════

def build_llm() -> anthropic.LLM:
    opts = dict(
        # ── model ────────────────────────────────────────────────────────────
        # The plugin's own documented default and what LiveKit validates against.
        # (Earlier attempts used claude-haiku-4-5 taken from Anthropic's API docs —
        # picking a model the INTEGRATION hasn't validated is how we lost a day.)
        # Sonnet 4.6 works but is SLOW on a call: measured 4.6s / 6.3s / 11s / 17s
        # from turn-committed to reply on a live test. Haiku 4.5 is the small/fast
        # model in the family and returned 0.75s TTFT with this exact prompt+tools in
        # a direct API test. Its earlier failure here was strict-mode schema, NOT the
        # model — that is fixed below, so it is worth having.
        # Revert to "claude-sonnet-4-6" if answer quality drops on real objections.
        model="claude-haiku-4-5",

        # ── ⚠️ THE FIX. Note the LEADING UNDERSCORE — the parameter is
        #    `_strict_tool_schema`, and it DEFAULTS TO True. ──────────────────
        # With strict on, Anthropic compiles the tool schemas into a sampling
        # grammar. This agent's 9 tools blow the grammar size limit and EVERY
        # request dies with 400 "Schema is too complex" (earlier, with optional
        # params, the same mechanism produced 400 "...exponential compilation
        # cost ... limit: 16 parameters with unions"). On a call that looks like
        # the bot simply never speaking. Non-strict tools skip grammar
        # compilation entirely.
        # We give up strict type coercion, which this agent never relied on:
        # every tool parameter is a plain string and the tools validate their
        # own inputs (premium gate, missing-field gate, consent gate).
        _strict_tool_schema=False,

        # ── prompt caching ───────────────────────────────────────────────────
        # The system prompt (~8k chars) and the 9 tool schemas are byte-identical
        # on every turn of every call, so they are an ideal cache prefix. The
        # plugin marks the last tool + system block with cache_control ephemeral.
        # Cuts input cost and time-to-first-token on every turn after the first.
        caching="ephemeral",

        # ── generation ───────────────────────────────────────────────────────
        # Replies are 1-2 spoken sentences; 512 is ample and bounds a runaway
        # generation (which on a phone call is dead air the caller pays for).
        max_tokens=512,

        # ── temperature / top_k: DELIBERATELY NOT SET ────────────────────────
        # The anthropic SDK 1.0.0 REMOVED the sampling params from
        # AsyncMessages.create(). Plugin 1.7.0 still forwards them, so setting
        # temperature here raises:
        #     TypeError: AsyncMessages.create() got an unexpected keyword
        #                argument 'temperature'
        # ...on every turn. The plugin only forwards params you actually set
        # (NotGivenOr), so leaving these out is what keeps the call legal.
        # This is the same plugin-vs-SDK skew as the httpx2 issue below; the
        # real fix is `pip install "anthropic<1.0"` — see the module docstring.

        # ── tools ────────────────────────────────────────────────────────────
        # "auto" = the model decides when to call. Never "required": this agent
        # must be free to just talk (greeting, objection handling, goodbye).
        tool_choice="auto",
        # Sequential tool calls. The flow is a scripted ladder (save → consent →
        # handoff) with hard gates that read prior state, so predictable ordering
        # matters more here than the round-trip saved by parallel calls.
        parallel_tool_calls=False,

        # ── transport ────────────────────────────────────────────────────────
        # NOTE: `timeout` is deliberately NOT set. Its type is httpx.Timeout, and
        # this venv has the anthropic SDK on httpx2 — passing one re-introduces
        # the exact version skew handled below. Leave it to the plugin default.
        max_retries=2,
    )

    try:
        llm = anthropic.LLM(**opts)
    except TypeError as e:
        # Version skew: newer `anthropic` SDKs moved to httpx2 while
        # livekit-plugins-anthropic still builds an httpx.AsyncClient and hands
        # it over, which the SDK rejects ("Expected an instance of
        # httpx2.AsyncClient"). The plugin only builds that client when none is
        # passed (`client or ...`), so supplying our own skips the broken path.
        if "http_client" not in str(e):
            raise
        from anthropic import AsyncClient
        log.warning("[smarthealth] anthropic SDK/plugin httpx-vs-httpx2 skew — "
                    "supplying our own AsyncClient. Fix: align the two packages.")
        llm = anthropic.LLM(**opts, client=AsyncClient())

    log.info(f"[smarthealth] LLM: {opts['model']} "
             f"(strict_tool_schema=False, caching=ephemeral, "
             f"max_tokens={opts['max_tokens']}, temperature=SDK-default)")
    return llm



async def entrypoint(ctx: JobContext):
    await ctx.connect()

    participant = await ctx.wait_for_participant()
    attrs = participant.attributes
    prospect = {
        "name":     attrs.get("dialer.prospect_name"),
        "phone":    attrs.get("dialer.phone"),
        "campaign": attrs.get("dialer.campaign"),
        "call_id":  attrs.get("dialer.call_id"),
        "state":    attrs.get("dialer.state"),
        "postcode": attrs.get("dialer.postcode"),
    }
    log.info(f"[smarthealth] call room={ctx.room.name} prospect={prospect} "
             f"state={state_for(prospect)}")
    call_started = datetime.now(timezone.utc)

    agent = SmartHealthAgent(prospect)

    # With Deepgram's interim transcripts a word-gate CAN fire mid-sentence, so a stray
    # background "yeah" no longer cuts the bot off. Only enable when the STT actually
    # streams interims — on Sarvam this makes the bot talk over the caller until they
    # stop. See DEVELOPER_GUIDE §11.1.
    _min_words = 2 if os.getenv("STT_PROVIDER", "deepgram").lower() == "deepgram" else 0

    def _make_session(**extra):
        return AgentSession(
            stt=build_stt(),
            llm=build_llm(),          # Claude — every option set in build_llm(), not .env
            tts=build_tts(),
            vad=ctx.proc.userdata["vad"],
            **extra,
        )

    # Prefer TurnHandlingOptions with interruption mode "vad". Two wins: it silences the
    # min_interruption_words deprecation warning, and it stops LiveKit initialising the
    # ADAPTIVE interruption detector — a Cloud-only feature that can never authenticate
    # on a self-hosted stack. Left enabled it retries 0.1s -> 2.0s -> 2.0s and only gives
    # up at ~4.4s, i.e. right through the window the caller is waiting for the greeting.
    # If anything about this API differs in your version we fall straight back to the
    # configuration that is known to work today — this must not break a working bot.
    try:
        from livekit.agents import TurnHandlingOptions
        session = _make_session(turn_handling=TurnHandlingOptions(
            interruption={"mode": "vad", "min_words": _min_words}))
        log.info("[smarthealth] turn handling: VAD interruption "
                 f"(min_words={_min_words}), adaptive detector disabled")
    except Exception as e:
        log.info(f"[smarthealth] TurnHandlingOptions unavailable/incompatible ({e}); "
                 f"using legacy interruption settings")
        session = _make_session(**({"min_interruption_words": _min_words}
                                   if _min_words else {}))

    # ── Disposition: fire once, immediately, and awaited ─────────────────────
    # Caller hangup lets the worker linger ~20s; a bot hangup (end_call → delete_room)
    # tears down in ~30ms. Start on participant_disconnect so the webhook goes out at
    # once, and AWAIT the same task in shutdown so the process cannot exit mid-POST —
    # otherwise a qualified lead is silently lost on the bot-hangup path.
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
            log.warning(f"[smarthealth] disposition await failed: {e}")
    ctx.add_shutdown_callback(_on_shutdown)

    @ctx.room.on("participant_disconnected")
    def _on_participant_left(_participant):
        _start_disposition_once()

    await session.start(room=ctx.room, agent=agent)

    # ── Max call duration (billing safety) ──────────────────────────────────
    async def _max_duration_guard():
        try:
            await asyncio.sleep(MAX_CALL_SECONDS)
            log.warning(f"[smarthealth] max duration {MAX_CALL_SECONDS:.0f}s — force hangup")
            try:
                await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            except Exception as e:
                log.warning(f"[smarthealth] max-duration hangup failed: {e}")
        except asyncio.CancelledError:
            pass
    asyncio.create_task(_max_duration_guard())

    # ── Silence nudge: presence check first, then re-engage, then hang up ────
    NUDGE_AFTER = float(os.getenv("NUDGE_AFTER", "8.0"))
    MAX_NUDGES = 3
    _idle = {"task": None, "nudges": 0}

    # If the silence is OUR fault (LLM stalled/errored), do not nudge. We once went mute
    # for ~20s on a provider error and then asked the caller "are you still there?" —
    # blaming them for our outage. Track recent session errors and stay quiet instead.
    _last_error = {"at": 0.0}
    ERROR_QUIET_FOR = 20.0   # seconds after an error during which we suppress nudges

    try:
        @session.on("error")
        def _on_session_error(ev):
            _last_error["at"] = time.monotonic()
            log.warning(f"[smarthealth] session error: {getattr(ev, 'error', ev)}")
    except Exception as e:      # event name differs across versions — non-fatal
        log.debug(f"[smarthealth] could not subscribe to session errors: {e}")

    def _cancel_idle():
        t = _idle["task"]
        if t and not t.done():
            t.cancel()
        _idle["task"] = None

    async def _idle_timer():
        try:
            await asyncio.sleep(NUDGE_AFTER)
            if _idle["nudges"] >= MAX_NUDGES:
                log.info("[smarthealth] no response after nudges — hanging up dead line")
                try:
                    await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
                except Exception as e:
                    log.warning(f"[smarthealth] idle hangup failed: {e}")
                return
            if time.monotonic() - _last_error["at"] < ERROR_QUIET_FOR:
                log.info("[smarthealth] suppressing nudge — recent bot-side error, the "
                         "silence is ours, not the caller's")
                return
            _idle["nudges"] += 1
            if _idle["nudges"] == 1:
                instr = ("The line has gone quiet. In ONE short line, check they can still "
                         "hear you — e.g. 'Sorry, are you still there? Can you hear me okay?'")
            else:
                instr = ("Still quiet. In ONE short line, gently re-ask the question you were "
                         "waiting on, worded differently. Say something NEW — never repeat a "
                         "line you have already used, and never re-explain the offer.")
            await session.generate_reply(instructions=instr)
        except asyncio.CancelledError:
            pass

    def _arm_idle():
        _cancel_idle()
        _idle["task"] = asyncio.create_task(_idle_timer())

    @session.on("user_state_changed")
    def _on_user_state(ev):
        if str(getattr(ev, "new_state", "")) == "speaking":
            # First real human speech: a call that later ends with no explicit outcome
            # should log as "call back required", not as an unanswered dial.
            if agent.outcome == "DROPPED":
                agent.outcome = "NO_OUTCOME"
            _idle["nudges"] = 0
            _cancel_idle()

    @session.on("agent_state_changed")
    def _on_agent_state(ev):
        st = str(getattr(ev, "new_state", ""))
        if st == "speaking":
            _cancel_idle()
        elif st == "listening":
            _arm_idle()

    # ── Dialer-side AMD: machine already detected upstream → don't pitch ─────
    if (attrs.get("dialer.answered_by") or "").lower() in ("machine", "voicemail", "amd", "fax"):
        agent.outcome = "VOICEMAIL"
        log.info("[smarthealth] dialer flagged answering machine — hanging up")
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        return

    # ── DNCR backstop. The authoritative wash happens in your dialer BEFORE the
    #    number is ever dialled — there is no AI exemption under the Do Not Call
    #    Register Act. This only catches a number your own systems already flagged.
    if (attrs.get("dialer.dnd_flag") or "").lower() in ("true", "1", "yes", "dnc", "dnd"):
        agent.outcome = "DNC"
        log.warning("[smarthealth] dialer flagged DNC — hanging up without pitching")
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        return

    await session.generate_reply(
        instructions=("Deliver the OPENING script between <<<SCRIPT>>> and <<<END SCRIPT>>> "
                      "verbatim, then stop and wait for their reply.")
    )


def prewarm(proc):
    """Runs once per worker subprocess BEFORE it takes a call, so warm processes are
    call-ready and there is no per-call model load."""
    _assert_stat_fresh()
    if HANDOFF_MODE in ("cold_transfer", "warm_transfer") and not ADVISOR_SIP_URI:
        log.error(f"[smarthealth] HANDOFF_MODE={HANDOFF_MODE} but ADVISOR_SIP_URI is "
                  f"unset — handoffs will degrade to callback.")
    if not BROKER_NAME:
        log.warning("[smarthealth] BROKER_NAME unset — AU caller-ID rules expect the "
                    "entity on whose behalf the call is made to be identifiable.")
    # activation_threshold 0.6: rejects background chatter without dropping soft
    # speakers (0.8 is aggressive). min_speech_duration ignores sub-100ms blips.
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=0.6,
        min_speech_duration=0.10,
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="smarthealth-bot",   # MUST match agentName in the SIP dispatch rule
            num_idle_processes=3,
        )
    )
