"""What a call cost: LiveKit's per-model usage (LLM tokens, TTS characters, STT seconds)
priced at the providers' pay-as-you-go rates, plus the carrier's per-minute rate.

No LiveKit import, so the report also runs where the agent can't:

    python -m inbound_agent.costs                    # TRANSCRIPT_LOG_PATH
    python -m inbound_agent.costs data/calls.jsonl

Rates change — re-check the pricing pages before quoting a client. Unknown models are
listed under `unpriced` instead of being counted as free.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Rate:
    amount: float
    currency: str = "USD"

    def usd(self, inr_per_usd: float) -> float:
        return self.amount / inr_per_usd if self.currency == "INR" else self.amount


@dataclass(frozen=True)
class LLMRate:
    """USD per 1M tokens."""
    input: float
    cached_input: float
    cache_write: float
    output: float


# Pay-as-you-go list prices, read 2026-11-12. Keys are model-name prefixes, so
# "aura-2" covers every Aura-2 voice and dated model ids match their base name.
LLM_RATES = {
    "gemini-2.5-flash":      LLMRate(0.30, 0.03, 0.30, 2.50),
    # gemini-2.5-flash requests are routed here from 2026-12-01, same price.
    "gemini-3.5-flash-lite": LLMRate(0.30, 0.03, 0.30, 2.50),
    "claude-haiku-4-5":      LLMRate(1.00, 0.10, 1.25, 5.00),
}
STT_PER_MINUTE = {
    "nova-3":    Rate(0.0077),          # Deepgram, monolingual streaming
    "saaras":    Rate(0.49, "INR"),     # Sarvam
}
TTS_PER_1K_CHARS = {
    "aura-2":    Rate(0.030),           # Deepgram
    "bulbul:v3": Rate(3.00, "INR"),     # Sarvam
}


@dataclass(frozen=True)
class Pricing:
    inr_per_usd: float = 88.0
    telephony_usd_per_minute: float = 0.0


def _lookup(rates: dict, model: str):
    model = (model or "").lower().removeprefix("models/")
    matches = [k for k in rates if model.startswith(k)]
    return rates[max(matches, key=len)] if matches else None


def usage_records(usage: Iterable) -> list[dict]:
    """LiveKit ModelUsage objects (or dicts already) → plain dicts without zero fields."""
    out = []
    for u in usage or ():
        d = u.model_dump() if hasattr(u, "model_dump") else dict(u)
        out.append({k: v for k, v in d.items() if v not in (0, 0.0)})
    return out


def _llm_usd(u: dict, rate: LLMRate) -> float:
    cached = u.get("input_cached_tokens", 0)
    written = u.get("input_cache_creation_tokens", 0)
    # input_tokens already includes the cached and cache-write tokens.
    fresh = max(u.get("input_tokens", 0) - cached - written, 0)
    return (fresh * rate.input + cached * rate.cached_input + written * rate.cache_write
            + u.get("output_tokens", 0) * rate.output) / 1_000_000


def call_cost(usage: list[dict], duration_seconds: float, pricing: Pricing = Pricing()) -> dict:
    parts = {"stt": 0.0, "tts": 0.0, "llm": 0.0}
    unpriced = []
    for u in usage:
        kind, model = u.get("type", ""), u.get("model", "")
        if kind == "llm_usage":
            rate = _lookup(LLM_RATES, model)
            if rate:
                parts["llm"] += _llm_usd(u, rate)
        elif kind == "stt_usage":
            rate = _lookup(STT_PER_MINUTE, model)
            if rate:
                parts["stt"] += u.get("audio_duration", 0.0) / 60 * rate.usd(pricing.inr_per_usd)
        elif kind == "tts_usage":
            rate = _lookup(TTS_PER_1K_CHARS, model)
            if rate:
                parts["tts"] += (u.get("characters_count", 0) / 1000
                                 * rate.usd(pricing.inr_per_usd))
        else:
            continue   # turn detector / interruption models run locally
        if rate is None:
            unpriced.append(f"{u.get('provider', '')} {model}".strip())

    parts["telephony"] = duration_seconds / 60 * pricing.telephony_usd_per_minute
    total = sum(parts.values())
    cost = {f"{k}_usd": round(v, 5) for k, v in parts.items()}
    cost["total_usd"] = round(total, 5)
    cost["total_inr"] = round(total * pricing.inr_per_usd, 2)
    if unpriced:
        cost["unpriced"] = unpriced
    return cost


def summarize(rows: Iterable[dict]) -> dict[str, dict]:
    """Calls with a cost, grouped by campaign plus an "ALL" row."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if "cost" in r:
            groups[r.get("campaign") or "?"].append(r)
            groups["ALL"].append(r)
    out = {}
    for name, calls in groups.items():
        minutes = sum(c.get("duration_seconds", 0) for c in calls) / 60
        total = sum(c["cost"]["total_usd"] for c in calls)
        out[name] = {
            "calls": len(calls),
            "minutes": round(minutes, 1),
            "total_usd": round(total, 4),
            "per_call_usd": round(total / len(calls), 4),
            "per_minute_usd": round(total / minutes, 4) if minutes else 0.0,
            **{f"{k}_usd": round(sum(c["cost"].get(f"{k}_usd", 0) for c in calls), 4)
               for k in ("stt", "tts", "llm", "telephony")},
        }
    return out


def main(argv: list[str]) -> int:
    from .settings import get_settings

    path = Path(argv[0] if argv else get_settings().transcript_log_path or "data/calls.jsonl")
    if not path.exists():
        print(f"no call log at {path}")
        return 1
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    summary = summarize(rows)
    skipped = len(rows) - summary.get("ALL", {}).get("calls", 0)
    if not summary:
        print(f"{path}: {len(rows)} call(s), none with cost data yet")
        return 0
    inr = get_settings().usd_inr_rate
    print(f"{path}: {len(rows)} call(s), {skipped} without cost data (logged before costing)")
    for name, s in sorted(summary.items(), key=lambda kv: kv[0] != "ALL"):
        print(f"\n{name}: {s['calls']} calls, {s['minutes']} min")
        print(f"  total      ${s['total_usd']:.4f}  (INR {s['total_usd'] * inr:.2f})")
        print(f"  per call   ${s['per_call_usd']:.4f}  (INR {s['per_call_usd'] * inr:.2f})")
        print(f"  per minute ${s['per_minute_usd']:.4f}  (INR {s['per_minute_usd'] * inr:.2f})")
        print(f"  split      STT ${s['stt_usd']}  TTS ${s['tts_usd']}  LLM ${s['llm_usd']}"
              f"  telephony ${s['telephony_usd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
