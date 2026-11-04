"""Numbers → spoken English words, applied to the LLM's output just before TTS.

The LLM writes numbers as digits ("499"); this filter turns them into English words so
every voice speaks them correctly in any sentence language. It runs downstream of
generation, so it adds no turn latency.
"""

from __future__ import annotations

import logging
import re
from typing import AsyncIterable, AsyncIterator

log = logging.getLogger(__name__)

try:
    from num2words import num2words as _num2words
except ImportError:
    _num2words = None
    log.warning("num2words not installed — numbers will be spoken as raw digits")

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MONEY_RE = re.compile(r"([£$€])\s?(\d[\d,]*)(?:\.(\d{2}))?(?!\d)")
# Held back between stream chunks: a number that may continue ("4" + "99", "99." + "99")
# or a currency symbol whose amount hasn't arrived yet ("£" + "99.99").
_TRAILING_NUMBER_RE = re.compile(r"[£$€]?\s?[\d,\.]*\d\.?$|[£$€]\s?$")

# symbol → (singular, plural, minor unit)
_CURRENCIES = {
    "£": ("pound", "pounds", "pence"),
    "$": ("dollar", "dollars", "cents"),
    "€": ("euro", "euros", "cents"),
}

# Longer runs are identifiers (phone numbers, references), not quantities.
_MAX_CARDINAL_DIGITS = 6


def number_to_words(num_str: str) -> str:
    """'499' -> 'four hundred and ninety-nine'. Long runs and leading zeros are read
    digit by digit ('0412…' -> 'zero four one two …')."""
    if _num2words is None:
        return num_str
    clean = num_str.replace(",", "")
    try:
        if "." in clean:
            int_part, frac = clean.split(".", 1)
            left = number_to_words(int_part) if int_part else "zero"
            return left + " point " + " ".join(_num2words(int(d)) for d in frac)
        if len(clean) > _MAX_CARDINAL_DIGITS or (len(clean) > 1 and clean[0] == "0"):
            return " ".join(_num2words(int(d)) for d in clean)
        return _num2words(int(clean))
    except (ValueError, OverflowError):
        return num_str


def money_to_words(symbol: str, whole: str, minor: str | None) -> str:
    """('£', '99', '99') -> 'ninety-nine pounds ninety-nine' — how a person says a price."""
    one, many, minor_unit = _CURRENCIES[symbol]
    amount = int(whole.replace(",", ""))
    cents = int(minor) if minor else 0
    if amount == 0 and cents:
        return f"{number_to_words(str(cents))} {minor_unit}"
    words = f"{number_to_words(str(amount))} {one if amount == 1 else many}"
    return f"{words} {number_to_words(str(cents))}" if cents else words


def replace_numbers(text: str) -> str:
    text = _MONEY_RE.sub(lambda m: " " + money_to_words(*m.groups()) + " ", text)
    # Padding detaches a number glued to Devanagari text so the voice pauses naturally.
    return _NUMBER_RE.sub(lambda m: " " + number_to_words(m.group()) + " ", text)


async def convert_numbers_in_stream(text_stream: AsyncIterable[str]) -> AsyncIterator[str]:
    """Streaming version of `replace_numbers`. A trailing digit run is held back until
    the next chunk, so a number split across chunks ("4" + "99") converts as one."""
    buf = ""
    async for chunk in text_stream:
        buf += chunk.translate(_DEVANAGARI_DIGITS)
        m = _TRAILING_NUMBER_RE.search(buf)
        if m and m.start() > 0:
            emit, buf = buf[:m.start()], buf[m.start():]
        elif m:
            emit = ""
        else:
            emit, buf = buf, ""
        if emit:
            yield replace_numbers(emit)
    if buf:
        yield replace_numbers(buf)
