"""The rules from client feedback that are enforced in code, not just in the script."""

from types import SimpleNamespace

import pytest

from inbound_agent import tools
from inbound_agent.call_state import CallState
from inbound_agent.campaigns import CampaignRegistry
from inbound_agent.leads import Lead
from inbound_agent.phones import check_phone
from inbound_agent.postcodes import FOUND, NOT_FOUND, UNAVAILABLE, PostcodeResult


@pytest.fixture
def cover(campaigns_dir):
    return CampaignRegistry.load(campaigns_dir).get("appliance_cover_uk")


def _ctx(campaign, caller="07400123456"):
    state = CallState(campaign=campaign, caller=caller,
                      caller_phone=check_phone(caller, campaign.country))
    return SimpleNamespace(userdata=state), state


def _says(state, *turns):
    for turn in turns:
        state.add_transcript("user", turn)


# ── WhatsApp only on a mobile ──

async def test_whatsapp_refused_on_landline_caller_id(cover):
    ctx, state = _ctx(cover, caller="02079460000")
    result = await tools.share_details(ctx, "whatsapp", "")
    assert result.startswith("NOT SAVED") and "landline" in result
    assert state.whatsapp_number == ""


async def test_whatsapp_to_mobile_caller_id(cover):
    ctx, state = _ctx(cover)
    result = await tools.share_details(ctx, "whatsapp", "")
    assert result.startswith("Saved") and state.whatsapp_number == "+447400123456"


async def test_whatsapp_to_a_mobile_given_by_landline_caller(cover):
    ctx, state = _ctx(cover, caller="02079460000")
    _says(state, "Yes, it's zero seven four double zero, one two three four five six.")
    await tools.share_details(ctx, "whatsapp", "07400 123456")
    assert state.whatsapp_number == "+447400123456"


async def test_whatsapp_refused_on_landline_given(cover):
    ctx, state = _ctx(cover)
    _says(state, "0161 496 0000")
    result = await tools.share_details(ctx, "whatsapp", "0161 496 0000")
    assert result.startswith("NOT SAVED") and state.whatsapp_number == ""


async def test_whatsapp_same_as_the_number_already_given(cover):
    ctx, state = _ctx(cover, caller="02079460000")
    _says(state, "Use my mobile, 07700 123456.")
    await tools.save_answer(ctx, "contact_number", "07700 123456")
    _says(state, "I already told you, the same number.")
    result = await tools.share_details(ctx, "whatsapp", "")
    assert state.whatsapp_number == "+447700123456" and "3456" in result


async def test_whatsapp_to_the_line_they_are_calling_from(cover):
    ctx, state = _ctx(cover)
    _says(state, "My landline is 0161 496 0000.")
    await tools.save_answer(ctx, "contact_number", "0161 496 0000")
    await tools.share_details(ctx, "whatsapp", "the one I'm calling from")
    assert state.whatsapp_number == "+447400123456"


async def test_whatsapp_same_number_but_it_is_a_landline(cover):
    ctx, state = _ctx(cover, caller="02079460000")
    await tools.save_answer(ctx, "contact_number", "calling number")
    result = await tools.share_details(ctx, "whatsapp", "same number")
    assert result.startswith("NOT SAVED") and "landline" in result


async def test_whatsapp_withheld_caller(cover):
    ctx, state = _ctx(cover, caller="")
    assert (await tools.share_details(ctx, "whatsapp", "")).startswith("NOT SAVED")


# ── Email only when there's no WhatsApp ──

async def test_email_blocked_once_whatsapp_agreed(cover):
    ctx, state = _ctx(cover)
    await tools.share_details(ctx, "whatsapp", "")
    result = await tools.share_details(ctx, "email", "john dot smith at gmail dot com")
    assert result.startswith("NOT SAVED") and state.email == ""


async def test_spoken_email_saved_and_read_back(cover):
    ctx, state = _ctx(cover, caller="02079460000")
    result = await tools.share_details(ctx, "email", "john dot smith at gmail dot com")
    assert state.email == "john.smith@gmail.com"
    assert "john dot smith at gmail dot com" in result


