"""Call control on the current job's room: hang up and SIP transfer.

Uses the JobContext helpers, which are no-ops in console mode, so the same code runs
in local testing and on real calls."""

from __future__ import annotations

import logging

from livekit.agents import get_job_context

log = logging.getLogger(__name__)


async def hang_up() -> None:
    """Delete the room: livekit-sip sends BYE to FreeSWITCH and the call ends."""
    await get_job_context().delete_room()


async def transfer_caller(transfer_to: str) -> None:
    """Cold transfer via SIP REFER. Requires REFER to be allowed on the FreeSWITCH side.
    Raises on failure so the caller of this function can fall back to a callback."""
    jctx = get_job_context()
    if jctx.is_fake_job():
        log.info("console mode — transfer to %s simulated", transfer_to)
        return
    sip_participant = next(
        (p for p in jctx.room.remote_participants.values() if p.identity.startswith("sip_")),
        None,
    )
    if sip_participant is None:
        raise RuntimeError("no SIP participant in the room to transfer")
    await jctx.transfer_sip_participant(sip_participant, transfer_to, play_dialtone=True)
