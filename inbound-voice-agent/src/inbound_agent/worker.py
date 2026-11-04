"""LiveKit worker: one `entrypoint` run per inbound call, each in its own process."""

from __future__ import annotations

import logging

from livekit.agents import AgentSession, JobContext, JobProcess, WorkerOptions

from .agent import InboundAgent
from .call_guards import SilenceNudger, start_max_duration_guard
from .call_state import CallState, usable_phone
from .campaigns import CampaignRegistry
from .disposition import DispositionReporter
from .pipeline import build_llm, build_stt, build_tts, build_turn_handling, load_vad
from .prompts import build_greeting
from .settings import get_settings

log = logging.getLogger(__name__)

# SIP participant attributes. inbound.* are mapped from the X-headers the FreeSWITCH
# dialplan sets (deploy/livekit/trunk-inbound.json); sip.* are set by LiveKit itself
# and serve as the fallback.
ATTR_DID = ("inbound.did", "sip.trunkPhoneNumber")
ATTR_CALLER = ("inbound.caller", "sip.phoneNumber")
ATTR_CALL_ID = ("inbound.call_id", "sip.callID")
ATTR_CAMPAIGN = ("inbound.campaign",)
ATTR_RECORDING = ("inbound.recording",)


def _first(attrs: dict, keys: tuple[str, ...]) -> str:
    return next((attrs[k] for k in keys if attrs.get(k)), "")


def _build_call_state(ctx: JobContext, attrs: dict) -> CallState:
    settings = get_settings()
    did = _first(attrs, ATTR_DID)
    hint = _first(attrs, ATTR_CAMPAIGN)
    if ctx.is_fake_job() and not hint:
        hint = settings.console_campaign
    registry = CampaignRegistry.load(settings.campaigns_dir)
    campaign = registry.resolve(did=did, hint=hint)
    return CallState(
        campaign=campaign,
        caller=usable_phone(_first(attrs, ATTR_CALLER)),
        did=did,
        call_id=_first(attrs, ATTR_CALL_ID),
        room=ctx.room.name,
        recording=_first(attrs, ATTR_RECORDING),
    )


async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()
    await ctx.connect()
    participant = await ctx.wait_for_participant()

    # Console mode hands us a mock participant whose attributes are not a real dict.
    attrs = participant.attributes if isinstance(participant.attributes, dict) else {}
    state = _build_call_state(ctx, attrs)
    log.info("call room=%s did=%r caller=…%s campaign=%s stack=%s llm=%s", state.room,
             state.did, state.caller[-4:], state.campaign.key, state.campaign.voice.stack,
             settings.llm_provider)

    campaign = state.campaign
    session = AgentSession(
        userdata=state,
        stt=build_stt(campaign.voice, keyterms=(campaign.company, campaign.agent_name)),
        llm=build_llm(settings),
        tts=build_tts(campaign.voice),
        vad=ctx.proc.userdata["vad"],
        turn_handling=build_turn_handling(settings),
    )

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        if item is not None:
            state.add_transcript(str(getattr(item, "role", "")),
                                 getattr(item, "text_content", "") or "")

    reporter = DispositionReporter(state, settings)
    ctx.add_shutdown_callback(reporter.wait)
    ctx.room.on("participant_disconnected", lambda _p: reporter.start())

    await session.start(room=ctx.room, agent=InboundAgent(state))

    start_max_duration_guard(settings)
    SilenceNudger(session, state, settings).attach()

    session.say(build_greeting(state.campaign), allow_interruptions=True)


def prewarm(proc: JobProcess) -> None:
    """Runs once per worker process before it takes a call, so calls start warm."""
    settings = get_settings()
    proc.userdata["vad"] = load_vad(settings)

    registry = CampaignRegistry.load(settings.campaigns_dir)
    log.info("loaded %d campaign(s) from %s", len(registry), settings.campaigns_dir)
    if not settings.webhook_url:
        log.warning("DISPOSITION_WEBHOOK_URL unset — opt-outs and WhatsApp opt-ins will "
                    "only be logged, not acted on")


def build_worker_options() -> WorkerOptions:
    settings = get_settings()
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        agent_name=settings.agent_name,   # must match agent_name in the dispatch rule
        num_idle_processes=settings.num_idle_processes,
        host=settings.worker_host,
        port=settings.worker_port,
    )