async def test_unclear_email_gives_up_after_two_tries(cover):
    ctx, state = _ctx(cover)
    for _ in range(tools.MAX_EMAIL_REJECTIONS):
        assert "spell it" in await tools.share_details(ctx, "email", "john at gmail")
    result = await tools.share_details(ctx, "email", "john at gmail")
    assert "Don't ask again" in result and "don't offer post" in result
    assert state.email == "" and "email unclear" in state.notes


# ── Full name ──

async def test_first_name_only_is_not_saved(cover):
    ctx, state = _ctx(cover)
    result = await tools.save_answer(ctx, "full_name", "Robert")
    assert result.startswith("NOT SAVED") and "surname" in result
    assert "full_name" not in state.answers
    await tools.save_answer(ctx, "full_name", "Robert Smith")
    assert state.answers["full_name"] == "Robert Smith"


async def test_name_taken_after_two_refusals(cover):
    ctx, state = _ctx(cover)
    for _ in range(tools.MAX_NAME_REJECTIONS):
        await tools.save_answer(ctx, "full_name", "Robert")
    await tools.save_answer(ctx, "full_name", "Robert")
    assert state.answers["full_name"] == "Robert (surname not given)"


# ── Contact number: type worked out, never asked ──

async def test_contact_number_type_detected(cover):
    ctx, state = _ctx(cover)
    _says(state, "No, it's 0161 496 0000.")
    result = await tools.save_answer(ctx, "contact_number", "0161 496 0000")
    assert state.answers["contact_number"] == "0161 496 0000"
    assert state.answers["number_type"] == "landline"
    assert "NOT a mobile" in result


@pytest.mark.parametrize("answer", ["calling number", "yes", "Yes, it is the best number",
                                    "that's fine", "yeah, this one"])
async def test_contact_number_yes_means_calling_number(cover, answer):
    ctx, state = _ctx(cover)
    result = await tools.save_answer(ctx, "contact_number", answer)
    assert state.answers["number_type"] == "mobile"
    assert state.answers["contact_number"] == "07400 123456"
    assert "read it back" not in result


async def test_contact_number_no_is_not_saved(cover):
    ctx, state = _ctx(cover)
    result = await tools.save_answer(ctx, "contact_number", "no, a different one")
    assert result.startswith("NOT SAVED") and "contact_number" not in state.answers


async def test_contact_number_incomplete(cover):
    ctx, state = _ctx(cover)
    _says(state, "zero seven four zero")
    result = await tools.save_answer(ctx, "contact_number", "0740")
    assert result.startswith("NOT SAVED") and "4 digits" in result


async def test_padded_number_is_refused(cover):
    """The 2026-11-10 console test: 9 digits said, the model saved 11 by adding '00'."""
    ctx, state = _ctx(cover)
    _says(state, "Yeah. So it's zero seven nine four six zero one two three.")
    result = await tools.save_answer(ctx, "contact_number", "07946 012300")
    assert result.startswith("NOT SAVED") and "never add" in result.lower()
    assert "9 digits: 0 7 9 4 6 0 1 2 3" in result
    assert "contact_number" not in state.answers


async def test_number_spread_over_two_turns(cover):
    ctx, state = _ctx(cover)
    _says(state, "It's zero seven four zero zero", "one two three, four five six")
    await tools.save_answer(ctx, "contact_number", "07400 123456")
    assert state.answers["contact_number"] == "07400 123456"


# ── Postcode, then address read-back ──

def _fake_lookup(monkeypatch, result):
    async def lookup(text, base_url):
        return result
    monkeypatch.setattr(tools, "lookup_postcode", lookup)


async def test_address_needs_postcode_check_first(cover):
    ctx, state = _ctx(cover)
    assert "check_postcode" in await tools.save_answer(ctx, "address", "10 Downing St")
    assert (await tools.save_address(ctx, "10 Downing Street")).startswith("NOT SAVED")


