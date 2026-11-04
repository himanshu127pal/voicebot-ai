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


class SilenceNudger:
    """After the agent finishes speaking, wait `nudge_after_seconds` for the caller.
    1st silence → "can you hear me?"; later ones → offer WhatsApp/callback;
    after `max_nudges` unanswered → hang up the dead line. Caller speech resets it."""

    def __init__(self, session: AgentSession, state: CallState, settings: Settings):
        self._session = session
        self._state = state
        self._after = settings.nudge_after_seconds
        self._max = settings.max_nudges
        self._nudges = 0
        self._task: asyncio.Task | None = None

    def attach(self) -> None:
        self._session.on("user_state_changed", self._on_user_state)
        self._session.on("conversation_item_added", self._on_item)
        self._session.on("agent_state_changed", self._on_agent_state)

    def _on_user_state(self, ev) -> None:
        if str(getattr(ev, "new_state", "")) == "speaking":
            self._caller_active()

    def _on_item(self, ev) -> None:
        # A finished caller turn also counts; typed turns (console --text) never
        # produce a "speaking" state.
        item = getattr(ev, "item", None)
        if item is not None and str(getattr(item, "role", "")) == "user":
            self._caller_active()

    def _caller_active(self) -> None:
        self._state.mark_person_spoke()
        self._nudges = 0
        self._cancel()

    def _on_agent_state(self, ev) -> None:
        state = str(getattr(ev, "new_state", ""))
        if state == "speaking":
            self._cancel()
        elif state == "listening":
            # Only in true `listening`, not while STT is still transcribing.
            self._cancel()
            self._task = asyncio.create_task(self._wait_then_nudge())

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _wait_then_nudge(self) -> None:
        try:
            await asyncio.sleep(self._after)
            if self._nudges >= self._max:
                log.info("no response after %d nudges — hanging up", self._nudges)
                handle = self._session.generate_reply(instructions=_GOODBYE, tool_choice="none",
                                                      allow_interruptions=False)
                await handle.wait_for_playout()
                await telephony.hang_up()
                return
            self._nudges += 1
            instructions = _PRESENCE_CHECK if self._nudges == 1 else _RE_ENGAGE
            self._session.generate_reply(instructions=instructions)
        except asyncio.CancelledError:
            pass


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
