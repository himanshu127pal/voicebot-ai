"""
agent_worker.py — LiveKit agent-dispatch worker that hosts the Pipecat bot.

Run once:  python agent_worker.py dev      (or `start` in prod)
It registers as agent "naina-bot" and idles. Each inbound SIP call that hits the
dispatch rule triggers entrypoint() ONCE, for that call's room.

Env required (same key/secret as livekit.yaml):
  LIVEKIT_URL=ws://127.0.0.1:7880
  LIVEKIT_API_KEY=mykey
  LIVEKIT_API_SECRET=<secret>
"""

import os
from livekit import agents, api
from livekit.agents import JobContext, WorkerOptions, cli


def _make_token(room_name: str, identity: str = "naina-bot") -> str:
    """Mint a room-scoped JWT for Pipecat's transport to join with."""
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )


async def entrypoint(ctx: JobContext):
    """Called ONCE per dispatched call. ctx describes this call's room."""
    room_name = ctx.room.name                       # <-- ROOM NAME, given by LiveKit
    print(f"[worker] job for room: {room_name}")

    # Wait for the SIP caller to appear, then read the dialer context that
    # FreeSWITCH sent as X-headers (mapped to attributes in trunk.json).
    participant = await ctx.wait_for_participant()
    attrs = participant.attributes
    call_ctx = {
        "call_id":   attrs.get("dialer.call_id"),
        "prospect":  attrs.get("dialer.prospect_name"),
        "campaign":  attrs.get("dialer.campaign"),
        "phone":     attrs.get("dialer.phone"),
    }
    print(f"[worker] dialer context: {call_ctx}")

    # ── Build + run the Pipecat pipeline on LiveKit transport ──────────────────
    # NOTE: we let Pipecat's LiveKitTransport be the bot's presence in the room,
    # so we do NOT call ctx.connect() here. (Verify against the livekit-sip-agent
    # example for your versions — if it expects ctx.connect(), reuse ctx.room
    # instead of minting a separate token.)
    from pipecat.transports.livekit.transport import LiveKitTransport, LiveKitParams
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    transport = LiveKitTransport(
        url=os.environ["LIVEKIT_URL"],
        token=_make_token(room_name),
        room_name=room_name,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    # TODO: hand `transport` + `call_ctx` to your existing pipeline.
    #   from pipeline.main import bot
    #   await bot(transport=transport, prospect=call_ctx)
    # On hangup, keep your existing disposition flow:
    #   from control.disposition_map import map_outcome_to_code
    #   backend_client.post_dispose(code=..., call_id=call_ctx["call_id"])
    #
    # entrypoint() should stay awaited until the call ends; when it returns,
    # the job completes and the worker goes back to idle for the next call.


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="naina-bot",   # explicit dispatch — matches dispatch.json
        )
    )
