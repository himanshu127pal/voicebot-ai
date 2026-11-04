"""
agent_voicebot_web.py — ONE browser-demo worker for every telephony voicebot.

WHAT IT IS
  The web demo lets a visitor pick a voicebot and talk to it. Each bot is the SAME
  production agent that takes phone calls — same file, same prompt, same tools, same
  gates — run by this worker under a single agent_name, "voicebot-web":

      SIP  -> smarthealth-bot / roof-bot / finance-bot / movers-bot / warranty-bot
      web  -> /api/token {bot: "roof"} -> "voicebot-web"              (this file)
                 -> runs /opt/livekit/roof-bot/agent_roof_uk.py for that one call

  It replaces agent_smarthealth_web.py (smarthealth-web.service), which could only run
  Smart Health. Nothing on the telephony side imports or knows about this file.

  It also runs DEMO-ONLY bots that have no telephony service at all — mortgage and
  energy. Their agent files live in /opt/livekit/web-demo/bots/, and a .env there is
  optional: without one a bot runs on its code defaults and the worker's shared API keys.

WHY ONE WORKER, NOT ONE PER BOT
  Every worker keeps an idle process holding its own copy of Silero VAD. One demo worker
  per bot would multiply that on a box already running the telephony workers. One worker
  with num_idle_processes=1 costs exactly what smarthealth-web did.

THE HARD PART: MANY BOTS, ONE WORKER, CONFLICTING CONFIG
  Every agent reads COMPANY_NAME, AGENT_NAME, TTS_VOICE, STT_LANGUAGE,
  MAX_CALL_SECONDS and DISPOSITION_WEBHOOK_URL at IMPORT time, each from its own .env.
  Importing them all into one process would let whichever .env loaded first win — and
  the UK roof bot would introduce itself as Sarah from Smart Health, in an Australian
  accent.

  So no bot is imported up front. Every call already runs in its own job subprocess,
  and inside it, before the bot's code runs:
    1. the environment is rebuilt for the chosen bot, from the worker's starting
       environment: every key that ANY bot's .env defines is cleared (API credentials
       fall back to the worker's own), that bot's .env is applied, and the demo
       overrides go on top. Nothing outside those keys is touched
    2. the bot's file is executed as a FRESH module, with load_dotenv() made a no-op for
       the duration, so it cannot quietly re-add another bot's values from wherever
       python-dotenv happens to look
    3. its entrypoint runs, unchanged
  The bot therefore runs with exactly the config it has on the phone, apart from the
  overrides below. bot_environ() is pure so this is testable without LiveKit.

DEMO OVERRIDES — the only behaviour that differs from a phone call
  • MACHINE_GUARD=0. The guard hangs up when a dialled number reaches an IVR or another
    company's AI. Nobody is dialled here — a person clicked a button — so it can only
    misfire, and a misfire ends a client demo mid-sentence. HARD RULE 0 in each prompt
    stays on.
  • DISPOSITION_WEBHOOK_URL is blanked unless DEMO_POST_DISPOSITIONS=1. Otherwise demo
    calls land in the production lead tables — and on the finance bot, a prospect trying
    the demo with their real email would be sent a Quick Funding information pack. Each
    demo call's disposition still goes to DEMO_DATA_DIR/<bot>.csv.
  • TTS can be swapped per call via the page's ?tts= parameter (whitelisted server-side).
    Absent by default, so each bot speaks in its own production voice.
  • WEB_DEMO=1. On the phone these bots offer to use "the number you're calling from";
    on the page nobody is on a call, so they read the visitor's verified number back and
    ask them to confirm it instead. Only the bots that ask for a number look at it.

TELEMETRY — read-only; a failure here can never take down a call
  AgentSession.start is wrapped to capture the agent and session. A 400ms poller then
  publishes the agent's qualification dataclass on the data channel:
    demo.state       snapshot: bot names, fields, missing, outcome, consent, done
    demo.event       {kind, label} milestones                (page: console only)
    demo.transcript  {role, text, src, final}                (page: console only)
    demo.metrics     {kind: eou|llm|tts, ms}                 (page: console only)
    demo.error       {message} — the chosen bot could not be started

Install:  same venv as the telephony agents. Nothing extra.
Run:      /opt/livekit/bot/.venv/bin/python agent_voicebot_web.py start
Env:      see voicebot-web.service
"""

