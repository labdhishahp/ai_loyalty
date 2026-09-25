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
    scopes         permissions the caller must hold. Checked at dispatch against
                   the context's scopes, so a tool cannot be reached by someone
                   whose role does not include it -- regardless of which route,
                   agent or protocol got them here.

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


# Validation keywords that providers' strict tool modes reject. Anthropic
# returns 400 "For 'integer' type, properties maximum, minimum are not
# supported"; other gateways vary. Pydantic still enforces every one of them
# server-side, so removing them from the advertised schema loses no safety --
# but it would lose INFORMATION, so each is folded into the description instead.
# The model is then told the constraint in words and the validator still refuses
# anything outside it.
UNPORTABLE_KEYWORDS = {
    "minimum": "at least {}", "maximum": "at most {}",
    "exclusiveMinimum": "greater than {}", "exclusiveMaximum": "less than {}",
    "minLength": "at least {} characters", "maxLength": "at most {} characters",
    "minItems": "at least {} items", "maxItems": "at most {} items",
    "pattern": "matching {}",
}


def _inline_refs(node, defs: dict):
    """Replace every $ref with the definition it points at.

    Pydantic factors nested models into $defs and references them with $ref.
    That is valid JSON Schema, but provider strict modes vary in whether they
    resolve it, and a schema that silently means something different on another
    gateway is exactly what the provider boundary exists to prevent. Inlining
    produces a self-contained schema that means the same thing everywhere.

    Safe here because these models form a tree: a self-referencing model would
    recurse forever, and would also be a sign the tool wants two tools.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1], {})
            merged = {**_inline_refs(target, defs),
                      **{k: v for k, v in node.items() if k != "$ref"}}
            return merged
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _portable(prop: dict) -> None:
    """Strip strict-mode-hostile keywords, preserving them as prose."""
    prop.pop("title", None)
    notes = [text.format(prop.pop(keyword))
             for keyword, text in UNPORTABLE_KEYWORDS.items() if keyword in prop]
    if notes:
        existing = prop.get("description", "").rstrip()
        prop["description"] = f"{existing} ({'; '.join(notes)})".strip()
    for nested in prop.get("anyOf", []):
        _portable(nested)
    if isinstance(prop.get("items"), dict):
        _strip_all(prop["items"])
    for nested in prop.get("properties", {}).values():
        _portable(nested)


def _strip_all(schema: dict) -> None:
    """Apply _portable to every property, at every depth."""
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        _portable(prop)


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler needs to know about WHO is calling and WHY.

    Previously a handler received only a database connection, which was enough
    to read but not enough to write: a proposal created by the agent had no way
    to record which investigation produced it, so `run_id` was hardcoded to None
    and every agent-created proposal was orphaned from its evidence.

    Carrying a context rather than adding a second parameter is deliberate. Two
    more callers are coming that are not the agent -- an MCP server acting for
    an external client, and eventually a signed-in human -- and each supplies a
    different actor, different permissions and no run at all. A context object
    means those callers differ in the VALUE they pass, not in the shape of the
    call.
    """

    conn: object                       # psycopg connection
    actor: str = "system"              # who is asking; recorded on anything written
    run_id: str | None = None          # the investigation, when there is one

    # TWO INDEPENDENT GATES, and both must open.
    #
    # allow_writes is a property of the RUN: an investigation that was started
    # read-only stays read-only even for someone who could have enabled writes.
    # scopes is a property of the CALLER: what this person or service may do at
    # all. An analyst running a write-enabled investigation still cannot approve
    # anything, because approval is not in their scopes.
    #
    # Collapsing these into one flag would mean "this run may write" and "this
    # caller may write" were the same statement, and they are not.
    allow_writes: bool = False
    scopes: frozenset[str] = frozenset({"read"})


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[..., ToolResult]
    mutates: bool = False
    scopes: tuple[str, ...] = ("read",)
    timeout_seconds: float = 30.0

    # Strict mode asks the provider to constrain generation to the schema, so
    # arguments arrive guaranteed to validate. It is off by default here, and
    # that is a deliberate decision rather than an oversight.
    #
    # The provider compiles every strict schema into a single grammar and
    # refuses the request when it grows too large. With this tool set it does:
    # first "The compiled grammar is too large", then, after thinning it,
    # "Schemas contains too many optional parameters (26), which would make
    # grammar compilation inefficient".
    #
    # The only ways to satisfy that are to carry fewer tools or to make optional
    # parameters required. Both are worse than the thing strict buys. These
    # tools are optional-heavy on purpose -- a caller should be able to ask for
    # a metric without naming a country, a tier, a granularity and a filter --
    # and forcing all of that to be supplied would make every call noisier to
    # get the schema past a compiler.
    #
    # Nothing is actually lost. Pydantic validates every argument server-side
    # regardless, which is the real guarantee; strict would only have saved a
    # round trip when the model got one wrong. When it does, it receives a
    # readable error naming the field and corrects itself -- a path the loop
    # supports and the tests cover.
    strict: bool = False

    def schema(self) -> dict:
        """JSON Schema for the model. Flat by convention: nested objects make
        provider-specific strict modes behave differently, and a tool that needs
        a nested argument is usually two tools."""
        schema = self.input_model.model_json_schema()
        schema = _inline_refs(schema, schema.get("$defs", {}))
        schema.pop("$defs", None)
        schema.pop("title", None)
        _strip_all(schema)
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
                 "input_schema": t.schema(), "strict": t.strict}
                for t in sorted(tools, key=lambda t: t.name)]

    def dispatch(self, name: str, raw_input: dict,
                 ctx: ToolContext) -> ToolResult | ToolFailure:
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

        if tool.mutates and not ctx.allow_writes:
            return ToolFailure(name, call_id, "not_permitted",
                               f"{name} changes data and this run is read-only.")

        missing = set(tool.scopes) - set(ctx.scopes)
        if missing:
            return ToolFailure(name, call_id, "not_permitted",
                               f"{name} requires the "
                               f"{', '.join(sorted(missing))} permission, which "
                               f"{ctx.actor} does not have.")

        try:
            validated = tool.input_model.model_validate(raw_input or {})
        except ValidationError as exc:
            return ToolFailure(name, call_id, "validation",
                               _readable_validation_error(exc))

        conn = ctx.conn
        try:
            with conn.cursor() as cur:
                # set_config(), not SET LOCAL: Postgres's SET does not accept
                # bound parameters, and interpolating the value into the
                # statement would be a needless injection point.
                cur.execute("select set_config('statement_timeout', %s, true)",
                            (str(int(tool.timeout_seconds * 1000)),))
            result = tool.handler(validated, ctx)
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
         timeout_seconds: float = 30.0, strict: bool = False):
    """Decorator registering a handler as a tool."""
    def wrap(handler):
        REGISTRY.register(Tool(name=name, description=description,
                               input_model=input_model, handler=handler,
                               mutates=mutates, scopes=scopes,
                               timeout_seconds=timeout_seconds, strict=strict))
        return handler
    return wrap
