"""The inbound agent — a Naina replica with the call-back prompt and tools."""

import logging

from livekit.agents import Agent

from .call_state import CallState
from .numbers import convert_numbers_in_stream
from .prompts import build_instructions
from .redaction import REDACTION_NOTE, has_sensitive_number, redact_numbers
from .settings import get_settings
from .tools import ALL_TOOLS

log = logging.getLogger(__name__)


def _text_of(chunk) -> str:
    if isinstance(chunk, str):
        return chunk
    return getattr(getattr(chunk, "delta", None), "content", None) or ""


def _redact_message(message) -> None:
    text = message.text_content or ""
    if has_sensitive_number(text):
        log.warning("removed a card/account-length number from the caller's turn")
        message.content = [redact_numbers(text) + REDACTION_NOTE]


class InboundAgent(Agent):
    def __init__(self, state: CallState):
        can_transfer = bool(state.campaign.human_transfer_uri or get_settings().human_transfer_uri)
        super().__init__(
            instructions=build_instructions(state.campaign, state.caller, can_transfer),
            tools=ALL_TOOLS,
        )

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Strip card/account-length numbers from the caller's turn before the LLM sees it."""
        _redact_message(new_message)

    async def llm_node(self, chat_ctx, tools, model_settings):
        # Typed/text turns skip on_user_turn_completed, so check every caller turn here too.
        for item in chat_ctx.items:
            if getattr(item, "role", None) == "user":
                _redact_message(item)
        state: CallState = self.session.userdata
        state.reply_text = ""
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            state.reply_text += _text_of(chunk)
            yield chunk

    async def tts_node(self, text, model_settings):
        """Speak digits as English words, whatever the sentence language."""
        async for frame in Agent.default.tts_node(
            self, convert_numbers_in_stream(text), model_settings
        ):
            yield frame