import os
import re
import sys
import json
import asyncio
import logging
import importlib
import importlib.util
from dataclasses import asdict, is_dataclass

import dotenv

# The worker process needs LiveKit credentials to register. They're identical for every
# bot, so they come from one .env. override=False: anything set in the unit file wins.
# Per-bot settings are NOT taken from here — they're rebuilt per call, below.
WEB_BASE_ENV = os.getenv("WEB_BASE_ENV", "/opt/livekit/bot/.env")
dotenv.load_dotenv(WEB_BASE_ENV)
# The environment every call is rebuilt FROM. Taken once, here, so a call's settings can
# never depend on which bot the previous call in this process happened to run.
_BASE_ENVIRON = dict(os.environ)

from livekit import api
from livekit.agents import AgentSession, JobContext, WorkerOptions, cli

log = logging.getLogger("voicebot.web")

# Plugins register themselves on import and must do so on the main thread. Import every
# plugin any bot uses here, at module level, so a bot importing one inside a job finds
# it already registered. google and cartesia are optional; the others are not.
for _plugin in ("silero", "deepgram", "anthropic", "google", "cartesia"):
    try:
        importlib.import_module(f"livekit.plugins.{_plugin}")
    except Exception as _e:
        log.warning("[web] plugin livekit.plugins.%s unavailable: %s", _plugin, _e)
from livekit.plugins import silero  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════
def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


WEB_AGENT_NAME = os.getenv("DEMO_AGENT_NAME", "voicebot-web")
# 8081 smarthealth-bot, 8082 smarthealth-web (retired by this), 8083 roof-bot,
# 8084 finance-bot. 8085 lets this run ALONGSIDE smarthealth-web during the cutover.
WEB_HTTP_PORT = int(os.getenv("DEMO_WORKER_HTTP_PORT", "8085"))
_HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DATA_DIR = os.getenv("DEMO_DATA_DIR", os.path.join(_HERE, "demo-dispositions"))
DEMO_POST_DISPOSITIONS = _env_bool("DEMO_POST_DISPOSITIONS", False)

POLL_INTERVAL = 0.4        # snapshot cadence; imperceptible in a UI, trivial on CPU
HEARTBEAT_EVERY = 2.5      # republish unchanged state so a late-joining tab catches up

# The bots this worker can run: (production agent file, that bot's .env). Each can be
# repointed with WEB_BOT_<KEY>_FILE / WEB_BOT_<KEY>_ENV. Keys must match demo_bots.py.
BOTS = {
    "smarthealth": ("/opt/livekit/bot/agent_smarthealth_claude_dg.py",
                    "/opt/livekit/bot/.env"),
    "roof":        ("/opt/livekit/roof-bot/agent_roof_uk.py",
                    "/opt/livekit/roof-bot/.env"),
    "finance":     ("/opt/livekit/finance-bot/agent_finance_au.py",
                    "/opt/livekit/finance-bot/.env"),
    "movers":      ("/opt/livekit/movers-bot/agent_movers_au.py",
                    "/opt/livekit/movers-bot/.env"),
    "warranty":    ("/opt/livekit/warranty-bot/agent_warranty_uk.py",
                    "/opt/livekit/warranty-bot/.env"),
    # Demo-only: no SIP trunk, no service. The .env is optional.
    "mortgage":    (os.path.join(_HERE, "bots", "agent_mortgage_leads_au.py"),
                    os.path.join(_HERE, "bots", "mortgage.env")),
    "energy":      (os.path.join(_HERE, "bots", "agent_energy_assist_au.py"),
                    os.path.join(_HERE, "bots", "energy.env")),
}
# A dispatch with no bot key comes from the pre-multi-bot server, which only had this one.
DEFAULT_BOT = "smarthealth"

