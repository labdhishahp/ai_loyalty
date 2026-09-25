"""The agent's write capability: filling in a form, and nothing else.

TWO TOOLS, AND THE SPLIT IS THE POINT.

  preview_campaign      READ-ONLY. Resolves the audience and runs every policy
                        check against a hypothetical campaign, writing nothing.
                        The model can iterate here as many times as it likes --
                        adjust the discount, widen the audience, re-check --
                        without leaving a trail of abandoned drafts for a human
                        to wade through.

  create_campaign_proposal   WRITES a draft. Inert: it sends nothing, charges
                        nothing and touches no L-Mart business data. Execution
                        is a separate path that runs only after a human approves.

The model cannot execute anything. It cannot approve anything. Its entire write
capability is "record a proposed campaign", and every proposal is validated on
creation whether or not the model bothered to preview it -- a draft nobody
checked looks identical to one that passed, and that difference matters to
whoever opens the approval screen.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from actions import proposals
from actions.audience import AudienceSpec, resolve
from actions.policy_engine import validate

from .catalog import _Input
from .envelope import ToolResult
from .registry import ToolContext, tool


class _CampaignShape(_Input):
    """Fields shared by previewing and proposing, so the two cannot diverge."""

    channel: Literal["email", "push", "sms"]
    offer_type: Literal["points_multiplier", "percentage_discount"] = Field(
        description="One or the other, never both: policy prohibits stacking a "
                    "multiplier on a discount.")
    offer_value: float = Field(
        description="For points_multiplier, the multiplier (e.g. 2 means 2x "
                    "points). For percentage_discount, the percentage (e.g. 10 "
                    "means 10% off).")
    countries: list[str] = Field(
        default_factory=list,
        description="Country codes from get_reference_data. Empty targets all.")
    tiers: list[str] = Field(
        default_factory=list,
        description="Tier codes. Empty targets everyone, INCLUDING non-members "
                    "and the lowest tier -- which lowers the discount ceiling "
                    "that applies.")
    tier_as_of: str | None = Field(
        default=None,
        description="Required when tiers is set: an ISO date, or 'period_end'. "
                    "Same meaning as in get_metric.")
    lapsed_min_days: int | None = Field(
        default=None,
        description="Only include customers with no order for at least this "
                    "many days.")
    lapsed_max_days: int | None = Field(
        default=None,
        description="...and no more than this many. Customers who have never "
                    "ordered are excluded when this is set.")
    holdout_pct: int = Field(
        default=0,
        description="Percentage held back from the send so incremental effect "
                    "can be measured. Required above a certain audience size.")

    def audience(self) -> dict:
        return {"countries": self.countries, "tiers": self.tiers,
                "tier_as_of": self.tier_as_of,
                "lapsed_min_days": self.lapsed_min_days,
                "lapsed_max_days": self.lapsed_max_days}


class PreviewInput(_CampaignShape):
    pass


@tool("preview_campaign",
      "Checks a campaign idea WITHOUT creating anything: resolves how many "
      "customers it would reach, reports who was excluded and why, and runs "
      "every governance rule against it. Use it before proposing, and use it "
      "repeatedly -- adjusting an offer or an audience and re-checking costs "
      "nothing. Each failed check names the rule, the limit and what you asked "
      "for, so it tells you how to fix it.",
      PreviewInput)
def preview_campaign(inp: PreviewInput, ctx: ToolContext) -> ToolResult:
    conn = ctx.conn
    spec = AudienceSpec.from_dict(inp.audience())
    resolved = resolve(conn, spec)
    result = validate(conn, offer_type=inp.offer_type,
                      offer_value=inp.offer_value, holdout_pct=inp.holdout_pct,
                      audience=resolved, spec=spec)
    verdict = "would pass" if result.ok else "would FAIL"
    return ToolResult(
        tool="preview_campaign", call_id="",
        data={"valid": result.ok, **result.to_dict(),
              "excluded_no_consent": resolved.excluded_no_consent,
              "excluded_dormant_gb": resolved.excluded_dormant_gb,
              "tiers_present": resolved.tiers_present},
        summary=(f"{verdict} validation: {resolved.size:,} recipients "
                 f"({resolved.description}). "
                 + ("; ".join(c.detail for c in result.failures)
                    if result.failures else "All checks pass.")))


class ProposeInput(_CampaignShape):
    name: str = Field(
        description="Follows '<Market> <Segment> <Purpose> - <Month Year>', "
                    "e.g. 'UK Gold Reactivation - October 2026'.")
    objective: str = Field(description="What this campaign is for, in a sentence.")
    programme: str = Field(
        description="The programme it belongs to. Reuse an existing programme "
                    "name from get_reference_data when continuing one.")
    rationale: str = Field(
        description="Why this campaign, why this audience, why this offer. A "
                    "human reads this before approving.")
    evidence_call_ids: list[str] = Field(
        description="call_ids of the findings that justify this. A proposal "
                    "with no evidence is a guess, and the approval screen shows "
                    "this list.")


@tool("create_campaign_proposal",
      "Records a proposed campaign as a DRAFT for human approval. It sends "
      "nothing and changes no customer data. Validation runs automatically on "
      "creation, so preview_campaign first and fix any failures -- a proposal "
      "that fails its checks can be created but will not be approvable. You "
      "cannot approve or execute a campaign; a person does that.",
      ProposeInput, mutates=True, scopes=("propose",))
def create_campaign_proposal(inp: ProposeInput, ctx: ToolContext) -> ToolResult:
    conn = ctx.conn
    proposal = proposals.create(
        # The investigation that produced this. Previously hardcoded to None,
        # which severed every agent-created proposal from the evidence
        # justifying it: the approval screen could never link back to the run,
        # and the cited call_ids pointed at a trace nobody could find.
        conn, run_id=ctx.run_id, created_by=ctx.actor,
        name=inp.name, objective=inp.objective,
        programme=inp.programme, channel=inp.channel, offer_type=inp.offer_type,
        offer_value=inp.offer_value, audience=inp.audience(),
        holdout_pct=inp.holdout_pct, rationale=inp.rationale,
        evidence_call_ids=inp.evidence_call_ids)
    validation = proposal["validation"]
    return ToolResult(
        tool="create_campaign_proposal", call_id="",
        data={"proposal_id": str(proposal["proposal_id"]),
              "status": proposal["status"],
              "audience_size": proposal["audience_size"],
              "validation": validation},
        summary=(f"Draft proposal {proposal['proposal_id']} created: "
                 f"{proposal['name']}, {proposal['audience_size']:,} recipients, "
                 f"validation {'passed' if validation['ok'] else 'FAILED'}. "
                 f"Awaiting human approval."))
