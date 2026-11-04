from pathlib import Path

import pytest

from inbound_agent.campaigns import Campaign, VoiceConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def english_campaign() -> Campaign:
    return Campaign(
        key="uk",
        company="Acme Homes",
        agent_name="Emma",
        purpose="a free roof check",
        benefit="It's free.",
        dids=("+44 20 7946 0000",),
        voice=VoiceConfig.from_dict({"stack": "deepgram", "stt_language": "en-GB",
                                     "tts_voice": "aura-2-pandora-en"}),
    )


@pytest.fixture
def campaigns_dir() -> Path:
    return PROJECT_ROOT / "config" / "campaigns"
