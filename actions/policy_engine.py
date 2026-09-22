"""Deterministic validation of a proposed campaign.

WHY THIS IS CODE AND NOT A PROMPT. A business rule has to be right every time,
be auditable afterwards, and give the same answer on the same input. A prompt
gives none of those three; a function gives all of them, costs nothing, and can
be unit-tested. The model may read the policy documents and reason about them --
that is what makes its explanation useful -- but nothing it concludes is load
bearing.

IT RUNS TWICE. Once when a proposal is validated, so the model can correct
itself, and again at execution, server-side, on the exact content that was
approved. The first run is a convenience; the second is the control. A validator
that only runs when someone remembers to ask is not a guarantee.

EVERY CHECK RETURNS A REASON, not a boolean. A proposal that fails should tell
whoever reads it -- model or human -- which rule, what the limit is, and what the
proposal asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from .audience import Audience, AudienceSpec, data_as_of
from .rules import (CONTACT_CAP_PER_WEEK, CONTACT_WINDOW_DAYS,
                    DISCOUNT_CEILING_PCT, HOLDOUT_REQUIRED_ABOVE, MAX_AUDIENCE,
                    MIN_AUDIENCE, MIN_HOLDOUT_PCT, POINTS_MULTIPLIER_CEILING,
                    SENIOR_APPROVAL_ABOVE, TIER_ORDER)

Severity = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    rule: str
    severity: Severity
    detail: str
    policy: str          # the document a reader can go and read


@dataclass
class Validation:
    checks: list[Check] = field(default_factory=list)
    audience_size: int = 0
    audience_description: str = ""
    requires_senior_approval: bool = False

    @property
    def ok(self) -> bool:
        return not any(c.severity == "fail" for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "fail"]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "audience_size": self.audience_size,
            "audience_description": self.audience_description,
            "requires_senior_approval": self.requires_senior_approval,
            "checks": [{"rule": c.rule, "severity": c.severity,
                        "detail": c.detail, "policy": c.policy}
                       for c in self.checks],
        }


CONTACT_SQL = """
    select count(*) from (
        select e.customer_id
        from lmart.campaign_events e
        join lmart.campaigns c on c.campaign_id = e.campaign_id
        where e.event_type = 'sent'
          and e.customer_id = any(%(ids)s)
          and c.sent_on > %(since)s
        group by e.customer_id
        having count(*) >= %(cap)s
    ) at_cap
