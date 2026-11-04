"""
agent_smarthealth_web.py — the browser-demo worker.

WHAT IT IS
  The SAME agent as agent_smarthealth_claude_dg.py — same script, same 9 tools, same
  compliance gates, same voice — registered under a DIFFERENT agent_name so it can be
  dispatched from a web page without ever competing with telephony.

      SIP  call  -> dispatch rule -> agent_name "smarthealth-bot"   (unchanged)
      web  demo  -> /api/token    -> agent_name "smarthealth-web"   (this file)

WHY IT IMPORTS RATHER THAN COPIES
  A forked 1900-line copy drifts the first time the script changes, and then the demo
  stops matching what the client actually bought. This file imports the production
  module and adds exactly one thing on top: a read-only telemetry feed on a LiveKit
  data channel, so the web UI can render the lead brief filling in live.

  It changes NO behaviour. Everything it adds is wrapped so that a failure in the
  telemetry can never take down a call — worst case the panel stops updating and the
  conversation carries on.

HOW THE TELEMETRY WORKS
  Two small monkeypatches capture references the base entrypoint keeps local:
    - SmartHealthAgent.__init__  -> stash the agent (gives us agent.qual, .outcome, ...)
    - AgentSession.start         -> stash the session (gives us transcript events)
  Then a 400ms poller diffs the Qualification dataclass and publishes a snapshot when
  anything changes. Polling beats wrapping the tools: it touches none of the tool
  internals, so it cannot alter a gate's return value or break the premium check.

  Topics published (JSON, reliable):
    demo.state       full snapshot: fields, outcome, consent, progress, premium
    demo.transcript  {role, text, final} per utterance
    demo.event       {kind, label} milestones — consent, gates, outcome changes

Install:  same venv as the production agent. Nothing extra.
Run:      ./.venv/bin/python agent_smarthealth_web.py start

Env:
  DEMO_BASE_MODULE=agent_smarthealth_claude_dg   # swap to agent_smarthealth for Gemini
  DEMO_AGENT_NAME=smarthealth-web                # must match the web server's
  MACHINE_GUARD=0                                # see below — set in the .service file

⚠️ MACHINE_GUARD IS DELIBERATELY OFF FOR THIS WORKER. The base agent hangs up when a
dialled number is answered by an IVR, voicemail or another company's AI — a real
production failure where our bot reframed such a call as a "training exercise" and
captured a fabricated lead. The web demo has no dialled number: the other party is a
human who clicked a button, so the guard can only produce false positives, and one of
those ends a client demo mid-sentence. The PROMPT-level rule (HARD RULE 0) is inherited
and stays on — it stops the bot discussing its own script, which is desirable here too.
"""

import os
import json
import asyncio
import logging
import importlib
from dataclasses import asdict

from dotenv import load_dotenv
load_dotenv()

from livekit.agents import AgentSession, JobContext, WorkerOptions, cli

log = logging.getLogger("smarthealth.web")

BASE_MODULE = os.getenv("DEMO_BASE_MODULE", "agent_smarthealth_claude_dg")
WEB_AGENT_NAME = os.getenv("DEMO_AGENT_NAME", "smarthealth-web")
# NOT 8081 — that is the LiveKit default and the telephony worker already has it.
WEB_HTTP_PORT = int(os.getenv("DEMO_WORKER_HTTP_PORT", "8082"))
POLL_INTERVAL = 0.4          # snapshot cadence; imperceptible in a UI, trivial on CPU
HEARTBEAT_EVERY = 2.5        # republish unchanged state so a late-joining tab catches up

base = importlib.import_module(BASE_MODULE)
log.info("[web] wrapping %s (agent_name=%s)", BASE_MODULE, WEB_AGENT_NAME)

# Live handles the base entrypoint keeps as locals. Populated by the patches below.
_live: dict = {}

_orig_agent_init = base.SmartHealthAgent.__init__


def _patched_agent_init(self, *a, **kw):
    _orig_agent_init(self, *a, **kw)
    _live["agent"] = self


base.SmartHealthAgent.__init__ = _patched_agent_init

_orig_session_start = AgentSession.start


async def _patched_session_start(self, *a, **kw):
    _live["session"] = self
    return await _orig_session_start(self, *a, **kw)


AgentSession.start = _patched_session_start

# ── Per-call TTS override ────────────────────────────────────────────────────
# The demo page can pick the voice engine so the two can be A/B'd in front of a client.
# base.build_tts() reads TTS_PROVIDER/TTS_VOICE from the environment, which is fixed for
# the life of a process — so we swap the function rather than the environment.
#
# This patch lives in the WEB worker's process only. The telephony worker runs a
# different file in a different process and never imports this module, so its TTS is
# untouched no matter what the page selects.
_orig_build_tts = base.build_tts


