"""Every tunable parameter of the L-Mart dataset, in one file.

The constants below are not arbitrary. Each one under "PLANTED CAUSAL
STRUCTURE" encodes a specific business event that the AI employee is meant to
discover. docs/planted-truths.md explains what each is and which gold question
it answers. If you change a value here, re-run `python -m seed.verify` -- it
asserts that the intended signals are still present and strong enough to find.
"""

from __future__ import annotations

from datetime import date

# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------
# A fixed seed is a correctness requirement, not a convenience. Evals compare
# agent runs against a known answer key; if the data moved between runs, a score
# change would be uninterpretable.
RANDOM_SEED = 20260921

CUSTOMER_COUNT = 5_000

# Two full years, ending at the last complete month before "today" (2026-09).
# Two years is the minimum that supports year-over-year comparison, which is the
# *correct* method for a seasonal business -- see SEASONALITY below.
HISTORY_START = date(2024, 9, 1)
HISTORY_END = date(2026, 8, 31)

# The question the system is being built to answer is scoped to this window.
FOCUS_PERIOD_START = date(2026, 6, 1)
FOCUS_PERIOD_END = date(2026, 8, 31)


# --------------------------------------------------------------------------
# Population shape
# --------------------------------------------------------------------------
COUNTRIES = {"GB": 0.40, "IN": 0.30, "AE": 0.15, "SG": 0.15}

CITIES = {
    "GB": ["London", "Manchester", "Birmingham", "Leeds", "Glasgow"],
    "IN": ["Mumbai", "Bengaluru", "Delhi", "Hyderabad", "Pune"],
    "AE": ["Dubai", "Abu Dhabi", "Sharjah"],
    "SG": ["Singapore"],
}

# Tier mix at enrolment. Gold is ~18% so that GB Gold (~0.40 * 0.18 * 5000 = 360)
# is a large enough cohort for period-over-period analysis to be statistically
# meaningful rather than noise.
TIER_MIX = {"BRONZE": 0.45, "SILVER": 0.30, "GOLD": 0.18, "PLATINUM": 0.07}

# How much more often a tier shops, relative to Silver. Higher tiers are more
# engaged -- which is what makes the composition effect (C3) bite.
TIER_ORDER_RATE = {"BRONZE": 0.70, "SILVER": 1.00, "GOLD": 1.50, "PLATINUM": 2.20}

# Not every customer joins the loyalty programme. Non-members give "engagement"
# a meaningful denominator instead of being a universal property.
LOYALTY_ENROLMENT_RATE = 0.82

# Share of customers acquired BEFORE the history window opens. Year-over-year
# cohort comparison is only meaningful if most members existed a year ago; if
# sign-ups were spread evenly across the window, a "cohort shrank/grew" finding
# would mostly be an artefact of acquisition timing rather than behaviour.
PRE_WINDOW_SIGNUP_SHARE = 0.85

MARKETING_OPT_IN_RATE = 0.88

# Lognormal parameters for a customer's intrinsic daily order probability.
# mu chosen so the mean lands near 0.030/day (~11 orders/year at tier 1.0).
BASE_RATE_MU = -3.687
BASE_RATE_SIGMA = 0.60
MAX_DAILY_ORDER_PROB = 0.35

CATEGORIES = ["GROCERY", "BEAUTY", "ELECTRONICS", "APPAREL", "HOME", "BABY"]

# Baseline share of basket lines by category, before per-customer affinity.
CATEGORY_WEIGHTS = {
    "GROCERY": 0.34,
    "BEAUTY": 0.16,
    "ELECTRONICS": 0.10,
    "APPAREL": 0.18,
    "HOME": 0.15,
    "BABY": 0.07,
}

# Monthly demand multiplier. Real retail is seasonal, and this is deliberate:
# it means a naive "last 3 months vs previous 3 months" comparison is confounded,
# and the correct method is year-over-year. An agent that gets this wrong should
# be penalised by the eval -- so the trap has to exist in the data.
SEASONALITY = {
    1: 0.82, 2: 0.86, 3: 0.95, 4: 0.98, 5: 1.02, 6: 1.06,
    7: 1.10, 8: 1.08, 9: 0.97, 10: 1.02, 11: 1.22, 12: 1.35,
}

# Baseline channel mix before the mid-2026 shift (R2).
CHANNEL_MIX_BASE = {"store": 0.52, "web": 0.18, "app": 0.30}


# ==========================================================================
# PLANTED CAUSAL STRUCTURE
#
# The headline question is:
#   "Why has engagement among Gold customers in the UK dropped over the
#    last three months?"
#
# The honest answer is multi-causal. Four real causes of differing size, plus
# two red herrings that a careless investigator will mistake for causes.
# ==========================================================================

# --- C1: the reactivation programme stopped (largest ACTIONABLE cause) -----
# A monthly email to lapsed GB Gold members ran for 21 months and then stopped
# after May 2026 when its budget was reallocated. Discoverable as an absence:
# there are simply no campaign waves after 2026-05-05.
CAMPAIGN_PROGRAMME = "UK Gold Reactivation"
CAMPAIGN_FIRST_SEND = date(2024, 9, 5)
CAMPAIGN_LAST_SEND = date(2026, 5, 5)
CAMPAIGN_SEND_DAY = 5                 # monthly, on the 5th
CAMPAIGN_LAPSED_DAYS = 21             # targets members with no order in 21 days
CAMPAIGN_LIFT = 1.85                  # order-rate multiplier ...
CAMPAIGN_LIFT_WINDOW_DAYS = 18        # ... for 18 days after a send
CAMPAIGN_DELIVERY_RATE = 0.97
CAMPAIGN_OPEN_RATE = 0.34             # of delivered
CAMPAIGN_CLICK_RATE = 0.28            # of opened
CAMPAIGN_UNSUB_RATE = 0.004           # of delivered

