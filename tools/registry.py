"""The registry: the only way the model touches anything.

One entry per capability, holding everything needed to expose it safely:

    name           what the model calls it
    description    what the model reads when choosing -- the highest-leverage
                   prompt text in the system
    input_model    a Pydantic model, which does three jobs at once: it generates
                   the JSON Schema the model is given, it validates what comes
                   back, and it types the handler's argument. One definition,
                   three uses.
    handler        ordinary Python; knows nothing about models or prompts
    mutates        read or write. The write partition is empty today, and that
                   is a fact anyone can check rather than a promise.
    scopes         required permissions. Unused until Milestone 4 adds identity,
                   but present now because retrofitting authorization onto a
                   dispatcher is how it ends up applied inconsistently.

PROTOCOL-AGNOSTIC BY CONSTRUCTION. Nothing here knows about Anthropic, OpenAI or
MCP. `schemas()` emits plain JSON Schema; each provider adapts it. An MCP server
enumerating this registry is an adapter, not a rewrite.

TIMEOUTS ARE ENFORCED IN POSTGRES, not with threads. Every tool here is
database-bound, so `statement_timeout` cancels the actual work rather than
abandoning a thread that keeps running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError

from core.errors import ActionableError

from .envelope import ToolFailure, ToolResult, new_call_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[..., ToolResult]
    mutates: bool = False
    scopes: tuple[str, ...] = ("read",)
    timeout_seconds: float = 30.0

    def schema(self) -> dict:
        """JSON Schema for the model. Flat by convention: nested objects make
        provider-specific strict modes behave differently, and a tool that needs
        a nested argument is usually two tools."""
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        schema["additionalProperties"] = False
        schema.setdefault("required", [])
        return schema


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def readable(self) -> list[Tool]:
        return [t for t in self._tools.values() if not t.mutates]

    def writable(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.mutates]

    def schemas(self, include_writes: bool = False) -> list[dict]:
        """Plain JSON Schema tool definitions, provider-neutral."""
        tools = self._tools.values() if include_writes else self.readable()
        return [{"name": t.name, "description": t.description,
                 "input_schema": t.schema()}
                for t in sorted(tools, key=lambda t: t.name)]

    def dispatch(self, name: str, raw_input: dict, conn,
                 allow_writes: bool = False) -> ToolResult | ToolFailure:
        """Validate, authorise, execute. Never raises for an expected failure.

        The model's output is untrusted input to this system, exactly like a
        browser form post. This function is the trust boundary.
        """
        call_id = new_call_id()
        tool = self._tools.get(name)
        if tool is None:
            return ToolFailure(name, call_id, "unknown_tool",
                               f"No tool named {name!r}. Available: "
                               f"{', '.join(self.names())}")

        if tool.mutates and not allow_writes:
            return ToolFailure(name, call_id, "not_permitted",
                               f"{name} changes data and this run is read-only.")

        try:
            validated = tool.input_model.model_validate(raw_input or {})
        except ValidationError as exc:
            return ToolFailure(name, call_id, "validation",
                               _readable_validation_error(exc))

        try:
            with conn.cursor() as cur:
                # set_config(), not SET LOCAL: Postgres's SET does not accept
                # bound parameters, and interpolating the value into the
                # statement would be a needless injection point.
                cur.execute("select set_config('statement_timeout', %s, true)",
                            (str(int(tool.timeout_seconds * 1000)),))
            result = tool.handler(validated, conn)
        except ActionableError as exc:
            # Written for the caller and safe to return verbatim. This is how
            # the model learns, for instance, that a tier filter needs an
            # as-of date -- a distinction worth ~21 points of the headline.
            conn.rollback()
            return ToolFailure(name, call_id, "validation", str(exc))
        except Exception as exc:                        # noqa: BLE001
            conn.rollback()
            # The model gets the class name only: a database error quotes the
            # failing statement, and the model must not learn the schema from
            # an error message. The full detail goes to the log, because
            # otherwise a bug in a handler is undiagnosable from the outside --
            # which is exactly how this line came to be written.
            log.exception("tool %s failed", name)
            return ToolFailure(name, call_id, "internal",
                               f"{name} failed ({type(exc).__name__}). "
                               f"Try different arguments or another tool.")

        return ToolResult(tool=name, call_id=call_id, summary=result.summary,
                          data=result.data, meta=result.meta,
                          internals=result.internals)


def _readable_validation_error(exc: ValidationError) -> str:
    """Pydantic's default rendering is for developers. The model needs to know
    which field was wrong and what to send instead."""
    parts = []
    for error in exc.errors():
        field = ".".join(str(p) for p in error["loc"]) or "(input)"
        parts.append(f"{field}: {error['msg']}")
    return "Invalid arguments. " + "; ".join(parts)


REGISTRY = Registry()


def tool(name: str, description: str, input_model: type[BaseModel],
         mutates: bool = False, scopes: tuple[str, ...] = ("read",),
         timeout_seconds: float = 30.0):
    """Decorator registering a handler as a tool."""
    def wrap(handler):
        REGISTRY.register(Tool(name=name, description=description,
                               input_model=input_model, handler=handler,
                               mutates=mutates, scopes=scopes,
                               timeout_seconds=timeout_seconds))
        return handler
    return wrap
