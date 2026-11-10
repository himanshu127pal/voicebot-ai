"""UK postcodes: turn what the caller said into a clean postcode, then check it really
exists and find its area with postcodes.io (free, no API key, built on ONS / Royal Mail
open data). See docs/DATA_CHECKS.md.

The lookup fails soft: if postcodes.io is slow or down, the agent reads the postcode
back and carries on — a checker outage must never stall a call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

FOUND = "found"
NOT_FOUND = "not_found"        # well-formed, but no such postcode
BAD_FORMAT = "bad_format"      # doesn't look like a UK postcode at all
UNAVAILABLE = "unavailable"    # lookup disabled, timed out or errored

# Countries we accept for UK-only cover. The Channel Islands and the Isle of Man are not
# legally in the UK but are accepted for now (client decision, 2026-11-10).
UK_COUNTRIES = frozenset({"England", "Scotland", "Wales", "Northern Ireland",
                          "Channel Islands", "Isle of Man"})

# The letters at the start of every UK postcode, plus GY/JE/IM (Channel Islands, Isle of
# Man). Fixed by Royal Mail; rejects STT garbage before any lookup.
POSTCODE_AREAS = frozenset("""
    AB AL B BA BB BD BH BL BN BR BS BT CA CB CF CH CM CO CR CT CV CW DA DD DE DG DH DL DN
    DT DY E EC EH EN EX FK FY G GL GU HA HD HG HP HR HS HU HX IG IP IV KA KT KW KY L LA LD
    LE LL LN LS LU M ME MK ML N NE NG NN NP NR NW OL OX PA PE PH PL PO PR RG RH RM S SA SE
    SG SK SL SM SN SO SP SR SS ST SW SY TA TD TF TN TQ TR TS TW UB W WA WC WD WF WN WR WS
    WV YO ZE GY JE IM
""".split())

_LOOKUP_TIMEOUT_SECONDS = 3.0

# Outward code (area + district) then inward code (sector + unit): "SW1A" + "1AA".
_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}[0-9][A-Z0-9]?)([0-9][A-Z]{2})$")
_POSTCODE_IN_TEXT_RE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b")

_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_LETTER_NAMES = {
    "bee": "B", "see": "C", "sea": "C", "dee": "D", "ee": "E", "eff": "F", "gee": "G",
    "aitch": "H", "haitch": "H", "jay": "J", "kay": "K", "el": "L", "ell": "L", "em": "M",
    "en": "N", "pee": "P", "queue": "Q", "cue": "Q", "ar": "R", "are": "R", "ess": "S",
    "tee": "T", "tea": "T", "you": "U", "vee": "V", "ex": "X", "why": "Y", "zed": "Z",
    "zee": "Z",
}
_NATO = {
    "alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E",
    "foxtrot": "F", "golf": "G", "hotel": "H", "india": "I", "juliet": "J", "juliett": "J",
    "kilo": "K", "lima": "L", "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U",
    "victor": "V", "whiskey": "W", "whisky": "W", "xray": "X", "yankee": "Y", "zulu": "Z",
}
_FILLER = {"my", "postcode", "post", "code", "is", "its", "it's", "it", "that's", "thats",
           "the", "um", "uh", "er", "erm", "and", "space", "please", "yes", "yeah"}


def _spoken_tokens(text: str) -> list[str]:
    """'S for Sugar W one A, double A' → ['S', 'W', '1', 'A', 'A', 'A']."""
    words = re.sub(r"[^a-z0-9' ]", " ", text.lower().replace("x-ray", "xray")).split()
    out: list[str] = []
    repeat = 1
    i = 0
    while i < len(words):
        w = words[i]
        if w == "double" and i + 1 < len(words) and words[i + 1] == "you":
            out.append("W" * repeat)
            repeat, i = 1, i + 2
            continue
        if w in ("double", "triple"):
            repeat = 2 if w == "double" else 3
            i += 1
            continue
        # "S for Sugar" — the letter is the one before "for"; skip "for <word>". A short
        # or number word after it means "for" was the digit four.
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w == "for" and out and len(nxt) > 2 and nxt not in _UNITS and nxt not in _TEENS:
            i += 2
            continue
        if w == "for":
            w = "four"
        if w in _FILLER:
            i += 1
            continue
        if w in _TENS:
            value = _TENS[w]
            if i + 1 < len(words) and words[i + 1] in _UNITS and _UNITS[words[i + 1]]:
                value += _UNITS[words[i + 1]]
                i += 1
            token = str(value)
        elif w in _TEENS:
            token = str(_TEENS[w])
        elif w in _UNITS:
            token = str(_UNITS[w])
        elif w in _NATO:
            token = _NATO[w]
        elif w in _LETTER_NAMES:
            token = _LETTER_NAMES[w]
        else:
            token = w.upper()
        out.append(token * repeat)
        repeat = 1
        i += 1
    return out


def _well_formed(s: str) -> re.Match | None:
    m = _POSTCODE_RE.match(s)
    if m and re.match(r"[A-Z]+", m.group(1)).group() in POSTCODE_AREAS:
        return m
    return None


def _fix_lookalikes(s: str) -> str:
    """O/0 and I/1 sound and look alike ("C R O" is CR0, Croydon). The inward code is
    always digit-letter-letter; in the outward code, try each reading and keep the one
    whose area letters are a real postcode area."""
    if not 5 <= len(s) <= 7:
        return s
    to_letter = str.maketrans("01", "OI")
    to_digit = str.maketrans("OI", "01")
    outward, inward = s[:-3], s[-3:]
    inward = inward[0].translate(to_digit) + inward[1:].translate(to_letter)
    readings = [""]
    for i, ch in enumerate(outward):
        options = {ch, ch.translate(to_letter if i == 0 else to_digit)}
        if i > 0:
            options.add(ch.translate(to_letter))
        readings = [r + o for r in readings for o in sorted(options, key=lambda o: o != ch)]
    for reading in readings:
        if _well_formed(reading + inward):
            return reading + inward
    return outward + inward


def normalise_uk_postcode(text: str | None) -> str:
    """What the caller said → 'SW1A 1AA', or '' if it can't be a UK postcode."""
    raw = (text or "").strip()
    if not raw:
        return ""
    for m in _POSTCODE_IN_TEXT_RE.finditer(raw.upper()):
        if _well_formed(m.group(1) + m.group(2)):
            return f"{m.group(1)} {m.group(2)}"
    m = _well_formed(_fix_lookalikes("".join(_spoken_tokens(raw))))
    return f"{m.group(1)} {m.group(2)}" if m else ""


