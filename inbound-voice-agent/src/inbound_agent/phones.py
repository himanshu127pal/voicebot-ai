"""Phone-number checks: is it a real number, which country, and is it a mobile or a landline.

Built on Google's libphonenumber rules (the `phonenumbers` package): offline, no API key,
and the numbering plans are refreshed with every package release. WhatsApp only works on
mobiles, so this decides whether a WhatsApp opt-in can be taken on a number.
See docs/DATA_CHECKS.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

from .campaigns import digits_only

MOBILE = "mobile"
LANDLINE = "landline"
# US/Canada (and a few others) give mobiles and landlines the same number ranges, so the
# number alone can't tell them apart. Only a paid carrier lookup can.
MOBILE_OR_LANDLINE = "mobile_or_landline"
OTHER = "other"        # freephone, premium rate, VoIP, personal numbers, pagers
INVALID = "invalid"    # withheld, too short, or not a real number anywhere

_KINDS = {
    PhoneNumberType.MOBILE: MOBILE,
    PhoneNumberType.FIXED_LINE: LANDLINE,
    PhoneNumberType.FIXED_LINE_OR_MOBILE: MOBILE_OR_LANDLINE,
}

_COUNTRY_ADJECTIVE = {
    "GB": "UK", "AU": "Australian", "US": "US", "CA": "Canadian", "IE": "Irish",
    "NZ": "New Zealand", "IN": "Indian", "IM": "Isle of Man", "JE": "Jersey",
    "GG": "Guernsey",
}

_KIND_WORDS = {
    MOBILE: "mobile",
    LANDLINE: "landline",
    MOBILE_OR_LANDLINE: "number (it could be a mobile or a landline)",
    OTHER: "business or non-mobile number",
}

_MIN_DIGITS = 6


@dataclass(frozen=True)
class PhoneInfo:
    raw: str = ""
    kind: str = INVALID
    e164: str = ""        # "+447400123456"
    national: str = ""    # "07400 123456"
    region: str = ""      # ISO country code, "GB"

    @property
    def valid(self) -> bool:
        return self.kind != INVALID

    @property
    def can_whatsapp(self) -> bool:
        return self.kind in (MOBILE, MOBILE_OR_LANDLINE)

    @property
    def last4(self) -> str:
        return digits_only(self.e164 or self.raw)[-4:]

    @property
    def spoken(self) -> str:
        """'07400 123456' -> '0 7 4 0 0, 1 2 3 4 5 6': digit by digit, a pause between
        groups. The number filter then voices each digit as a word."""
        groups = (self.national or digits_only(self.raw)).split()
        return ", ".join(" ".join(g for g in group if g.isdigit()) for group in groups)

    def describe(self) -> str:
        """'a UK mobile', 'an Australian landline', 'an overseas (FR) mobile' …"""
        if not self.valid:
            return "not a valid phone number"
        country = _COUNTRY_ADJECTIVE.get(self.region, f"overseas ({self.region})")
        words = f"{country} {_KIND_WORDS[self.kind]}"
        vowel_sound = words[0] in "AEIOUaeiou" and not words.startswith(("UK", "US"))
        return ("an " if vowel_sound else "a ") + words


def check_phone(raw: str | None, default_region: str = "") -> PhoneInfo:
    """Parse a number as it arrives from a carrier or a caller: '+44 7400 123456',
    '447400123456', '07400123456', '0044 7400 123456'. National numbers ('07…') need
    `default_region` (the campaign country, e.g. 'GB')."""
    text = (raw or "").strip()
    digits = digits_only(text)
    if len(digits) < _MIN_DIGITS:
        return PhoneInfo(raw=text)

    if text.startswith("+"):
        candidates = ["+" + digits]
    elif digits.startswith("00"):
        candidates = ["+" + digits[2:]]
    else:
        candidates = [digits]
        if not digits.startswith("0"):
            # Carriers often drop the '+': 447400123456 is +44 7400 123456.
            candidates.append("+" + digits)

    for candidate in candidates:
        try:
            number = phonenumbers.parse(candidate, default_region or None)
        except NumberParseException:
            continue
        if not phonenumbers.is_valid_number(number):
            continue
        return PhoneInfo(
            raw=text,
            kind=_KINDS.get(phonenumbers.number_type(number), OTHER),
            e164=phonenumbers.format_number(number, PhoneNumberFormat.E164),
            national=phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL),
            region=phonenumbers.region_code_for_number(number) or "",
        )
    return PhoneInfo(raw=text)


# The line they're calling on.
_CALLER_LINE_HINTS = (
    "calling number", "calling from", "this number", "this one", "this phone",
    "number you called", "number you rang", "you just called", "current number",
    "the one i'm on", "the one im on", "on this line", "number i'm on", "number im on",
)
# Agreement, or "the number I already gave you".
_SAME_NUMBER_HINTS = _CALLER_LINE_HINTS + (
    "same", "already", "told you", "gave you", "that number", "that one", "best number",
    "it is the best", "it's the best", "that's fine", "that's right", "correct", "right",
    "yes", "yeah", "yep", "sure",
)


def means_calling_number(answer: str) -> bool:
    """'yes', 'calling number', 'it's the best number', 'I already told you' — no digits,
    just agreement: they're happy with the number already on file."""
    low = (answer or "").strip().lower()
    if digits_only(low) or re.search(r"^no\b|\bnot\b|\bdifferent\b|\banother\b", low):
        return False
    return any(h in low for h in _SAME_NUMBER_HINTS)


def means_caller_line(answer: str) -> bool:
    """'the one I'm calling from' — specifically the caller ID, not a number given earlier."""
    low = (answer or "").strip().lower()
    return any(h in low for h in _CALLER_LINE_HINTS)


_SPOKEN_DIGITS = {"zero": "0", "oh": "0", "o": "0", "nought": "0", "one": "1", "two": "2",
                  "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                  "eight": "8", "nine": "9"}


def heard_digits(text: str | None) -> str:
    """Every digit the caller said, in order: 'zero seven double four, 1 2' → '074412'."""
    out: list[str] = []
    repeat = 1
    for tok in re.findall(r"[a-z]+|\d", (text or "").lower()):
        if tok in ("double", "triple"):
            repeat = 2 if tok == "double" else 3
            continue
        digit = tok if tok.isdigit() else _SPOKEN_DIGITS.get(tok)
        if digit is not None:
            out.append(digit * repeat)
        repeat = 1
    return "".join(out)


def gives_digits(text: str | None) -> bool:
    """A few digits at least — 'this one' or 'the one I'm on' is not a number."""
    return len(heard_digits(text)) >= 3


def matches_what_was_said(phone: PhoneInfo, caller_turns: list[str]) -> bool:
    """True if the caller actually said this number's digits. Stops the model 'completing'
    a number: '0 7 9 4 6 0 1 2 3' (9 digits) saved as '07946 012300'.
    Compares the number without its country code or leading 0, so '07400…' and
    '44 7400…' both match."""
    heard = heard_digits(" ".join(caller_turns))
    national = phonenumbers.parse(phone.e164).national_number if phone.e164 else ""
    return bool(national) and str(national) in heard
