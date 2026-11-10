"""Per-call state. One instance per call, shared by the tools (via session userdata),
the call guards and the disposition reporter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .campaigns import Campaign, digits_only
from .phones import PhoneInfo
from .redaction import REDACTION_NOTE, redact_numbers


class Outcome:
    TRANSFERRED = "TRANSFERRED"
    INTERESTED = "INTERESTED"
    CALLBACK = "CALLBACK"
    WHATSAPP_OPT_IN = "WHATSAPP_OPT_IN"
    EMAIL_OPT_IN = "EMAIL_OPT_IN"
    OPT_OUT = "OPT_OUT"
    NOT_INTERESTED = "NOT_INTERESTED"
    WRONG_NUMBER = "WRONG_NUMBER"
    INFO_ONLY = "INFO_ONLY"
    NO_OUTCOME = "NO_OUTCOME"   # a person spoke but no outcome was reached
    DROPPED = "DROPPED"         # nobody spoke

    # Outcomes the LLM may set directly via set_disposition.
    MANUAL = (NOT_INTERESTED, WRONG_NUMBER, INFO_ONLY)


def usable_phone(raw: str | None) -> str:
    """Caller ID may be withheld ('anonymous', 'unknown', empty)."""
    d = digits_only(raw)
    return d if len(d) >= 6 else ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CallState:
    campaign: Campaign
    caller: str = ""
    caller_phone: PhoneInfo = field(default_factory=PhoneInfo)   # mobile / landline / …
    # The best number they confirmed (caller ID or one they gave) — "same number" later.
    contact_phone: PhoneInfo = field(default_factory=PhoneInfo)
    did: str = ""
    call_id: str = ""
    room: str = ""
    recording: str = ""
    started_at: datetime = field(default_factory=_now)

    outcome: str = Outcome.DROPPED
    interested: bool = False
    do_not_call: bool = False
    whatsapp_number: str = ""
    email: str = ""
    callback_when: str = ""
    transferred: bool = False
    notes: str = ""
    answers: dict[str, str] = field(default_factory=dict)   # campaign question field → answer

    # Validation progress — how many times a detail was rejected, so the agent stops
    # pushing after a couple of tries instead of annoying the caller.
    name_rejections: int = 0
    email_rejections: int = 0
    postcode_checks: int = 0
    postcode: str = ""             # "SW1A 1AA"
    postcode_area: str = ""        # "Westminster, London" (from postcodes.io)
    postcode_verified: bool = False

    # What the LLM has said so far in its current reply (set by InboundAgent.llm_node).
    reply_text: str = ""

    transcript: list[dict] = field(default_factory=list)

    def add_transcript(self, role: str, text: str) -> None:
        text = redact_numbers((text or "").replace(REDACTION_NOTE, "")).strip()
        if text:
            self.transcript.append({"role": role, "text": text, "at": _now().isoformat()})

    def recent_caller_turns(self, count: int = 4) -> list[str]:
        """What the caller said in their last few turns — a number is often spread over
        two ('zero seven four…' / '…one two three')."""
        return [t["text"] for t in self.transcript if t["role"] == "user"][-count:]

    def add_note(self, note: str) -> None:
        note = (note or "").strip()
        if note:
            self.notes = f"{self.notes}; {note}" if self.notes else note

    def mark_person_spoke(self) -> None:
        if self.outcome == Outcome.DROPPED:
            self.outcome = Outcome.NO_OUTCOME

    def final_outcome(self) -> str:
        """One headline outcome for the dialer; the individual flags are reported too.
        A do-not-call request outranks 'interested' — compliance first."""
        if self.transferred:
            return Outcome.TRANSFERRED
        if self.do_not_call:
            if self.whatsapp_number:
                return Outcome.WHATSAPP_OPT_IN
            if self.email:
                return Outcome.EMAIL_OPT_IN
            return Outcome.OPT_OUT
        if self.interested:
            return Outcome.INTERESTED
        if self.callback_when:
            return Outcome.CALLBACK
        if self.whatsapp_number:
            return Outcome.WHATSAPP_OPT_IN
        if self.email:
            return Outcome.EMAIL_OPT_IN
        return self.outcome
