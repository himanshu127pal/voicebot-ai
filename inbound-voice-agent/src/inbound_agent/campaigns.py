"""Campaign scripts: one YAML file per campaign in config/campaigns/.

A call is matched to a campaign by the DID the customer dialled. The registry is
re-loaded per call (the files are tiny), so script edits go live without a restart.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

log = logging.getLogger(__name__)

STACK_SARVAM = "sarvam"      # Hindi/English, auto-detect (the Naina stack)
STACK_DEEPGRAM = "deepgram"  # AU/UK/US English
_STACKS = (STACK_SARVAM, STACK_DEEPGRAM)

_REQUIRED = ("key", "company", "agent_name", "purpose")
_PLACEHOLDER = "REPLACE"
# Carriers deliver the same DID as +44 20…, 4420… or 020…; matching on the trailing
# digits covers all three without per-carrier rules.
_DID_SUFFIX_LEN = 9
_MIN_DID_DIGITS = 6
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class CampaignError(ValueError):
    """A campaign file is invalid."""


def digits_only(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


@dataclass(frozen=True)
class VoiceConfig:
    stack: str = STACK_SARVAM
    stt_language: str = "unknown"
    tts_language: str = "hi-IN"
    tts_voice: str = ""
    speaker: str = ""
    keyterms: tuple[str, ...] = ()   # deepgram: words STT must not mishear (names, brands)

    @property
    def bilingual(self) -> bool:
        return self.stack == STACK_SARVAM

    @classmethod
    def from_dict(cls, data: dict | None) -> "VoiceConfig":
        data = data or {}
        stack = str(data.get("stack", STACK_SARVAM)).lower()
        if stack not in _STACKS:
            raise CampaignError(f"voice.stack must be one of {_STACKS}, got {stack!r}")
        if stack == STACK_SARVAM:
            return cls(
                stack=stack,
                stt_language="unknown",
                tts_language=str(data.get("tts_language", "hi-IN")),
                speaker=str(data.get("speaker", "")),
            )
        keyterms = data.get("keyterms") or []
        if not isinstance(keyterms, list):
            raise CampaignError("voice.keyterms must be a list of words/phrases")
        return cls(
            stack=stack,
            stt_language=str(data.get("stt_language", "en-US")),
            tts_voice=str(data.get("tts_voice", "aura-2-thalia-en")),
            keyterms=tuple(str(k).strip() for k in keyterms if str(k).strip()),
        )


@dataclass(frozen=True)
class FaqItem:
    question: str
    answer: str


@dataclass(frozen=True)
class Question:
    """A detail to collect from an interested caller; the answer is saved under `field`."""
    field: str
    ask: str


def _parse_questions(raw) -> tuple[Question, ...]:
    questions: list[Question] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("field") or not item.get("ask"):
            raise CampaignError("each entry under `questions` needs `field` and `ask`")
        name = str(item["field"]).strip().lower()
        if not _FIELD_RE.match(name):
            raise CampaignError(f"question field {name!r} must be lowercase letters, digits "
                                f"and underscores")
        if name in seen:
            raise CampaignError(f"duplicate question field {name!r}")
        seen.add(name)
        questions.append(Question(name, " ".join(str(item["ask"]).split())))
    return tuple(questions)


def _parse_timezone(raw) -> str:
    name = str(raw or "").strip()
    if name:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise CampaignError(f"unknown timezone {name!r}") from e
    return name


@dataclass(frozen=True)
class Campaign:
    key: str
    company: str
    agent_name: str
    purpose: str
    benefit: str = ""
    greeting: str = ""
    facts: tuple[str, ...] = ()
    faq: tuple[FaqItem, ...] = ()
    questions: tuple[Question, ...] = ()
    extra_instructions: str = ""
    dids: tuple[str, ...] = ()
    is_default: bool = False
    whatsapp_enabled: bool = True
    email_enabled: bool = True
    human_transfer_uri: str = ""
    timezone: str = ""   # IANA name, e.g. Europe/London — drives {time_greeting}
    voice: VoiceConfig = field(default_factory=VoiceConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "Campaign":
        if not isinstance(data, dict):
            raise CampaignError("file must contain a mapping at the top level")
        missing = [k for k in _REQUIRED if not str(data.get(k) or "").strip()]
        if missing:
            raise CampaignError(f"missing required field(s): {', '.join(missing)}")

        channels = data.get("channels") or {}
        faq = tuple(
            FaqItem(str(item["q"]).strip(), str(item["a"]).strip())
            for item in (data.get("faq") or [])
            if isinstance(item, dict) and item.get("q") and item.get("a")
        )
        return cls(
            key=str(data["key"]).strip(),
            company=str(data["company"]).strip(),
            agent_name=str(data["agent_name"]).strip(),
            purpose=str(data["purpose"]).strip(),
            benefit=str(data.get("benefit") or "").strip(),
            greeting=" ".join(str(data.get("greeting") or "").split()),
            facts=tuple(str(f).strip() for f in (data.get("facts") or []) if str(f).strip()),
            faq=faq,
            questions=_parse_questions(data.get("questions")),
            extra_instructions=str(data.get("extra_instructions") or "").strip(),
            dids=tuple(str(d) for d in (data.get("dids") or [])),
            is_default=bool(data.get("default", False)),
            whatsapp_enabled=bool(channels.get("whatsapp", True)),
            email_enabled=bool(channels.get("email", True)),
            human_transfer_uri=str(data.get("human_transfer_uri") or "").strip(),
            timezone=_parse_timezone(data.get("timezone")),
            voice=VoiceConfig.from_dict(data.get("voice")),
        )

    def has_placeholders(self) -> bool:
        spoken = [self.company, self.agent_name, self.purpose, self.benefit, self.greeting,
                  *self.facts, *(f.answer for f in self.faq), *(q.ask for q in self.questions)]
        return any(_PLACEHOLDER in s for s in spoken)


FALLBACK_CAMPAIGN = Campaign(
    key="_fallback",
    company="our team",
    agent_name="Naina",
    purpose="one of our services",
)


class CampaignRegistry:
    def __init__(self, campaigns: list[Campaign]):
        self._by_key: dict[str, Campaign] = {}
        self._by_did: dict[str, Campaign] = {}
        self._default: Campaign | None = None

        for c in campaigns:
            if c.key in self._by_key:
                log.error("duplicate campaign key %r — keeping the first one", c.key)
                continue
            self._by_key[c.key] = c
            for did in c.dids:
                d = digits_only(did)
                if len(d) < _MIN_DID_DIGITS:
                    log.warning("campaign %r: ignoring DID %r (not a phone number)", c.key, did)
                    continue
                if d in self._by_did:
                    log.error("DID %s is listed in both %r and %r — keeping %r",
                              did, self._by_did[d].key, c.key, self._by_did[d].key)
                    continue
                self._by_did[d] = c
            if c.is_default:
                if self._default is None:
                    self._default = c
                else:
                    log.error("more than one default campaign (%r, %r) — using %r",
                              self._default.key, c.key, self._default.key)

    @classmethod
    def load(cls, directory: Path) -> "CampaignRegistry":
        """Load every *.yaml/*.yml in `directory`; files starting with '_' are templates.
        A broken file is logged and skipped so one typo can't take every campaign down."""
        campaigns: list[Campaign] = []
        if not directory.is_dir():
            log.error("campaigns directory %s does not exist", directory)
            return cls(campaigns)
        for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
            if path.name.startswith("_"):
                continue
            try:
                with path.open(encoding="utf-8") as f:
                    campaign = Campaign.from_dict(yaml.safe_load(f))
            except (OSError, yaml.YAMLError, CampaignError, KeyError, TypeError) as e:
                log.error("skipping campaign file %s: %s", path.name, e)
                continue
            if campaign.has_placeholders():
                log.warning("campaign %r (%s) still contains %s placeholders — they will "
                            "be spoken aloud", campaign.key, path.name, _PLACEHOLDER)
            campaigns.append(campaign)
        return cls(campaigns)

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, key: str) -> Campaign | None:
        return self._by_key.get(key)

    def resolve(self, did: str = "", hint: str = "") -> Campaign:
        """Explicit campaign hint → exact DID → DID suffix → default → built-in fallback."""
        if hint and hint in self._by_key:
            return self._by_key[hint]

        d = digits_only(did)
        if d:
            if d in self._by_did:
                return self._by_did[d]
            if len(d) >= _DID_SUFFIX_LEN:
                tail = d[-_DID_SUFFIX_LEN:]
                matches = {c.key: c for k, c in self._by_did.items() if k.endswith(tail)}
                if len(matches) == 1:
                    return next(iter(matches.values()))

        if self._default is not None:
            if d:
                log.warning("DID %r matches no campaign — using default %r", did,
                            self._default.key)
            return self._default
        log.error("no campaign for DID %r and no default campaign — using fallback", did)
        return FALLBACK_CAMPAIGN
