"""How an investigation ends: a structured answer, not prose.

WHY STRUCTURE. The eval has to mark answers, and marking free text means either
a human or another model deciding whether "it basically said that" counts. Both
are slow and noisy. A structured answer makes most of the marking deterministic:
did it name a cause, did it cite evidence, did it rule the red herring out with
a reason, did it say which comparison it used.

WHY A TOOL RATHER THAN A RESPONSE FORMAT. Finishing becomes an explicit act the
model chooses, in the same mechanism as every other capability, and it works
identically on both providers. A response-format constraint would differ between
Anthropic and an OpenAI-compatible gateway and would leak into the agent loop.

It is registered as a normal read tool -- it changes no business data. The
runtime recognises it and completes the run.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tools.envelope import ToolResult
from tools.registry import tool

SUBMIT_FINDINGS = "submit_findings"


class Cause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="Short label, e.g. 'Beauty stockout in GB'.")
    explanation: str = Field(description="What happened and how it produced the "
                                         "observed change.")
    evidence_call_ids: list[str] = Field(
        description="call_ids of the tool results supporting this. Required.")
    importance: Literal["largest", "significant", "minor"] = Field(
        description="Relative weight against the other causes you found.")
    confidence: Literal["high", "medium", "low"]


class RuledOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    why_not: str = Field(description="Why this does not explain the change. "
                                     "'Not mentioned' is not ruling out.")
    evidence_call_ids: list[str] = Field(default_factory=list)


class FindingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(description="Two or three sentences answering the "
                                      "question directly.")
    verdict: Literal["confirmed", "partly_confirmed", "not_confirmed",
                     "inconclusive"] = Field(
        description="Whether the premise of the question held up.")
    metrics_used: list[str] = Field(
        description="Which measures you based the answer on, e.g. "
                    "['orders_per_member', 'revenue_per_member'].")
    comparison_basis: str = Field(
        description="Which periods you compared and why, e.g. 'year over year, "
                    "because the business is seasonal'.")
    cohort_basis: str = Field(
        description="How you defined the group, including whether membership was "
                    "held fixed in time or re-resolved per period, and what "
                    "difference that made.")
    causes: list[Cause] = Field(description="Ordered, most important first.")
    ruled_out: list[RuledOut] = Field(
        description="Candidate explanations you considered and rejected, each "
                    "with a reason.")
    limitations: str = Field(description="What you could not determine.")
    recommended_next: str = Field(description="The most useful next action.")


@tool(SUBMIT_FINDINGS,
      "Submits your final answer and ends the investigation. Call this exactly "
      "once, when you can support your conclusions with cited evidence. Every "
      "cause and every dismissal must reference the call_ids that back it. This "
      "is the only way to finish: conclusions written as prose without calling "
      "this are not recorded as an answer.",
      FindingsInput)
def submit_findings(inp: FindingsInput, conn) -> ToolResult:
    # Storing the answer is the runtime's job -- it owns the run row. This
    # handler exists so the capability is defined in one place with everything
    # else, and validated by the same machinery.
    return ToolResult(tool=SUBMIT_FINDINGS, call_id="",
                      data=inp.model_dump(mode="json"),
                      summary=f"Findings submitted: {inp.verdict}, "
                              f"{len(inp.causes)} cause(s), "
                              f"{len(inp.ruled_out)} ruled out.")
