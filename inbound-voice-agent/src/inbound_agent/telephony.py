"""Call control on the current job's room: hang up and SIP transfer."""

from __future__ import annotations

import logging

from livekit.agents import get_job_context

log = logging.getLogger(__name__)


async def hang_up() -> None:
    """End the call for real.

    On a SIP call, deleting the LiveKit room makes livekit-sip send BYE to FreeSWITCH.
    In console mode ``delete_room`` is a no-op, so we also shut the job down — otherwise
    silence hang-up appears to do nothing and the session runs until Ctrl+C.
    """
    jctx = get_job_context()
    try:
        await jctx.delete_room()
    except Exception as e:
        log.warning("delete_room failed: %s", e)
    try:
        # Ends the worker job (and console session). Safe if delete_room already tore
        # the room down — shutdown is idempotent from the framework's side.
        jctx.shutdown(reason="agent hang-up")
    except Exception as e:
        log.warning("job shutdown failed: %s", e)


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
