import pytest

from inbound_agent.call_state import CallState
from inbound_agent.costs import Pricing, call_cost, summarize, usage_records
from inbound_agent.disposition import build_payload

DEEPGRAM_GEMINI_CALL = [
    {"type": "llm_usage", "provider": "Google", "model": "gemini-2.5-flash",
     "input_tokens": 1_000_000, "input_cached_tokens": 600_000, "output_tokens": 10_000},
    {"type": "stt_usage", "provider": "Deepgram", "model": "nova-3", "audio_duration": 300.0},
    {"type": "tts_usage", "provider": "Deepgram", "model": "aura-2-pandora-en",
     "characters_count": 2000},
    {"type": "eot_usage", "provider": "livekit", "model": "turn-detector", "total_requests": 40},
]


def test_deepgram_gemini_call():
    cost = call_cost(DEEPGRAM_GEMINI_CALL, duration_seconds=300)
    # 400k fresh × 0.30 + 600k cached × 0.03 + 10k out × 2.50, per 1M
    assert cost["llm_usd"] == pytest.approx(0.12 + 0.018 + 0.025)
    assert cost["stt_usd"] == pytest.approx(5 * 0.0077)
    assert cost["tts_usd"] == pytest.approx(2 * 0.030)
    assert cost["telephony_usd"] == 0
    assert cost["total_usd"] == pytest.approx(0.163 + 0.0385 + 0.06, abs=1e-5)
    assert "unpriced" not in cost


def test_anthropic_cache_writes_and_dated_model_id():
    usage = [{"type": "llm_usage", "model": "claude-haiku-4-5-20251001",
              "input_tokens": 1_000_000, "input_cached_tokens": 500_000,
              "input_cache_creation_tokens": 100_000, "output_tokens": 0}]
    # 400k × 1.00 + 500k × 0.10 + 100k × 1.25
    assert call_cost(usage, 0)["llm_usd"] == pytest.approx(0.4 + 0.05 + 0.125)


def test_sarvam_rupees_and_telephony():
    usage = [{"type": "stt_usage", "model": "saaras:v3", "audio_duration": 60.0},
             {"type": "tts_usage", "model": "bulbul:v3", "characters_count": 1000}]
    cost = call_cost(usage, 120, Pricing(inr_per_usd=100, telephony_usd_per_minute=0.01))
    assert cost["stt_usd"] == pytest.approx(0.0049)
    assert cost["tts_usd"] == pytest.approx(0.03)
    assert cost["telephony_usd"] == pytest.approx(0.02)
    assert cost["total_inr"] == pytest.approx(5.49)


def test_unknown_model_is_flagged_not_free():
    usage = [{"type": "llm_usage", "provider": "openai", "model": "gpt-9", "input_tokens": 5}]
    assert call_cost(usage, 0)["unpriced"] == ["openai gpt-9"]


def test_usage_records_drop_zero_fields():
    class FakeUsage:
        def model_dump(self):
            return {"type": "stt_usage", "model": "nova-3", "input_tokens": 0,
                    "audio_duration": 12.5}
    assert usage_records([FakeUsage()]) == [
        {"type": "stt_usage", "model": "nova-3", "audio_duration": 12.5}]


def test_payload_carries_usage_and_cost(english_campaign):
    state = CallState(campaign=english_campaign)
    payload = build_payload(state, "inbound-bot", usage=DEEPGRAM_GEMINI_CALL)
    assert len(payload["usage"]) == 4
    assert payload["cost"]["total_usd"] > 0
    assert list(payload)[-1] == "transcript"


def test_summarize_skips_calls_logged_before_costing():
    rows = [
        {"campaign": "uk", "duration_seconds": 60, "cost": {"total_usd": 0.04, "llm_usd": 0.01}},
        {"campaign": "uk", "duration_seconds": 180, "cost": {"total_usd": 0.12, "llm_usd": 0.03}},
        {"campaign": "uk", "duration_seconds": 500},
    ]
    s = summarize(rows)["ALL"]
    assert s["calls"] == 2
    assert s["per_call_usd"] == pytest.approx(0.08)
    assert s["per_minute_usd"] == pytest.approx(0.04)
    assert s["llm_usd"] == pytest.approx(0.04)