# Never stripped when switching bots: credentials are shared, and removing one because a
# bot's .env happens to omit it would break the call rather than isolate it.
_SHARED_KEY = re.compile(r"(_API_KEY|_API_SECRET)$|^LIVEKIT_URL$|^GOOGLE_APPLICATION_CREDENTIALS$")


def bot_paths(key: str) -> tuple[str, str]:
    file_default, env_default = BOTS[key]
    up = key.upper()
    return (os.getenv(f"WEB_BOT_{up}_FILE", file_default),
            os.getenv(f"WEB_BOT_{up}_ENV", env_default))


# ══════════════════════════════════════════════════════════════════════════════
# Per-call isolation
# ══════════════════════════════════════════════════════════════════════════════
def _read_env(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        return dict(dotenv.dotenv_values(path))
    except Exception as e:
        log.error("[web] could not read %s: %s", path, e)
        return {}


_OVERRIDE_KEYS = ("MACHINE_GUARD", "DISPOSITION_WEBHOOK_URL", "DISPOSITION_CSV_PATH",
                  "WEB_DEMO")


def bot_environ(key: str, base: dict) -> tuple[dict, set]:
    """(names to set, names to remove) so that `key`'s call runs with its own config.
    Pure: `base` is the environment the worker started with; nothing is modified.

    Only MANAGED names are touched — every name some bot's .env defines, plus the demo
    overrides. Anything else in the process environment (systemd's, LiveKit's own) is
    left exactly as it is."""
    per_bot = {k: _read_env(bot_paths(k)[1]) for k in BOTS}
    managed = set().union(*(v.keys() for v in per_bot.values())) | set(_OVERRIDE_KEYS)
    target = {}

    # 1. Credentials the chosen bot's .env leaves out fall back to the worker's own,
    #    rather than being removed and breaking the call.
    for name in managed:
        if _SHARED_KEY.search(name) and base.get(name):
            target[name] = base[name]

    # 2. The chosen bot's own .env. A bare `KEY` line (no '=') parses as None: skip it.
    for name, val in per_bot.get(key, {}).items():
        if val is not None:
            target[name] = val

    # 3. Demo overrides, LAST, so no .env can undo them.
    target["MACHINE_GUARD"] = "0"
    # Tells the bot it is on the web demo, not a phone call. The only thing it changes:
    # the bots that would otherwise say "shall I use the number I'm calling you on?"
    # read the number we have back instead, because nobody is on a call here and that
    # question makes no sense to someone sitting at a browser.
    target["WEB_DEMO"] = "1"
    if not DEMO_POST_DISPOSITIONS:
        target["DISPOSITION_WEBHOOK_URL"] = ""
    target["DISPOSITION_CSV_PATH"] = os.path.join(DEMO_DATA_DIR, f"{key}.csv")

    # Everything else managed is REMOVED, so the bot's code default applies — never
    # another bot's value.
    return target, managed - set(target)


def apply_environ(target: dict, unset: set) -> None:
    for name in unset:
        os.environ.pop(name, None)
    for name, val in target.items():
        os.environ[name] = val


def _noop_load_dotenv(*_a, **_kw) -> bool:
    return True


def load_bot_module(key: str):
    """Execute the bot's production file as a fresh module. Fresh every time: a module
    object left over from an earlier call would carry that call's import-time config."""
    path, _ = bot_paths(key)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} not found (set WEB_BOT_{key.upper()}_FILE)")
    name = f"webbot_{key}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses and function_tool resolve annotations through
    # sys.modules[cls.__module__].
    sys.modules[name] = mod
    real_load_dotenv = dotenv.load_dotenv
    dotenv.load_dotenv = _noop_load_dotenv   # the bot's `from dotenv import load_dotenv`
    try:                                      # binds to this for the duration of exec
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        dotenv.load_dotenv = real_load_dotenv
    if not callable(getattr(mod, "entrypoint", None)):
        raise AttributeError(f"{path} has no entrypoint()")
    return mod


