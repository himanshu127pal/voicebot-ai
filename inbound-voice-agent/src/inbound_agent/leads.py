"""Who is calling back: the customer record from the outbound list, found by caller ID.

We rang this number first, so we usually already hold the customer's name and address —
the agent confirms them instead of making the caller spell everything out. For now the
records come from a YAML file (LEADS_FILE); config/test_leads.yaml holds fake customers
for console tests. No file, no match or a withheld number → None, and the agent asks for
the details as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from .phones import PhoneInfo, check_phone
from .postcodes import normalise_uk_postcode

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Lead:
    full_name: str = ""
    first_line: str = ""   # "14 Hawthorn Grove"
    town: str = ""         # "Stretford, Manchester"
    postcode: str = ""     # "M32 0JG"

    @property
    def has_address(self) -> bool:
        return bool(self.first_line and self.postcode)

    @property
    def address(self) -> str:
        return ", ".join(p for p in (self.first_line, self.town, self.postcode) if p)

    @classmethod
    def from_dict(cls, row: dict) -> "Lead":
        def text(key: str) -> str:
            return " ".join(str(row.get(key) or "").split())

        postcode = text("postcode")
        return cls(
            full_name=text("full_name"),
            first_line=text("first_line"),
            town=text("town"),
            postcode=normalise_uk_postcode(postcode) or postcode.upper(),
        )


def find_lead(path: str | Path | None, caller: PhoneInfo, country: str = "") -> Lead | None:
    """The record for the number calling us, or None. A missing or broken file is logged
    and treated as no record, so it can never stop a call."""
    if not path or not caller.valid:
        return None
    try:
        with Path(path).open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        log.error("could not read leads file %s: %s", path, e)
        return None
    rows = data.get("leads") if isinstance(data, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if check_phone(str(row.get("phone") or ""), country).e164 == caller.e164:
            return Lead.from_dict(row)
    return None
