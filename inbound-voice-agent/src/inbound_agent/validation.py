"""Checks on what the caller said before it is saved: full name and email address.

The model will happily save "Robert" as a full name, or hand back a plausible email it
invented one character of. These checks run in code so the rule holds on every call.
"""

from __future__ import annotations

import re

_TITLES = {"mr", "mrs", "ms", "miss", "mx", "dr", "sir", "madam", "prof", "lady", "lord"}
_NAME_LEAD_IN = re.compile(
    r"^\s*(?:my\s+name\s+is|my\s+name's|my\s+full\s+name\s+is|the\s+name\s+is|name\s+is|"
    r"it's|it\s+is|its|this\s+is|i'm|i\s+am|im|call\s+me)\s+",
    re.IGNORECASE,
)
_NAME_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")


def name_words(text: str | None) -> list[str]:
    """'My name is Mr Robert O'Neil' → ['Robert', "O'Neil"] — titles and lead-ins dropped."""
    cleaned = _NAME_LEAD_IN.sub("", (text or "").strip())
    return [w for w in _NAME_WORD.findall(cleaned) if w.lower().rstrip(".") not in _TITLES]


def is_full_name(text: str | None) -> bool:
    """A first name AND a surname. A lone initial ('Robert J') doesn't count."""
    return sum(1 for w in name_words(text) if len(w) >= 2) >= 2


# ── Spoken email addresses ── (same rules as agent_finance_au.py, tuned for the UK)
_SPOKEN_SUBS = (
    # Longest first — " at " would otherwise eat " at the rate ".
    (" at the rate of ", "@"), (" at the rate ", "@"), (" at sign ", "@"),
    (" at symbol ", "@"), (" at ", "@"),
    (" full stop ", "."), (" dot ", "."), (" point ", "."), (" period ", "."),
    (" under score ", "_"), (" underscore ", "_"),
    (" hyphen ", "-"), (" dash ", "-"), (" minus ", "-"),
    (" plus ", "+"),
)
# STT splits well-known providers; safe because no real address contains a space.
_DOMAIN_FIXES = (
    ("g mail", "gmail"), ("gee mail", "gmail"), ("hot mail", "hotmail"),
    ("out look", "outlook"), ("i cloud", "icloud"), ("yahoo mail", "yahoo"),
    ("bt internet", "btinternet"), ("virgin media", "virginmedia"), ("talk talk", "talktalk"),
)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@"
                       r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def normalise_email(raw: str | None) -> str:
    """A validated email address, or '' if what they said doesn't resolve to one.
    '' rather than a best guess: an almost-right address silently goes nowhere."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    s = " " + s.replace(",", " ").replace("  ", " ") + " "
    for spoken, symbol in _SPOKEN_SUBS:
        s = s.replace(spoken, symbol)
    for wrong, right in _DOMAIN_FIXES:
        s = s.replace(wrong, right)
    # A space left inside the domain means the utterance wasn't understood
    # ("john at at gmail dot com" would otherwise become john@atgmail.com).
    if s.count("@") == 1 and any(c.isspace() for c in s.split("@")[1].strip()):
        return ""
    s = "".join(s.split()).strip(".")
    s = re.sub(r"\.{2,}", ".", s)
    if s.count("@") != 1 or len(s) > 160:
        return ""
    local, _, domain = s.partition("@")
    if not local or "." not in domain:
        return ""
    return s if _EMAIL_RE.match(s) else ""


def spell_out_email(addr: str) -> str:
    """'j.smith@gmail.com' → 'j dot smith at gmail dot com', for the read-back."""
    return (addr.replace(".", " dot ").replace("@", " at ").replace("_", " underscore ")
            .replace("-", " dash ").replace("  ", " ").strip())
