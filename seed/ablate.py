"""Measure how much each planted cause actually contributes to the headline drop.

WHY THIS EXISTS: docs/planted-truths.md is the answer key the agent will be
graded against. Writing "the campaign stop is the largest cause" from intuition
would make the answer key a guess -- and an eval is only as good as its key.

This rebuilds the dataset with one mechanism disabled at a time and reports how
much of the decline disappears. That is a counterfactual, which is exactly the
attribution question a human analyst could never actually answer on real data.
Here we can, because we own the data-generating process.

Each run diverges in its random stream once behaviour differs, so single-run
deltas carry sampling noise. Averaging over several seeds keeps the ranking
stable; the spread across seeds is reported so the noise is visible rather than
hidden.

Deliberately does NOT share aggregation code with seed/verify.py. verify.py
checks the shipped CSVs independently; this works in memory on relative deltas.
Keeping them separate means a mistake in one cannot silently validate the other.

Run:  python -m seed.ablate
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from . import config, generate

# Five seeds: with a ~250-member cohort, three left contributions of a few
# points-per-seed indistinguishable from sampling noise.
SEEDS = [20260921, 424242, 70707, 13131, 909090]


def _months(start: date, end: date) -> list[str]:
    out, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


FOCUS = _months(config.FOCUS_PERIOD_START, config.FOCUS_PERIOD_END)
PRIOR = _months(date(2025, 6, 1), date(2025, 8, 31))


def headline(customers, tables) -> tuple[float, float]:
    """Year-over-year change for GB Gold, on TWO metrics, as percentages.

    Returns (orders per member per month, net revenue per member per month).

    Two metrics because "engagement" is not one number, and the planted causes
    do not all land on the same one: a stopped campaign costs visits, a stockout
    costs basket value. Measuring only one would make a real cause look like
    noise -- which is exactly the mistake the dataset is designed to punish.

    'GB Gold' is resolved from tier_history at each point in time -- the same way
    the agent will have to resolve it, and the reason the headline moves for
    reasons that are not behavioural at all.
    """
    spans = defaultdict(list)
    account_customer = {c.account_id: c.customer_id
                        for c in customers if c.account_id is not None}
    for row in tables.tier_history:
        spans[account_customer[row[1]]].append((
            date.fromisoformat(row[3]),
            date.fromisoformat(row[4]) if row[4] else None,
            row[2],
        ))

    def tier_on(customer_id: int, when: date) -> str | None:
        for start, end, tier in spans.get(customer_id, ()):
            if start <= when and (end is None or when < end):
                return tier
        return None

    country = {c.customer_id: c.country_code for c in customers}
    orders_by = defaultdict(int)
    revenue_by = defaultdict(int)
    for row in tables.orders:
        key = (row[4][:7], row[1])
        orders_by[key] += 1
        revenue_by[key] += row[7]

    def rates(months: list[str], as_at: date) -> tuple[float, float]:
        cohort = [c.customer_id for c in customers
                  if country[c.customer_id] == "GB"
                  and tier_on(c.customer_id, as_at) == "GOLD"]
        if not cohort:
            return 0.0, 0.0
        denominator = len(cohort) * len(months)
        pairs = [(m, cid) for m in months for cid in cohort]
        return (sum(orders_by.get(k, 0) for k in pairs) / denominator,
                sum(revenue_by.get(k, 0) for k in pairs) / denominator)

    now_orders, now_revenue = rates(FOCUS, config.FOCUS_PERIOD_END)
    old_orders, old_revenue = rates(PRIOR, date(2025, 8, 31))
    return (
        (now_orders - old_orders) / old_orders * 100 if old_orders else 0.0,
        (now_revenue - old_revenue) / old_revenue * 100 if old_revenue else 0.0,
    )


# Each entry disables exactly one mechanism by overriding config constants.
FAR_FUTURE = date(2099, 1, 1)
ABLATIONS = {
    "C1 campaign stop":     {"CAMPAIGN_LAST_SEND": config.HISTORY_END},
    "C2 Beauty stockout":   {"BEAUTY_STOCKOUT_FROM": FAR_FUTURE},
    "C3 tier review":       {"TIER_REVIEW_PROMOTE_FRACTION": 0.0},
    # Kept in the study even though it is classified as a red herring: the
    # answer key asserts it explains nothing, and that claim needs evidence.
    "R3 points policy":     {"POINTS_POLICY_CHANGE_DATE": FAR_FUTURE},
}


def run_with(overrides: dict, seed: int) -> tuple[float, float]:
    original = {k: getattr(config, k) for k in overrides}
    for key, value in overrides.items():
        setattr(config, key, value)
    try:
        customers, _, tables = generate.build(seed=seed, quiet=True)
        return headline(customers, tables)
    finally:
        for key, value in original.items():
            setattr(config, key, value)


def main() -> int:
    print(f"Ablation study: GB Gold orders/member/month, "
          f"{FOCUS[0]}..{FOCUS[-1]} vs {PRIOR[0]}..{PRIOR[-1]}")
    print(f"Averaged over {len(SEEDS)} seeds.\n")

    base = [run_with({}, s) for s in SEEDS]
    base_orders = statistics.mean(v[0] for v in base)
    base_revenue = statistics.mean(v[1] for v in base)
    print(f"  baseline (all causes active)    "
          f"orders {base_orders:+6.1f}%    revenue {base_revenue:+6.1f}%\n")
    print("  with one cause DISABLED -- the gap is that cause's contribution:\n")
    print(f"    {'cause':<22} {'orders/member':>22}   {'revenue/member':>22}")

    results = []
    for label, overrides in ABLATIONS.items():
        values = [run_with(overrides, s) for s in SEEDS]
        mean_orders = statistics.mean(v[0] for v in values)
        mean_revenue = statistics.mean(v[1] for v in values)
        spread = statistics.pstdev([v[0] for v in values])
        results.append((label, mean_orders - base_orders,
                        mean_revenue - base_revenue, spread))

    for label, d_orders, d_revenue, spread in sorted(results, key=lambda r: -r[1]):
        print(f"    {label:<22} {d_orders:+8.1f}pp (sd {spread:4.1f})   "
              f"{d_revenue:+8.1f}pp")

    print("\n  Contributions do not sum to the headline: causes interact, and the")
    print("  composition effect changes which people the other causes are measured on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
