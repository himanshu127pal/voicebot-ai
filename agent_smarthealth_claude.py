"""
agent_smarthealth_claude.py — PRODUCTION worker. Claude Haiku 4.5 primary with
cross-provider failover. SUPERSEDES agent_smarthealth.py and agent_smarthealth_g3.py
(archive those; the campaign logic here is byte-identical, only the LLM layer differs).

WHY WE LEFT GEMINI
Three separate production breakages, all from one vendor:
  1. gemini-2.5-flash → 503 "model is overloaded"        (Avataar campaign)
  2. gemini-2.5-flash → empty streaming responses,
     "no response generated" / FinishReason.STOP          (livekit/agents#4706)
  3. gemini-3.7-flash → 503 "high demand" again
Root cause of #1 and #3 is the endpoint, not the model: the AI Studio endpoint
(generativelanguage.googleapis.com) has NO dedicated capacity — every tier shares one
pool, so a demand spike 503s you regardless of model or billing. Vertex AI is the same
models with a 99.5% uptime SLO and optional reserved throughput; it remains selectable
here as LLM_PROVIDER=vertex if you ever want to go back to Gemini properly.

THE LLM LAYER (build_llm / _make_llm below)
  • Primary: OPENAI (gpt-5.6-terra). livekit-plugins-openai is LiveKit's most
    battle-tested LLM integration and also fronts Groq/Cerebras/Fireworks/xAI/Azure
    via openai.LLM.with_*(), so one proven code path reaches many vendors.
    Do NOT switch it to a gpt-realtime-* model: those are speech-to-speech and would
    discard the Deepgram+Cartesia cascade.
  • Claude (LLM_PROVIDER=claude) is wired but BLOCKED on livekit-plugins-anthropic
    1.7.0: the plugin sends a tool payload the API rejects with 400 "Schema is too
    complex", even though the identical tools+prompt sent directly are accepted in
    0.75s. It also has an httpx/httpx2 skew. Revisit when the plugin updates, or
    reach Claude through Bedrock (livekit-plugins-aws) instead.
  • claude-haiku-4-5 — note NO date suffix; the dated ID is rejected.
    Haiku 4.5 has no thinking mode and does not accept `effort` (it errors), so there
    is no latency knob to tune — the opposite of the Gemini 3 trap where thinking
    defaults to HIGH and silently slows every turn. 200K context, $1/$5 per MTok.
  • Fallback: Groq, via FallbackAdapter — fails over in ~100ms instead of burning ~11s
    of retries and then dying. The fallback is deliberately a DIFFERENT VENDOR: a
    second model from the same provider shares the same capacity pool and would go
    down in the same instant.
  • Swap either with LLM_PROVIDER / LLM_FALLBACK_PROVIDER: claude | groq | vertex | gemini.

⚠️ RE-TEST THE PROMPT. The prompt was tuned against Gemini — the sandwiched HARD RULES,
the tool-gate refusal strings, the objection ladder. Claude follows instructions more
literally, which generally helps a compliance script, but the behaviour will not be
identical. Re-run the disqualification paths (no policy / under 25 / sub-$300 premium),
the "are you a bot" answer, and the DNC stop before putting this on live traffic.

INSTALL
  pip install -U "livekit-agents[anthropic,groq,deepgram,cartesia,silero,google]" \
                 python-dotenv aiohttp
  ANTHROPIC_API_KEY is required; GROQ_API_KEY for the fallback.

RUN ONLY ONE smarthealth worker at a time unless you override AGENT_WORKER_NAME —
they share the agent name, so LiveKit would load-balance between them. Setting
AGENT_WORKER_NAME + a second dispatch rule is how you A/B two models on live calls.

Everything below this line is unchanged from agent_smarthealth.py.
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
  pip install "livekit-agents[deepgram,cartesia,silero]" python-dotenv aiohttp
  # optional fallbacks: livekit-plugins-elevenlabs livekit-plugins-sarvam

Env (.env):
  LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
  DEEPGRAM_API_KEY, CARTESIA_API_KEY, GOOGLE_API_KEY
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
import inspect
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
from livekit.plugins import google, silero

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

@dataclass
class Qualification:
    """One lead's captured answers. Everything is stored as the caller SAID it —
    normalisation belongs downstream, not in a live call."""
    insurer:           str = ""   # BUPA / HCF / NIB / Medibank / ...
    cover_type:        str = ""   # single / couple / family / single parent
    premium_amount:    str = ""   # as said: "$180", "two hundred"
    premium_frequency: str = ""   # fortnightly / monthly / quarterly
    hospital_extras:   str = ""   # hospital / extras / both
    permanent_resident: str = ""  # yes / no
    main_interest:     str = ""   # reduce premiums / add benefits   (MANDATORY)
    full_name:         str = ""   # one field, not first+last — see the union-type note
    email:             str = ""
    postcode:          str = ""

    # Fields an advisor cannot work a lead without.
    REQUIRED = ("insurer", "cover_type", "premium_amount", "premium_frequency",
                "main_interest", "full_name", "postcode")

    def missing_required(self) -> list[str]:
        return [f for f in self.REQUIRED if not getattr(self, f).strip()]

    def monthly_premium(self) -> float | None:
        """Normalise the premium to a monthly figure for the MIN_MONTHLY_PREMIUM gate.
        Returns None if it can't be read confidently — the gate then does NOT fire,
        because dropping a real lead on a parse failure is worse than one extra transfer."""
        import re
        m = re.search(r"\d[\d,]*(?:\.\d+)?", self.premium_amount.replace(" ", ""))
        if not m:
            return None
        try:
            amount = float(m.group().replace(",", ""))
        except ValueError:
            return None
        freq = self.premium_frequency.strip().lower()
        if "fort" in freq:      return amount * 26 / 12
        if "week" in freq:      return amount * 52 / 12
        if "month" in freq:     return amount
        if "quart" in freq:     return amount / 3
        if "annu" in freq or "year" in freq: return amount / 12
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
        "full_name":  q["full_name"],
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
                "call, not a form. Never read a list of questions in one breath.\n\n"

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
    # ⚠️ HARD API LIMIT — READ BEFORE ADDING A TOOL PARAMETER.
    # The Anthropic API rejects a request whose tool schemas contain more than 16
    # union-typed parameters, with:
    #   400 "Schemas contains too many parameters with union types ... (limit: 16)"
    # An OPTIONAL parameter (one with a default) is serialised as a union/nullable type
    # and counts; a REQUIRED parameter does not. We hit exactly 20 and every call died,
    # so the count is deliberately kept low here:
    # Trimming to 11 optional params cleared that check but then hit a second, broader
    # one: 400 "Schema is too complex." So EVERY parameter here is now REQUIRED — a
    # required param serialises as a plain {"type":"string"} with no anyOf node at all,
    # which is the simplest schema the API can be given. The cost is that the model must
    # pass "" for fields it has not learned yet; the docstrings say so explicitly and
    # save_qualification ignores empty values.
    #   • ZERO optional parameters across all 9 tools — keep it that way
    # If you add optional params, re-count first. Gemini tolerates a fat schema; Claude
    # will refuse the entire request, which reads as "the bot never speaks".
    @function_tool
    async def save_qualification(
        self, context: RunContext,
        insurer: str, cover_type: str, premium_amount: str,
        premium_frequency: str, hospital_extras: str,
        permanent_resident: str, main_interest: str,
        full_name: str, email: str, postcode: str,
    ):
        """Record qualification answers. Call this every time you learn something new.

        PASS EVERY ARGUMENT. For anything the caller has NOT told you yet, pass an
        empty string "" — never guess a value, never invent one. Previously-saved
        fields are kept, so sending "" never erases an earlier answer.
        Use the caller's own words.
        insurer: BUPA / HCF / NIB / Medibank / ...
        cover_type: single / couple / family / single parent.
        premium_amount: the figure as said ('$180', 'about two hundred').
        premium_frequency: fortnightly / monthly / quarterly.
        hospital_extras: hospital / extras / both.
        permanent_resident: yes / no.
        main_interest: 'reduce premiums' or 'add benefits' — MANDATORY before handoff.
        full_name: their name as given. email/postcode: as given."""
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
    async def request_handoff(self, context: RunContext, remarks: str):
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
                            remarks: str):
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
    async def disqualify(self, context: RunContext, reason: str, remarks: str):
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
    async def register_dnc(self, context: RunContext, remarks: str):
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
                              remarks: str, decline_reason: str):
        """Record a final outcome that isn't covered by the other tools.
        outcome: NOT_INTERESTED (requires decline_reason, in their own words) or
        NO_OUTCOME (talked, no decision reached).
        decline_reason: required argument — their stated reason for NOT_INTERESTED,
        or an empty string "" for any other outcome.
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


