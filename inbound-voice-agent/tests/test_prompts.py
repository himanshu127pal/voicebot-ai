from dataclasses import replace
from datetime import datetime, timezone

from inbound_agent.campaigns import Campaign, FaqItem, Question, VoiceConfig
from inbound_agent.prompts import build_greeting, build_instructions


def test_default_greeting_states_who_and_why(english_campaign):
    greeting = build_greeting(english_campaign)
    assert "Acme Homes" in greeting
    assert "Emma" in greeting
    assert "a free roof check" in greeting


def test_custom_greeting_used_verbatim(english_campaign):
    c = replace(english_campaign, greeting="Namaste, Acme se Naina bol rahi hoon.")
    assert build_greeting(c) == "Namaste, Acme se Naina bol rahi hoon."


def test_english_campaign_is_english_only(english_campaign):
    prompt = build_instructions(english_campaign, "61412345678")
    assert "speak English only" in prompt
    assert "Devanagari" not in prompt
    assert "ending in 5678" in prompt


def test_bilingual_campaign_has_language_sandwich():
    c = Campaign(key="hi", company="Acme", agent_name="Naina", purpose="p",
                 voice=VoiceConfig.from_dict({"stack": "sarvam"}))
    prompt = build_instructions(c)
    assert prompt.count("Devanagari") >= 1
    final_block = prompt.split("MOST IMPORTANT")[1]
    assert "CURRENT language" in final_block
    assert "number is withheld" in prompt


def test_final_block_is_last(english_campaign):
    c = replace(english_campaign, extra_instructions="Never discuss pricing.",
                faq=(FaqItem("Is this spam?", "No."),))
    prompt = build_instructions(c)
    assert prompt.index("Never discuss pricing.") < prompt.index("MOST IMPORTANT")
    assert prompt.index("Is this spam?") < prompt.index("MOST IMPORTANT")


def test_time_greeting_uses_campaign_timezone(english_campaign):
    c = replace(english_campaign, timezone="Europe/London",
                greeting="{time_greeting}, this is Stacy.")
    utc = timezone.utc
    assert build_greeting(c, datetime(2026, 1, 5, 9, 0, tzinfo=utc)) == "Good morning, this is Stacy."
    assert build_greeting(c, datetime(2026, 1, 5, 14, 0, tzinfo=utc)).startswith("Good afternoon")
    assert build_greeting(c, datetime(2026, 1, 5, 19, 0, tzinfo=utc)).startswith("Good evening")
    # British Summer Time: 11:30 UTC is already afternoon in London
    assert build_greeting(c, datetime(2026, 7, 1, 11, 30, tzinfo=utc)).startswith("Good afternoon")
    assert build_greeting(replace(c, timezone="")).startswith("Hello,")


def test_questions_block(english_campaign):
    c = replace(english_campaign, questions=(Question("move_date", "When are you moving?"),))
    prompt = build_instructions(c)
    assert "## QUESTIONS" in prompt
    assert "\"When are you moving?\" → move_date" in prompt
    assert "save_answer(field, answer)" in prompt
    assert prompt.index("## QUESTIONS") < prompt.index("MOST IMPORTANT")
    assert "## QUESTIONS" not in build_instructions(english_campaign)


def test_conversation_style_block(english_campaign):
    prompt = build_instructions(english_campaign)
    assert "## HOW YOU TALK" in prompt
    assert "never guess" in prompt
    assert prompt.index("## HOW YOU TALK") < prompt.index("## HOW TO HANDLE THE CALL")


def test_no_transfer_target_means_callbacks(english_campaign):
    prompt = build_instructions(english_campaign, can_transfer=False)
    assert "NOT AVAILABLE" in prompt
    assert "or, if they want to talk now, transfer_to_human" not in prompt
    assert "NOT AVAILABLE" not in build_instructions(english_campaign)


def test_compliance_rules_present(english_campaign):
    prompt = build_instructions(english_campaign)
    assert "NEVER TAKE PAYMENT, CARD OR BANK DETAILS" in prompt
    assert "Never claim to be human" in prompt


def test_channels_disabled_skips_whatsapp_offer(english_campaign):
    c = replace(english_campaign, whatsapp_enabled=False, email_enabled=False)
    handling = build_instructions(c).split("## HOW TO HANDLE THE CALL:")[1].split("## TOOLS:")[0]
    assert "WhatsApp" not in handling
