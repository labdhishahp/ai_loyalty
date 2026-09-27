"""A provider-neutral conversation, so the agent never learns who answered.

The abstraction sits one level above "generate text": an agent needs

    (system, messages, tools) -> (text | tool calls, usage)

Anthropic and OpenAI-compatible endpoints differ in wire format, not in shape.
Keeping the neutral form here means the agent loop, the trace, the eval and the
UI are all written once, and swapping Qwen for Claude is configuration.

WHY NOT REUSE A PROVIDER'S MESSAGE TYPE. Tool results are where the two diverge
most: Anthropic puts every result in ONE user message as tool_result blocks,
while OpenAI wants a separate message per result. Adopting either shape would
bake that provider's model into the agent and the stored trace, and the trace is
the thing we most want to outlive a provider choice.

`provider_payload` is the one concession: an opaque blob an adapter may attach to
an assistant turn and hand back verbatim when the SAME provider replays the
conversation. It carries things like Anthropic's thinking blocks, which are
meaningless to another provider and must not be interpreted by the agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    content: str            # JSON the model reads
    is_error: bool = False


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens)


# --- neutral messages ------------------------------------------------------

@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    provider_payload: Any = None       # opaque; replayed only to the same provider


@dataclass(frozen=True)
class ToolResultsMessage:
    results: tuple[ToolOutcome, ...]


Message = UserMessage | AssistantMessage | ToolResultsMessage


@dataclass(frozen=True)
class Completion:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str          # end_turn | tool_use | max_tokens | refusal | other
    usage: Usage
    provider_payload: Any = None

    def as_assistant_message(self) -> AssistantMessage:
        return AssistantMessage(self.text, self.tool_calls, self.provider_payload)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    # Constrain generation to the schema. Off by default: the provider limits
    # how large the combined grammar may be, and this tool set exceeds it.
    # See tools/registry.py for the full reasoning.
    strict: bool = False


class LLMError(Exception):
    """The provider could not be reached or refused the request."""


class Provider(Protocol):
    name: str
    model: str

    def complete(self, *, system: str, messages: list[Message],
                 tools: list[ToolSpec], max_tokens: int) -> Completion: ...