def _make_llm(provider: str):
    """One LLM instance by provider name. Kept separate from build_llm() so the same
    factory builds both the primary and the fallback."""
    provider = provider.strip().lower()

    if provider == "inference":
        # LiveKit Inference — LiveKit's OWN model gateway, so it is the best-integrated
        # path by definition. Their published tool-heavy voice benchmark ("Hotel
        # Receptionist", 100 scenarios) puts google/gemma-4-31b-it top on all three axes
        # at once: 82% pass (ties the best), 136ms median TTFT (4.6x faster than
        # gpt-5.4), $0.40/$1.20 per 1M (~12x cheaper).
        #   https://livekit.com/benchmarks/capabilities
        # ⚠️ AUTH: this uses LIVEKIT_API_KEY/SECRET, but they must be LiveKit CLOUD
        # credentials with inference grants — the self-hosted server's own `mykey` will
        # 401, exactly like the cloud turn detector and adaptive interruption do here.
        # Media stays self-hosted; only model calls go to the gateway.
        # ⚠️ Community reports a ~400ms TTFT floor on the shared gateway vs the 136ms
        # benchmark figure, so measure it on your own traffic before assuming 136ms.
        from livekit.agents import inference
        model = os.getenv("INFERENCE_MODEL", "google/gemma-4-31b-it")
        log.info(f"[smarthealth] LLM(inference): {model}")
        return inference.LLM(model=model)

    if provider == "openai":
        from livekit.plugins import openai as openai_plugin
        # TEXT model — NOT a gpt-realtime-* model. The realtime family is
        # speech-to-speech (audio in, audio out); using one would discard our
        # Deepgram STT + Cartesia TTS cascade and is the architecture we already
        # rejected for telephony (it cannot endpoint phone audio reliably).
        # Pick from models LiveKit has ACTUALLY BENCHMARKED on a tool-heavy voice task
        # (https://livekit.com/benchmarks/capabilities) — pass rate / median TTFT / cost:
        #   gpt-4.1        77% / 547ms / $2.00-$8.00   <- DEFAULT: not a reasoning model,
        #                                                 so it sidesteps the whole
        #                                                 reasoning_effort-vs-tools trap
        #   gpt-5.6-luna   77% / 684ms / $1.00-$6.00   cheaper; reasoning model
        #   gpt-5.4        82% / 626ms / $5.00-$22.50  best pass rate, pricey
        # NOTE gpt-5.6-terra is NOT in the benchmark — it was a guess from the model
        # docs and is why we ended up debugging instead of talking. Prefer a row above.
        model = os.getenv("OPENAI_MODEL", "gpt-4.1")
        kwargs = {"model": model}

        # ── reasoning_effort="none" is LOAD-BEARING. Do not remove. ──
        # gpt-5.6-* are REASONING models. On /v1/chat/completions the API rejects
        # reasoning together with function tools:
        #   400 "Function tools with reasoning_effort are not supported for
        #        gpt-5.6-terra ... set reasoning_effort to 'none'."
        # which on a call looks like the bot never speaking. We want reasoning off
        # regardless — on a phone call it is pure added latency before the first
        # token, the same trap as Gemini 3 defaulting thinking_level to HIGH.
        effort = os.getenv("OPENAI_REASONING_EFFORT", "none")
        if "reasoning_effort" in inspect.signature(openai_plugin.LLM.__init__).parameters:
            kwargs["reasoning_effort"] = effort
        else:
            log.error("[smarthealth] livekit-plugins-openai has no reasoning_effort "
                      "option — a gpt-5.6-* model will 400 when tools are attached. "
                      "Upgrade the plugin, or set OPENAI_MODEL to a non-reasoning model.")

        log.info(f"[smarthealth] LLM(openai): {model} "
                 f"(reasoning_effort={kwargs.get('reasoning_effort', 'unsupported')})")
        return openai_plugin.LLM(**kwargs)

    if provider == "claude":
        from livekit.plugins import anthropic as anthropic_plugin
        # claude-haiku-4-5 — note NO date suffix; the dated form is rejected.
        # Haiku 4.5 has no thinking mode (nothing to disable — it is fast by default),
        # and `effort` is NOT a valid param on it: passing one errors. So unlike Gemini 3
        # there is no latency knob to set here, which is exactly what we want on a call.
        model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
        kwargs = {"model": model}

        # ── strict_tool_schema=False is LOAD-BEARING. Do not "tidy" it away. ──
        # Anthropic compiles STRICT tool schemas into a sampling grammar before it will
        # answer. Our 9 tools (save_qualification alone carries 10 properties, each with
        # a description) blow the grammar-size limit, and EVERY request then fails with
        #     400 invalid_request_error: "Schema is too complex."
        # ...which on a call looks like the bot simply never speaking. Non-strict tools
        # skip grammar compilation entirely; this is Anthropic's documented workaround.
        # What we give up is strict type coercion, which this agent never relied on:
        # every tool parameter is a plain string and the tools validate their own input.
        if "strict_tool_schema" in inspect.signature(anthropic_plugin.LLM.__init__).parameters:
            kwargs["strict_tool_schema"] = False
        else:
            log.error("[smarthealth] livekit-plugins-anthropic has no strict_tool_schema "
                      "option — with this many tools expect 400 'Schema is too complex'. "
                      "Upgrade: pip install -U livekit-plugins-anthropic")

        log.info(f"[smarthealth] LLM(claude): {model} "
                 f"(strict_tool_schema={kwargs.get('strict_tool_schema', 'unsupported')})")
        try:
            return anthropic_plugin.LLM(**kwargs)
        except TypeError as e:
            # DEPENDENCY VERSION SKEW, not a config problem: newer `anthropic` SDKs
            # moved to httpx2, but an older livekit-plugins-anthropic still constructs
            # an httpx.AsyncClient and hands it over, which the SDK rejects with
            # "Expected an instance of httpx2.AsyncClient but got httpx.AsyncClient".
            # The plugin only builds that client when none is passed (`client or ...`),
            # so supplying our own skips the broken path entirely. The proper fix is
            # aligning the two packages; this keeps calls flowing until you do.
            if "http_client" not in str(e):
                raise
            from anthropic import AsyncClient
            log.warning(
                "[smarthealth] livekit-plugins-anthropic / anthropic SDK version skew "
                "(httpx vs httpx2) — supplying our own AsyncClient as a workaround. "
                "Fix properly: pip install -U livekit-plugins-anthropic anthropic"
            )
            return anthropic_plugin.LLM(**kwargs, client=AsyncClient())

    if provider == "groq":
        # NOTE: livekit-plugins-openai also fronts Groq/Cerebras/Fireworks/xAI/etc via
        # openai.LLM.with_groq(...) / .with_cerebras(...). That path rides the most
        # battle-tested LLM plugin in LiveKit, so prefer it if this dedicated plugin
        # ever misbehaves the way livekit-plugins-anthropic did.
        from livekit.plugins import groq
        # ⚠️ VERIFY against Groq's current catalog — their model IDs get retired often,
        # and standard-tier RPM limits are low enough to cap concurrent calls.
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        log.info(f"[smarthealth] LLM(groq): {model}")
        return groq.LLM(model=model)

    if provider == "vertex":
        # Same Gemini models, but through Google Cloud instead of the AI Studio shared
        # pool: 99.5% uptime SLO + optional dedicated throughput, which is the actual
        # fix for the 503s. Needs a GCP project and GOOGLE_APPLICATION_CREDENTIALS.
        model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
        location = os.getenv("VERTEX_LOCATION", "australia-southeast1")
        project = os.getenv("VERTEX_PROJECT", "")
        kwargs = {"model": model, "vertexai": True, "location": location}
        if project:
            kwargs["project"] = project
        try:
            llm = google.LLM(**kwargs, thinking_level=os.getenv("GEMINI_THINKING_LEVEL", "low"))
        except TypeError:
            log.error("[smarthealth] plugin has no thinking_level — Gemini 3 will run on "
                      "HIGH thinking (slow). pip install -U 'livekit-agents[google]'")
            llm = google.LLM(**kwargs)
        log.info(f"[smarthealth] LLM(vertex): {model} @ {location}")
        return llm

    if provider == "gemini":
        # AI Studio shared endpoint — NO dedicated capacity. This is what was throwing
        # 503 "high demand" on both 2.5 and 3.7. Kept only for comparison testing.
        model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
        try:
            llm = google.LLM(model=model,
                             thinking_level=os.getenv("GEMINI_THINKING_LEVEL", "low"))
        except TypeError:
            llm = google.LLM(model=model)
        log.warning(f"[smarthealth] LLM(gemini/AI-Studio): {model} — no dedicated "
                    f"capacity, expect intermittent 503s under load")
        return llm

    raise ValueError(f"unknown LLM provider {provider!r} "
                     f"(inference | openai | claude | groq | vertex | gemini)")


