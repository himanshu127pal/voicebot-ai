"""
naina_agent.py — Pure LiveKit Agents voice bot (no Pipecat).

Runs as an agent-dispatch worker. LiveKit creates a room per inbound SIP call
and dispatches this agent (name "naina-bot", matching the SIP dispatch rule).

Run:
  ./.venv/bin/python naina_agent.py download-files   # fetch VAD model (once)
  ./.venv/bin/python naina_agent.py dev              # local dev
  ./.venv/bin/python naina_agent.py start            # production

Install:
  pip install "livekit-agents[deepgram,openai,silero]"

Env (.env in this dir):
  LIVEKIT_URL=ws://127.0.0.1:7880
  LIVEKIT_API_KEY=mykey
  LIVEKIT_API_SECRET=<secret>
  DEEPGRAM_API_KEY=...        # STT (+ TTS via Aura)
  DEEPSEEK_API_KEY=...        # LLM (OpenAI-compatible)
  BACKEND_URL=...             # dialer backend for disposition POST
"""

import os
import logging

from dotenv import load_dotenv

# Load .env into the environment. Runs at import time, so the per-job SUBPROCESS
# (which re-imports this module) also gets the keys — not just the parent worker.
load_dotenv()

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import deepgram, openai, silero

log = logging.getLogger("naina")


# ── Disposition mapping (inlined — was control/disposition_map.py) ─────────────
FLOW_OUTCOME_TO_CODE = {
    "BOOKED": "Sale",
    "CALLBACK": "Callback Requested",
    "NOT_INTERESTED": "Not Interested",
    "DNC": "DND",
    "OUT_OF_ZONE": "Not Interested",
    "DROPPED": "Follow-up",
    "IDLE_DROPPED": "Follow-up",
}


def map_outcome_to_code(outcome: str) -> str:
    return FLOW_OUTCOME_TO_CODE.get(outcome, "Follow-up")


class Naina(Agent):
    """The sales agent. Instructions + tools here; STT/LLM/TTS on the session."""

    def __init__(self, prospect: dict):
        name = prospect.get("name") or "there"
        super().__init__(
            instructions=(
                # TODO: paste your real sales script / persona here.
                f"You are Naina, a friendly sales agent. The prospect's name is {name}. "
                f"Speak naturally. Qualify the lead, pitch, and when the call reaches an "
                f"outcome, call set_disposition with one of: "
                f"BOOKED, CALLBACK, NOT_INTERESTED, DNC, OUT_OF_ZONE."
            )
        )
        self._prospect = prospect
        self.outcome: str = "DROPPED"   # default if call drops before an outcome

    @function_tool
    async def set_disposition(self, context: RunContext, outcome: str):
        """Record the final call outcome.
        outcome: one of BOOKED, CALLBACK, NOT_INTERESTED, DNC, OUT_OF_ZONE."""
        self.outcome = outcome
        log.info(f"[naina] disposition set → {outcome}")
        return "recorded"


async def entrypoint(ctx: JobContext):
    await ctx.connect()

    # Wait for the SIP caller; read dialer context (FreeSWITCH X-headers → attributes).
    participant = await ctx.wait_for_participant()
    attrs = participant.attributes
    prospect = {
        "name":     attrs.get("dialer.prospect_name"),
        "phone":    attrs.get("dialer.phone"),
        "campaign": attrs.get("dialer.campaign"),
        "call_id":  attrs.get("dialer.call_id"),
    }
    log.info(f"[naina] call room={ctx.room.name} prospect={prospect}")

    agent = Naina(prospect)

    session = AgentSession(
        # ── "for now" stack: Deepgram STT/TTS + DeepSeek LLM ──
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=openai.LLM.with_deepseek(model="deepseek-chat"),   # reads DEEPSEEK_API_KEY
        tts=deepgram.TTS(model="aura-2-thalia-en"),            # English; swap to sarvam.TTS for Indic
        vad=silero.VAD.load(),
        # turn_detection omitted → VAD handles turn-taking. (Add the new
        # livekit.agents.inference.TurnDetector later for smarter endpointing.)
    )

    async def _on_shutdown():
        code = map_outcome_to_code(agent.outcome)
        log.info(f"[naina] call ended → {agent.outcome} → {code} (call_id={prospect['call_id']})")
        # TODO: POST to dialer backend, e.g. requests.post(f"{os.environ['BACKEND_URL']}/api/call/dispose", ...)
    ctx.add_shutdown_callback(_on_shutdown)

    await session.start(room=ctx.room, agent=agent)

    await session.generate_reply(
        instructions="Greet the prospect by name and open the conversation."
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="naina-bot",   # MUST match agentName in the SIP dispatch rule
        )
    )
