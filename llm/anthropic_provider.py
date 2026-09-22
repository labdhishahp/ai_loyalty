"""Anthropic adapter.

Model defaults to claude-opus-5. Adaptive thinking is on by default on this
model family, and `budget_tokens` is rejected outright, so effort is the cost
lever rather than a thinking budget.
"""

from __future__ import annotations

from typing import Any

import anthropic

from core import config

from .base import (AssistantMessage, Completion, LLMError, Message, ToolCall,
                   ToolResultsMessage, ToolSpec, Usage, UserMessage)

# Sonnet 5 rather than Opus 5: 2.5x cheaper per token, and the first real
# investigation showed the cost is dominated by resending a growing conversation
# rather than by reasoning depth. Override with ANTHROPIC_MODEL when a measured
# comparison needs the stronger model.
DEFAULT_MODEL = "claude-sonnet-5"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None, effort: str | None = None):
        self.model = model or config.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
        # Effort is the cost/quality lever on this family. Default medium for
        # routine development; raise it for measured eval runs.
        self.effort = effort or config.get("ANTHROPIC_EFFORT", "medium")
        self._client = anthropic.Anthropic(
            api_key=config.require("ANTHROPIC_API_KEY"))

    def _to_wire(self, messages: list[Message]) -> list[dict]:
        wire: list[dict] = []
        for message in messages:
            if isinstance(message, UserMessage):
                wire.append({"role": "user", "content": message.text})
            elif isinstance(message, AssistantMessage):
                # Replay the provider's own blocks when we have them: that is
                # what preserves thinking blocks across turns.
                if message.provider_payload:
                    wire.append({"role": "assistant",
                                 "content": message.provider_payload})
                    continue
                content: list[dict] = []
                if message.text:
                    content.append({"type": "text", "text": message.text})
                for call in message.tool_calls:
                    content.append({"type": "tool_use", "id": call.id,
                                    "name": call.name, "input": call.arguments})
                wire.append({"role": "assistant", "content": content})
            else:
                # EVERY tool result in ONE user message. Splitting them across
                # messages silently teaches the model to stop calling tools in
                # parallel, which costs turns for the rest of the run.
                wire.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r.call_id,
                     "content": r.content, "is_error": r.is_error}
                    for r in message.results]})
        return wire

    def complete(self, *, system: str, messages: list[Message],
                 tools: list[ToolSpec], max_tokens: int = 16000) -> Completion:
        # Every turn resends the whole conversation, so by step 12 the same
        # 40,000 tokens have been paid for a dozen times. Two cache breakpoints
        # fix that, placed by the rule that a cache is a PREFIX match:
        #   1. the last tool definition -- covers tools and system, which never
        #      change within a run;
        #   2. the second-to-last message -- covers all settled history, leaving
        #      only the newest turn uncached.
        # Measured effect on a 14-step run: the repeated prefix drops to ~10% of
        # its uncached price. Without this the token budget is spent on paying
        # for the same text over and over rather than on investigating.
        wire_tools = [{"name": t.name, "description": t.description,
                       "input_schema": t.input_schema, "strict": t.strict}
                      for t in tools]
        if wire_tools:
            wire_tools[-1]["cache_control"] = {"type": "ephemeral"}

        wire_messages = self._to_wire(messages)
        if len(wire_messages) >= 2:
            _mark_cacheable(wire_messages[-2])

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=wire_messages,
                # strict guarantees the arguments validate against the schema,
                # which removes a whole class of retry logic.
                tools=wire_tools,
                output_config={"effort": self.effort},
            )
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc

        text_parts, calls = [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input)))

        return Completion(
            text="\n".join(text_parts) or None,
            tool_calls=tuple(calls),
            stop_reason=response.stop_reason or "other",
            # Cache reads and writes are input tokens too: a budget that
            # ignored them would not bound real spend.
            usage=Usage(response.usage.input_tokens
                        + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
                        + (getattr(response.usage, "cache_creation_input_tokens", 0) or 0),
                        response.usage.output_tokens),
            # Kept verbatim so thinking blocks survive the next turn.
            provider_payload=[b.model_dump() for b in response.content],
        )


def _mark_cacheable(message: dict) -> None:
    """Put a cache breakpoint on a message's last content block."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content,
                               "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = {"type": "ephemeral"}


def _mark_cacheable(message: dict) -> None:
    """Put a cache breakpoint on a message's last content block."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content,
                               "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = {"type": "ephemeral"}
