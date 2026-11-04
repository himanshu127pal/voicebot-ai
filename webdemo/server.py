"""
server.py — signup gate, token and dispatch service for the browser voice demo.

WHAT THIS IS FOR
  Lets a prospect talk to the SAME qualification agent from a web page instead of a
  phone call — but only after they've told us who they are and verified their WhatsApp
  number. The telephony path is untouched: SIP calls dispatch each bot's own
  agent_name; this page dispatches "voicebot-web" with the bot the visitor picked
  (see demo_bots.py for the catalog, agent_voicebot_web.py for the worker).

THE GATE, AND WHY IT IS ENFORCED HERE RATHER THAN IN THE PAGE
  Hiding the start button until a form is filled stops nobody — /api/token is a public
  URL. So the check lives on /api/token itself:

    1. POST /api/otp/send    details + phone → we call the OTP provider, store a
                             PENDING signup row bound to the otpId it returns
    2. POST /api/otp/verify  otpId + code → we verify with the provider; on success the
                             row becomes VERIFIED and we set a signed httpOnly cookie
    3. POST /api/token       refuses without that cookie, and caps calls per signup

  Security properties that matter:
    • The OTP API key never leaves this process. The browser only talks to us.
    • The verified phone comes from OUR row for that otpId, never from the request —
      a user cannot verify their own number and then claim someone else's.
    • The cookie is HMAC-signed and httpOnly; the page cannot read or forge it.
    • SameSite=Lax means another site cannot make a visitor's browser start a demo.
    • Sends are rate-limited per IP, per phone and globally. An open send endpoint is a
      free way for anyone to spam WhatsApp codes (and cost) through our account.

  THE LOCKS (demo_guard), and why they're in SQLite rather than memory
    Wrong codes and code requests are counted per phone number and per network, and
    crossing a threshold locks that subject for a while — 15 minutes the first time,
    an hour every time after that. The counters live in the database, not the process,
    for one reason: a restart must not clear a lock. An attacker who can reset your
    rate limiter by waiting for a deploy does not have to beat it.

    Guessing a 6-digit code is ~1 in 1,000,000 per try, so 3 tries is generous. What
    the lock really stops is the cheap attack: pumping codes at a number to harass its
    owner, or grinding attempts across a pool of numbers from one network.

  THE DEMO CAP
    Three limits, all enforced: per signup, per phone number and per network, over a
    rolling window (DEMO_CALL_WINDOW_HOURS). A visitor cannot get more demos by filling
    the form in again with the same number, and an office cannot get more by using ten
    different ones. Every reserved call is a row in demo_call_log, so the counts survive
    a restart and can be audited later.

WHY SQLITE
  Signups are leads, so they're stored — but in a local file, not a database service.
  Nothing new to run, nothing that can go down and take the demo with it.

Run:
  pip install -r requirements.txt
  uvicorn server:app --host 127.0.0.1 --port 8090
(Behind Caddy, which terminates TLS. Browsers refuse microphone access on plain HTTP,
and the Secure cookie is only sent over HTTPS.)

Env: see .env.example.
"""

import os
import re
import hmac
import json
import time
import base64
import asyncio
import hashlib
import secrets
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Body, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from livekit import api

# Load .env BEFORE importing local modules: anything they read at import time must see
# it. (email_policy used to be imported first, so its EMAIL_* settings in .env were
# silently ignored in favour of the code defaults.)
load_dotenv()

from email_policy import EMAIL_REQUIRE_COMPANY, check_company_email  # noqa: E402
from demo_bots import demo_attrs, enabled_keys, public_catalog        # noqa: E402
from countries import DEFAULT_ISO, fuzzy_phone, public_list as country_list  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webdemo")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, "static")

# ── LiveKit ──────────────────────────────────────────────────────────────────
LIVEKIT_URL        = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_API_KEY    = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
# What the BROWSER dials. Must be wss:// in production — a page served over HTTPS
# cannot open a ws:// socket (mixed content), and mic capture requires HTTPS anyway.
PUBLIC_LIVEKIT_URL = os.getenv("PUBLIC_LIVEKIT_URL", "").strip()
DEMO_AGENT_NAME    = os.getenv("DEMO_AGENT_NAME", "voicebot-web")
DEMO_PASSCODE      = os.getenv("DEMO_PASSCODE", "").strip()
BRAND_COMPANY      = os.getenv("BRAND_COMPANY", os.getenv("COMPANY_NAME", "BTBIZ"))
BRAND_AGENT        = os.getenv("BRAND_AGENT", os.getenv("AGENT_NAME", "Sarah"))
ROOM_TTL_MINUTES   = int(os.getenv("DEMO_ROOM_TTL_MINUTES", "30"))
CONTACT_WHATSAPP   = re.sub(r"\D", "", os.getenv("CONTACT_WHATSAPP", ""))

# ── Signup gate ──────────────────────────────────────────────────────────────
# Kill switch. If the OTP provider is down during a pitch, set this to 0 and restart —
# the page falls back to one-click demos without a redeploy.
DEMO_REQUIRE_SIGNUP = _env_bool("DEMO_REQUIRE_SIGNUP", True)
OTP_API_BASE        = os.getenv("OTP_API_BASE", "https://wapanel.btbiz.in").rstrip("/")
OTP_API_KEY         = os.getenv("OTP_API_KEY", "").strip()
OTP_CODE_LENGTH     = int(os.getenv("OTP_CODE_LENGTH", "6"))
DEMO_PASS_HOURS     = int(os.getenv("DEMO_PASS_HOURS", "24"))
OTP_MAX_SENDS_PER_HOUR    = int(os.getenv("OTP_MAX_SENDS_PER_HOUR", "60"))


def _int_env(name: str, default: int, low: int = 0) -> int:
    try:
        return max(low, int(str(os.getenv(name, default)).strip()))
    except ValueError:
        log.error("%s=%r is not a number — using %s", name, os.getenv(name), default)
        return default


