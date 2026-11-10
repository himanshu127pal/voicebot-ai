import pytest

from inbound_agent.phones import (
    INVALID,
    LANDLINE,
    MOBILE,
    MOBILE_OR_LANDLINE,
    OTHER,
    check_phone,
    gives_digits,
    heard_digits,
    matches_what_was_said,
    means_calling_number,
)


@pytest.mark.parametrize("raw", ["07400123456", "+447400123456", "447400123456",
                                 "0044 7400 123456", "+44 (0)7400 123456"])
def test_uk_mobile_in_every_carrier_format(raw):
    p = check_phone(raw, "GB")
    assert p.kind == MOBILE and p.can_whatsapp
    assert p.e164 == "+447400123456" and p.region == "GB"
    assert p.describe() == "a UK mobile"


@pytest.mark.parametrize("raw", ["02079460000", "+441614960000", "441314960000"])
def test_uk_landline_blocks_whatsapp(raw):
    p = check_phone(raw, "GB")
    assert p.kind == LANDLINE and not p.can_whatsapp
    assert p.describe() == "a UK landline"


@pytest.mark.parametrize("raw", ["03001234567", "08001234567", "05612345678", "07012345678"])
def test_uk_non_geographic_numbers_are_not_mobiles(raw):
    p = check_phone(raw, "GB")
    assert p.kind == OTHER and not p.can_whatsapp


def test_other_countries():
    assert check_phone("+61412345678", "GB").describe() == "an Australian mobile"
    assert check_phone("0412345678", "AU").kind == MOBILE
    us = check_phone("+12015550123", "GB")
    assert us.kind == MOBILE_OR_LANDLINE and us.can_whatsapp


@pytest.mark.parametrize("raw", ["", "anonymous", "12345", "07400 12"])
def test_withheld_or_incomplete_is_invalid(raw):
    p = check_phone(raw, "GB")
    assert p.kind == INVALID and not p.valid and not p.can_whatsapp


def test_national_number_needs_a_country():
    assert check_phone("07400123456").kind == INVALID
    assert check_phone("447400123456").kind == MOBILE   # international without '+'


def test_spoken_read_back_is_digit_by_digit():
    assert check_phone("07400123456", "GB").spoken == "0 7 4 0 0, 1 2 3 4 5 6"
    assert check_phone("02079460000", "GB").last4 == "0000"


def test_heard_digits():
    assert heard_digits("zero seven four double zero, 1 2 3") == "07400123"
    assert heard_digits("oh seven triple nine") == "07999"
    assert not gives_digits("yeah, this one") and gives_digits("zero seven four")


def test_matches_what_was_said():
    mobile = check_phone("07400123456", "GB")
    assert matches_what_was_said(mobile, ["it's zero seven four zero zero", "123456"])
    assert matches_what_was_said(mobile, ["44 7400 123456"])   # with the country code
    padded = check_phone("07946012300", "GB")
    assert not matches_what_was_said(padded, ["zero seven nine four six zero one two three"])
    assert not matches_what_was_said(mobile, [])


@pytest.mark.parametrize("answer,same", [
    ("calling number", True), ("yes, this one", True), ("the number I'm calling from", True),
    ("07400 123456", False), ("no, use my work phone", False),
])
def test_means_calling_number(answer, same):
    assert means_calling_number(answer) is same
