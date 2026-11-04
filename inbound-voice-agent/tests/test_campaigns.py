import textwrap

import pytest

from inbound_agent.campaigns import (
    FALLBACK_CAMPAIGN,
    Campaign,
    CampaignError,
    CampaignRegistry,
    VoiceConfig,
)


def _write(dir_, name, body):
    (dir_ / name).write_text(textwrap.dedent(body), encoding="utf-8")


def test_shipped_campaigns_load(campaigns_dir):
    registry = CampaignRegistry.load(campaigns_dir)
    assert registry.get("naina_default") is not None
    assert registry.get("roof_uk") is not None
    assert registry.resolve(did="+1 555 000 0000").key == "naina_default"


def test_appliance_cover_campaign(campaigns_dir):
    c = CampaignRegistry.load(campaigns_dir).get("appliance_cover_uk")
    assert c is not None and not c.has_placeholders()
    assert c.agent_name == "Stacy" and c.timezone == "Europe/London"
    assert [q.field for q in c.questions][:3] == ["full_name", "machine_working", "machine_make"]
    assert c.voice.stt_language == "en-GB"


def test_unknown_timezone_rejected():
    with pytest.raises(CampaignError, match="timezone"):
        Campaign.from_dict({"key": "k", "company": "c", "agent_name": "a", "purpose": "p",
                            "timezone": "Mars/Olympus"})


def test_voice_keyterms(campaigns_dir):
    cover = CampaignRegistry.load(campaigns_dir).get("appliance_cover_uk")
    assert "Hotpoint" in cover.voice.keyterms
    with pytest.raises(CampaignError, match="keyterms"):
        VoiceConfig.from_dict({"stack": "deepgram", "keyterms": "Hotpoint"})


def test_questions_validation():
    base = {"key": "k", "company": "c", "agent_name": "a", "purpose": "p"}
    c = Campaign.from_dict({**base, "questions": [{"field": "Move_Date", "ask": "When?"}]})
    assert c.questions[0].field == "move_date"
    with pytest.raises(CampaignError, match="duplicate"):
        Campaign.from_dict({**base, "questions": [{"field": "a", "ask": "x"},
                                                  {"field": "a", "ask": "y"}]})
    with pytest.raises(CampaignError, match="lowercase"):
        Campaign.from_dict({**base, "questions": [{"field": "move date", "ask": "x"}]})
    with pytest.raises(CampaignError, match="field"):
        Campaign.from_dict({**base, "questions": [{"ask": "x"}]})


def test_template_is_ignored(campaigns_dir):
    assert CampaignRegistry.load(campaigns_dir).get("my_campaign") is None


def test_did_lookup_handles_formats(english_campaign):
    registry = CampaignRegistry([english_campaign])
    assert registry.resolve(did="+442079460000").key == "uk"
    assert registry.resolve(did="442079460000").key == "uk"
    assert registry.resolve(did="02079460000").key == "uk"   # national format, suffix match


def test_hint_wins_over_did(english_campaign):
    other = Campaign(key="au", company="X", agent_name="Y", purpose="Z")
    registry = CampaignRegistry([english_campaign, other])
    assert registry.resolve(did="+442079460000", hint="au").key == "au"


def test_unknown_did_without_default_uses_fallback(english_campaign):
    registry = CampaignRegistry([english_campaign])
    assert registry.resolve(did="+61 2 9999 9999") is FALLBACK_CAMPAIGN


def test_duplicate_did_keeps_first(english_campaign):
    dup = Campaign(key="dup", company="X", agent_name="Y", purpose="Z",
                   dids=english_campaign.dids)
    registry = CampaignRegistry([english_campaign, dup])
    assert registry.resolve(did="442079460000").key == "uk"


def test_missing_required_field_raises():
    with pytest.raises(CampaignError, match="purpose"):
        Campaign.from_dict({"key": "k", "company": "c", "agent_name": "a"})


def test_bad_file_is_skipped(tmp_path):
    _write(tmp_path, "good.yaml", """
        key: good
        company: Good Co
        agent_name: Emma
        purpose: something
    """)
    _write(tmp_path, "broken.yaml", "key: [unclosed")
    _write(tmp_path, "incomplete.yaml", "key: only_key")
    registry = CampaignRegistry.load(tmp_path)
    assert len(registry) == 1
    assert registry.get("good") is not None


def test_voice_defaults_per_stack():
    sarvam = VoiceConfig.from_dict({"stack": "sarvam"})
    assert sarvam.bilingual and sarvam.stt_language == "unknown"
    deepgram = VoiceConfig.from_dict({"stack": "deepgram"})
    assert not deepgram.bilingual and deepgram.tts_voice.startswith("aura")
    with pytest.raises(CampaignError):
        VoiceConfig.from_dict({"stack": "openai"})


def test_placeholders_detected():
    c = Campaign(key="k", company="REPLACE Co", agent_name="a", purpose="p")
    assert c.has_placeholders()
