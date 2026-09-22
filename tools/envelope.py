"""What a tool hands back.

Two audiences, one object, and they must not see the same thing.

THE MODEL sees `for_model()`: a summary line, the data, and the parameters that
were actually used. It does NOT see the SQL that produced the number. The metrics
layer exists precisely so the model never authors queries; showing it the query
would teach it the shape of the schema and invite it to reason about joins it is
not allowed to write.

THE TRACE sees `for_trace()`: everything, SQL included, so a human can audit any
number back to the statement that produced it. That is the whole point of having
a trace rather than logs.

Every result carries a `call_id`. The model cites it in its final answer, and the
UI turns that citation into a link back to the step that produced the evidence.
A claim with no call_id behind it is an assertion, not a finding.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def new_call_id() -> str:
    return uuid.uuid4().hex[:12]


def json_safe(value: Any) -> Any:
    """Convert database values into something both JSON and jsonb accept.

    Postgres hands back Decimal for numeric and date/datetime for temporal
    columns, and neither survives json.dumps. This is applied once, at the
    boundary where a result leaves a tool, because it is needed twice over:
    the result is persisted to jsonb for the trace AND serialised into the
    conversation the model reads. Converting in each caller is how the two
    quietly diverge.

    Decimal becomes float deliberately: these values are already measurements,
    and a string would force every consumer -- including the UI -- to parse it.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


@dataclass(frozen=True)
class ToolResult:
    tool: str
    call_id: str
    summary: str                      # one line; the model reads this first
    data: Any
    meta: dict = field(default_factory=dict)      # resolved params, row counts
    internals: dict = field(default_factory=dict)  # SQL etc. -- trace only
    ok: bool = True

    def for_model(self) -> dict:
        return json_safe({"ok": True, "call_id": self.call_id,
                          "summary": self.summary, "data": self.data,
                          "used": self.meta})

    def for_trace(self) -> dict:
        return {**self.for_model(), "tool": self.tool,
                "internals": json_safe(self.internals)}


@dataclass(frozen=True)
class ToolFailure:
    """A failure the model is expected to read and recover from.

    `message` is written for the model, not for a log file: it says what was
    wrong and what to do instead. Returning a structured error rather than
    raising is what lets a tool-calling loop self-correct, which is one of the
    more useful properties of this design.
    """

    tool: str
    call_id: str
    error: str                        # machine-readable: validation|not_found|internal
    message: str
    ok: bool = False

    def for_model(self) -> dict:
        return {"ok": False, "call_id": self.call_id,
                "error": self.error, "message": self.message}

    def for_trace(self) -> dict:
        return {**self.for_model(), "tool": self.tool}
