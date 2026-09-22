"""Write L-Mart's knowledge corpus to markdown files.

WHY A SCRIPT RATHER THAN HAND-EDITED FILES. The corpus has to stay consistent
with two other things: the causal structure planted in seed/config.py, and the
rules Milestone 3's validator enforces. A policy that says 15% while the
validator enforces 12% is worse than no policy -- the agent would cite a rule
the system does not apply. Generating both from one place makes that
contradiction hard to introduce.

The documents themselves are real prose, not templates: retrieval quality
depends on them reading like documents someone wrote.

Run:  python -m knowledge.build_corpus
"""

from __future__ import annotations

import json
import pathlib
import textwrap

CORPUS = pathlib.Path(__file__).parent / "corpus"

# Discount ceilings by tier. The single source of truth for both the policy
# prose below and actions/policy_engine.py.
DISCOUNT_CEILING_PCT = {"BRONZE": 10, "SILVER": 12, "GOLD": 15, "PLATINUM": 20}
POINTS_MULTIPLIER_CEILING = 3.0
CONTACT_CAP_PER_WEEK = 2
MIN_AUDIENCE = 50
MAX_AUDIENCE = 50_000
SENIOR_APPROVAL_ABOVE = 10_000
DORMANCY_MONTHS_GB = 24

DOCS: list[dict] = [

# ---------------------------------------------------------------- policies
{"slug": "loyalty-programme-policy", "title": "L-Mart Loyalty Programme Policy",
 "doc_type": "policy", "jurisdiction": "GLOBAL", "effective_from": "2024-01-01",
 "body": f"""
## Purpose
This policy defines the L-Mart loyalty programme: how membership works, how
tiers are earned, and how points are accrued and redeemed.

## Tiers
There are four tiers: Bronze, Silver, Gold and Platinum. Tier is determined by
trailing twelve-month net spend measured in GBP. Thresholds are Bronze from the
first qualifying purchase, Silver at GBP 500, Gold at GBP 1,500 and Platinum at
GBP 4,000.

## Points accrual
Members earn one point per GBP 1.00 of net spend, after discounts. Higher tiers
earn at a multiplier: Bronze and Silver at 1.0x, Gold at 1.5x, Platinum at 2.0x.
Points accrue on the order date and are available immediately.

## Redemption
Points are redeemed in blocks of 1,000, each worth GBP 10.00 against a future
order. A member may redeem one block per order. Redemption consumes the oldest
points first.

## Validity
Points expire after a fixed period from the date they were earned. The current
validity period is set by the Points Validity Policy, which is maintained
separately because it has changed.

## Membership
Enrolment is optional and free. A customer may shop without being a member;
non-members earn no points and are not subject to this policy.
"""},

{"slug": "points-validity-policy-2026", "title": "Points Validity Policy (2026)",
 "doc_type": "policy", "jurisdiction": "GLOBAL", "effective_from": "2026-02-01",
 "supersedes": "points-validity-policy-2024",
 "body": """
## Change summary
With effect from 1 February 2026, loyalty points expire **twelve months** after
the date they were earned. The previous validity period was eighteen months.

## Retrospective application
The shortened period applies to all points outstanding on the effective date,
not only to points earned after it. Points earned before 1 February 2025 were
therefore expired in a single sweep on the effective date.

## Rationale
The change was made to bring the liability carried on the balance sheet in line
with the industry norm and to reduce the cost of the outstanding points float.
Finance estimated the one-off reduction in liability at approximately GBP 60,000.

## Member communication
Affected members were notified in the January 2026 statement email. No
compensating credit was issued.

## Known concerns raised at approval
Loyalty Operations noted that retrospective application would fall hardest on
long-tenured members in higher tiers, who accrue at a multiplier and therefore
carry larger balances. The concern was recorded and the change proceeded.
"""},

{"slug": "points-validity-policy-2024", "title": "Points Validity Policy (2024)",
 "doc_type": "policy", "jurisdiction": "GLOBAL", "effective_from": "2024-01-01",
 "effective_to": "2026-02-01",
 "body": """
## Validity period
Loyalty points expire eighteen months after the date they were earned.

## Expiry processing
Expiry is processed at the start of each calendar month. Members are notified
thirty days before points are due to expire.

**This policy has been superseded. See the Points Validity Policy (2026).**
"""},

{"slug": "tier-review-policy", "title": "Loyalty Tier Review Policy",
 "doc_type": "policy", "jurisdiction": "GLOBAL", "effective_from": "2024-01-01",
 "body": """
## Continuous qualification
A member's tier is recalculated whenever a qualifying purchase is made. A member
who crosses a threshold is upgraded with effect from that date.

## Periodic review
In addition, Loyalty Operations runs a **tier review** in February each year.
The review identifies members whose trailing twelve-month spend places them
above their current tier's threshold and promotes them in a single batch. This
exists because continuous qualification can lag for members whose spend is
seasonal.

## Effect on reporting
A batch promotion moves members between tiers on one date. Tier-level averages
therefore change on that date **without any member changing their behaviour**:
the members promoted out of a tier are by construction its highest-spending
members, so the tier they leave sees its per-member averages fall and the tier
they join sees its averages fall as well.

Analysts comparing a tier across periods that span a review must resolve tier
membership at a fixed point in time, or they will attribute a composition change
to a behavioural one. This is the most common reporting error associated with
the review and it recurs every year.

## Demotion
Members are not demoted at review. Demotion occurs only at the annual
anniversary of enrolment and only where trailing spend has fallen below the
tier threshold for two consecutive quarters.
"""},

{"slug": "campaign-governance-policy", "title": "Campaign Governance Policy",
 "doc_type": "policy", "jurisdiction": "GLOBAL", "effective_from": "2025-06-01",
 "body": f"""
## Scope
This policy governs every outbound marketing campaign: email, push and SMS. It
applies to all markets. Regional policies may be stricter but never weaker.

## Consent
A campaign may only target customers who hold marketing consent. Customers who
have unsubscribed must be excluded at audience resolution, not filtered at send
time.

## Contact frequency
No customer may receive more than **{CONTACT_CAP_PER_WEEK} marketing messages in
any rolling seven-day period**, across all programmes and all channels. A
proposed campaign whose audience would breach this cap must be reduced or
rescheduled.

## Discount ceilings
The maximum percentage discount that may be offered, by tier, is:

| Tier | Maximum discount |
| ---- | ---------------- |
| Bronze | {DISCOUNT_CEILING_PCT['BRONZE']}% |
| Silver | {DISCOUNT_CEILING_PCT['SILVER']}% |
| Gold | {DISCOUNT_CEILING_PCT['GOLD']}% |
| Platinum | {DISCOUNT_CEILING_PCT['PLATINUM']}% |

Where a campaign targets several tiers, the ceiling of the **lowest** tier in the
audience applies.

## Points multipliers
A points multiplier offer may not exceed **{POINTS_MULTIPLIER_CEILING}x** base
earn. A points multiplier may not be combined with a percentage discount in the
same campaign; the two are alternatives, not a stack.

## Audience size
A campaign must target at least **{MIN_AUDIENCE}** customers, below which results
cannot be measured, and no more than **{MAX_AUDIENCE}**. Any campaign above
**{SENIOR_APPROVAL_ABOVE}** recipients requires senior marketing approval in
addition to the standard approval.

## Approval
Every campaign requires human approval before execution. Approval is recorded
against the exact content approved; any subsequent change voids it.
"""},

{"slug": "gb-marketing-compliance", "title": "GB Marketing Compliance Standard",
 "doc_type": "policy", "jurisdiction": "GB", "effective_from": "2025-01-01",
 "body": f"""
## Applicability
This standard applies to all marketing sent to customers resident in Great
Britain and is stricter than the global Campaign Governance Policy where the two
differ.

## Dormancy
Customers with **no purchase in the preceding {DORMANCY_MONTHS_GB} months** may
not be sent marketing. Consent is treated as stale after that period and must be
refreshed before contact resumes.

## Unsubscribe
Every message must carry a working one-click unsubscribe. Unsubscribe requests
take effect immediately and apply across all programmes.

## Record keeping
The audience of every campaign, and the consent state of each recipient at the
time of sending, must be retained for twenty-four months.
"""},

{"slug": "in-marketing-compliance", "title": "India Marketing Compliance Standard",
 "doc_type": "policy", "jurisdiction": "IN", "effective_from": "2025-01-01",
 "body": """
## Applicability
Applies to customers resident in India.

## Timing
Commercial SMS and push notifications may only be sent between 09:00 and 21:00
India Standard Time. Email is not time-restricted.

## Registration
SMS sender identifiers and message templates must be registered before use.
Unregistered templates are rejected by the aggregator and count against the
sender's reputation.
"""},

{"slug": "ae-sg-marketing-compliance",
 "title": "UAE and Singapore Marketing Compliance Standard",
 "doc_type": "policy", "jurisdiction": "AE", "effective_from": "2025-01-01",
 "body": """
## Applicability
Applies to customers resident in the United Arab Emirates and Singapore.

## Consent
Consent must be explicit and specific to the channel. Consent to email does not
imply consent to SMS.

## Do-not-call
Singapore numbers must be checked against the national Do Not Call registry
before any SMS campaign. The check is valid for thirty days.
"""},
# -------------------------------------------------------- memos & incidents
{"slug": "fy27-marketing-budget-reallocation",
 "title": "FY27 Marketing Budget Reallocation",
 "doc_type": "memo", "jurisdiction": "GLOBAL", "effective_from": "2026-05-20",
 "body": """
## Decision
The FY27 marketing plan reallocates spend from retention programmes to new
customer acquisition, following the board's growth target for the year.

## Programmes affected
The following recurring programmes are discontinued with effect from the June
2026 cycle:

- **UK Gold Reactivation** (monthly email to lapsed Gold members in GB). Last
  wave sent 5 May 2026.
- Ireland Silver Winback (quarterly). Last wave sent April 2026.

The Global Rewards Digest is unaffected and continues on its monthly cadence.

## Basis for the decision
Retention programmes were ranked by cost per incremental order. UK Gold
Reactivation ranked mid-table on that measure. The paper noted that its
measured incremental lift was materially higher than the Digest's, but its
per-recipient cost was also higher because of the smaller, more targeted
audience.

## Risk accepted
Loyalty Operations flagged that discontinuing UK Gold Reactivation would remove
the only lapse-triggered contact for the GB Gold segment, and that no
replacement was planned. The risk was accepted on the basis that the segment's
absolute revenue contribution is small relative to acquisition upside. No
monitoring was put in place to detect the effect.
"""},

{"slug": "gb-beauty-supply-incident-2026",
 "title": "Incident Report: GB Beauty Availability, April 2026",
 "doc_type": "memo", "jurisdiction": "GB", "effective_from": "2026-04-14",
 "body": """
## Summary
A change of distributor for the Beauty and Personal Care category left a
majority of Beauty SKUs unavailable in Great Britain from the first week of
April 2026. The gap was expected to last four weeks; as of the date of this
report it is ongoing.

## Scope
Approximately two thirds of the GB Beauty range is affected, including the
fastest-moving skincare and haircare lines. Other categories and other markets
are unaffected.

## Commercial effect observed
GB Beauty revenue has fallen sharply. Merchandising also reports a second-order
effect: Beauty is frequently a basket anchor rather than a standalone purchase,
so some affected customers are not substituting within the basket but are
deferring the trip entirely. The effect on trip frequency is therefore expected
to exceed the direct category loss.

## Customers most exposed
Segments that over-index on Beauty are disproportionately affected. Loyalty
Operations identified GB Gold members as the most Beauty-weighted cohort in the
GB base.

## Status
A replacement distributor is contracted from Q4 2026. No customer communication
has been issued.
"""},

{"slug": "app-relaunch-june-2026", "title": "Mobile App Relaunch, June 2026",
 "doc_type": "memo", "jurisdiction": "GLOBAL", "effective_from": "2026-06-01",
 "body": """
## Summary
The rebuilt L-Mart app was released in all markets in June 2026, replacing the
previous version. Adoption has been strong and app share of orders has risen
substantially across every market and every loyalty tier.

## Effect on reporting
Channel mix has shifted from store to app globally. Because the shift is uniform
across markets and tiers, it does not explain differences BETWEEN segments. Any
analysis attributing a segment-specific change to the app relaunch should first
check whether the same shift is present in segments that did not change.

## Not affected
Total order volume is unchanged by the relaunch; the effect is on where orders
are placed, not whether they happen.
"""},

# ------------------------------------------------------------- playbooks
{"slug": "playbook-lapsed-member-reactivation",
 "title": "Playbook: Reactivating Lapsed Loyalty Members",
 "doc_type": "playbook", "jurisdiction": "GLOBAL", "effective_from": "2025-03-01",
 "body": """
## When to use
A cohort's active rate has fallen while the cohort's composition is unchanged,
and the fall is concentrated in members who have not purchased recently.

## Recommended offer
A **points multiplier** is the preferred instrument for lapsed high-tier members.
It costs less per redemption than a percentage discount, it rewards a return
trip rather than discounting a trip that would have happened anyway, and it does
not train the segment to wait for markdowns.

A percentage discount is preferred only where the lapse is price-driven, for
example after a competitor promotion.

## Audience definition
Target members with no purchase in 21 to 90 days. Members lapsed beyond 90 days
respond materially worse and should be handled by the winback playbook. Check
the dormancy rule in the relevant market before including long-lapsed members.

## Cadence
Monthly is the tested cadence. More frequent contact does not improve response
and consumes the contact frequency allowance that other programmes need.

## Measurement
Hold out a random ten percent of the audience and measure incremental orders
against that holdout. Do not measure this campaign by its conversion rate; see
the note on incrementality in the campaign measurement guideline.
"""},

{"slug": "playbook-category-disruption",
 "title": "Playbook: Responding to a Category Availability Gap",
 "doc_type": "playbook", "jurisdiction": "GLOBAL", "effective_from": "2025-09-01",
 "body": """
## When to use
A category has become unavailable in a market and customers who bought it
regularly are reducing their overall purchase frequency, not merely substituting.

## Priority
Identify the cohort that over-indexes on the affected category before doing
anything else. A category gap that looks small in aggregate can be severe for a
specific segment.

## Recommended response
Substitution-led, not discount-led. Promote the nearest available alternatives to
the affected cohort and, where the gap is expected to persist, say so plainly
rather than repeatedly promoting an empty shelf.

A discount does not fix an availability problem and trains the segment to expect
one. Use a discount only to retain the trip, never to sell the missing category.

## What not to do
Do not run a broad category promotion in the affected market while availability
is impaired: it drives traffic to a page that cannot convert and depresses
measured campaign performance for months afterward.
"""},

{"slug": "playbook-tier-retention", "title": "Playbook: Retaining a Declining Tier",
 "doc_type": "playbook", "jurisdiction": "GLOBAL", "effective_from": "2025-03-01",
 "body": """
## Before acting
Establish whether the tier's decline is behavioural or compositional. If members
were promoted out of the tier in the period, per-member averages will fall with
no behaviour change and a campaign will address a problem that does not exist.
Re-measure on a cohort fixed at a date before any tier movement.

## If the decline is behavioural
Segment by recency. Recently active members need relevance, not incentive;
lapsed members need a reason to return. Sending one offer to both wastes margin
on the first group.

## If the decline is compositional
No campaign is warranted. Report the composition effect and, if the tier's
absolute contribution matters, measure the promoted members in their new tier.
"""},

# ------------------------------------------------------------ guidelines
{"slug": "guideline-campaign-measurement",
 "title": "Guideline: Measuring Campaign Effectiveness",
 "doc_type": "guideline", "jurisdiction": "GLOBAL", "effective_from": "2025-03-01",
 "body": """
## Conversion rate is not effectiveness
A campaign's conversion rate counts recipients who purchased within the
attribution window. It does not distinguish purchases the campaign CAUSED from
purchases that would have happened anyway. A broad campaign sent to an active
audience will always show a healthy conversion rate, because active customers
buy regardless.

Broadcast programmes sent to the whole member base are the usual offenders: they
report the highest conversion rates in the portfolio and frequently have close to
zero incremental effect.

## Measure incrementality
Hold out a random portion of the eligible audience and compare. Where a holdout
was not run, compare the same audience's behaviour in the weeks following a send
against equivalent weeks with no send.

## Absence of a campaign
When a recurring programme stops, nothing in the campaign records marks the
stop; there are simply no further waves. Any analysis of a change in engagement
should check which programmes were sending before the period and which were
sending during it.
"""},

{"slug": "guideline-cohort-analysis",
 "title": "Guideline: Cohort Analysis and Composition Effects",
 "doc_type": "guideline", "jurisdiction": "GLOBAL", "effective_from": "2025-03-01",
 "body": """
## The question to ask first
When a group's average changes, ask whether the group changed before asking
whether its members changed. Any attribute that can move over time -- loyalty
tier, segment membership, market -- redefines the group as it moves.

## Two readings
"Gold members" can mean whoever is Gold today, or whoever was Gold at a chosen
past date. Measured across a period containing tier movement, the two give
different answers. Neither is wrong; reporting one without saying which is.

## Method
Measure both. Hold the cohort fixed at a date before any known movement, and
re-resolve it per period. The difference between the two is the composition
effect, and it should be reported as a number rather than described.

## Seasonality
Compare like periods. L-Mart's business is strongly seasonal, with November and
December well above trend and January and February below it. Comparing a quarter
against the immediately preceding quarter confounds the change under
investigation with the time of year; compare against the same period a year
earlier.
"""},

# ------------------------------------------------------------ postmortems
{"slug": "postmortem-gb-gold-reactivation-2025",
 "title": "Post-mortem: UK Gold Reactivation, 2025 Programme Year",
 "doc_type": "postmortem", "jurisdiction": "GB", "effective_from": "2026-01-15",
 "body": """
## Programme
Monthly email to GB Gold members with no purchase in the preceding 21 days,
offering a points multiplier on their next order.

## Result
Measured against a ten percent holdout across twelve waves, the programme
produced a clear positive lift in orders among recipients in the eighteen days
following each send. Lift was consistent month to month and did not decay over
the programme year.

## Cost
Cost per incremental order was above the portfolio median, driven by the small
audience per wave rather than by the offer itself.

## Recommendation
Continue. Consider widening the lapse window from 21 to 30 days to increase
audience size and improve cost per incremental order, and test a second
touchpoint for non-openers.
"""},

{"slug": "postmortem-global-rewards-digest-2025",
 "title": "Post-mortem: Global Rewards Digest, 2025",
 "doc_type": "postmortem", "jurisdiction": "GLOBAL", "effective_from": "2026-01-15",
 "body": """
## Programme
Monthly email to every consenting loyalty member in every market, summarising
points balance and current offers.

## Result
The Digest reports the highest conversion rate of any programme in the
portfolio. Measured against a holdout, its **incremental** effect was not
distinguishable from zero.

The explanation is audience composition: the Digest goes to the entire member
base, most of whom are active and would have purchased regardless. Its
conversion rate measures the base's purchase rate, not the campaign's effect.

## Recommendation
Retain as a service communication. Do not cite its conversion rate as evidence
of marketing effectiveness, and do not use it as the comparison when evaluating
targeted programmes.
"""},

{"slug": "postmortem-beauty-bundle-2025",
 "title": "Post-mortem: GB Beauty Bundle Promotion, Autumn 2025",
 "doc_type": "postmortem", "jurisdiction": "GB", "effective_from": "2025-11-30",
 "body": """
## Programme
A 15% discount on Beauty bundles to GB Gold and Platinum members.

## Result
Strong response. GB Gold over-indexes heavily on Beauty relative to other GB
segments, and the campaign confirmed that: response among GB Gold was more than
double the GB average.

## Note for future planning
The same concentration is a risk as well as an opportunity. Any disruption to
Beauty availability in GB would affect this segment disproportionately.
"""},

{"slug": "postmortem-points-expiry-communication",
 "title": "Post-mortem: 2026 Points Expiry Communication",
 "doc_type": "postmortem", "jurisdiction": "GLOBAL", "effective_from": "2026-04-30",
 "body": """
## Background
The Points Validity Policy change of February 2026 expired a large volume of
outstanding points in a single sweep.

## Complaints
Complaint volume rose in February and returned to baseline by April. Complaints
were concentrated among long-tenured members in higher tiers, as predicted.

## Behavioural effect
Loyalty Operations tested whether affected members reduced purchase frequency
in the following quarter. **No measurable effect was found.** Members who lost
points continued to purchase at the same rate as comparable members who did not.

The conclusion recorded at the time: the expiry was a customer experience
failure and a communication failure, but not a demand event. Analyses of
engagement changes in 2026 should not attribute them to the expiry without
evidence specific to that claim.

## Recommendation
Pre-notify at ninety days for any future validity change, and do not apply a
shortened period retrospectively.
"""},

{"slug": "guideline-brand-voice", "title": "Guideline: Brand Voice in Campaigns",
 "doc_type": "guideline", "jurisdiction": "GLOBAL", "effective_from": "2025-01-01",
 "body": """
## Tone
Plain, warm and specific. Say what the offer is in the first line. Avoid
urgency language that is not true -- no countdowns on offers that are not
expiring, and no scarcity claims without scarcity.

## Naming
Campaign names follow `<Market> <Segment> <Purpose> - <Month Year>`, for example
"UK Gold Reactivation - May 2026". Names appear in reporting and must be
readable a year later.

## Claims
Any numeric claim in creative must be traceable to a source. Never state a
points balance or a discount in creative that the offer does not actually apply.
"""},

{"slug": "playbook-campaign-proposal-checklist",
 "title": "Playbook: Campaign Proposal Checklist",
 "doc_type": "playbook", "jurisdiction": "GLOBAL", "effective_from": "2025-06-01",
 "body": """
Before a campaign proposal goes for approval, confirm:

1. **The problem is real.** The metric it addresses has been measured, and a
   composition effect has been ruled out.
2. **The audience is defined by a rule**, not a list, and the rule is stated in
   terms an analyst can re-run.
3. **Consent** is respected at audience resolution, not at send time.
4. **The contact frequency cap** is not breached for any recipient.
5. **The offer is within the discount or multiplier ceiling** for the lowest
   tier in the audience.
6. **A holdout** is defined, unless the audience is below the measurement
   minimum.
7. **The expected cost** is stated, with the assumption behind it.
8. **The success measure** is incremental, not conversion rate.
"""},

]

def write() -> None:
    CORPUS.mkdir(exist_ok=True)
    for doc in DOCS:
        meta = {k: v for k, v in doc.items() if k != "body"}
        path = CORPUS / f"{doc['slug']}.md"
        path.write_text(
            "---\n" + json.dumps(meta, indent=2) + "\n---\n"
            + textwrap.dedent(doc["body"]).strip() + "\n")
    print(f"wrote {len(DOCS)} documents to {CORPUS}")


if __name__ == "__main__":
    write()
