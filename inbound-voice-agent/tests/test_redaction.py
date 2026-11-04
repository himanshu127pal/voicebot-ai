from inbound_agent.redaction import has_sensitive_number, redact_numbers


def test_card_number_written():
    text = "my card is 4532 1234 5678 9012 expiry soon"
    assert has_sensitive_number(text)
    assert redact_numbers(text) == "my card is [number removed] expiry soon"


def test_card_number_spoken():
    spoken = "it's four five three two one two three four five six seven eight"
    assert has_sensitive_number(spoken)
    assert "four five" not in redact_numbers(spoken)


def test_phone_numbers_are_not_redacted():
    assert not has_sensitive_number("call me on +61 412 345 678")
    assert not has_sensitive_number("zero four one two three four five six seven eight")


def test_transcript_never_stores_card_numbers(english_campaign):
    from inbound_agent.call_state import CallState
    from inbound_agent.redaction import REDACTION_NOTE

    state = CallState(campaign=english_campaign)
    state.add_transcript("user", "card 4532 1234 5678 9012")
    state.add_transcript("user", "card [number removed]" + REDACTION_NOTE)
    assert [t["text"] for t in state.transcript] == ["card [number removed]"] * 2


def test_ordinary_speech_untouched():
    text = "a two bedroom unit, moving on the 15th, one piano"
    assert redact_numbers(text) == text
