from inbound_agent.call_state import CallState, Outcome, usable_phone
from inbound_agent.disposition import build_payload


def test_withheld_caller_id():
    assert usable_phone("anonymous") == ""
    assert usable_phone(None) == ""
    assert usable_phone("+61 412 345 678") == "61412345678"


def test_default_outcomes(english_campaign):
    state = CallState(campaign=english_campaign)
    assert state.final_outcome() == Outcome.DROPPED
    state.mark_person_spoke()
    assert state.final_outcome() == Outcome.NO_OUTCOME


def test_do_not_call_outranks_interest(english_campaign):
    state = CallState(campaign=english_campaign, interested=True, do_not_call=True)
    assert state.final_outcome() == Outcome.OPT_OUT


def test_whatsapp_instead_of_calls(english_campaign):
    state = CallState(campaign=english_campaign, do_not_call=True, whatsapp_number="61412345678")
    assert state.final_outcome() == Outcome.WHATSAPP_OPT_IN


def test_transfer_wins(english_campaign):
    state = CallState(campaign=english_campaign, transferred=True, do_not_call=True)
    assert state.final_outcome() == Outcome.TRANSFERRED


def test_payload_shape(english_campaign):
    state = CallState(campaign=english_campaign, caller="61412345678", did="442079460000",
                      call_id="abc", do_not_call=True)
    state.add_transcript("assistant", "Hi there")
    state.add_transcript("user", "   ")
    state.answers["move_date"] = "next Friday"
    state.add_note("no longer moving")
    state.add_note("call after 5")
    payload = build_payload(state, agent_name="inbound-bot")
    assert payload["answers"] == {"move_date": "next Friday"}
    assert payload["notes"] == "no longer moving; call after 5"
    assert payload["outcome"] == "OPT_OUT"
    assert payload["disposition_code"] == "DNC"
    assert payload["do_not_call"] is True
    assert payload["campaign"] == "uk"
    assert payload["direction"] == "inbound"
    assert len(payload["transcript"]) == 1