def prepare_bot(key: str):
    apply_environ(*bot_environ(key, _BASE_ENVIRON))
    os.makedirs(DEMO_DATA_DIR, exist_ok=True)
    return load_bot_module(key)


# ══════════════════════════════════════════════════════════════════════════════
# Capture the live agent + session
# ══════════════════════════════════════════════════════════════════════════════
# Every bot builds its agent and session as locals inside its entrypoint. They all hand
# both to AgentSession.start, so wrapping that one method reaches them without touching
# any bot's code.
_live: dict = {}
_orig_session_start = AgentSession.start


async def _patched_session_start(self, *a, **kw):
    _live["session"] = self
    agent = kw.get("agent", a[0] if a else None)
    if agent is not None:
        _live["agent"] = agent
    return await _orig_session_start(self, *a, **kw)


AgentSession.start = _patched_session_start


def install_tts_override(mod, choice: str) -> None:
    """Swap the bot's build_tts for this call only. The bots call build_tts() through
    their module globals when the session is built, so replacing the attribute is enough.
    Any failure falls back to the bot's own voice — never a mute call."""
    orig = mod.build_tts
    provider, _, voice = choice.partition(":")

    def build_tts():
        try:
            if provider == "cartesia":
                from livekit.plugins import cartesia
                log.info("[web] TTS override: cartesia sonic-2 voice=%s", voice or "DEFAULT")
                return cartesia.TTS(model="sonic-2", **({"voice": voice} if voice else {}))
            if provider == "deepgram" and voice.startswith("aura"):
                from livekit.plugins import deepgram
                log.info("[web] TTS override: deepgram %s", voice)
                return deepgram.TTS(model=voice)
            raise ValueError(f"unsupported TTS choice {choice!r}")
        except Exception as e:
            log.error("[web] TTS override %r failed (%s) — using the bot's own voice",
                      choice, e)
            return orig()

    mod.build_tts = build_tts


# ══════════════════════════════════════════════════════════════════════════════
# Telemetry
# ══════════════════════════════════════════════════════════════════════════════
async def _send(ctx: JobContext, topic: str, obj: dict) -> None:
    """Fire-and-forget. Silent on failure by design: the page is a nicety, the call is
    the product."""
    try:
        lp = getattr(ctx.room, "local_participant", None)
        if lp is None:
            return
        await lp.publish_data(json.dumps(obj), reliable=True, topic=topic)
    except Exception as e:
        log.debug("[web] publish %s failed: %s", topic, e)


# Each bot flags a completed handoff under its own name.
_DONE_FLAGS = ("_handoff_requested", "_callback_requested", "_info_requested",
               "_booking_confirmed", "_intro_requested", "_guidance_given")
_premium_cache: dict = {}


def _derived(mod, qual, agent=None) -> list[dict]:
    """Values the page can't work out itself: a bot's own demo_derived() rows (the movers
    quote, the mortgage price, blocked card numbers), else Smart Health's premium gate.

    monthly_premium() logs a warning when it can't parse its input, and this runs every
    400ms — so it's only called once both halves are present, and once per distinct
    input, or a half-captured premium floods the journal."""
    hook = getattr(agent, "demo_derived", None)
    if callable(hook):
        try:
            return [dict(r) for r in hook()]
        except Exception as e:
            log.debug("[web] demo_derived failed: %s", e)
            return []
    if qual is None or not callable(getattr(qual, "monthly_premium", None)):
        return []
    amount = str(getattr(qual, "premium_amount", "") or "").strip()
    freq = str(getattr(qual, "premium_frequency", "") or "").strip()
    if not (amount and freq):
        return []
    if (amount, freq) not in _premium_cache:
        try:
            _premium_cache[(amount, freq)] = qual.monthly_premium()
        except Exception:
            _premium_cache[(amount, freq)] = None
    monthly = _premium_cache[(amount, freq)]
    if monthly is None:
        return []
    value, tone = f"≈ ${monthly:,.0f} a month", ""
    floor = getattr(mod, "MIN_MONTHLY_PREMIUM", None)
    if floor:
        above = monthly >= floor
        value += f", {'above' if above else 'below'} the ${floor:,.0f} threshold"
        tone = "good" if above else "bad"
    return [{"label": "Monthly premium", "value": value, "tone": tone}]


