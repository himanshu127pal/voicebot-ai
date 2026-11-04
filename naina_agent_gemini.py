"""
naina_agent_gemini.py — Gemini Live (voice-to-voice) with EXTERNAL turn detection.

Gemini's built-in server-side VAD endpoints telephony audio unreliably (either
stalls ~20s or fires early with overlapping/truncated replies). So we DISABLE it
(automatic_activity_detection.disabled=True) and let LiveKit's Silero VAD +
audio TurnDetector decide turn boundaries — the part we can control.

Run:
  ./.venv/bin/python naina_agent_gemini.py download-files   # VAD + turn-detector models
  ./.venv/bin/python naina_agent_gemini.py start

Install:
  pip install "livekit-agents[google,silero]" python-dotenv

Env (.env):
  LIVEKIT_URL=ws://127.0.0.1:7880
  LIVEKIT_API_KEY=mykey
  LIVEKIT_API_SECRET=<secret>
  GOOGLE_API_KEY=...
  BACKEND_URL=...
"""

import os
import logging

from dotenv import load_dotenv
load_dotenv()

from google.genai import types
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    function_tool,
    inference,
)
from livekit.plugins import google, silero

log = logging.getLogger("naina")


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
    def __init__(self, prospect: dict):
        name = prospect.get("name") or "there"
        super().__init__(
            instructions=(
                # TODO: paste your real sales script / persona here.
                f"You are Naina, a friendly sales agent. Speak in the caller's language "
                f"(Hindi/English). The prospect's name is {name}. Qualify, pitch, and when "
                f"the call reaches an outcome call set_disposition with one of: "
                f"BOOKED, CALLBACK, NOT_INTERESTED, DNC, OUT_OF_ZONE."
            )
        )
        self._prospect = prospect
        self.outcome: str = "DROPPED"

    @function_tool
    async def set_disposition(self, context: RunContext, outcome: str):
        """Record the final call outcome.
        outcome: one of BOOKED, CALLBACK, NOT_INTERESTED, DNC, OUT_OF_ZONE."""
        self.outcome = outcome
        log.info(f"[naina] disposition set → {outcome}")
        return "recorded"


async def entrypoint(ctx: JobContext):
    await ctx.connect()

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
        # ── EXTERNAL turn detection: LiveKit owns endpointing, not Gemini ──
        turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector()),
        vad=silero.VAD.load(),                     # feeds the turn detector
        llm=google.beta.realtime.RealtimeModel(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            voice="Puck",
            temperature=0.8,
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True,                 # turn OFF Gemini's server-side VAD
                ),
            ),
        ),
    )

    async def _on_shutdown():
        code = map_outcome_to_code(agent.outcome)
        log.info(f"[naina] call ended → {agent.outcome} → {code} (call_id={prospect['call_id']})")
        # TODO: POST disposition to BACKEND_URL with code + call_id.
    ctx.add_shutdown_callback(_on_shutdown)

    await session.start(room=ctx.room, agent=agent)

    # Proactive greeting. generate_reply works for the FIRST turn (before any user
    # speech). If it misbehaves with external VAD (livekit/agents#5408), switch to:
    #     await session.say("Hello, main Naina baat kar rahi hoon...")
    await session.generate_reply(
        instructions="Greet the prospect by name and open the conversation."
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="naina-bot",
            num_idle_processes=1,
        )
    )