# --- C2: UK Beauty supply failure (second largest) -------------------------
# From April 2026 a majority of Beauty SKUs became unavailable in GB. GB Gold
# members over-index on Beauty (see GOLD_GB_BEAUTY_AFFINITY), so this hits them
# harder than any other cohort. Discoverable as a collapse in GB Beauty revenue
# from April onward.
#
# Measured contribution (seed/ablate.py): +5.5pp on orders per member, +6.9pp on
# revenue per member. Slightly larger on spend, as expected -- but it moves both,
# because a basket that empties entirely becomes a trip that never happened.
#
# (An earlier comment here claimed the effect was "large on revenue and small on
# orders". Ablation did not support that, and the comment was corrected rather
# than the constant tuned to fit it.)
BEAUTY_STOCKOUT_FROM = date(2026, 4, 1)
BEAUTY_STOCKOUT_COUNTRY = "GB"
BEAUTY_STOCKOUT_SKU_FRACTION = 0.65   # share of Beauty SKUs unavailable
GOLD_GB_BEAUTY_AFFINITY = 2.6         # GB Gold buy Beauty 2.6x the base weight

# --- C3: composition effect (a trap, not a behaviour change) ---------------
# In February 2026 L-Mart ran a tier review and promoted the highest-value Gold
# members to Platinum. Those members were the most frequent shoppers, so Gold's
# *average* engagement fell without any individual shopping less. Only visible
# via tier_history: a fixed cohort (whoever was Gold in Jan 2026) shows a much
# smaller decline than the "currently Gold" population does.
TIER_REVIEW_DATE = date(2026, 2, 1)
TIER_REVIEW_PROMOTE_FRACTION = 0.16   # top share of GB Gold, by intrinsic rate

# --- R3: retroactive points-expiry policy change (RED HERRING) -------------
# On 2026-02-01 L-Mart shortened points validity from 18 months to 12, and
# applied it RETROACTIVELY. Everything earned before 2025-02-01 expired at once.
# This is a discrete, dated business event -- a one-off spike in the ledger --
# rather than a gradual trend, which is what makes it findable at all.
#
# It falls hardest on long-tenured savers, and because Gold earns points at 1.5x
# they had the largest balances to lose. It is the single most dramatic-looking
# event in the dataset -- hundreds of thousands of points vanishing in one day,
# four months before the decline.
#
# It has NO measurable effect on engagement.
#
# That is deliberate, and it was measured, not assumed: seed/ablate.py showed a
# contribution of -0.5pp, indistinguishable from noise. Rather than tune it up
# into a cause, it is kept as the hardest red herring in the dataset. The most
# salient event, correlated with the timing, that nonetheless explains nothing.
# An agent that names it as the cause has confused magnitude with relevance --
# and that is the single most common failure in real analysis.
#
# (An earlier version of this seed modelled expiry as a flat 18-month rule. That
# produces expiries that grow smoothly with earnings, so there is no event to
# discover -- only a trend that any cohort would show. A policy change with a
# date is both more realistic and actually detectable.)
POINTS_EXPIRY_MONTHS = 18             # validity before the policy change
POINTS_EXPIRY_MONTHS_AFTER = 12       # validity after it
POINTS_POLICY_CHANGE_DATE = date(2026, 2, 1)
POINTS_PER_MINOR = 100                # 1 point per GBP 1.00 spent
REDEMPTION_BLOCK_POINTS = 1000        # redeemed in blocks of 1000 ...
REDEMPTION_BLOCK_VALUE_MINOR = 1000   # ... worth GBP 10.00
REDEMPTION_PROBABILITY = 0.18         # per order, when balance allows
# No behavioural multiplier: the expiry changes balances, not behaviour.

# --- Control programme (not a cause) --------------------------------------
# A monthly rewards digest to every opted-in member, in every country, which
# keeps running right through the focus period. It exists so that "the campaign
# stopped" cannot be answered vaguely: marketing did NOT stop, one specific
# reactivation programme did. It also has NO lift -- its apparent conversions
# are orders that would have happened anyway, which is an honest trap for an
# agent that reads conversion rate as proof of effectiveness.
DIGEST_PROGRAMME = "Global Rewards Digest"
DIGEST_SEND_DAY = 12
DIGEST_ATTRIBUTION_WINDOW_DAYS = 7
DIGEST_DELIVERY_RATE = 0.96
DIGEST_OPEN_RATE = 0.22
DIGEST_CLICK_RATE = 0.09

# --- R1: masking (red herring) --------------------------------------------
# Total GB revenue is UP over the focus period, because Silver and Platinum grew.
# An agent that checks only country-level totals will conclude nothing is wrong.
NON_GOLD_GROWTH_PER_MONTH = 0.012     # compounding monthly uplift, Silver+Platinum

# --- R2: channel shift (red herring) --------------------------------------
# A global app relaunch moved share from store to app in mid-2026. Real, visible,
# and correlated with the timing -- but it affects every country and tier
# equally, so it cannot explain a GB-Gold-specific decline. Ruling it out
# requires a comparison, not just an observation.
APP_SHIFT_START = date(2026, 6, 1)
APP_SHIFT_END = date(2026, 8, 31)
APP_SHIFT_MAGNITUDE = 0.15            # share moved from store to app, globally


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
import pathlib  # noqa: E402  (kept next to the constant it defines)

OUT_DIR = pathlib.Path(__file__).parent / "out"
