"""
receiver.py — disposition webhook receiver for all five voice campaigns.

    smarthealth-bot ─┐
    roof-bot ────────┤
    finance-bot ─────┼─ POST http://127.0.0.1:8095/disposition/... ─→ MySQL (localhost)
    movers-bot ──────┤
    warranty-bot ────┘

Five endpoints, one per campaign, because the payloads are shaped differently (see
DISPOSITION_WEBHOOK_SPEC.md §9). Each has its own explicit mapper, so a payload change
shows up as a loud error rather than a column full of nulls.

The browser demo does NOT post here: voicebot-web blanks the webhook, so demo calls
never land in the lead tables.

DESIGN CONSTRAINTS THAT DROVE THIS
  • The bots allow 10 seconds and DO NOT RETRY. One INSERT is milliseconds; anything
    slower risks losing a lead outright. Nothing here does work off the hot path.
  • A failed insert returns 500 AND appends the payload to a JSONL file. The bot logs
    the failure and still has its local CSV, so a lead survives three ways.
  • A connection per request, not a pool. At a few calls a minute the cost is
    negligible, and it sidesteps the classic long-lived-process failure where MySQL
    times out an idle connection and every later insert dies on a stale socket.

Run:
  pip install -r requirements.txt
  uvicorn receiver:app --host 127.0.0.1 --port 8095

Env (.env in this folder):
  DB_HOST=127.0.0.1
  DB_PORT=3306
  DB_NAME=voicebot
  DB_USER=voicebot
  DB_PASSWORD=...
  WEBHOOK_TOKEN=            # optional; must match the bots' DISPOSITION_WEBHOOK_TOKEN
  FAILED_LOG=/opt/disposition-receiver/failed_inserts.jsonl
"""

import os
import json
import logging
from datetime import datetime, timezone

import pymysql
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.responses import JSONResponse

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dispositions")

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "voicebot"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "voicebot"),
    charset="utf8mb4",
    autocommit=True,
    connect_timeout=5,
    read_timeout=5,
    write_timeout=5,
)
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
FAILED_LOG = os.getenv("FAILED_LOG", "failed_inserts.jsonl")

if not DB["password"]:
    log.error("DB_PASSWORD is empty — every insert will fail. Set it in .env.")

app = FastAPI(title="Voicebot disposition receiver")


# ── Helpers ──────────────────────────────────────────────────────────────────
def _auth(request: Request) -> None:
    """Bearer check, only enforced when a token is configured. The bots send
    'Authorization: Bearer <DISPOSITION_WEBHOOK_TOKEN>' when theirs is set."""
    if not WEBHOOK_TOKEN:
        return
    got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if got != WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