# ── Lockouts ─────────────────────────────────────────────────────────────────
# Wrong codes are counted per PHONE NUMBER, not per code: a new code is free to ask
# for, so a per-code count would be no limit at all.
OTP_MAX_ATTEMPTS = _int_env("OTP_MAX_ATTEMPTS", 3, 1)
# How long a subject is locked, escalating with each lock. The LAST value repeats, so
# "15,60" means 15 minutes the first time and an hour every time after that.
OTP_LOCK_LADDER = [max(1, int(x)) for x in
                   str(os.getenv("OTP_LOCK_LADDER_MINUTES", "15,60")).replace(" ", "").split(",")
                   if x.isdigit()] or [15, 60]
# Quiet for this long and the ladder resets — one bad day shouldn't follow a real
# customer around for ever.
OTP_STRIKE_RESET_HOURS = _int_env("OTP_STRIKE_RESET_HOURS", 24, 1)
# Failures this old are forgotten. Without it, three typos spread over a month would
# lock someone out.
OTP_FAIL_WINDOW_MIN = _int_env("OTP_FAIL_WINDOW_MINUTES", 60, 1)
# Resend guards. The provider has its own cooldown; this one is ours, because the page
# is not what enforces it.
OTP_RESEND_COOLDOWN_SEC = _int_env("OTP_RESEND_COOLDOWN_SEC", 60, 0)
OTP_MAX_SENDS_PER_PHONE = _int_env("OTP_MAX_SENDS_PER_PHONE", 3, 1)
OTP_SEND_WINDOW_MIN     = _int_env("OTP_SEND_WINDOW_MINUTES", 15, 1)
# Wrong codes from one network, across ANY number. Cycling numbers must not be free.
OTP_MAX_FAILS_PER_IP    = _int_env("OTP_MAX_FAILS_PER_IP", 10, 1)

# ── How many demos ───────────────────────────────────────────────────────────
# All three are enforced. Per phone and per network stop the obvious way around the
# per-signup cap: fill the form in again.
DEMO_MAX_CALLS_PER_SIGNUP = _int_env("DEMO_MAX_CALLS_PER_SIGNUP", 2, 1)
DEMO_MAX_CALLS_PER_PHONE  = _int_env("DEMO_MAX_CALLS_PER_PHONE", 2, 1)
DEMO_MAX_CALLS_PER_IP     = _int_env("DEMO_MAX_CALLS_PER_IP", 2, 1)
# Rolling window for those two. 0 = for ever (two demos per number, full stop).
DEMO_CALL_WINDOW_HOURS    = _int_env("DEMO_CALL_WINDOW_HOURS", 24, 0)
DEMO_COOKIE_SECURE  = _env_bool("DEMO_COOKIE_SECURE", True)
SIGNUPS_DB          = os.getenv("SIGNUPS_DB", os.path.join(_HERE, "demo_signups.db"))
COOKIE              = "btbiz_demo_pass"

_session_secret = os.getenv("DEMO_SESSION_SECRET", "").strip()
if not _session_secret:
    # Works, but every restart signs everyone out. Set it in .env to keep passes valid.
    _session_secret = secrets.token_hex(32)
    log.warning("DEMO_SESSION_SECRET unset — generated a temporary one. Verified "
                "visitors will have to verify again after every restart.")
_SECRET = _session_secret.encode()


# Per-call TTS selection. WHITELISTED — this value becomes a model/voice id in a paid
# API call, so a request must never be able to name an arbitrary one. Provider and
# voice are one value because their id formats are mutually invalid.
TTS_CHOICES = {
    "deepgram:aura-2-theia-en":                     "Deepgram Aura-2 · Australian",
    "cartesia:2a12b36c-7f9b-4c3a-9f7a-72731b15323a": "Cartesia Sonic-2",
}
# No default: each bot speaks in its own production voice (the UK roof bot must not
# be forced onto an Australian one). Only an explicit, whitelisted ?tts= overrides it.

if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
    log.error("LIVEKIT_API_KEY / LIVEKIT_API_SECRET are unset — every token mint "
              "will fail. Copy them from livekit.yaml.")
if not PUBLIC_LIVEKIT_URL:
    log.warning("PUBLIC_LIVEKIT_URL unset — falling back to LIVEKIT_URL (%s). That only "
                "works if you are opening the page on the server itself.", LIVEKIT_URL)
if DEMO_REQUIRE_SIGNUP and not OTP_API_KEY:
    log.error("DEMO_REQUIRE_SIGNUP is on but OTP_API_KEY is unset — nobody can verify, "
              "so nobody can start a demo. Set OTP_API_KEY or DEMO_REQUIRE_SIGNUP=0.")

# Control-plane calls want http(s), not ws(s). Accept either in config and normalise.
_API_URL = LIVEKIT_URL.replace("wss://", "https://").replace("ws://", "http://")

app = FastAPI(title="BTBIZ voice demo")