def spoken_postcode(postcode: str) -> str:
    """'SW1A 1AA' → 'S W 1 A, 1 A A' — one character at a time, a pause between halves,
    which is how the TTS reads a postcode most clearly (qa/tts-2026-11-09)."""
    return ", ".join(" ".join(half) for half in (postcode or "").split())


@dataclass(frozen=True)
class PostcodeResult:
    status: str
    postcode: str = ""        # "SW1A 1AA" — set whenever the text was a well-formed postcode
    area: str = ""            # "Westminster" (local authority)
    region: str = ""          # "London" (England only; empty elsewhere)
    country: str = ""         # "England", "Scotland", "Channel Islands" …
    outcode_area: str = ""    # NOT_FOUND only: area(s) of the first half, e.g. "Westminster"

    @property
    def in_uk(self) -> bool:
        return self.country in UK_COUNTRIES

    @property
    def place(self) -> str:
        """'Westminster, London' / 'Glasgow City, Scotland'."""
        parts = [p for p in (self.area, self.region or self.country) if p]
        return ", ".join(dict.fromkeys(parts))


_cache: dict[str, PostcodeResult] = {}


async def _get_json(session: aiohttp.ClientSession, url: str) -> tuple[int, dict]:
    async with session.get(url) as r:
        try:
            body = await r.json(content_type=None)
        except ValueError:
            body = {}
        return r.status, body if isinstance(body, dict) else {}


async def lookup_postcode(text: str, base_url: str) -> PostcodeResult:
    postcode = normalise_uk_postcode(text)
    if not postcode:
        return PostcodeResult(BAD_FORMAT)
    if not base_url:
        return PostcodeResult(UNAVAILABLE, postcode)
    if postcode in _cache:
        return _cache[postcode]

    base = base_url.rstrip("/")
    outward, inward = postcode.split()
    timeout = aiohttp.ClientTimeout(total=_LOOKUP_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            status, body = await _get_json(session, f"{base}/postcodes/{outward}{inward}")
            if status == 200 and isinstance(body.get("result"), dict):
                res = body["result"]
                result = PostcodeResult(FOUND, res.get("postcode") or postcode,
                                        area=res.get("admin_district") or "",
                                        region=res.get("region") or "",
                                        country=res.get("country") or "")
            elif status == 404:
                status, body = await _get_json(session, f"{base}/outcodes/{outward}")
                areas = []
                if status == 200 and isinstance(body.get("result"), dict):
                    areas = body["result"].get("admin_district") or []
                result = PostcodeResult(NOT_FOUND, postcode,
                                        outcode_area=" or ".join(a for a in areas[:2] if a))
            else:
                log.warning("postcode lookup %s -> HTTP %s", postcode, status)
                return PostcodeResult(UNAVAILABLE, postcode)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("postcode lookup %s failed: %s", postcode, e)
        return PostcodeResult(UNAVAILABLE, postcode)

    _cache[postcode] = result
    return result
