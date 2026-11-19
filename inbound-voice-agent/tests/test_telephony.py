"""hang_up must end console jobs too (delete_room is a no-op there)."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

livekit = sys.modules.setdefault("livekit", types.ModuleType("livekit"))
agents = sys.modules.get("livekit.agents")
if agents is None or not hasattr(agents, "get_job_context"):
    agents = types.ModuleType("livekit.agents")
    sys.modules["livekit.agents"] = agents
    livekit.agents = agents


@pytest.mark.asyncio
async def test_hang_up_deletes_room_and_shuts_job(monkeypatch):
    # Import after stub is in place; reload if an older telephony was cached.
    import inbound_agent.telephony as telephony

    jctx = MagicMock()
    jctx.delete_room = AsyncMock()
    monkeypatch.setattr(telephony, "get_job_context", lambda: jctx)

    await telephony.hang_up()

    jctx.delete_room.assert_awaited_once()
    jctx.shutdown.assert_called_once_with(reason="agent hang-up")


@pytest.mark.asyncio
async def test_hang_up_still_shuts_job_if_delete_room_fails(monkeypatch):
    import inbound_agent.telephony as telephony

    jctx = MagicMock()
    jctx.delete_room = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(telephony, "get_job_context", lambda: jctx)

    await telephony.hang_up()

    jctx.shutdown.assert_called_once_with(reason="agent hang-up")
