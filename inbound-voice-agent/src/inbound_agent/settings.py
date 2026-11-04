"""Runtime settings, read once from the environment. `.env` is loaded on import so the
per-call worker subprocesses see the same values as the parent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    return int(_str(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_str(name, str(default)))


@dataclass(frozen=True)
class Settings:
    agent_name: str
    worker_host: str
    worker_port: int
    num_idle_processes: int

    campaigns_dir: Path
    console_campaign: str
    llm_provider: str
    gemini_model: str
    anthropic_model: str

    max_call_seconds: float
    nudge_after_seconds: float
    max_nudges: int
    human_transfer_uri: str

    webhook_url: str
    webhook_token: str
    transcript_log_path: str

    vad_activation_threshold: float
    vad_min_speech_duration: float

    min_endpointing_delay: float
    max_endpointing_delay: float
    min_interruption_duration: float
    min_interruption_words: int
    false_interruption_timeout: float
    preemptive_tts: bool


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        agent_name=_str("INBOUND_AGENT_NAME", "inbound-bot"),
        worker_host=_str("WORKER_HTTP_HOST", "127.0.0.1"),
        worker_port=_int("WORKER_HTTP_PORT", 8088),
        num_idle_processes=_int("NUM_IDLE_PROCESSES", 2),
        campaigns_dir=Path(_str("INBOUND_CAMPAIGNS_DIR",
                                str(PROJECT_ROOT / "config" / "campaigns"))),
        # Console mode has no DID, so this picks the campaign. Ignored on real calls.
        console_campaign=_str("CONSOLE_CAMPAIGN"),
        llm_provider=_str("LLM_PROVIDER", "google").lower(),
        gemini_model=_str("GEMINI_MODEL", "gemini-2.5-flash"),
        anthropic_model=_str("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        max_call_seconds=_float("MAX_CALL_SECONDS", 300),
        nudge_after_seconds=_float("NUDGE_AFTER_SECONDS", 10),
        max_nudges=_int("MAX_NUDGES", 3),
        human_transfer_uri=_str("HUMAN_TRANSFER_URI"),
        webhook_url=_str("DISPOSITION_WEBHOOK_URL"),
        webhook_token=_str("DISPOSITION_WEBHOOK_TOKEN"),
        transcript_log_path=_str("TRANSCRIPT_LOG_PATH"),
        # 0.6 rejects quiet background chatter without dropping soft speakers (default 0.5).
        vad_activation_threshold=_float("VAD_ACTIVATION_THRESHOLD", 0.6),
        vad_min_speech_duration=_float("VAD_MIN_SPEECH_DURATION", 0.10),
        # Turn-taking. The turn detector replies after MIN when the caller sounds finished
        # and waits up to MAX when they sound mid-thought ("I'm moving from… um…").
        min_endpointing_delay=_float("MIN_ENDPOINTING_DELAY", 0.5),
        max_endpointing_delay=_float("MAX_ENDPOINTING_DELAY", 3.0),
        # One real word is needed to cut the agent off, so a cough or line noise doesn't;
        # if a cut-off turns out to be noise, the agent resumes after FALSE_INTERRUPTION_TIMEOUT.
        min_interruption_duration=_float("MIN_INTERRUPTION_DURATION", 0.5),
        min_interruption_words=_int("MIN_INTERRUPTION_WORDS", 1),
        false_interruption_timeout=_float("FALSE_INTERRUPTION_TIMEOUT", 1.0),
        # Start synthesising the reply before the turn is confirmed — faster first audio.
        preemptive_tts=_str("PREEMPTIVE_TTS", "true").lower() in ("1", "true", "yes"),
    )
