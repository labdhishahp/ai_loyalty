"""A provider that replays a prepared script instead of calling anything.

WHY IT LIVES HERE, next to the real providers rather than in the test folder:
it implements the same Provider protocol, and keeping it beside the others is
what stops it drifting out of step when that protocol changes. A stale fake that
still satisfies an old interface is worse than no fake at all.

WHY IT EXISTS. The agent loop has a lot of behaviour that has nothing to do with
a model being clever: budgets stopping a run before it spends, a repeated call
being suppressed, a conversation being rebuilt correctly from the database
between requests, a tool failure reaching the model as an error it can recover
from. Every one of those is deterministic, and testing them against a live model
would be slow, flaky, expensive, and would not even test them reliably -- a real
model might simply not repeat a call on the turn you needed it to.

It also records the requests it received, so a test can assert what the loop
SENT, which is the half of the contract a live call cannot check at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import Completion, Message, ToolCall, ToolSpec, Usage

DEFAULT_USAGE = Usage(input_tokens=1000, output_tokens=100)


@dataclass
class Request:
    system: str
    messages: list[Message]
    tools: list[ToolSpec]


@dataclass
class ScriptedProvider:
    """Returns the next Completion from `script` on every call."""

    script: list[Completion]
    name: str = "fake"
    model: str = "scripted"
    requests: list[Request] = field(default_factory=list)

    def complete(self, *, system: str, messages: list[Message],
                 tools: list[ToolSpec], max_tokens: int = 16000) -> Completion:
        self.requests.append(Request(system, list(messages), list(tools)))
        if not self.script:
            raise AssertionError(
                "The agent asked for another turn than the script provides. "
                "Either the loop is not stopping when it should, or the script "
                "is short.")
        return self.script.pop(0)


def says(text: str, stop_reason: str = "end_turn",
         usage: Usage = DEFAULT_USAGE) -> Completion:
    return Completion(text=text, tool_calls=(), stop_reason=stop_reason,
                      usage=usage)


def calls(*tool_calls: ToolCall, text: str | None = None,
          usage: Usage = DEFAULT_USAGE) -> Completion:
    return Completion(text=text, tool_calls=tuple(tool_calls),
                      stop_reason="tool_use", usage=usage)


def call(tool: str, arguments: dict, call_id: str | None = None) -> ToolCall:
    return ToolCall(id=call_id or f"toolu_{tool}_{abs(hash(str(arguments))) % 10**8}",
                    name=tool, arguments=arguments)