# ══════════════════════════════════════════════════════════════════════════════
# Storage
# ══════════════════════════════════════════════════════════════════════════════
@contextmanager
def _db():
    conn = sqlite3.connect(SIGNUPS_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _db() as c:
        # WAL lets a read (a /api/me on page load) proceed during a write.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS demo_signups (
              id                INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at        TEXT NOT NULL,
              status            TEXT NOT NULL,            -- pending | verified
              verified_at       TEXT,
              otp_id            TEXT UNIQUE,
              person_name       TEXT NOT NULL,
              company_name      TEXT NOT NULL,
              company_email     TEXT NOT NULL,
              email_verified_at TEXT,                     -- reserved for email OTP
              phone             TEXT NOT NULL,            -- E.164, as verified
              business_category TEXT NOT NULL,
              ip                TEXT,
              user_agent        TEXT,
              demo_calls        INTEGER NOT NULL DEFAULT 0,
              last_call_at      TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_signups_phone   ON demo_signups(phone)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_signups_status  ON demo_signups(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_signups_created ON demo_signups(created_at)")
        # Added after the first deploy. ALTER is how an existing demo_signups.db picks
        # them up; rows from before stay NULL. business_category is now free text.
        have = {r["name"] for r in c.execute("PRAGMA table_info(demo_signups)")}
        for col in ("usecase", "last_bot"):
            if col not in have:
                c.execute(f"ALTER TABLE demo_signups ADD COLUMN {col} TEXT")

        # Lock state per subject. In the database and not in memory on purpose: a
        # restart must not hand an attacker a clean slate.
        c.execute("""
            CREATE TABLE IF NOT EXISTS demo_guard (
              subject      TEXT PRIMARY KEY,     -- 'phone:+9198…' | 'ip:203.0.113.4'
              fails        INTEGER NOT NULL DEFAULT 0,
              strikes      INTEGER NOT NULL DEFAULT 0,   -- locks so far; picks the ladder
              locked_until TEXT,
              last_fail_at TEXT,
              last_lock_at TEXT,
              updated_at   TEXT NOT NULL
            )""")
        # One row per code we paid to send. Drives the resend cooldown and the per-phone
        # send ceiling, and answers "how many did we send that number?" later.
        c.execute("""
            CREATE TABLE IF NOT EXISTS demo_otp_sends (
              id    INTEGER PRIMARY KEY AUTOINCREMENT,
              at    TEXT NOT NULL,
              phone TEXT NOT NULL,
              ip    TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sends_phone ON demo_otp_sends(phone, at)")
        # One row per RESERVED demo call, deleted if the dispatch then fails. The
        # per-phone and per-network caps are counted from here, so they hold across
        # different signup rows for the same number.
        c.execute("""
            CREATE TABLE IF NOT EXISTS demo_call_log (
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              at        TEXT NOT NULL,
              signup_id INTEGER,
              phone     TEXT NOT NULL,
              ip        TEXT,
              bot       TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_calls_phone ON demo_call_log(phone, at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_calls_ip    ON demo_call_log(ip, at)")

        # Calls taken before this table existed would otherwise read as zero, handing
        # every existing lead a fresh allowance. Seed one row per call already counted.
        if not c.execute("SELECT 1 FROM demo_call_log LIMIT 1").fetchone():
            seeded = 0
            for r in c.execute("SELECT id, phone, ip, demo_calls, last_call_at, "
                               "created_at, last_bot FROM demo_signups "
                               "WHERE demo_calls > 0"):
                when = r["last_call_at"] or r["created_at"]
                for _ in range(int(r["demo_calls"] or 0)):
                    c.execute("INSERT INTO demo_call_log (at, signup_id, phone, ip, bot) "
                              "VALUES (?,?,?,?,?)",
                              (when, r["id"], r["phone"], r["ip"], r["last_bot"]))
                    seeded += 1
            if seeded:
                log.info("seeded demo_call_log with %s call(s) from existing signups", seeded)


_init_db()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dt(value) -> datetime | None:
    """Parse one of our own ISO timestamps. Returns UTC-aware, or None."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _age_s(value) -> float:
    d = _dt(value)
    return 1e9 if d is None else (datetime.now(timezone.utc) - d).total_seconds()


def _human_wait(seconds: int) -> str:
    """'15 minutes' / 'an hour' / '40 seconds'. The exact second is never the point,
    and 'try again in 3540 seconds' reads like a machine sneering at you."""
    if seconds < 90:
        return f"{max(seconds, 1)} seconds"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minutes"
    hours = seconds / 3600
    if 0.9 <= hours <= 1.1:
        return "an hour"
    return f"{round(hours, 1):g} hours"


# ══════════════════════════════════════════════════════════════════════════════
# Lockouts
# ══════════════════════════════════════════════════════════════════════════════
def _lock_minutes(strikes: int) -> int:
    """strikes = locks BEFORE this one. Past the end of the ladder, the last step
    repeats — an attacker never runs out of ladder, a customer never gets a week."""
    return OTP_LOCK_LADDER[min(max(strikes, 0), len(OTP_LOCK_LADDER) - 1)]


def lock_seconds(subject: str) -> int:
    """Seconds this subject stays locked for. 0 = not locked."""
    with _db() as c:
        row = c.execute("SELECT locked_until FROM demo_guard WHERE subject=?",
                        (subject,)).fetchone()
    until = _dt(row["locked_until"]) if row else None
    if until is None:
        return 0
    left = (until - datetime.now(timezone.utc)).total_seconds()
    return int(left) + 1 if left > 0 else 0


def lock_now(subject: str, why: str) -> int:
    """Lock a subject, escalating on its record. Returns the seconds it's locked for."""
    now = _now()
    with _db() as c:
        row = c.execute("SELECT * FROM demo_guard WHERE subject=?", (subject,)).fetchone()
        strikes = int(row["strikes"]) if row else 0
        if row and _age_s(row["last_lock_at"]) > OTP_STRIKE_RESET_HOURS * 3600:
            strikes = 0                          # long quiet spell: start the ladder again
        minutes = _lock_minutes(strikes)
        until = (datetime.now(timezone.utc)
                 + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        c.execute(
            """INSERT INTO demo_guard (subject, fails, strikes, locked_until,
                                       last_fail_at, last_lock_at, updated_at)
               VALUES (?, 0, ?, ?, ?, ?, ?)
               ON CONFLICT(subject) DO UPDATE SET
                 fails=0, strikes=excluded.strikes, locked_until=excluded.locked_until,
                 last_lock_at=excluded.last_lock_at, updated_at=excluded.updated_at""",
            (subject, strikes + 1, until, now, now, now))
    log.warning("LOCKED %s for %s min (%s), strike %s", subject, minutes, why, strikes + 1)
    return minutes * 60


def guard_fail(subject: str, threshold: int, why: str) -> dict:
    """Count one failure. Locks the subject when it reaches `threshold`.
    Returns {"locked": bool, "seconds": int, "left": int}."""
    now = _now()
    with _db() as c:
        row = c.execute("SELECT * FROM demo_guard WHERE subject=?", (subject,)).fetchone()
        # Failures go stale: three typos a month apart are not an attack.
        fails = int(row["fails"]) if row and _age_s(row["last_fail_at"]) <= OTP_FAIL_WINDOW_MIN * 60 else 0
        fails += 1
        c.execute(
            """INSERT INTO demo_guard (subject, fails, strikes, last_fail_at, updated_at)
               VALUES (?, ?, 0, ?, ?)
               ON CONFLICT(subject) DO UPDATE SET
                 fails=excluded.fails, last_fail_at=excluded.last_fail_at,
                 updated_at=excluded.updated_at""",
            (subject, fails, now, now))
    if fails >= threshold:
        return {"locked": True, "seconds": lock_now(subject, why), "left": 0}
    return {"locked": False, "seconds": 0, "left": max(threshold - fails, 0)}


def guard_clear(subject: str) -> None:
    """A correct code clears the failure count — but NOT the strike history, so the
    next lock still escalates."""
    with _db() as c:
        c.execute("UPDATE demo_guard SET fails=0, locked_until=NULL, updated_at=? "
                  "WHERE subject=?", (_now(), subject))


def _send_stats(phone: str) -> tuple[float, int]:
    """(seconds since the last code to this number, codes sent inside the window)."""
    since = (datetime.now(timezone.utc)
             - timedelta(minutes=OTP_SEND_WINDOW_MIN)).isoformat(timespec="seconds")
    with _db() as c:
        row = c.execute("SELECT MAX(at) AS last, COUNT(*) AS n FROM demo_otp_sends "
                        "WHERE phone=? AND at>=?", (phone, since)).fetchone()
    return _age_s(row["last"] if row else None), int(row["n"] if row else 0)


def _calls_since() -> str:
    if DEMO_CALL_WINDOW_HOURS <= 0:
        return "0000-01-01T00:00:00+00:00"        # for ever
    return (datetime.now(timezone.utc)
            - timedelta(hours=DEMO_CALL_WINDOW_HOURS)).isoformat(timespec="seconds")


def _calls_used(phone: str, ip: str) -> tuple[int, int]:
    """Demo calls this number and this network have used inside the window."""
    since = _calls_since()
    with _db() as c:
        by_phone = c.execute("SELECT COUNT(*) FROM demo_call_log WHERE phone=? AND at>=?",
                             (phone, since)).fetchone()[0]
        by_ip = c.execute("SELECT COUNT(*) FROM demo_call_log WHERE ip=? AND at>=?",
                          (ip, since)).fetchone()[0] if ip else 0
    return int(by_phone), int(by_ip)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════
def _fail(status: int, message: str, **extra) -> JSONResponse:
    """Every error the page shows has the same shape: a human sentence in `detail`,
    plus optional machine hints (fields, restart, code) beside it."""
    return JSONResponse(status_code=status, content={"detail": message, **extra})


def _locked_fail(seconds: int, message: str, scope: str, **extra) -> JSONResponse:
    """423 Locked, with everything the page needs to run its own countdown. The page
    shows this in a popup — it is the one refusal a visitor can do nothing about, so
    it has to say what happened and exactly how long it lasts."""
    until = (datetime.now(timezone.utc)
             + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    resp = _fail(423, message, code="locked", scope=scope, lockSeconds=seconds,
                 lockedUntil=until, **extra)
    resp.headers["Retry-After"] = str(seconds)
    return resp


def _client_ip(request: Request) -> str:
    # This service binds 127.0.0.1 and is reachable only through Caddy, which sets
    # X-Forwarded-For to the real client address — so the header is trustworthy HERE.
    # Expose port 8090 directly and it becomes spoofable, which defeats the IP limits.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_hits: dict[str, list[float]] = {}


def _limit(key: str, max_hits: int, window_s: int) -> bool:
    """Sliding-window limiter, in-process. Correct for the single uvicorn worker this
    service runs; move to Redis before running more than one."""
    now = time.time()
    hits = [t for t in _hits.get(key, []) if now - t < window_s]
    if len(hits) >= max_hits:
        _hits[key] = hits
        return False
    hits.append(now)
    _hits[key] = hits
    if len(_hits) > 5000:                       # bound memory against IP churn
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
            _hits.pop(k, None)
    return True


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _mask(phone: str) -> str:
    """+919876543210 -> +91•••••••210. Enough to recognise, not enough to harvest."""
    if len(phone) < 8:
        return phone
    return phone[:3] + "•" * (len(phone) - 6) + phone[-3:]


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _unsign(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        want = _b64(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return None
        data = json.loads(_unb64(body))
        if int(data.get("exp", 0)) < time.time():
            return None
        return data
    except Exception:
        return None


def _signup_from_request(request: Request):
    data = _unsign(request.cookies.get(COOKIE, ""))
    if not data:
        return None
    with _db() as c:
        return c.execute(
            "SELECT * FROM demo_signups WHERE id=? AND status='verified'",
            (data.get("sid"),)).fetchone()


def _public_signup(row, ip: str = "") -> dict:
    """What's left is the SMALLEST of the three allowances, because all three are
    enforced — showing the per-signup number alone would promise calls the phone or
    network cap won't give."""
    used = row["demo_calls"] or 0
    left = max(DEMO_MAX_CALLS_PER_SIGNUP - used, 0)
    scope = "signup"
    by_phone, by_ip = _calls_used(row["phone"], ip)
    if (phone_left := max(DEMO_MAX_CALLS_PER_PHONE - by_phone, 0)) < left:
        left, scope = phone_left, "phone"
    if ip and (ip_left := max(DEMO_MAX_CALLS_PER_IP - by_ip, 0)) < left:
        left, scope = ip_left, "network"
    return {
        "name": row["person_name"],
        "company": row["company_name"],
        "phoneMasked": _mask(row["phone"]),
        "callsUsed": used,
        "callsMax": DEMO_MAX_CALLS_PER_SIGNUP,
        "callsRemaining": left,
        "limitScope": scope if left == 0 else "",
    }


def _verified_response(row, ip: str = "") -> JSONResponse:
    ttl = DEMO_PASS_HOURS * 3600
    resp = JSONResponse({"verified": True, **_public_signup(row, ip)})
    resp.set_cookie(
        COOKIE, _sign({"sid": row["id"], "exp": int(time.time()) + ttl}),
        max_age=ttl, httponly=True, secure=DEMO_COOKIE_SECURE,
        samesite="lax", path="/")
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# OTP provider
# ══════════════════════════════════════════════════════════════════════════════
async def _otp_call(path: str, body: dict) -> tuple[int, dict]:
    """POST to the provider. Returns (status, json). Transport failures come back as
    5xx so callers have a single code path."""
    url = f"{OTP_API_BASE}/api/v1/otp/{path}"
    headers = {"Authorization": f"Bearer {OTP_API_KEY}",
               "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=body, headers=headers) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {"message": (await r.text())[:200]}
                return r.status, (data if isinstance(data, dict) else {})
    except asyncio.TimeoutError:
        log.error("OTP provider timeout on %s", path)
        return 504, {"message": "timeout"}
    except Exception as e:
        log.error("OTP provider unreachable on %s: %s", path, e)
        return 502, {"message": str(e)}


def _provider_message(data: dict, fallback: str) -> str:
    """The provider returns `message` as a list of strings for validation errors and as
    a plain string otherwise (verified against the live API). Handle both."""
    m = data.get("message")
    if isinstance(m, list):
        m = next((x for x in m if isinstance(x, str) and x.strip()), None)
    if isinstance(m, str) and m.strip():
        return m.strip()
    err = data.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        return err["message"]
    return fallback


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints — signup
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/config")
async def config():
    """Everything the page needs that isn't a secret."""
    return {
        "company": BRAND_COMPANY,
        "agent": BRAND_AGENT,
        "signupRequired": DEMO_REQUIRE_SIGNUP,
        "codeLength": OTP_CODE_LENGTH,
        "requireCompanyEmail": EMAIL_REQUIRE_COMPANY,
        "contactWhatsapp": CONTACT_WHATSAPP,
        "passcodeRequired": bool(DEMO_PASSCODE),
        "livekitUrl": PUBLIC_LIVEKIT_URL or LIVEKIT_URL,
        "bots": public_catalog(),
        "defaultBot": enabled_keys()[0],
        # Every country, so nobody is stuck because their code wasn't on our shortlist.
        # The page filters this list; the server re-checks whatever comes back.
        "countries": country_list(),
        "defaultCountry": DEFAULT_ISO,
        # So the page can warn BEFORE the last attempt, rather than explaining a lock
        # after the fact.
        "otpMaxAttempts": OTP_MAX_ATTEMPTS,
        "otpMaxSends": OTP_MAX_SENDS_PER_PHONE,
        "resendCooldownSec": OTP_RESEND_COOLDOWN_SEC,
        "callsMax": min(DEMO_MAX_CALLS_PER_SIGNUP, DEMO_MAX_CALLS_PER_PHONE),
    }


@app.get("/api/me")
async def me(request: Request):
    row = _signup_from_request(request)
    if row is None:
        return {"verified": False}
    return {"verified": True, **_public_signup(row, _client_ip(request))}


@app.post("/api/email/check")
async def email_check(request: Request, payload: dict = Body(default={})):
    """Inline feedback on the email field, before the visitor fills in their phone and
    triggers a WhatsApp code. /api/otp/send applies the same rule from the same module;
    this is only the early warning, never the enforcement."""
    if not _limit(f"emailcheck:ip:{_client_ip(request)}", 60, 60):
        return _fail(429, "Too many requests. Please wait a moment.")
    email = (payload.get("email") or "").strip().lower()
    if len(email) > 160 or not _EMAIL_RE.match(email):
        return {"ok": False, "message": "Enter a valid email address."}
    reason = check_company_email(email)
    return {"ok": reason is None, "message": reason or ""}


@app.post("/api/otp/send")
async def otp_send(request: Request, payload: dict = Body(default={})):
    if not OTP_API_KEY:
        return _fail(503, "Verification is temporarily unavailable. Please try again "
                          "shortly.")
    ip = _client_ip(request)

    # Honeypot: a field real visitors never see. Anything filling it is a bot, and bots
    # are exactly who would use this endpoint to pump WhatsApp codes.
    if (payload.get("website") or "").strip():
        log.warning("otp send honeypot tripped ip=%s", ip)
        return _fail(400, "Something went wrong. Please refresh and try again.")

    name     = (payload.get("name") or "").strip()
    company  = (payload.get("company") or "").strip()
    email    = (payload.get("email") or "").strip().lower()
    category = " ".join(str(payload.get("category") or "").split())
    usecase  = " ".join(str(payload.get("usecase") or "").split())
    # The page sends its picked country alongside the number, but everything is
    # re-derived here: the page is a convenience, not the check.
    phone, phone_problem = fuzzy_phone(payload.get("phone"), payload.get("cc"),
                                       payload.get("iso"))

    fields = {}
    if not 2 <= len(name) <= 80:
        fields["name"] = "Enter your full name."
    if not 2 <= len(company) <= 120:
        fields["company"] = "Enter your company name."
    if len(email) > 160 or not _EMAIL_RE.match(email):
        fields["email"] = "Enter a valid email address."
    else:
        email_reason = check_company_email(email)
        if email_reason:
            fields["email"] = email_reason
            # Domain only: enough to tune the list, without logging the address itself.
            log.info("signup rejected: non-company email domain=%s", email.rsplit("@", 1)[-1])
    if not 2 <= len(category) <= 80:
        fields["category"] = "Enter your business category."
    if len(usecase) < 5:
        fields["usecase"] = "Tell us briefly what you'd like a voicebot to do."
    elif len(usecase) > 500:
        fields["usecase"] = "Please keep this under 500 characters."
    if phone_problem or not _PHONE_RE.match(phone):
        fields["phone"] = phone_problem or ("Enter a valid mobile number, including the "
                                            "country code.")
    if fields:
        return _fail(422, "Please check the highlighted fields.", fields=fields)

    # ── Locks before anything that costs money ───────────────────────────────
    # A locked number must not be able to spend another WhatsApp message, and the page
    # needs the countdown rather than a vague "too many requests".
    if (secs := lock_seconds(f"phone:{phone}")):
        return _locked_fail(secs, f"This number is locked for {_human_wait(secs)} after "
                                  f"too many failed attempts. Please try again after "
                                  f"that.", "phone")
    if (secs := lock_seconds(f"ip:{ip}")):
        return _locked_fail(secs, f"Too many failed attempts from your network. Please "
                                  f"try again in {_human_wait(secs)}.", "network")

    # ── Resend guards, from the database ─────────────────────────────────────
    # The provider has its own cooldown and the page runs a countdown, but neither is a
    # control: anything can POST here. These two are.
    since_last, sent_recently = _send_stats(phone)
    if (wait := int(OTP_RESEND_COOLDOWN_SEC - since_last)) > 0:
        return _fail(429, f"A code was just sent to that number. You can ask for another "
                          f"in {_human_wait(wait)}.", code="cooldown", retryAfter=wait)
    if sent_recently >= OTP_MAX_SENDS_PER_PHONE:
        secs = lock_now(f"phone:{phone}", "too many codes requested")
        return _locked_fail(secs, f"That's {OTP_MAX_SENDS_PER_PHONE} codes to this number "
                                  f"in {OTP_SEND_WINDOW_MIN} minutes. It's locked for "
                                  f"{_human_wait(secs)} — if the code isn't arriving, "
                                  f"message us instead.", "phone", restart=True)

    # Cheapest check first, broadest last: a global ceiling catches distributed abuse
    # that no per-IP limit would. (Per-phone sends are covered above, in the database.)
    if not _limit(f"otp:ip:{ip}", 5, 900):
        return _fail(429, "Too many code requests from your network. Please try again "
                          "in 15 minutes.")
    if not _limit("otp:global", OTP_MAX_SENDS_PER_HOUR, 3600):
        log.error("otp GLOBAL send limit hit (%s/h) — possible abuse",
                  OTP_MAX_SENDS_PER_HOUR)
        return _fail(429, "We're getting a lot of requests right now. Please try again "
                          "in a few minutes.")

    reference = f"demo_{secrets.token_hex(6)}"
    status, data = await _otp_call("send", {"phone": phone, "reference": reference})

    if not (200 <= status < 300 and data.get("success")):
        msg = _provider_message(data, "We couldn't send a code to that number.")
        if status == 401:
            log.error("OTP provider rejected OUR API KEY (401) — check OTP_API_KEY")
            return _fail(503, "Verification is temporarily unavailable. Please try "
                              "again shortly.")
        log.warning("otp send failed status=%s phone=%s msg=%r", status, _mask(phone), msg)
        if status == 429:
            return _fail(429, msg)
        if 400 <= status < 500:
            # The provider's 4xx is about the input (bad number, cooldown) — it's the
            # user's to fix, so show it.
            return _fail(400, msg, fields={"phone": msg})
        return _fail(502, "We couldn't send your code just now. Please try again.")

    d = data.get("data") or {}
    otp_id = d.get("otpId")
    if not otp_id:
        log.error("otp send succeeded without an otpId: %r", data)
        return _fail(502, "We couldn't send your code just now. Please try again.")

    with _db() as c:
        c.execute(
            """INSERT INTO demo_signups
               (created_at, status, otp_id, person_name, company_name, company_email,
                phone, business_category, usecase, ip, user_agent)
               VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), otp_id, name, company, email, phone, category, usecase, ip,
             (request.headers.get("user-agent") or "")[:300]))
        c.execute("INSERT INTO demo_otp_sends (at, phone, ip) VALUES (?,?,?)",
                  (_now(), phone, ip))
    log.info("otp sent phone=%s company=%r category=%r ref=%s (%s/%s in %smin)",
             _mask(phone), company, category, reference, sent_recently + 1,
             OTP_MAX_SENDS_PER_PHONE, OTP_SEND_WINDOW_MIN)

    # Whichever cooldown is longer, theirs or ours — offering a resend the server will
    # refuse is worse than no button at all.
    ours = datetime.now(timezone.utc) + timedelta(seconds=OTP_RESEND_COOLDOWN_SEC)
    theirs = _dt(d.get("resendAvailableAt"))
    resend_at = max(ours, theirs) if theirs else ours

    return {
        "otpId": otp_id,
        "expiresAt": d.get("expiresAt"),
        "resendAvailableAt": resend_at.isoformat(timespec="seconds"),
        "phoneMasked": _mask(phone),
        "attemptsAllowed": OTP_MAX_ATTEMPTS,
        "sendsRemaining": max(OTP_MAX_SENDS_PER_PHONE - (sent_recently + 1), 0),
    }


@app.post("/api/otp/verify")
async def otp_verify(request: Request, payload: dict = Body(default={})):
    ip = _client_ip(request)
    otp_id = (payload.get("otpId") or "").strip()
    code = re.sub(r"\D", "", str(payload.get("code") or ""))

    if not otp_id or len(code) != OTP_CODE_LENGTH:
        return _fail(400, f"Enter the {OTP_CODE_LENGTH}-digit code from WhatsApp.")
    if not _limit(f"verify:ip:{ip}", 20, 900):
        return _fail(429, "Too many attempts. Please try again in 15 minutes.")

    with _db() as c:
        row = c.execute("SELECT * FROM demo_signups WHERE otp_id=?", (otp_id,)).fetchone()
    if row is None:
        return _fail(400, "That code has expired. Please request a new one.", restart=True)
    if row["status"] == "verified":
        # A double-click or network retry resubmits a code that already worked. Treat
        # it as the success it is rather than an error the user can't act on.
        return _verified_response(row, ip)

    # The number being verified comes from OUR row, so a lock cannot be dodged by
    # verifying someone else's otpId.
    if (secs := lock_seconds(f"phone:{row['phone']}")):
        return _locked_fail(secs, f"This number is locked for {_human_wait(secs)} after "
                                  f"too many incorrect codes.", "phone", restart=True)
    if (secs := lock_seconds(f"ip:{ip}")):
        return _locked_fail(secs, f"Too many failed attempts from your network. Please "
                                  f"try again in {_human_wait(secs)}.", "network")

    status, data = await _otp_call("verify", {"otpId": otp_id, "code": code})

    if not (200 <= status < 300 and data.get("success")):
        if status == 401:
            log.error("OTP provider rejected OUR API KEY (401) on verify")
            return _fail(503, "Verification is temporarily unavailable. Please try "
                              "again shortly.")
        log.warning("otp verify failed status=%s otp=%s msg=%r", status, otp_id,
                    _provider_message(data, ""))
        if 400 <= status < 500:
            return _fail(400, "That code has expired. Please request a new one.",
                         restart=True)
        return _fail(502, "We couldn't check your code just now. Please try again.")

    d = data.get("data") or {}
    if not d.get("verified"):
        reason = str(d.get("reason") or "").lower()
        left = d.get("attemptsRemaining")
        log.info("otp verify rejected otp=%s reason=%s left=%s", otp_id, reason, left)

        # An expired or already-used code is not a guess — it costs the visitor nothing
        # and counts against nobody.
        if reason in ("expired", "otp_expired", "code_expired"):
            return _fail(400, "That code has expired. Please request a new one.",
                         reason=reason, restart=True)
        if reason in ("already_verified", "already_used", "used", "consumed"):
            return _fail(400, "That code has already been used. Please request a new "
                              "one.", reason=reason, restart=True)

        # Everything else is a wrong code. Count it against the NUMBER and the network,
        # not the code: the provider's own attempt counter resets with every new code,
        # so on its own it cannot stop someone cycling codes at a number all day.
        guard = guard_fail(f"phone:{row['phone']}", OTP_MAX_ATTEMPTS, "wrong code")
        guard_fail(f"ip:{ip}", OTP_MAX_FAILS_PER_IP, "wrong codes across numbers")
        if guard["locked"]:
            log.warning("otp LOCKOUT phone=%s after %s wrong codes",
                        _mask(row["phone"]), OTP_MAX_ATTEMPTS)
            return _locked_fail(
                guard["seconds"],
                f"That's {OTP_MAX_ATTEMPTS} incorrect codes. This number is locked for "
                f"{_human_wait(guard['seconds'])}.", "phone",
                reason=reason, restart=True)

        remaining = guard["left"]
        if isinstance(left, int):
            remaining = min(remaining, max(left, 0))
        if remaining <= 0:
            # The provider ran out of attempts on this code before our lock fired.
            return _fail(400, "Too many incorrect attempts. Please request a new code.",
                         reason=reason, restart=True, attemptsRemaining=0)
        plural = "attempt" if remaining == 1 else "attempts"
        return _fail(400, f"That code isn't right. {remaining} {plural} left before this "
                          f"number is locked.", reason=reason, attemptsRemaining=remaining)

    with _db() as c:
        c.execute("UPDATE demo_signups SET status='verified', verified_at=? WHERE id=?",
                  (_now(), row["id"]))
        row = c.execute("SELECT * FROM demo_signups WHERE id=?", (row["id"],)).fetchone()
    # The right code clears the failure counts. Strikes are kept on purpose, so a
    # repeat offender's next lock is still the longer one.
    guard_clear(f"phone:{row['phone']}")
    guard_clear(f"ip:{ip}")
    log.info("signup VERIFIED id=%s phone=%s company=%r category=%r",
             row["id"], _mask(row["phone"]), row["company_name"], row["business_category"])
    return _verified_response(row, ip)


@app.post("/api/signout")
async def signout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints — the call
# ══════════════════════════════════════════════════════════════════════════════
_MAX_PER_10MIN = int(os.getenv("DEMO_MAX_STARTS_PER_10MIN", "12"))


@app.post("/api/token")
async def token(request: Request, payload: dict = Body(default={})):
    """Mint a join token AND put an agent in the room. Both, or neither."""
    if DEMO_PASSCODE and str(payload.get("passcode", "")).strip() != DEMO_PASSCODE:
        raise HTTPException(status_code=401, detail="Wrong passcode.")
    if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
        raise HTTPException(status_code=500,
                            detail="Server is missing LIVEKIT_API_KEY/SECRET.")

    signup = None
    if DEMO_REQUIRE_SIGNUP:
        signup = _signup_from_request(request)
        if signup is None:
            return _fail(401, "Please verify your details to start the demo.",
                         code="signup_required")

    # Which bot. Checked BEFORE the call is reserved, so a stale page asking for a bot
    # that's since been switched off doesn't cost the visitor one of their calls.
    bots = enabled_keys()
    bot = str(payload.get("bot") or "").strip().lower() or bots[0]
    if bot not in bots:
        return _fail(400, "That voicebot isn't available. Please pick another one.",
                     code="unknown_bot")

    if not _limit("token:global", _MAX_PER_10MIN, 600):
        return _fail(429, "The demo is busy right now. Please try again in a few "
                          "minutes.", code="busy")

    # Reserve the call BEFORE dispatching, conditionally, so two tabs racing at the
    # limit can't both get through. Refunded below if the dispatch fails.
    ip = _client_ip(request)
    log_id = None
    if signup is not None:
        window = (f" in the last {DEMO_CALL_WINDOW_HOURS} hours"
                  if DEMO_CALL_WINDOW_HOURS else "")
        by_phone, by_ip = _calls_used(signup["phone"], ip)
        # Per phone and per network, checked first: both hold across signup rows, so
        # filling the form in again with the same number buys nothing.
        if by_phone >= DEMO_MAX_CALLS_PER_PHONE:
            return _fail(429, f"This number has already had {DEMO_MAX_CALLS_PER_PHONE} "
                              f"demo calls{window}. Message us and we'll set up a full "
                              f"trial.", code="call_limit", scope="phone")
        if by_ip >= DEMO_MAX_CALLS_PER_IP:
            return _fail(429, f"This network has already had {DEMO_MAX_CALLS_PER_IP} "
                              f"demo calls{window}. Message us and we'll set up a full "
                              f"trial.", code="call_limit", scope="network")
        with _db() as c:
            cur = c.execute(
                "UPDATE demo_signups SET demo_calls = demo_calls + 1, last_call_at = ?, "
                "last_bot = ? WHERE id = ? AND demo_calls < ?",
                (_now(), bot, signup["id"], DEMO_MAX_CALLS_PER_SIGNUP))
            reserved = cur.rowcount == 1
            if reserved:
                # Same transaction as the counter, so the two can never disagree.
                log_id = c.execute(
                    "INSERT INTO demo_call_log (at, signup_id, phone, ip, bot) "
                    "VALUES (?,?,?,?,?)",
                    (_now(), signup["id"], signup["phone"], ip, bot)).lastrowid
        if not reserved:
            return _fail(429, f"You've used all {DEMO_MAX_CALLS_PER_SIGNUP} demo calls. "
                              f"Message us and we'll set up a full trial.",
                         code="call_limit", scope="signup")

    def _refund():
        if signup is not None:
            with _db() as c:
                c.execute("UPDATE demo_signups SET demo_calls = MAX(demo_calls - 1, 0) "
                          "WHERE id = ?", (signup["id"],))
                if log_id is not None:
                    c.execute("DELETE FROM demo_call_log WHERE id = ?", (log_id,))

    if signup is not None:
        # From the verified row — never from the request body.
        name, phone = signup["person_name"], signup["phone"]
    else:
        name = (payload.get("name") or "").strip()[:60]
        phone = (payload.get("phone") or "").strip()[:20]
    state    = (payload.get("state") or "").strip().upper()[:3]
    postcode = (payload.get("postcode") or "").strip()[:4]

    tts = (payload.get("tts") or "").strip()
    if tts and tts not in TTS_CHOICES:
        log.warning("rejected unknown tts=%r — using the bot's own voice", tts)
        tts = ""

    room     = f"demo-{bot}-{secrets.token_hex(4)}"
    call_id  = f"web-{secrets.token_hex(6)}"
    identity = f"web-{secrets.token_hex(4)}"

    # The same dialer.* keys the agent reads off a SIP participant, which is why the
    # agent needs no web-specific branch.
    attrs = {
        "dialer.campaign": f"web-demo-{bot}",
        "dialer.call_id":  call_id,
        "dialer.source":   "web",
        "dialer.bot":      bot,
    }
    if tts:
        attrs["dialer.tts"] = tts
    # Stand-in campaign data some bots expect — the movers bot's online inquiry. Server-
    # side only, from demo_bots.py; nothing in the request can set these.
    for k, v in demo_attrs(bot).items():
        attrs[f"dialer.{k}"] = v[:120]
    if name:
        attrs["dialer.prospect_name"] = name
    if phone:
        attrs["dialer.phone"] = phone
    if state:
        attrs["dialer.state"] = state
    if postcode:
        attrs["dialer.postcode"] = postcode
    if signup is not None:
        attrs["dialer.signup_id"] = str(signup["id"])
        attrs["dialer.company"] = signup["company_name"][:120]

    lkapi = api.LiveKitAPI(url=_API_URL, api_key=LIVEKIT_API_KEY,
                           api_secret=LIVEKIT_API_SECRET)
    try:
        # Dispatch FIRST so the agent is already in the room when the browser arrives.
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=DEMO_AGENT_NAME,
                room=room,
                metadata=json.dumps({"source": "web-demo", "call_id": call_id,
                                     "bot": bot, "tts": tts,
                                     "signup_id": signup["id"] if signup else None}),
            )
        )
        log.info("dispatched %s bot=%s -> room=%s signup=%s tts=%s", DEMO_AGENT_NAME,
                 bot, room, signup["id"] if signup else "-", tts or "bot default")
    except Exception as e:
        _refund()
        log.error("agent dispatch failed for room=%s: %s", room, e)
        return _fail(503, "We couldn't start the agent just now. Please try again in a "
                          "moment — this didn't use up one of your calls.")
    finally:
        await lkapi.aclose()

    grant = api.VideoGrants(
        room_join=True, room=room,
        can_publish=True, can_subscribe=True, can_publish_data=True,
    )
    at = (api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
          .with_identity(identity)
          .with_name(name or "Web demo")
          .with_grants(grant)
          .with_ttl(timedelta(minutes=ROOM_TTL_MINUTES)))
    try:
        at = at.with_attributes(attrs)
    except AttributeError:
        log.warning("AccessToken.with_attributes unavailable; passing context as metadata")
        at = at.with_metadata(json.dumps(attrs))

    result = {
        "url": PUBLIC_LIVEKIT_URL or LIVEKIT_URL,
        "token": at.to_jwt(),
        "room": room,
        "identity": identity,
        "callId": call_id,
        "bot": bot,
        "tts": tts,
        "ttsLabel": TTS_CHOICES.get(tts, ""),
    }
    if signup is not None:
        with _db() as c:
            fresh = c.execute("SELECT * FROM demo_signups WHERE id=?",
                              (signup["id"],)).fetchone()
        result["callsRemaining"] = _public_signup(fresh, ip)["callsRemaining"]
    return result


@app.post("/api/end")
async def end(payload: dict = Body(default={})):
    """Belt-and-braces hangup: an explicit room delete guarantees the worker subprocess
    is reclaimed even if a tab is force-closed."""
    room = (payload.get("room") or "").strip()
    if not room.startswith("demo-"):      # never let this touch a live SIP room
        raise HTTPException(status_code=400, detail="Not a demo room.")
    lkapi = api.LiveKitAPI(url=_API_URL, api_key=LIVEKIT_API_KEY,
                           api_secret=LIVEKIT_API_SECRET)
    try:
        await lkapi.room.delete_room(api.DeleteRoomRequest(room=room))
    except Exception as e:
        log.info("delete_room(%s) failed (usually already gone): %s", room, e)
    finally:
        await lkapi.aclose()
    return {"ok": True}


@app.middleware("http")
async def _revalidate_static(request, call_next):
    """StaticFiles sends an ETag but no Cache-Control, so browsers may serve a cached
    app.js without revalidating — a deploy then silently doesn't take effect."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/healthz")
async def healthz():
    with _db() as c:
        locked = c.execute("SELECT COUNT(*) FROM demo_guard WHERE locked_until > ?",
                           (_now(),)).fetchone()[0]
    return JSONResponse({"ok": True, "agent": DEMO_AGENT_NAME, "bots": enabled_keys(),
                         "signupRequired": DEMO_REQUIRE_SIGNUP,
                         "otpConfigured": bool(OTP_API_KEY),
                         "maxAttempts": OTP_MAX_ATTEMPTS,
                         "lockLadderMinutes": OTP_LOCK_LADDER,
                         "callsPerSignup": DEMO_MAX_CALLS_PER_SIGNUP,
                         "callsPerPhone": DEMO_MAX_CALLS_PER_PHONE,
                         "callsPerIp": DEMO_MAX_CALLS_PER_IP,
                         "callWindowHours": DEMO_CALL_WINDOW_HOURS,
                         "lockedNow": int(locked)})


@app.get("/")
async def index():
    return FileResponse(os.path.join(_STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=_STATIC), name="static")
