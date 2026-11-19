"""Guards that keep a call from sitting in dead air or running forever."""

from __future__ import annotations

import asyncio
import logging

from livekit.agents import AgentSession

from . import telephony
from .call_state import CallState
from .settings import Settings

log = logging.getLogger(__name__)

# Dead air = presence checks only. Do NOT pitch WhatsApp / email / callback while they
# have not answered — that sounds like a sales push into silence (see UID-1795086754).
_PRESENCE_CHECKS = (
    "The caller has gone quiet. In the SAME language you were speaking, ONE short line: "
    "gently check they can still hear you. Do not offer WhatsApp, email, a callback, or "
    "any product details.",
    "The caller is still quiet. In the SAME language, ONE short NEW line: check again that "
    "they're there (e.g. are you still with me?). Never repeat your last line. Do not offer "
    "WhatsApp, email, a callback, or any product details.",
    "Still no reply. In the SAME language, ONE short NEW presence check only — different "
    "wording from before. Do not offer WhatsApp, email, a callback, or any product details.",
)
_GOODBYE = (
    "The line has stayed silent after several checks. In the SAME language, ONE short "
    "polite goodbye only: you'll let them go, they're welcome to call back any time. "
    "Do not ask a question. Do not offer WhatsApp, email, or a callback."
)

# Don't let a stuck TTS/LLM block hang-up (recording UID-1795024111 looped for minutes).
_GOODBYE_PLAYOUT_TIMEOUT = 8.0


class SilenceNudger:
    """After the agent finishes speaking, wait `nudge_after_seconds` for the caller.
    Each unanswered silence → a short presence check only ("still there?");
    after `max_nudges` unanswered → goodbye and hang up. No WhatsApp / details offers
    into dead air.

    Only a real caller turn (STT / typed text with words) resets the nudge counter.
    VAD "speaking" alone does not — TV, fan, breathing and line noise trip VAD without
    producing a transcript, and used to keep dead calls alive forever.

    Hang-up must not depend on goodbye finishing: agent `speaking` used to cancel the
    hang-up task, and a stuck `wait_for_playout` would never reach `hang_up()`.
    """

    def __init__(self, session: AgentSession, state: CallState, settings: Settings):
        self._session = session
        self._state = state
        self._after = settings.nudge_after_seconds
        self._max = settings.max_nudges
        self._nudges = 0
        self._task: asyncio.Task | None = None
        self._closing = False   # goodbye + hang-up in progress — ignore cancels / new waits

    def attach(self) -> None:
        # Not user_state_changed: VAD "speaking" fires on noise/breathing without words.
        self._session.on("conversation_item_added", self._on_item)
        self._session.on("agent_state_changed", self._on_agent_state)

    def _on_item(self, ev) -> None:
        item = getattr(ev, "item", None)
        if item is None or str(getattr(item, "role", "")) != "user":
            return
        # Empty / whitespace-only items are not a reply (and console --text still sends
        # real text through this event).
        if not (getattr(item, "text_content", None) or "").strip():
            return
        self._caller_active()

    def _caller_active(self) -> None:
        if self._closing:
            return
        self._state.mark_person_spoke()
        self._nudges = 0
        self._cancel()

    def _on_agent_state(self, ev) -> None:
        if self._closing:
            return
        state = str(getattr(ev, "new_state", ""))
        if state == "speaking":
            self._cancel()
        elif state == "listening":
            # Only in true `listening`, not while STT is still transcribing.
            self._cancel()
            self._task = asyncio.create_task(self._wait_then_nudge())

    def _cancel(self) -> None:
        if self._closing:
            return
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _wait_then_nudge(self) -> None:
        try:
            await asyncio.sleep(self._after)
            if self._nudges >= self._max:
                await self._goodbye_and_hang_up()
                return
            self._nudges += 1
            # Cycle presence-only lines; never pitch offers while they're silent.
            instructions = _PRESENCE_CHECKS[(self._nudges - 1) % len(_PRESENCE_CHECKS)]
            self._session.generate_reply(instructions=instructions)
        except asyncio.CancelledError:
            pass

    async def _goodbye_and_hang_up(self) -> None:
        """Say a short goodbye (best-effort), then always end the call.

        `_closing` blocks speaking/listening from cancelling us or starting a new wait.
        Hang-up is shielded and does not wait forever on TTS.
        """
        if self._closing:
            return
        self._closing = True
        log.info("no response after %d nudges — hanging up", self._nudges)

        async def _end() -> None:
            try:
                handle = self._session.generate_reply(
                    instructions=_GOODBYE, tool_choice="none", allow_interruptions=False)
                await asyncio.wait_for(handle.wait_for_playout(),
                                       timeout=_GOODBYE_PLAYOUT_TIMEOUT)
            except Exception as e:
                log.warning("silence goodbye failed/timed out: %s — hanging up anyway", e)
            try:
                # Stop the agent loop immediately (console + SIP); don't drain more speech.
                self._session.shutdown(drain=False)
            except Exception as e:
                log.warning("session shutdown failed: %s", e)
            try:
                await telephony.hang_up()
            except Exception as e:
                log.warning("silence hang-up failed: %s", e)

        # Even if this task is cancelled mid-await, finish hang-up.
        await asyncio.shield(_end())


def start_max_duration_guard(settings: Settings) -> asyncio.Task:
    """Billing safety: force-hang-up any call that runs past MAX_CALL_SECONDS."""

    async def _guard() -> None:
        try:
            await asyncio.sleep(settings.max_call_seconds)
            log.warning("max call duration %.0fs reached — hanging up", settings.max_call_seconds)
            await telephony.hang_up()
        except asyncio.CancelledError:
            pass

    return asyncio.create_task(_guard())