def snapshot(key: str, mod, agent) -> dict:
    """Everything the page renders, from whichever bot is running. Generic on purpose:
    field labels and order live in demo_bots.py, so this never needs a per-bot branch."""
    qual = getattr(agent, "qual", None)
    fields = {}
    if qual is not None and is_dataclass(qual):
        fields = {k: "" if v is None else str(v) for k, v in asdict(qual).items()}
    try:
        missing = list(qual.missing_required()) if qual is not None else []
    except Exception:
        missing = []
    return {
        "bot": {"key": key,
                "agent": str(getattr(mod, "AGENT_NAME", "") or ""),
                "company": str(getattr(mod, "COMPANY_NAME", "") or "")},
        "fields": fields,
        "missing": missing,
        "outcome": str(getattr(agent, "outcome", "") or ""),
        "consent": bool(getattr(agent, "consent_given", False)),
        "done": any(bool(getattr(agent, f, False)) for f in _DONE_FLAGS),
        "callback": str(getattr(agent, "callback_request", "") or ""),
        "declineReason": str(getattr(agent, "decline_reason", "") or ""),
        "derived": _derived(mod, qual, agent),
    }


def diff_events(prev: dict | None, cur: dict) -> list[dict]:
    if prev is None:
        return []
    out = []
    for k, v in cur["fields"].items():
        if v and v != prev["fields"].get(k):
            out.append({"kind": "field", "label": f"{k}: {v}"})
    if cur["consent"] and not prev["consent"]:
        out.append({"kind": "consent", "label": "Explicit consent recorded"})
    if cur["done"] and not prev["done"]:
        out.append({"kind": "handoff", "label": "Handoff recorded"})
    if cur["outcome"] != prev["outcome"]:
        out.append({"kind": "outcome", "label": f"Outcome: {cur['outcome']}"})
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
            session = _live.get("session")
            if session is not None and not wired:
                wired = _wire_session(ctx, session)

            cur = snapshot(_live["key"], _live["mod"], agent)
            now = loop.time()
            if cur != prev:
                for ev in diff_events(prev, cur):
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


def _wire_session(ctx: JobContext, session) -> bool:
    """Transcript + latency on our own topics. LiveKit ships native transcriptions, but
    their delivery API has moved between versions; owning both ends means an SDK bump
    can't break the demo."""
    try:
        @session.on("user_input_transcribed")
        def _on_user(ev):
            text = (getattr(ev, "transcript", "") or "").strip()
            if text:
                asyncio.create_task(_send(ctx, "demo.transcript", {
                    "role": "user", "text": text, "src": "stt",
                    "final": bool(getattr(ev, "is_final", True))}))

        @session.on("conversation_item_added")
        def _on_item(ev):
            item = getattr(ev, "item", None)
            if item is None:
                return
            role = str(getattr(item, "role", ""))
            text = (getattr(item, "text_content", "") or "").strip()
            if text and role in ("assistant", "user"):
                asyncio.create_task(_send(ctx, "demo.transcript", {
                    "role": "agent" if role == "assistant" else "user",
                    "text": text, "src": "turn", "final": True}))

        @session.on("metrics_collected")
        def _on_metrics(ev):
            m = getattr(ev, "metrics", None)
            if m is None:
                return
            kind, out = type(m).__name__, None
            if kind == "TTSMetrics" and not getattr(m, "cancelled", False):
                out = {"kind": "tts", "ms": round((getattr(m, "ttfb", 0) or 0) * 1000)}
            elif kind == "LLMMetrics" and not getattr(m, "cancelled", False):
                out = {"kind": "llm", "ms": round((getattr(m, "ttft", 0) or 0) * 1000)}
            elif kind == "EOUMetrics":
                out = {"kind": "eou",
                       "ms": round((getattr(m, "end_of_utterance_delay", 0) or 0) * 1000)}
            if out and out["ms"] > 0:
                asyncio.create_task(_send(ctx, "demo.metrics", out))

        log.info("[web] transcript + metrics feed wired")
    except Exception as e:
        log.warning("[web] could not wire session events: %s", e)
    return True     # either way, don't retry every tick


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint
# ══════════════════════════════════════════════════════════════════════════════
async def _refuse(ctx: JobContext, message: str) -> None:
    """The chosen bot can't run. Tell the page why, then close the room — otherwise the
    visitor sits in silence wondering whether their microphone works."""
    try:
        await ctx.connect()
        # The browser joins a moment after dispatch; repeat so it catches one.
        for _ in range(10):
            await _send(ctx, "demo.error", {"message": message})
            await asyncio.sleep(0.6)
    except Exception as e:
        log.warning("[web] could not deliver the error to the page: %s", e)
    finally:
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        except Exception as e:
            log.info("[web] delete_room after refusal failed: %s", e)


