"""OpenAI-compatible adapter — the CoE AI Gateway, and anything speaking the
same protocol (LiteLLM, vLLM, SGLang, OpenRouter, Together).

Written and tested against the neutral contract, but NOT yet exercised against
the CoE gateway itself: its base URL and model id could not be discovered. See
docs/coe-gateway-investigation.md. Filling in COE_BASE_URL and COE_MODEL is all
that is required -- no code change.

The one real difference from Anthropic is tool results. Anthropic wants every
result in a single user message; OpenAI wants one message per result, each
carrying its own tool_call_id. That divergence is contained here, which is the
reason the neutral message format exists at all.
"""

from __future__ import annotations

import json

from openai import APIError, OpenAI

from core import config

from .base import (AssistantMessage, Completion, LLMError, Message, ToolCall,
                   ToolResultsMessage, ToolSpec, Usage, UserMessage)

# OpenAI reports finish_reason with different names for the same events.
STOP_REASONS = {"stop": "end_turn", "tool_calls": "tool_use",
                "length": "max_tokens", "content_filter": "refusal"}


class OpenAICompatibleProvider:
    name = "coe"

    def __init__(self, model: str | None = None):
        self.model = model or config.require(
            "COE_MODEL", "The exact model id the gateway expects.")
        self._client = OpenAI(
            api_key=config.require("COE_API_KEY"),
            base_url=config.require("COE_BASE_URL", "Must end in /v1."),
        )

    def _to_wire(self, system: str, messages: list[Message]) -> list[dict]:
        # No separate `system` parameter in this protocol: it is the first message.
        wire: list[dict] = [{"role": "system", "content": system}]
        for message in messages:
            if isinstance(message, UserMessage):
                wire.append({"role": "user", "content": message.text})
            elif isinstance(message, AssistantMessage):
                entry: dict = {"role": "assistant",
                               "content": message.text or None}
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name,
                                      "arguments": json.dumps(c.arguments)}}
                        for c in message.tool_calls]
                wire.append(entry)
            else:
                # One message PER result here, unlike Anthropic.
                wire.extend({"role": "tool", "tool_call_id": r.call_id,
                             "content": r.content} for r in message.results)
        return wire

    def complete(self, *, system: str, messages: list[Message],
                 tools: list[ToolSpec], max_tokens: int = 16000) -> Completion:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=self._to_wire(system, messages),
                tools=[{"type": "function",
                        "function": {"name": t.name, "description": t.description,
                                     "parameters": t.input_schema}}
                       for t in tools] or None,
            )
        except APIError as exc:
            raise LLMError(f"CoE gateway request failed: {exc}") from exc

        choice = response.choices[0]
        calls = []
        for call in (choice.message.tool_calls or []):
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # Malformed arguments are a tool failure, not a crash: the loop
                # returns the error and the model usually corrects itself.
                arguments = {"__invalid_json__": call.function.arguments}
            calls.append(ToolCall(call.id, call.function.name, arguments))

        usage = response.usage
        return Completion(
            text=choice.message.content or None,
            tool_calls=tuple(calls),
            stop_reason=STOP_REASONS.get(choice.finish_reason, "other"),
            usage=Usage(getattr(usage, "prompt_tokens", 0) or 0,
                        getattr(usage, "completion_tokens", 0) or 0),
        )