def _dt(value):
    """'2026-09-01T05:51:38.060361+00:00' -> naive UTC datetime for MySQL.

    MySQL DATETIME has no timezone, so the offset must be applied and dropped rather
    than ignored — otherwise a non-UTC sender would silently store local time."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        log.warning("unparseable timestamp %r — storing NULL", value)
        return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _s(value, limit: int):
    """Trim to the column width. A caller reading out a long address should not fail
    the whole insert on a Data too long error and cost us the lead."""
    if value is None:
        return None
    s = str(value)
    if len(s) > limit:
        log.warning("truncating %d chars to %d: %r", len(s), limit, s[:60])
        return s[:limit]
    return s


def _bool(value) -> int:
    return 1 if value is True or str(value).lower() in ("true", "1", "yes") else 0


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _insert(sql: str, params: tuple, payload: dict, table: str) -> int:
    conn = None
    try:
        conn = pymysql.connect(**DB)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.lastrowid
    except Exception as e:
        # Never lose the lead. It also exists in the bot's CSV, but this file is the one
        # a replay reads from, and it sits next to the service that failed.
        log.error("INSERT into %s FAILED: %s", table, e)
        try:
            with open(FAILED_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"table": table, "error": str(e),
                                    "at": datetime.now(timezone.utc).isoformat(),
                                    "payload": payload}, ensure_ascii=False) + "\n")
        except Exception as e2:
            log.critical("could not even write the failure log: %s", e2)
        raise HTTPException(status_code=500, detail=f"insert failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Campaign A — Smart Health ────────────────────────────────────────────────
SH_SQL = """
INSERT INTO sh_call_dispositions (
  call_id, agent, campaign,
  prospect_name, prospect_phone, prospect_state, prospect_postcode,
  outcome, disposition_code, consent_to_contact, callback_request,
  remarks, decline_reason,
  started_at, ended_at, duration_seconds,
  qual_holds_policy, qual_over_min_age, qual_insurer, qual_cover_type,
  qual_premium_amount, qual_premium_frequency, qual_hospital_extras,
  qual_permanent_resident, qual_main_interest, qual_first_name, qual_last_name,
  qual_email, qual_postcode, qual_decision_maker,
  usage_stt_seconds, usage_llm_tokens, usage_tts_characters,
  raw_payload
) VALUES (
  %s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s,%s,
  %s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s, %s,%s,%s,
  %s,%s,%s, %s
)
"""


@app.post("/disposition/smarthealth")
async def smarthealth(request: Request, payload: dict = Body(...)):
    _auth(request)
    p = payload.get("prospect") or {}
    q = payload.get("qualification") or {}
    u = payload.get("usage") or {}

    params = (
        _s(payload.get("call_id"), 64), _s(payload.get("agent"), 32),
        _s(payload.get("campaign"), 64),
        _s(p.get("name"), 120), _s(p.get("phone"), 32),
        _s(p.get("state"), 8), _s(p.get("postcode"), 12),
        _s(payload.get("outcome") or "DROPPED", 32),
        _s(payload.get("disposition_code"), 64),
        _bool(payload.get("consent_to_contact")),
        _s(payload.get("callback_request"), 120),
        payload.get("remarks"), payload.get("decline_reason"),
        _dt(payload.get("started_at")), _dt(payload.get("ended_at")),
        _num(payload.get("duration_seconds")),
        _s(q.get("holds_policy"), 32), _s(q.get("over_min_age"), 32),
        _s(q.get("insurer"), 64), _s(q.get("cover_type"), 32),
        _s(q.get("premium_amount"), 120), _s(q.get("premium_frequency"), 32),
        _s(q.get("hospital_extras"), 32), _s(q.get("permanent_resident"), 32),
        _s(q.get("main_interest"), 64), _s(q.get("first_name"), 80),
        _s(q.get("last_name"), 80), _s(q.get("email"), 160),
        _s(q.get("postcode"), 12), _s(q.get("decision_maker"), 32),
        _num(u.get("stt_audio_duration")), u.get("llm_total_tokens"),
        u.get("tts_characters_count"),
        json.dumps(payload, ensure_ascii=False),
    )
    row_id = _insert(SH_SQL, params, payload, "sh_call_dispositions")
    log.info("smarthealth id=%s call_id=%s outcome=%s",
             row_id, payload.get("call_id"), payload.get("outcome"))
    return {"status": "ok", "id": row_id}


# ── Campaign B — Roof UK ─────────────────────────────────────────────────────
ROOF_SQL = """
INSERT INTO roof_call_dispositions (
  call_id, campaign, phone, prospect_name,
  started_at, ended_at, duration_sec,
  outcome, outcome_note, consent_given, callback_agreed, callback_request,
  decline_reason, remarks,
  qual_homeowner, qual_interested, qual_roof_age, qual_checked_recently,
  qual_first_name, qual_last_name, qual_address, qual_callback_number,
  missing_required, raw_payload
) VALUES (
  %s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s,%s, %s,%s,
  %s,%s,%s,%s, %s,%s,%s,%s, %s,%s
)
"""


@app.post("/disposition/roof")
async def roof(request: Request, payload: dict = Body(...)):
    _auth(request)
    q = payload.get("qualification") or {}
    dur = payload.get("duration_sec")

    params = (
        _s(payload.get("call_id"), 64), _s(payload.get("campaign"), 64),
        _s(payload.get("phone"), 32), _s(payload.get("prospect_name"), 120),
        _dt(payload.get("started_at")), _dt(payload.get("ended_at")),
        int(dur) if isinstance(dur, (int, float)) else None,
        _s(payload.get("outcome") or "DROPPED", 32),
        _s(payload.get("outcome_note"), 80),
        _bool(payload.get("consent_given")), _bool(payload.get("callback_agreed")),
        _s(payload.get("callback_request"), 120),
        payload.get("decline_reason"), payload.get("remarks"),
        _s(q.get("homeowner"), 32), _s(q.get("interested"), 32),
        _s(q.get("roof_age"), 120), _s(q.get("checked_recently"), 64),
        _s(q.get("first_name"), 80), _s(q.get("last_name"), 80),
        _s(q.get("address"), 255), _s(q.get("callback_number"), 32),
        json.dumps(payload.get("missing_required") or [], ensure_ascii=False),
        json.dumps(payload, ensure_ascii=False),
    )
    row_id = _insert(ROOF_SQL, params, payload, "roof_call_dispositions")
    log.info("roof id=%s call_id=%s outcome=%s",
             row_id, payload.get("call_id"), payload.get("outcome"))
    return {"status": "ok", "id": row_id}


# ── Campaign C — Finance AU ──────────────────────────────────────────────────
FIN_SQL = """
INSERT INTO fin_call_dispositions (
  call_id, campaign, phone, prospect_name,
  started_at, ended_at, duration_sec,
  outcome, outcome_note, consent_given, info_pack_sent, callback_request,
  decline_reason, remarks,
  qual_looking_for_finance, qual_loan_purpose, qual_amount, qual_business_stage,
  qual_business_age, qual_monthly_turnover, qual_loan_use, qual_employment_status,
  qual_income_amount, qual_income_frequency, qual_liabilities,
  qual_first_name, qual_last_name, qual_email, qual_email_spoken, qual_contact_number,
  usage_stt_seconds, usage_llm_tokens, usage_tts_characters,
  missing_required, raw_payload
) VALUES (
  %s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s,%s, %s,%s,
  %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,
  %s,%s,%s,%s,%s, %s,%s,%s, %s,%s
)
"""


@app.post("/disposition/finance")
async def finance(request: Request, payload: dict = Body(...)):
    _auth(request)
    q = payload.get("qualification") or {}
    u = payload.get("usage") or {}
    dur = payload.get("duration_sec")

    params = (
        _s(payload.get("call_id"), 64), _s(payload.get("campaign"), 64),
        _s(payload.get("phone"), 32), _s(payload.get("prospect_name"), 120),
        _dt(payload.get("started_at")), _dt(payload.get("ended_at")),
        int(dur) if isinstance(dur, (int, float)) else None,
        _s(payload.get("outcome") or "DROPPED", 32),
        _s(payload.get("outcome_note"), 80),
        _bool(payload.get("consent_given")), _bool(payload.get("info_pack_sent")),
        _s(payload.get("callback_request"), 120),
        payload.get("decline_reason"), payload.get("remarks"),
        _s(q.get("looking_for_finance"), 32), _s(q.get("loan_purpose"), 32),
        _s(q.get("amount"), 120), _s(q.get("business_stage"), 32),
        _s(q.get("business_age"), 120), _s(q.get("monthly_turnover"), 120),
        _s(q.get("loan_use"), 255), _s(q.get("employment_status"), 64),
        _s(q.get("income_amount"), 120), _s(q.get("income_frequency"), 32),
        q.get("liabilities"),
        _s(q.get("first_name"), 80), _s(q.get("last_name"), 80),
        # `or None` so "no email" is NULL, not an empty string. The difference matters:
        # the whole close of this campaign is an information email, so "did we get an
        # address?" is the report everyone will run, and `WHERE qual_email IS NOT NULL`
        # should mean exactly that. email_spoken keeps the raw words either way — it is
        # a repair hint for a human, never something to send mail to.
        _s(q.get("email") or None, 160), _s(q.get("email_spoken") or None, 255),
        _s(q.get("contact_number"), 32),
        _num(u.get("stt_audio_duration")), u.get("llm_total_tokens"),
        u.get("tts_characters_count"),
        json.dumps(payload.get("missing_required") or [], ensure_ascii=False),
        json.dumps(payload, ensure_ascii=False),
    )
    row_id = _insert(FIN_SQL, params, payload, "fin_call_dispositions")
    log.info("finance id=%s call_id=%s outcome=%s email=%s",
             row_id, payload.get("call_id"), payload.get("outcome"),
             "yes" if q.get("email") else "no")
    return {"status": "ok", "id": row_id}


# ── Campaign D — Movers AU ───────────────────────────────────────────────────
MOV_SQL = """
INSERT INTO mov_call_dispositions (
  call_id, campaign, phone, prospect_name,
  started_at, ended_at, duration_sec,
  outcome, outcome_note, consent_given, booking_confirmed, callback_request,
  decline_reason, remarks,
  qual_details_confirmed, qual_date_confirmed, qual_pickup_area, qual_dropoff_area,
  qual_move_date, qual_property_size, qual_items, qual_furniture, qual_heavy_items,
  qual_fragile_items, qual_access, qual_parking, qual_notes, qual_arrival_window,
  qual_full_name, qual_contact_number, qual_email, qual_email_spoken,
  qual_pickup_address, qual_delivery_address,
  quote_bedrooms, quote_movers, quote_vehicle_tonnes, quote_rate, quote_min_hours,
  quote_callout, quote_deposit, quote_estimate_min,
  card_numbers_blocked, missing_required, raw_payload
) VALUES (
  %s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s,%s, %s,%s,
  %s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,
  %s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s
)
"""


@app.post("/disposition/movers")
async def movers(request: Request, payload: dict = Body(...)):
    _auth(request)
    q = payload.get("qualification") or {}
    # Priced in code from the rate card, in dollars. Empty when the call ended before a
    # quote — every quote_* column is then NULL, which is the honest answer to
    # "what were they quoted?".
    quote = payload.get("quote") or {}
    dur = payload.get("duration_sec")

    params = (
        _s(payload.get("call_id"), 64), _s(payload.get("campaign"), 64),
        _s(payload.get("phone"), 32), _s(payload.get("prospect_name"), 120),
        _dt(payload.get("started_at")), _dt(payload.get("ended_at")),
        int(dur) if isinstance(dur, (int, float)) else None,
        _s(payload.get("outcome") or "DROPPED", 32),
        _s(payload.get("outcome_note"), 80),
        _bool(payload.get("consent_given")), _bool(payload.get("booking_confirmed")),
        _s(payload.get("callback_request"), 120),
        payload.get("decline_reason"), payload.get("remarks"),
        _s(q.get("details_confirmed"), 32), _s(q.get("date_confirmed"), 32),
        _s(q.get("pickup_area"), 120), _s(q.get("dropoff_area"), 120),
        _s(q.get("move_date"), 120), _s(q.get("property_size"), 120),
        q.get("items"), q.get("furniture"), q.get("heavy_items"),
        q.get("fragile_items"), q.get("access"), q.get("parking"), q.get("notes"),
        _s(q.get("arrival_window"), 64),
        _s(q.get("full_name"), 120), _s(q.get("contact_number"), 32),
        # `or None` so "no email" is NULL, not an empty string: the confirmation goes by
        # text when there is no address, so "did we get one?" is a real report.
        # email_spoken keeps the raw words as a repair hint — never something to mail.
        _s(q.get("email") or None, 160), _s(q.get("email_spoken") or None, 255),
        _s(q.get("pickup_address"), 255), _s(q.get("delivery_address"), 255),
        quote.get("bedrooms"), quote.get("movers"), _num(quote.get("truck_tonnes")),
        _num(quote.get("rate")), _num(quote.get("min_hours")),
        _num(quote.get("callout")), _num(quote.get("deposit")),
        _num(quote.get("estimate_min")),
        int(payload.get("card_numbers_blocked") or 0),
        json.dumps(payload.get("missing_required") or [], ensure_ascii=False),
        json.dumps(payload, ensure_ascii=False),
    )
    row_id = _insert(MOV_SQL, params, payload, "mov_call_dispositions")
    log.info("movers id=%s call_id=%s outcome=%s quote=%s",
             row_id, payload.get("call_id"), payload.get("outcome"),
             quote.get("estimate_min") or "none")
    return {"status": "ok", "id": row_id}


# ── Campaign E — Warranty UK ─────────────────────────────────────────────────
WTY_SQL = """
INSERT INTO wty_call_dispositions (
  call_id, campaign, phone, prospect_name,
  started_at, ended_at, duration_sec,
  outcome, outcome_note, consent_given, info_sent, eligibility, callback_request,
  decline_reason, remarks,
  qual_appliance, qual_appliance_confirmed, qual_appliance_age, qual_working_normally,
  qual_existing_fault, qual_already_covered, qual_interest, qual_discuss_with,
  qual_notes, qual_first_name, qual_last_name, qual_email, qual_email_spoken,
  qual_contact_number,
  quote_months, quote_price, quote_includes,
  card_numbers_blocked, missing_required, raw_payload
) VALUES (
  %s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s,%s,%s, %s,%s,
  %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s, %s,
  %s,%s,%s, %s,%s,%s
)
"""


@app.post("/disposition/warranty")
async def warranty(request: Request, payload: dict = Body(...)):
    _auth(request)
    q = payload.get("qualification") or {}
    # Stated by the code from the plan settings, in pounds. Empty whenever no price was
    # given — which includes every call where the appliance was too old to quote for.
    quote = payload.get("quote") or {}
    dur = payload.get("duration_sec")
    includes = quote.get("includes")

    params = (
        _s(payload.get("call_id"), 64), _s(payload.get("campaign"), 64),
        _s(payload.get("phone"), 32), _s(payload.get("prospect_name"), 120),
        _dt(payload.get("started_at")), _dt(payload.get("ended_at")),
        int(dur) if isinstance(dur, (int, float)) else None,
        _s(payload.get("outcome") or "DROPPED", 32),
        _s(payload.get("outcome_note"), 80),
        _bool(payload.get("consent_given")), _bool(payload.get("info_sent")),
        _s(payload.get("eligibility"), 16),
        _s(payload.get("callback_request"), 120),
        payload.get("decline_reason"), payload.get("remarks"),
        _s(q.get("appliance"), 120), _s(q.get("appliance_confirmed"), 32),
        _s(q.get("appliance_age"), 120), _s(q.get("working_normally"), 32),
        q.get("existing_fault"), _s(q.get("already_covered"), 160),
        _s(q.get("interest"), 120), _s(q.get("discuss_with"), 120),
        q.get("notes"), _s(q.get("first_name"), 80), _s(q.get("last_name"), 80),
        # `or None` so "no email" is NULL, not an empty string: the details go by text
        # when there is no address, so "could we email it?" is a real report.
        _s(q.get("email") or None, 160), _s(q.get("email_spoken") or None, 255),
        _s(q.get("contact_number"), 32),
        quote.get("months"), _num(quote.get("price")),
        json.dumps(includes, ensure_ascii=False) if includes else None,
        int(payload.get("card_numbers_blocked") or 0),
        json.dumps(payload.get("missing_required") or [], ensure_ascii=False),
        json.dumps(payload, ensure_ascii=False),
    )
    row_id = _insert(WTY_SQL, params, payload, "wty_call_dispositions")
    log.info("warranty id=%s call_id=%s outcome=%s eligibility=%s fault=%s",
             row_id, payload.get("call_id"), payload.get("outcome"),
             payload.get("eligibility") or "-",
             "yes" if (q.get("existing_fault") or "none").lower() not in ("", "none") else "no")
    return {"status": "ok", "id": row_id}


# ── Ops ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Proves the DB is actually reachable, not just that the process is up — a
    receiver that returns 200 while MySQL is down is worse than one that's stopped."""
    try:
        conn = pymysql.connect(**DB)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        return {"ok": True, "db": "up"}
    except Exception as e:
        log.error("health check failed: %s", e)
        return JSONResponse(status_code=503, content={"ok": False, "db": str(e)})


@app.get("/stats")
async def stats():
    """Today's calls by outcome, every campaign. Cheap sanity check after a batch."""
    out = {}
    try:
        conn = pymysql.connect(**DB)
        with conn.cursor() as cur:
            for label, table in (("smarthealth", "sh_call_dispositions"),
                                 ("roof", "roof_call_dispositions"),
                                 ("finance", "fin_call_dispositions"),
                                 ("movers", "mov_call_dispositions"),
                                 ("warranty", "wty_call_dispositions")):
                cur.execute(
                    f"SELECT outcome, COUNT(*) FROM {table} "
                    f"WHERE received_at >= CURDATE() GROUP BY outcome ORDER BY 2 DESC"
                )
                out[label] = {row[0]: row[1] for row in cur.fetchall()}
        conn.close()
        return out
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})