def _patched_build_tts():
    choice = _live.get("tts") or ""
    if not choice:
        return _orig_build_tts()          # no selection → the .env default
    provider, _, voice = choice.partition(":")
    try:
        if provider == "cartesia":
            from livekit.plugins import cartesia
            log.info("[web] TTS(cartesia): sonic-2 voice=%s", voice)
            return cartesia.TTS(model="sonic-2", **({"voice": voice} if voice else {}))
        if provider == "deepgram":
            from livekit.plugins import deepgram
            model = voice or base.DEEPGRAM_AU_VOICE
            log.info("[web] TTS(deepgram): %s", model)
            return deepgram.TTS(model=model)
        raise ValueError(f"unknown provider {provider!r}")
    except Exception as e:
        # A missing CARTESIA_API_KEY or an uninstalled plugin must not kill the call —
        # degrade to whatever the environment already does successfully.
        log.error("[web] TTS %r unavailable (%s) — falling back to the configured "
                  "default. Check the provider's API key is in .env.", choice, e)
        return _orig_build_tts()


base.build_tts = _patched_build_tts


# ── Publishing ───────────────────────────────────────────────────────────────
async def _send(ctx: JobContext, topic: str, obj: dict):
    """Fire-and-forget publish. Silent on failure by design: the page is a nicety,
    the call is the product."""
    try:
        lp = getattr(ctx.room, "local_participant", None)
        if lp is None:
            return
        await lp.publish_data(json.dumps(obj), reliable=True, topic=topic)
    except Exception as e:
        log.debug("[web] publish %s failed: %s", topic, e)


# Order matters — this is the order the panel renders, which mirrors the call script.
FIELD_ORDER = [
    ("holds_policy",       "Holds private cover"),
    ("insurer",            "Current insurer"),
    ("over_min_age",       "Aged 25 or over"),
    ("cover_type",         "Cover type"),
    ("premium_amount",     "Premium"),
    ("premium_frequency",  "Paid"),
    ("hospital_extras",    "Hospital / extras"),
    ("permanent_resident", "Permanent resident"),
    ("main_interest",      "Main interest"),
    ("decision_maker",     "Decision maker"),
    ("first_name",         "First name"),
    ("last_name",          "Last name"),
    ("email",              "Email"),
    ("postcode",           "Postcode"),
]


def _snapshot(agent) -> dict:
    qual = agent.qual
    fields = asdict(qual)
    try:
        missing = qual.missing_required()
    except Exception:
        missing = []
    try:
        monthly = qual.monthly_premium()
    except Exception:
        monthly = None
    required = list(getattr(qual, "REQUIRED", ()))
    captured = [f for f in required if f not in missing]
    return {
        "fields": fields,
        "order": [{"key": k, "label": lbl} for k, lbl in FIELD_ORDER],
        "required": required,
        "missing": missing,
        "capturedCount": len(captured),
        "requiredCount": len(required),
        "monthlyPremium": round(monthly, 2) if monthly is not None else None,
        "minMonthlyPremium": getattr(base, "MIN_MONTHLY_PREMIUM", None),
        "outcome": getattr(agent, "outcome", ""),
        "consent": bool(getattr(agent, "consent_given", False)),
        "remarks": getattr(agent, "remarks", ""),
        "declineReason": getattr(agent, "decline_reason", ""),
        "callback": getattr(agent, "callback_request", ""),
        "handoffRequested": bool(getattr(agent, "_handoff_requested", False)),
    }


def _diff_events(prev: dict, cur: dict) -> list[dict]:
    """Turn a state delta into human-readable milestones for the activity feed. This is
    what makes the demo legible to a non-technical viewer: they see WHY the bot did
    something, not just that a field changed."""
    out = []
    if prev is None:
        return out
    labels = {k: lbl for k, lbl in FIELD_ORDER}
    for key, val in cur["fields"].items():
        if val and val != prev["fields"].get(key):
            out.append({"kind": "field",
                        "label": f"{labels.get(key, key)}: {val}"})
    if cur["consent"] and not prev["consent"]:
        out.append({"kind": "consent", "label": "Explicit consent recorded"})
    if cur["handoffRequested"] and not prev["handoffRequested"]:
        out.append({"kind": "handoff", "label": "Handoff to advisor requested"})
    if cur["callback"] and cur["callback"] != prev["callback"]:
        out.append({"kind": "callback", "label": f"Callback booked: {cur['callback']}"})
    if cur["outcome"] != prev["outcome"]:
        out.append({"kind": "outcome", "label": f"Outcome set: {cur['outcome']}"})
    mp, floor = cur["monthlyPremium"], cur["minMonthlyPremium"]
    if mp is not None and prev["monthlyPremium"] is None:
        label = f"Premium normalised to ${mp:,.0f}/mo"
        if floor:
            verdict = "below" if mp < floor else "above"
            label += f" — {verdict} the ${floor:,.0f} threshold"
        out.append({"kind": "gate", "label": label})
    return out


async def _telemetry_loop(ctx: JobContext):
    prev, last_beat, wired = None, 0.0, False
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
            agent = _live.get("agent")
            if agent is None:
                continue

            # Transcript wiring, once the session exists.
            session = _live.get("session")
            if session is not None and not wired:
                wired = _wire_transcript(ctx, session)

            cur = _snapshot(agent)
            now = loop.time()
            if cur != prev:
                for ev in _diff_events(prev, cur):
                    await _send(ctx, "demo.event", ev)
                await _send(ctx, "demo.state", cur)
                prev, last_beat = cur, now
            elif now - last_beat > HEARTBEAT_EVERY:
                await _send(ctx, "demo.state", cur)
                last_beat = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("[web] telemetry tick failed: %s", e)


