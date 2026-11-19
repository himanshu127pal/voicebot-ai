"""Guards that keep a call from sitting in dead air or running forever."""

from __future__ import annotations

import asyncio
import logging

from livekit.agents import AgentSession

from . import telephony
from .call_state import CallState
from .settings import Settings

log = logging.getLogger(__name__)

_PRESENCE_CHECK = (
    "The caller has gone quiet. In the SAME language you were speaking, gently check they "
    "can still hear you in ONE short line."
)
_RE_ENGAGE = (
    "The caller is still quiet. In the SAME language, ONE short new line: offer to send the "
    "details on WhatsApp or to call back later. Never repeat a line you've already used."
)
_GOODBYE = (
    "The line has stayed silent. In the SAME language, ONE short polite line: you'll let "
    "them go, they're welcome to call back any time, goodbye. Do not ask a question."
)

# Don't let a stuck TTS/LLM block hang-up (recording UID-1795024111 looped for minutes).
_GOODBYE_PLAYOUT_TIMEOUT = 8.0


class SilenceNudger:
    """After the agent finishes speaking, wait `nudge_after_seconds` for the caller.
    1st silence → "can you hear me?"; later ones → offer WhatsApp/callback;
    after `max_nudges` unanswered → hang up the dead line.

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
            instructions = _PRESENCE_CHECK if self._nudges == 1 else _RE_ENGAGE
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
