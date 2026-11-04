"""End-of-call report: outcome, opt-in/opt-out flags and full transcript, POSTed to the
backend webhook and appended to a local JSONL log. Delivery fails soft — an outage must
never break call teardown."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from .call_state import CallState, Outcome
from .settings import Settings

log = logging.getLogger(__name__)

OUTCOME_TO_CODE = {
    Outcome.TRANSFERRED:     "Transferred to Human",
    Outcome.INTERESTED:      "Interested",
    Outcome.CALLBACK:        "Callback Requested",
    Outcome.WHATSAPP_OPT_IN: "WhatsApp Opt-in",
    Outcome.EMAIL_OPT_IN:    "Email Opt-in",
    Outcome.OPT_OUT:         "DNC",
    Outcome.NOT_INTERESTED:  "Not Interested",
    Outcome.WRONG_NUMBER:    "Wrong Number",
    Outcome.INFO_ONLY:       "Information Given",
    Outcome.NO_OUTCOME:      "Follow-up",
    Outcome.DROPPED:         "Dropped",
}

_WEBHOOK_TIMEOUT_SECONDS = 10


def build_payload(state: CallState, agent_name: str, ended_at: datetime | None = None) -> dict:
    """The JSON body sent to the backend. The backend owns the follow-up actions:
    block list (do_not_call), WhatsApp/email sending, callbacks and lead creation."""
    ended_at = ended_at or datetime.now(timezone.utc)
    outcome = state.final_outcome()
    return {
        "call_id":            state.call_id,
        "agent":              agent_name,
        "direction":          "inbound",
        "campaign":           state.campaign.key,
        "company":            state.campaign.company,
        "did":                state.did,
        "caller_number":      state.caller,
        "outcome":            outcome,
        "disposition_code":   OUTCOME_TO_CODE.get(outcome, "Follow-up"),
        "do_not_call":        state.do_not_call,
        "whatsapp_opt_in":    bool(state.whatsapp_number),
        "whatsapp_number":    state.whatsapp_number,
        "email_opt_in":       bool(state.email),
        "email":              state.email,
        "interested":         state.interested,
        "callback_requested": bool(state.callback_when),
        "callback_when":      state.callback_when,
        "transferred":        state.transferred,
        "notes":              state.notes,
        "answers":            state.answers,
        "recording":          state.recording,
        "room":               state.room,
        "started_at":         state.started_at.isoformat(),
        "ended_at":           ended_at.isoformat(),
        "duration_seconds":   round((ended_at - state.started_at).total_seconds(), 1),
        "transcript":         state.transcript,
    }


def _append_jsonl(path: str, payload: dict) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("could not append call log to %s: %s", path, e)


async def _post_webhook(url: str, token: str, payload: dict) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=_WEBHOOK_TIMEOUT_SECONDS)) as r:
                body = await r.text()
                log.info("disposition POST -> %s %s", r.status, body[:200])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("disposition POST failed: %s", e)


async def deliver(payload: dict, settings: Settings) -> None:
    if settings.transcript_log_path:
        _append_jsonl(settings.transcript_log_path, payload)
    if settings.webhook_url:
        await _post_webhook(settings.webhook_url, settings.webhook_token, payload)
    else:
        summary = {k: v for k, v in payload.items() if k != "transcript"}
        log.info("DISPOSITION_WEBHOOK_URL unset — not posted: %s", json.dumps(summary))


class DispositionReporter:
    """Sends the report exactly once.

    `start()` is called as soon as anyone leaves the room; `wait()` is awaited in the
    shutdown callback. The await is essential: on a bot hangup the room is deleted and
    the process exits within ~30ms, killing an un-awaited POST mid-flight.
    """

    def __init__(self, state: CallState, settings: Settings):
        self._state = state
        self._settings = settings
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        return self._task

    async def wait(self) -> None:
        try:
            await self.start()
        except Exception as e:  # teardown must never raise
            log.warning("disposition delivery failed: %s", e)

    async def _run(self) -> None:
        payload = build_payload(self._state, self._settings.agent_name)
        log.info("call ended → %s (%s)", payload["outcome"], payload["disposition_code"])
        await deliver(payload, self._settings)