def _wire_transcript(ctx: JobContext, session) -> bool:
    """Publish transcript lines on our own topic. LiveKit already ships native
    transcriptions, but their delivery API has moved between versions; owning both ends
    of this channel means the demo cannot be broken by an SDK bump."""
    try:
        # src="stt": raw recogniser output, for the live typing effect only. Deepgram
        # emits SEVERAL is_final segments per spoken turn ("Yes. I'm" then "twenty nine,
        # actually."), so these are never committed to the transcript on their own —
        # the page accumulates them into one provisional bubble.
        @session.on("user_input_transcribed")
        def _on_user(ev):
            text = (getattr(ev, "transcript", "") or "").strip()
            if not text:
                return
            asyncio.create_task(_send(ctx, "demo.transcript", {
                "role": "user", "text": text, "src": "stt",
                "final": bool(getattr(ev, "is_final", True)),
            }))

        # src="turn": the committed utterance as the LLM actually received it. This is
        # the authoritative line and the only one the page keeps.
        @session.on("conversation_item_added")
        def _on_item(ev):
            item = getattr(ev, "item", None)
            if item is None:
                return
            role = str(getattr(item, "role", ""))
            text = (getattr(item, "text_content", "") or "").strip()
            if not text or role not in ("assistant", "user"):
                return
            asyncio.create_task(_send(ctx, "demo.transcript", {
                "role": "agent" if role == "assistant" else "user",
                "text": text, "src": "turn", "final": True,
            }))

        # ── Latency telemetry ───────────────────────────────────────────────
        # What the caller experiences as "the pause" is three things in series:
        #   EOU  — how long endpointing takes to decide they stopped talking
        #   LLM  — time to first token
        #   TTS  — time to first audio byte
        # Publishing the breakdown turns "which voice sounds snappier" into a number,
        # which is the only way an A/B between two engines means anything.
        @session.on("metrics_collected")
        def _on_metrics(ev):
            m = getattr(ev, "metrics", None)
            if m is None:
                return
            kind = type(m).__name__
            out = None
            if kind == "TTSMetrics" and not getattr(m, "cancelled", False):
                out = {"kind": "tts", "ms": round((getattr(m, "ttfb", 0) or 0) * 1000),
                       "label": str(getattr(m, "label", "") or "")}
            elif kind == "LLMMetrics" and not getattr(m, "cancelled", False):
                out = {"kind": "llm", "ms": round((getattr(m, "ttft", 0) or 0) * 1000)}
            elif kind == "EOUMetrics":
                out = {"kind": "eou",
                       "ms": round((getattr(m, "end_of_utterance_delay", 0) or 0) * 1000)}
            # A zero or negative reading means the plugin didn't report it; publishing it
            # would drag the median toward a number nothing actually experienced.
            if out and out["ms"] > 0:
                asyncio.create_task(_send(ctx, "demo.metrics", out))

        log.info("[web] transcript + metrics feed wired")
        return True
    except Exception as e:
        log.warning("[web] could not wire session events: %s", e)
        return True     # don't retry every tick


# ── Entrypoint ───────────────────────────────────────────────────────────────
async def entrypoint(ctx: JobContext):
    _live.clear()

    # Which voice engine this call should use, chosen on the page and carried in the
    # dispatch metadata. Already whitelisted server-side, so it is safe to act on.
    try:
        meta = json.loads(getattr(ctx.job, "metadata", "") or "{}")
    except Exception:
        meta = {}
    _live["tts"] = (meta.get("tts") or "").strip()
    if _live["tts"]:
        log.info("[web] per-call TTS requested: %s", _live["tts"])

    task = asyncio.create_task(_telemetry_loop(ctx))

    async def _stop_telemetry():
        task.cancel()
    ctx.add_shutdown_callback(_stop_telemetry)

    try:
        # The real thing: connect, greet, qualify, dispose. Untouched.
        await base.entrypoint(ctx)
    except Exception:
        task.cancel()
        raise


if __name__ == "__main__":
    _opts = dict(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=base.prewarm,
        agent_name=WEB_AGENT_NAME,
        # Demos are one call at a time; the telephony worker keeps its own 3.
        num_idle_processes=1,
    )
    # Every worker runs a health-check HTTP server, default :8081. The telephony worker
    # on this box already holds that port, so a SECOND worker must be moved or it
    # crash-loops on EADDRINUSE before it ever registers. Loopback-only: nothing outside
    # the box has any business reaching it.
    try:
        opts = WorkerOptions(**_opts, host="127.0.0.1", port=WEB_HTTP_PORT)
        log.info("[web] health-check server on 127.0.0.1:%d", WEB_HTTP_PORT)
    except TypeError as e:
        log.warning("[web] WorkerOptions has no host/port in this version (%s) — if the "
                    "worker now fails with 'address already in use', set the telephony "
                    "worker and this one to different ports another way.", e)
        opts = WorkerOptions(**_opts)
    cli.run_app(opts)