"""


def validate(conn, *, offer_type: str, offer_value: float, holdout_pct: int,
             audience: Audience, spec: AudienceSpec,
             as_of: date | None = None) -> Validation:
    # Same anchor as audience resolution: a contact-frequency window measured
    # against the wall clock looks at a week in which nothing could have been
    # sent, and passes every time.
    today = as_of or data_as_of(conn)
    result = Validation(audience_size=audience.size,
                        audience_description=audience.description)
    add = result.checks.append

    # --- audience size -----------------------------------------------------
    if audience.size < MIN_AUDIENCE:
        add(Check("audience_minimum", "fail",
                  f"{audience.size} customers; the minimum is {MIN_AUDIENCE}, "
                  f"below which results cannot be measured. Widen the audience.",
                  "campaign-governance-policy"))
    elif audience.size > MAX_AUDIENCE:
        add(Check("audience_maximum", "fail",
                  f"{audience.size} customers exceeds the maximum of "
                  f"{MAX_AUDIENCE}. Narrow the audience.",
                  "campaign-governance-policy"))
    else:
        add(Check("audience_size", "pass",
                  f"{audience.size} customers, within {MIN_AUDIENCE}-"
                  f"{MAX_AUDIENCE}.", "campaign-governance-policy"))

    if audience.size > SENIOR_APPROVAL_ABOVE:
        result.requires_senior_approval = True
        add(Check("senior_approval", "warn",
                  f"{audience.size} recipients is above {SENIOR_APPROVAL_ABOVE}, "
                  f"so senior marketing approval is required in addition to the "
                  f"standard approval.", "campaign-governance-policy"))

    # --- offer within ceiling ---------------------------------------------
    if offer_type == "points_multiplier":
        if offer_value > POINTS_MULTIPLIER_CEILING:
            add(Check("points_multiplier_ceiling", "fail",
                      f"{offer_value}x exceeds the maximum of "
                      f"{POINTS_MULTIPLIER_CEILING}x.",
                      "campaign-governance-policy"))
        else:
            add(Check("points_multiplier_ceiling", "pass",
                      f"{offer_value}x is within the {POINTS_MULTIPLIER_CEILING}x "
                      f"maximum.", "campaign-governance-policy"))
    else:
        # The ceiling of the LOWEST tier in the audience applies, because the
        # offer reaches everyone in it. Requested tiers are not enough: an
        # audience with no tier filter contains Bronze members.
        present = [t for t in TIER_ORDER if t in audience.tiers_present]
        lowest = present[0] if present else "BRONZE"
        ceiling = DISCOUNT_CEILING_PCT[lowest]
        if offer_value > ceiling:
            add(Check("discount_ceiling", "fail",
                      f"{offer_value}% exceeds the {ceiling}% ceiling for "
                      f"{lowest}, the lowest tier present in this audience. "
                      f"Either lower the discount or exclude {lowest} members.",
                      "campaign-governance-policy"))
        else:
            add(Check("discount_ceiling", "pass",
                      f"{offer_value}% is within the {ceiling}% ceiling for "
                      f"{lowest}.", "campaign-governance-policy"))

    # --- contact frequency -------------------------------------------------
    if audience.customer_ids:
        since = today - timedelta(days=CONTACT_WINDOW_DAYS)
        at_cap = conn.execute(CONTACT_SQL,
                              {"ids": audience.customer_ids, "since": since,
                               "cap": CONTACT_CAP_PER_WEEK}).fetchone()[0]
        if at_cap:
            add(Check("contact_frequency", "fail",
                      f"{at_cap} of {audience.size} recipients have already "
                      f"received {CONTACT_CAP_PER_WEEK} messages in the last "
                      f"{CONTACT_WINDOW_DAYS} days. Reschedule, or exclude them.",
                      "campaign-governance-policy"))
        else:
            add(Check("contact_frequency", "pass",
                      f"No recipient is at the {CONTACT_CAP_PER_WEEK}-per-"
                      f"{CONTACT_WINDOW_DAYS}-day cap.",
                      "campaign-governance-policy"))

    # --- holdout -----------------------------------------------------------
    if audience.size > HOLDOUT_REQUIRED_ABOVE and holdout_pct < MIN_HOLDOUT_PCT:
        add(Check("holdout", "fail",
                  f"An audience of {audience.size} needs a holdout of at least "
                  f"{MIN_HOLDOUT_PCT}% to measure incremental effect; "
                  f"{holdout_pct}% was proposed.",
                  "guideline-campaign-measurement"))
    else:
        add(Check("holdout", "pass",
                  f"Holdout {holdout_pct}%." if holdout_pct else
                  f"No holdout required below {HOLDOUT_REQUIRED_ABOVE} recipients.",
                  "guideline-campaign-measurement"))

    # --- consent and dormancy, reported rather than checked ---------------
    # These cannot fail: the audience resolver already removed them. Surfacing
    # the counts is what lets a reviewer see that the rule was applied, instead
    # of trusting that it was.
    add(Check("consent", "pass",
              f"{audience.excluded_no_consent} customer(s) excluded for lacking "
              f"marketing consent.", "campaign-governance-policy"))
    if audience.excluded_dormant_gb:
        add(Check("gb_dormancy", "pass",
                  f"{audience.excluded_dormant_gb} GB customer(s) excluded as "
                  f"dormant.", "gb-marketing-compliance"))

    return result