async def test_postcode_checked_then_street_saved(cover, monkeypatch):
    _fake_lookup(monkeypatch, PostcodeResult(FOUND, "SW1A 1AA", area="Westminster",
                                             region="London", country="England"))
    ctx, state = _ctx(cover)
    result = await tools.check_postcode(ctx, "s w one a one a a")
    assert "S W 1 A, 1 A A, in Westminster" in result
    assert state.postcode_verified

    await tools.save_address(ctx, "10 Downing Street")
    assert state.answers["address"] == "10 Downing Street, Westminster, London, SW1A 1AA"


async def test_channel_islands_accepted(cover, monkeypatch):
    _fake_lookup(monkeypatch, PostcodeResult(FOUND, "JE2 4WD", area="Channel Islands",
                                             country="Channel Islands"))
    ctx, state = _ctx(cover)
    assert "UK ONLY" not in await tools.check_postcode(ctx, "JE2 4WD")
    assert state.postcode == "JE2 4WD"


async def test_postcode_outside_uk(cover, monkeypatch):
    _fake_lookup(monkeypatch, PostcodeResult(FOUND, "XX1 1XX", country="Narnia"))
    ctx, state = _ctx(cover)
    assert "UK ONLY" in await tools.check_postcode(ctx, "XX1 1XX")
    assert state.postcode == ""


async def test_postcode_not_found_then_accepted_after_max_tries(cover, monkeypatch):
    _fake_lookup(monkeypatch, PostcodeResult(NOT_FOUND, "SW1A 9ZZ",
                                             outcode_area="Westminster"))
    ctx, state = _ctx(cover)
    for _ in range(tools.MAX_POSTCODE_CHECKS - 1):
        result = await tools.check_postcode(ctx, "SW1A 9ZZ")
        assert result.startswith("NOT FOUND") and "Westminster" in result
    result = await tools.check_postcode(ctx, "SW1A 9ZZ")
    assert "don't ask again" in result
    assert state.postcode == "SW1A 9ZZ" and not state.postcode_verified


async def test_postcode_checker_down_does_not_block(cover, monkeypatch):
    _fake_lookup(monkeypatch, PostcodeResult(UNAVAILABLE, "SW1A 1AA"))
    ctx, state = _ctx(cover)
    result = await tools.check_postcode(ctx, "SW1A 1AA")
    assert "don't ask again" in result and state.postcode == "SW1A 1AA"


def test_address_tools_only_for_uk_campaigns(cover, english_campaign):
    assert tools.check_postcode in tools.tools_for(cover)
    assert tools.check_postcode not in tools.tools_for(english_campaign)


# ── Address on file (we called them first) ──

DANIEL = Lead("Daniel Whitmore", "14 Hawthorn Grove", "Stretford, Manchester", "M32 0JG")


async def test_address_on_file_confirmed_without_postcode(cover):
    ctx, state = _ctx(cover)
    state.lead = DANIEL
    await tools.confirm_address(ctx)
    assert state.answers["address"] == "14 Hawthorn Grove, Stretford, Manchester, M32 0JG"
    assert state.postcode == "M32 0JG" and state.address_on_file
    assert not state.postcode_verified


async def test_confirm_address_needs_a_record(cover):
    ctx, state = _ctx(cover)
    assert (await tools.confirm_address(ctx)).startswith("NOT SAVED")
    assert "address" not in state.answers


async def test_moved_caller_still_uses_postcode_check(cover):
    ctx, state = _ctx(cover)
    state.lead = DANIEL
    assert "confirm_address" in await tools.save_answer(ctx, "address", "5 New Road")
    assert "address" not in state.answers


def test_confirm_address_tool_only_with_an_address_on_file(cover, english_campaign):
    assert tools.confirm_address in tools.tools_for(cover, DANIEL)
    assert tools.confirm_address not in tools.tools_for(cover)
    assert tools.confirm_address not in tools.tools_for(cover, Lead(full_name="A B"))
    assert tools.confirm_address not in tools.tools_for(english_campaign, DANIEL)
