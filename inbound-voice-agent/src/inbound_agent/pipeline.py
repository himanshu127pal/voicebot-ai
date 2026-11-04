"""Builds the STT → LLM → TTS cascade and the VAD.

Two voice stacks, chosen per campaign:
  sarvam   — Saaras v3 STT (auto-detect) + Bulbul TTS. Hindi/English; the Naina stack.
  deepgram — Nova-3 STT + Aura-2 TTS. AU / UK / US English.
The LLM is always a TEXT model. Realtime speech-to-speech models could not endpoint
telephony audio reliably in testing — keep the cascade.
"""

from __future__ import annotations

import logging

from livekit.agents import NOT_GIVEN

# Every plugin is imported here, at module load: LiveKit only allows plugin registration
# on the main thread, so a lazy import inside a call crashes the job.
from livekit.plugins import anthropic, deepgram, google, sarvam, silero

from .campaigns import STACK_SARVAM, VoiceConfig
from .settings import Settings

log = logging.getLogger(__name__)

_DEFAULT_AURA_VOICE = "aura-2-thalia-en"


def build_stt(voice: VoiceConfig, keyterms: tuple[str, ...] = ()):
    if voice.stack == STACK_SARVAM:
        # "unknown" = auto-detect. A pinned language transliterates English into
        # Devanagari and the LLM then answers in Hindi.
        return sarvam.STT(model="saaras:v3", mode="transcribe", language="unknown")
    terms = list(dict.fromkeys(t for t in (*keyterms, *voice.keyterms) if t))
    return deepgram.STT(
        model="nova-3",
        language=voice.stt_language,
        smart_format=True,       # "the fourteenth", emails, phone numbers → clean text
        keyterm=terms or NOT_GIVEN,
    )


def build_turn_handling(settings: Settings) -> dict:
    return {
        "endpointing": {
            "min_delay": settings.min_endpointing_delay,
            "max_delay": settings.max_endpointing_delay,
        },
        "interruption": {
            "min_duration": settings.min_interruption_duration,
            "min_words": settings.min_interruption_words,
            "resume_false_interruption": True,
            "false_interruption_timeout": settings.false_interruption_timeout,
        },
        "preemptive_generation": {"enabled": True, "preemptive_tts": settings.preemptive_tts},
    }


def build_tts(voice: VoiceConfig):
    if voice.stack == STACK_SARVAM:
        kwargs = dict(
            target_language_code=voice.tts_language,
            output_audio_codec="linear16",   # raw PCM — no MP3 decode lag
            min_buffer_size=30,              # below the 50/150 defaults: faster first audio
            max_chunk_length=60,
        )
        if voice.speaker:
            kwargs["speaker"] = voice.speaker
        return sarvam.TTS(**kwargs)

    model = voice.tts_voice or _DEFAULT_AURA_VOICE
    if not model.startswith("aura"):
        # A non-Aura id returns 400 on every turn, which sounds exactly like a mute bot.
        log.error("tts_voice %r is not a Deepgram Aura voice — using %s", model,
                  _DEFAULT_AURA_VOICE)
        model = _DEFAULT_AURA_VOICE
    return deepgram.TTS(model=model)


def build_llm(settings: Settings):
    if settings.llm_provider == "anthropic":
        # _strict_tool_schema must stay False: strict schemas are compiled into a sampling
        # grammar, and with this many tools every request fails with 400 "Schema is too
        # complex" — the bot never speaks.
        # Parallel calls let one reply save every detail the caller volunteered at once.
        return anthropic.LLM(
            model=settings.anthropic_model,
            _strict_tool_schema=False,
            caching="ephemeral",
            max_tokens=512,
            parallel_tool_calls=True,
            max_retries=2,
        )
    if settings.llm_provider != "google":
        log.error("unknown LLM_PROVIDER %r — using google", settings.llm_provider)
    return google.LLM(model=settings.gemini_model)


def load_vad(settings: Settings):
    return silero.VAD.load(
        activation_threshold=settings.vad_activation_threshold,
        min_speech_duration=settings.vad_min_speech_duration,
    )