async def entrypoint(ctx: JobContext):
    _live.clear()
    try:
        meta = json.loads(getattr(ctx.job, "metadata", "") or "{}")
    except Exception:
        meta = {}
    key = str(meta.get("bot") or DEFAULT_BOT).strip().lower()

    if key not in BOTS:
        log.error("[web] dispatch asked for unknown bot %r (known: %s)", key, ", ".join(BOTS))
        await _refuse(ctx, "That voicebot isn't available right now.")
        return
    try:
        mod = prepare_bot(key)
    except Exception:
        log.exception("[web] could not start bot %r from %s", key, bot_paths(key)[0])
        await _refuse(ctx, "This voicebot couldn't start just now. Please try another one, "
                           "or try again in a moment.")
        return

    _live.update(key=key, mod=mod)
    log.info("[web] room=%s bot=%s agent=%r company=%r post_dispositions=%s",
             ctx.room.name, key, getattr(mod, "AGENT_NAME", ""),
             getattr(mod, "COMPANY_NAME", ""), DEMO_POST_DISPOSITIONS)

    tts = str(meta.get("tts") or "").strip()
    if tts:
        install_tts_override(mod, tts)

    task = asyncio.create_task(_telemetry_loop(ctx))

    async def _stop_telemetry():
        task.cancel()
    ctx.add_shutdown_callback(_stop_telemetry)

    try:
        # The real thing: connect, greet, qualify, dispose. Untouched.
        await mod.entrypoint(ctx)
    except Exception:
        task.cancel()
        raise


def prewarm(proc):
    """VAD only. Every production prewarm loads Silero with these same settings, and
    everything else they do is logging that needs a specific bot loaded."""
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=0.6,
        min_speech_duration=0.10,
    )


if __name__ == "__main__":
    for _key in BOTS:
        _file, _envf = bot_paths(_key)
        if not os.path.isfile(_file):
            log.warning("[web] bot %s: %s MISSING — it will refuse calls", _key, _file)
        elif not os.path.isfile(_envf):
            log.warning("[web] bot %s ready: %s (no .env — code defaults)", _key, _file)
        else:
            log.warning("[web] bot %s ready: %s", _key, _file)
    log.warning("[web] demo dispositions: %s", "POSTED to each bot's webhook"
                if DEMO_POST_DISPOSITIONS else f"CSV only, in {DEMO_DATA_DIR}")

    _opts = dict(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        agent_name=WEB_AGENT_NAME,   # MUST match DEMO_AGENT_NAME in the web server's .env
        num_idle_processes=1,
    )
    try:
        opts = WorkerOptions(**_opts, host="127.0.0.1", port=WEB_HTTP_PORT)
        log.warning("[web] health-check server on 127.0.0.1:%d", WEB_HTTP_PORT)
    except TypeError as e:
        log.warning("[web] WorkerOptions has no host/port in this version (%s)", e)
        opts = WorkerOptions(**_opts)
    cli.run_app(opts)
