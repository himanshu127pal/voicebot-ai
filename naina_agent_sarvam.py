"""
naina_agent_sarvam.py — Production stack: Sarvam STT + Sarvam TTS + Gemini LLM.

Indian-language voice bot on LiveKit Agents (no Pipecat). Runs as an
agent-dispatch worker; LiveKit spawns it per inbound SIP call. Handles the full
call: greet → qualify → pitch → dispose → hang up, with voicemail detection,
silence nudges, a max-call-duration cap, and a disposition webhook.

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
"""

import os
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

# Language for this deployment. Sarvam supports hi-IN, en-IN, ta-IN, te-IN,
# bn-IN, gu-IN, kn-IN, ml-IN, mr-IN, od-IN, pa-IN. Set per campaign.
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
            instructions=(
                # TODO: refine with your real sales script / persona.
                "You are Naina, a warm, confident sales agent on a PHONE CALL, speaking "
                "in the caller's language (Hindi/English). YOU lead the call — never wait "
                "passively or leave dead air. "
                # ── Concise but not passive (short turns = fast TTS) ──
                "Speak in short, natural turns of 1-2 sentences. Deliver your pitch in "
                "small steps, ONE point per turn — never a long paragraph. "
                # ── Always drive forward ──
                "Always move forward: END EVERY TURN by continuing to your next point or "
                "asking a question. Never end on a passive statement that leaves silence. "
                # ── Handle detours, then re-anchor ──
                "If the caller asks a side question (e.g. your name), answer it in a few "
                "words, THEN immediately return to what you were saying and continue — do "
                "NOT wait for them to acknowledge. "
                "If the caller is silent or just says 'ok'/'haan', continue to your next "
                "point yourself. "
                # ── Agenda ──
                "Agenda: greet -> confirm you're speaking to the right person -> check "
                "they're a business owner -> pitch the marketing platform one benefit at a "
                "time -> handle questions briefly -> ask if they're interested -> when an "
                "outcome is reached, call set_disposition with one of: "
                "BOOKED, CALLBACK, NOT_INTERESTED, DNC, OUT_OF_ZONE. "
                # ── Answering machine ──
                "If you reach an answering machine or voicemail — a recorded greeting, "
                "'leave a message after the tone', or a monologue that ignores you — call "
                "voicemail_detected IMMEDIATELY instead of pitching. "
                # ── Ending the call ──
                "When the conversation is finished (an outcome is reached, or the caller "
                "wants to hang up), say a short one-sentence goodbye, THEN call end_call "
                "to hang up. Always set_disposition before end_call. "
                f"{name_clause}"
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
        # ── Production stack: Sarvam STT/TTS + Groq LLM ──
        # STT: Saaras v3 (streaming, sub-150ms TTFT) replaces the slow/deprecated
        # saarika:v2.5 — this is the #1 latency fix.
        stt=sarvam.STT(model="saaras:v3", mode="transcribe", language=LANG),
        llm=google.LLM(model="gemini-2.5-flash"),             # text LLM (NOT realtime); reads GOOGLE_API_KEY
        # TTS: linear16 (raw PCM) instead of mp3 → no decode step before playout.
        tts=sarvam.TTS(target_language_code=LANG, output_audio_codec="linear16"),
        vad=ctx.proc.userdata["vad"],   # preloaded in prewarm() — not per-call
    )

    async def _on_shutdown():
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
    ctx.add_shutdown_callback(_on_shutdown)

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
    NUDGE_AFTER = 6.0      # seconds of silence before nudging
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
            await session.generate_reply(
                instructions=(
                    "The caller has gone silent. Continue the call yourself in one short "
                    "sentence: move to your next point or gently re-ask. Do not repeat "
                    "your last line verbatim."
                )
            )
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
    proc.userdata["vad"] = silero.VAD.load()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,        # preload VAD into each warm process
            agent_name="naina-bot",     # MUST match agentName in the SIP dispatch rule
            num_idle_processes=3,       # keep 3 warm processes ready (8-core box)
        )
    )
