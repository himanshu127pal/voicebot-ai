from livekit.agents import AgentSession

from inbound_agent.campaigns import VoiceConfig
from inbound_agent.pipeline import build_stt, build_turn_handling
from inbound_agent.settings import get_settings


async def test_turn_handling_is_accepted_by_agent_session():
    session = AgentSession(turn_handling=build_turn_handling(get_settings()))
    opts = session.options
    assert opts.interruption["min_words"] == 1
    assert opts.interruption["resume_false_interruption"] is True
    assert opts.preemptive_generation["enabled"] is True
    assert opts.endpointing["max_delay"] >= opts.endpointing["min_delay"]


def test_deepgram_stt_gets_keyterms(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test")
    voice = VoiceConfig(stack="deepgram", stt_language="en-GB", keyterms=("Hotpoint",))
    stt = build_stt(voice, keyterms=("Home Appliance Cover", "Stacy", "Hotpoint"))
    assert stt._opts.keyterm == ["Home Appliance Cover", "Stacy", "Hotpoint"]
    assert stt._opts.smart_format is True