def build_llm():
    """Primary LLM wrapped in a cross-PROVIDER FallbackAdapter.

    Why the fallback is not optional: this deployment has now been broken three separate
    times by a single provider — Gemini 503 'high demand' (2.5), empty streaming
    responses (2.5, livekit/agents#4706), then 503 again on 3.7. Each one cost ~11s of
    retries and then a dead turn. No provider is reliable enough to run alone on live
    calls. The adapter fails over in ~100ms, so an outage becomes a hiccup instead of a
    hung call. Crucially the fallback must be a DIFFERENT VENDOR — a second model from
    the same provider shares the same capacity pool and dies at the same moment.

    LLM_PROVIDER / LLM_FALLBACK_PROVIDER: inference | openai | claude | groq | vertex
    | gemini.
    Set LLM_FALLBACK_PROVIDER="" to disable the fallback (not recommended)."""
    primary_name = os.getenv("LLM_PROVIDER", "openai")
    fallback_name = os.getenv("LLM_FALLBACK_PROVIDER", "groq").strip()

    try:
        primary = _make_llm(primary_name)
    except Exception as e:
        # Construction failures (bad/missing key, plugin-vs-SDK version skew) happen
        # BEFORE any request, so FallbackAdapter cannot rescue them — it only covers
        # errors at call time. Without this the whole job task dies and the caller hears
        # dead air. Promote the fallback to primary instead of dropping the call.
        log.error(f"[smarthealth] primary provider {primary_name!r} FAILED TO "
                  f"INITIALISE ({e}) — promoting fallback {fallback_name!r} to primary.")
        if not fallback_name:
            raise
        return _make_llm(fallback_name)

    if not fallback_name or fallback_name.lower() == primary_name.strip().lower():
        log.warning("[smarthealth] no LLM fallback configured — a provider outage will "
                    "kill live calls. Set LLM_FALLBACK_PROVIDER.")
        return primary

    try:
        fallback = _make_llm(fallback_name)
    except Exception as e:
        # A missing fallback key must not stop the worker from serving calls.
        log.error(f"[smarthealth] fallback provider {fallback_name!r} unavailable ({e}) "
                  f"— running on {primary_name} ALONE, unprotected.")
        return primary

    try:
        from livekit.agents import llm as _llm
        adapter = _llm.FallbackAdapter([primary, fallback])
        log.info(f"[smarthealth] LLM failover: {primary_name} → {fallback_name}")
        return adapter
    except (ImportError, AttributeError) as e:
        log.error(f"[smarthealth] FallbackAdapter unavailable ({e}) — running on "
                  f"{primary_name} ALONE. Upgrade livekit-agents.")
        return primary


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

    session = AgentSession(
        stt=build_stt(),
        llm=build_llm(),        # Claude Haiku 4.5 + cross-vendor failover — see build_llm()
        tts=build_tts(),
        vad=ctx.proc.userdata["vad"],
        # With Deepgram's interim transcripts a word-gate CAN fire mid-sentence, so a
        # stray background "yeah" no longer cuts the bot off. Only enable when the STT
        # actually streams interims — on Sarvam this makes the bot talk over the caller
        # until they stop. See DEVELOPER_GUIDE §11.1.
        **({"min_interruption_words": 2}
           if os.getenv("STT_PROVIDER", "deepgram").lower() == "deepgram" else {}),
    )

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
            # MUST match agentName in the SIP dispatch rule. Override to run this
            # alongside the 2.5 worker for an A/B (needs its own dispatch rule + DID).
            agent_name=os.getenv("AGENT_WORKER_NAME", "smarthealth-bot"),
            num_idle_processes=3,
        )
    )
