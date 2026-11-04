"""Keeps card and bank-account numbers away from the LLM.

12+ consecutive digits, written ("4532 1234 …") or spoken ("four five three two …"), is
longer than any phone number and as long as the shortest card. Deliberately blunt: a
false positive only costs "please don't read that out"; a false negative sends a card
number to a third-party LLM.
"""

from __future__ import annotations

import re

SENSITIVE_DIGITS = 12

_DIGIT_WORDS = {"zero", "oh", "one", "two", "three", "four", "five", "six", "seven",
                "eight", "nine"}
_TOKEN_RE = re.compile(r"[A-Za-z]+|\d")

REDACTION_NOTE = (
    " [NOTE FOR YOU, NOT SAID BY THE CALLER: they began reading out a card or account "
    "number, which has been removed. Do not ask for it and do not refer to any digits. "
    "Say, kindly: 'Please don't read that out — we never take card or bank details over "
    "the phone.' Then carry on.]"
)


def sensitive_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = end = None
    count = 0
    for m in _TOKEN_RE.finditer(text or ""):
        tok = m.group().lower()
        if tok.isdigit() or tok in _DIGIT_WORDS:
            if start is None:
                start, count = m.start(), 0
            count += 1
            end = m.end()
        else:
            if start is not None and count >= SENSITIVE_DIGITS:
                spans.append((start, end))
            start, count = None, 0
    if start is not None and count >= SENSITIVE_DIGITS:
        spans.append((start, end))
    return spans


def has_sensitive_number(text: str) -> bool:
    return bool(sensitive_spans(text))


def redact_numbers(text: str) -> str:
    out = text or ""
    for s, e in reversed(sensitive_spans(out)):
        out = out[:s] + "[number removed]" + out[e:]
    return out
