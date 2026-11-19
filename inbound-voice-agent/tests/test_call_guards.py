"""SilenceNudger hang-up must survive the agent entering `speaking` for the goodbye.

livekit.agents is mocked so this file runs on machines where the native DLL is blocked.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# call_guards / telephony import livekit.agents at module load. On some Windows
# machines the real package's native DLL is blocked — stub enough to unit-test.
livekit = sys.modules.setdefault("livekit", types.ModuleType("livekit"))
agents = sys.modules.get("livekit.agents")
if agents is None or not hasattr(agents, "get_job_context"):
    agents = types.ModuleType("livekit.agents")
    sys.modules["livekit.agents"] = agents
    livekit.agents = agents
agents.AgentSession = getattr(agents, "AgentSession", object)
agents.get_job_context = getattr(agents, "get_job_context", lambda: None)

from inbound_agent.call_guards import SilenceNudger  # noqa: E402
from inbound_agent.call_state import CallState  # noqa: E402


def _settings(nudge_after: float = 0.01, max_nudges: int = 3):
    return types.SimpleNamespace(nudge_after_seconds=nudge_after, max_nudges=max_nudges)


@pytest.mark.asyncio
async def test_goodbye_speaking_does_not_cancel_hang_up(english_campaign, monkeypatch):
    hang_up = AsyncMock()
    monkeypatch.setattr("inbound_agent.call_guards.telephony.hang_up", hang_up)

    playout = asyncio.Event()

    class Handle:
        async def wait_for_playout(self):
            # Simulate LiveKit: goodbye starts → agent_state becomes speaking mid-await.
            nudger._on_agent_state(types.SimpleNamespace(new_state="speaking"))
            await playout.wait()

    session = MagicMock()
    session.generate_reply.return_value = Handle()
    state = CallState(campaign=english_campaign)
    nudger = SilenceNudger(session, state, _settings(max_nudges=0))
    # max_nudges=0 → first silence timeout goes straight to hang-up.

    task = asyncio.create_task(nudger._wait_then_nudge())
    await asyncio.sleep(0.05)   # past nudge_after
    assert nudger._closing is True
    assert hang_up.await_count == 0   # still playing goodbye

    # Listening again must not start a new wait while closing.
    nudger._on_agent_state(types.SimpleNamespace(new_state="listening"))
    assert nudger._task is None or nudger._task is task

    playout.set()
    await task
    hang_up.assert_awaited_once()
    assert nudger._closing is True


@pytest.mark.asyncio
async def test_caller_speech_ignored_during_closing(english_campaign, monkeypatch):
    hang_up = AsyncMock()
    monkeypatch.setattr("inbound_agent.call_guards.telephony.hang_up", hang_up)

    class Handle:
        async def wait_for_playout(self):
            nudger._caller_active()   # noise / cough during goodbye
            assert nudger._nudges == 3   # not reset

    session = MagicMock()
    session.generate_reply.return_value = Handle()
    state = CallState(campaign=english_campaign)
    nudger = SilenceNudger(session, state, _settings())
    nudger._nudges = 3
    await nudger._goodbye_and_hang_up()
    hang_up.assert_awaited_once()


def test_vad_speaking_alone_does_not_reset_nudges(english_campaign):
    """TV / breathing trip VAD 'speaking' without a transcript — must not count."""
    session = MagicMock()
    state = CallState(campaign=english_campaign)
    nudger = SilenceNudger(session, state, _settings())
    nudger.attach()
    nudger._nudges = 2

    # Old bug path: user_state_changed → speaking. We no longer listen to it.
    assert not any(
        c.args and c.args[0] == "user_state_changed"
        for c in session.on.call_args_list
    )
    # Even if something called the old handler name, it must not exist as a reset path.
    assert not hasattr(nudger, "_on_user_state")
    assert nudger._nudges == 2
    assert state.outcome == "DROPPED"


def test_empty_user_item_ignored_real_words_reset(english_campaign):
    session = MagicMock()
    state = CallState(campaign=english_campaign)
    nudger = SilenceNudger(session, state, _settings())
    nudger._nudges = 2

    nudger._on_item(types.SimpleNamespace(
        item=types.SimpleNamespace(role="user", text_content="   ")))
    assert nudger._nudges == 2
    assert state.outcome == "DROPPED"

    nudger._on_item(types.SimpleNamespace(
        item=types.SimpleNamespace(role="user", text_content="yes, go on")))
    assert nudger._nudges == 0
    assert state.outcome == "NO_OUTCOME"
