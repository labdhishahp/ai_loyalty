"""Assert that every planted signal is actually present in the generated data.

This is the first eval in the project, and it grades the DATASET, not the agent.
A seed whose intended causes are too weak to detect, or whose red herrings are
too weak to mislead, would make every downstream measurement meaningless -- the
agent could look right or wrong for reasons that have nothing to do with the agent.

Deliberately reads the CSVs and does its own aggregation rather than querying
Postgres. If it used the same SQL the metrics layer will use, a shared
misunderstanding of the schema would pass both checks.

Run:  python -m seed.verify
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date

from . import config

FAILURES: list[str] = []


def check(label: str, passed: bool, detail: str) -> None:
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {label}\n         {detail}")
    if not passed:
        FAILURES.append(label)


def load(name: str) -> list[dict]:
    with (config.OUT_DIR / f"{name}.csv").open() as handle:
        return list(csv.DictReader(handle))


def month_of(iso: str) -> str:
    return iso[:7]


def pct(new: float, old: float) -> float:
    return (new - old) / old * 100 if old else 0.0


def main() -> int:
    if not (config.OUT_DIR / "orders.csv").exists():
        print("No generated data. Run: python -m seed.generate", file=sys.stderr)
        return 1

    customers = load("customers")
    accounts = load("loyalty_accounts")
    tier_history = load("tier_history")
    orders = load("orders")
    order_items = load("order_items")
    products = load("products")
    categories = load("categories")
    campaigns = load("campaigns")
    campaign_events = load("campaign_events")
    points = load("points_ledger")

    country_of = {int(c["customer_id"]): c["country_code"] for c in customers}
    account_customer = {int(a["account_id"]): int(a["customer_id"]) for a in accounts}
    category_of_product = {
        int(p["product_id"]): int(p["category_id"]) for p in products}
    category_code = {int(c["category_id"]): c["code"] for c in categories}

    # --- tier as at a date, from tier_history (never from current_tier) -------
    spans_by_customer: dict[int, list[tuple[date, date | None, str]]] = defaultdict(list)
    for row in tier_history:
        customer_id = account_customer[int(row["account_id"])]
        spans_by_customer[customer_id].append((
            date.fromisoformat(row["effective_from"]),
            date.fromisoformat(row["effective_to"]) if row["effective_to"] else None,
            row["tier_code"],
        ))

    def tier_on(customer_id: int, when: date) -> str | None:
        for start, end, tier in spans_by_customer.get(customer_id, ()):
            if start <= when and (end is None or when < end):
                return tier
        return None

    # --- order aggregates ----------------------------------------------------
    orders_by_month_customer: dict[tuple[str, int], int] = defaultdict(int)
    revenue_by_month_customer: dict[tuple[str, int], int] = defaultdict(int)
    for row in orders:
        key = (month_of(row["ordered_at"]), int(row["customer_id"]))
        orders_by_month_customer[key] += 1
        revenue_by_month_customer[key] += int(row["net_amount_minor"])

    def cohort_stats(months: list[str], members: list[int]) -> tuple[float, float]:
        """Return (active rate, orders per member per month) for a cohort."""
        if not members or not months:
            return 0.0, 0.0
        active = 0
        total_orders = 0
        for month in months:
            for customer_id in members:
                count = orders_by_month_customer.get((month, customer_id), 0)
                total_orders += count
                if count:
                    active += 1
        denominator = len(members) * len(months)
        return active / denominator, total_orders / denominator

    def months_in(start: date, end: date) -> list[str]:
        out, year, month = [], start.year, start.month
        while (year, month) <= (end.year, end.month):
            out.append(f"{year:04d}-{month:02d}")
            month += 1
            if month == 13:
                year, month = year + 1, 1
        return out

    focus = months_in(config.FOCUS_PERIOD_START, config.FOCUS_PERIOD_END)
    prior_year = months_in(
        date(config.FOCUS_PERIOD_START.year - 1, config.FOCUS_PERIOD_START.month, 1),
        date(config.FOCUS_PERIOD_END.year - 1, config.FOCUS_PERIOD_END.month, 28))

    print("\nL-Mart seed verification")
    print(f"  focus period      {focus[0]} .. {focus[-1]}")
    print(f"  YoY comparison    {prior_year[0]} .. {prior_year[-1]}\n")

    # =====================================================================
    # HEADLINE: GB Gold engagement is measurably down year over year.
    # =====================================================================
    gb_gold_now = [
        int(c["customer_id"]) for c in customers
        if c["country_code"] == "GB"
        and tier_on(int(c["customer_id"]), config.FOCUS_PERIOD_END) == "GOLD"
    ]
    gb_gold_prior = [
        int(c["customer_id"]) for c in customers
        if c["country_code"] == "GB"
        and tier_on(int(c["customer_id"]), date(2025, 8, 31)) == "GOLD"
    ]
    active_now, opm_now = cohort_stats(focus, gb_gold_now)
    active_prior, opm_prior = cohort_stats(prior_year, gb_gold_prior)
    drop = pct(opm_now, opm_prior)
    check(
        "HEADLINE  GB Gold order frequency down year-over-year",
        drop <= -20,
        f"orders/member/month {opm_prior:.3f} -> {opm_now:.3f} ({drop:+.1f}%), "
        f"active rate {active_prior:.1%} -> {active_now:.1%}, "
        f"cohort size {len(gb_gold_prior)} -> {len(gb_gold_now)}",
    )

    # Checked separately because "engagement" is not one number. The causes do
    # not all land on the same metric, so a dataset that moved only one of them
    # would quietly make half the answer key unreachable.
    def cohort_revenue(months: list[str], members: list[int]) -> float:
        if not members or not months:
            return 0.0
        total = sum(revenue_by_month_customer.get((m, c), 0)
                    for m in months for c in members)
        return total / (len(members) * len(months))

    rev_now = cohort_revenue(focus, gb_gold_now)
    rev_prior = cohort_revenue(prior_year, gb_gold_prior)
    rev_drop = pct(rev_now, rev_prior)
    check(
        "HEADLINE  GB Gold spend per member down year-over-year",
        rev_drop <= -20,
        f"net revenue/member/month GBP {rev_prior / 100:,.2f} -> "
        f"{rev_now / 100:,.2f} ({rev_drop:+.1f}%)",
    )

    # =====================================================================
    # R1 (red herring): GB overall revenue is UP. A country-level check alone
    # would conclude nothing is wrong.
    # =====================================================================
    gb_customers = [int(c["customer_id"]) for c in customers if c["country_code"] == "GB"]
    gb_now = sum(revenue_by_month_customer.get((m, c), 0)
                 for m in focus for c in gb_customers)
    gb_prior = sum(revenue_by_month_customer.get((m, c), 0)
                   for m in prior_year for c in gb_customers)
    check(
        "R1  GB total revenue is UP while GB Gold is down (masking)",
        gb_now > gb_prior,
        f"GB net revenue GBP {gb_prior / 100:,.0f} -> {gb_now / 100:,.0f} "
        f"({pct(gb_now, gb_prior):+.1f}%)",
    )

    # =====================================================================
    # C1: the reactivation programme stopped after May 2026.
    # =====================================================================
    react = [c for c in campaigns if c["programme"] == config.CAMPAIGN_PROGRAMME]
    react_months = sorted({c["sent_on"][:7] for c in react})
    react_in_focus = [m for m in react_months if m in focus]
    sent_by_campaign: dict[int, int] = defaultdict(int)
    for event in campaign_events:
        if event["event_type"] == "sent":
            sent_by_campaign[int(event["campaign_id"])] += 1
    react_audience = [sent_by_campaign[int(c["campaign_id"])] for c in react]
    check(
        "C1  UK Gold Reactivation ran monthly then stopped before the focus period",
        len(react) >= 18 and not react_in_focus,
        f"{len(react)} waves, {react_months[0]} .. {react_months[-1]}, "
        f"0 in focus period; audience per wave "
        f"min {min(react_audience)} / median "
        f"{sorted(react_audience)[len(react_audience) // 2]} / max {max(react_audience)}",
    )

    # ... and that a control programme did NOT stop, so "marketing stopped" is wrong.
    digest_months = sorted({c["sent_on"][:7] for c in campaigns
                            if c["programme"] == config.DIGEST_PROGRAMME})
    check(
        "C1b Control programme kept running through the focus period",
        all(m in digest_months for m in focus),
        f"{config.DIGEST_PROGRAMME}: {len(digest_months)} waves, "
        f"last {digest_months[-1]}",
    )

    # =====================================================================
    # C2: GB Beauty revenue collapses from April 2026.
    # =====================================================================
    order_country = {int(o["order_id"]): country_of[int(o["customer_id"])] for o in orders}
    order_month = {int(o["order_id"]): month_of(o["ordered_at"]) for o in orders}
    beauty_id = next(int(c["category_id"]) for c in categories if c["code"] == "BEAUTY")
    gb_beauty_by_month: dict[str, int] = defaultdict(int)
    for item in order_items:
        order_id = int(item["order_id"])
        if order_country[order_id] != "GB":
            continue
        if category_of_product[int(item["product_id"])] != beauty_id:
            continue
        gb_beauty_by_month[order_month[order_id]] += int(item["line_amount_minor"])
    before = [gb_beauty_by_month.get(m, 0) for m in ("2026-01", "2026-02", "2026-03")]
    after = [gb_beauty_by_month.get(m, 0) for m in focus]
    beauty_change = pct(sum(after) / len(after), sum(before) / len(before))
    check(
        "C2  GB Beauty revenue collapsed from the stockout date",
        beauty_change <= -40,
        f"GBP/month {sum(before) / len(before) / 100:,.0f} (Jan-Mar) -> "
        f"{sum(after) / len(after) / 100:,.0f} (focus) ({beauty_change:+.1f}%)",
    )

    # =====================================================================
    # C3: composition effect. A FIXED cohort declines much less than the
    # "currently Gold" population -- because the best members left the tier.
    # =====================================================================
    fixed_cohort = [
        int(c["customer_id"]) for c in customers
        if c["country_code"] == "GB"
        and tier_on(int(c["customer_id"]), date(2026, 1, 15)) == "GOLD"
    ]
    _, opm_fixed_now = cohort_stats(focus, fixed_cohort)
    _, opm_fixed_prior = cohort_stats(prior_year, fixed_cohort)
    fixed_drop = pct(opm_fixed_now, opm_fixed_prior)
    promotions = [
        r for r in tier_history
        if r["reason"] == "upgrade"
        and r["effective_from"] == config.TIER_REVIEW_DATE.isoformat()
    ]
    check(
        "C3  Composition effect present, but a real behaviour change remains",
        fixed_drop > drop + 8 and fixed_drop <= -8,
        f"fixed Jan-2026 Gold cohort {fixed_drop:+.1f}% vs currently-Gold "
        f"{drop:+.1f}%: {fixed_drop - drop:+.1f}pp is composition, "
        f"{fixed_drop:+.1f}pp is genuine; "
        f"{len(promotions)} promotions on {config.TIER_REVIEW_DATE}",
    )

    # =====================================================================
    # C4: a points expiry wave in early 2026.
    # =====================================================================
    expiry_by_month: dict[str, int] = defaultdict(int)
    for entry in points:
        if entry["entry_type"] == "expire":
            expiry_by_month[month_of(entry["occurred_at"])] += -int(entry["points"])
    expiry_months = sorted(expiry_by_month)
    peak = max(expiry_by_month, key=lambda m: expiry_by_month[m]) if expiry_by_month else None
    # The previous version of this check accepted any peak in 2026-0*, which a
    # steadily rising trend satisfies. Expiries grow with earnings, so a trend is
    # the null result -- the assertion must be that there is a discrete SPIKE on
    # the policy change date, several times any ordinary month.
    others = sorted(v for m, v in expiry_by_month.items()
                    if m != config.POINTS_POLICY_CHANGE_DATE.strftime("%Y-%m"))
    typical = others[len(others) // 2] if others else 0
    spike = expiry_by_month.get(config.POINTS_POLICY_CHANGE_DATE.strftime("%Y-%m"), 0)
    check(
        "R3  Points expiry is a discrete spike on the policy change date",
        peak == config.POINTS_POLICY_CHANGE_DATE.strftime("%Y-%m")
        and typical > 0 and spike >= 3 * typical,
        f"{spike:,} points expired in {peak} vs a typical month of "
        f"{typical:,} ({spike / typical:.1f}x)" if typical else "no baseline",
    )

    # =====================================================================
    # R2 (red herring): a global channel shift, affecting everyone equally.
    # =====================================================================
    channel_by_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in orders:
        channel_by_month[month_of(row["ordered_at"])][row["channel"]] += 1

    def app_share(month: str) -> float:
        counts = channel_by_month[month]
        total = sum(counts.values())
        return counts["app"] / total if total else 0.0

    check(
        "R2  Global app share rose during the focus period (affects all cohorts)",
        app_share(focus[-1]) - app_share("2026-05") > 0.05,
        f"app share 2026-05 {app_share('2026-05'):.1%} -> "
        f"{focus[-1]} {app_share(focus[-1]):.1%}",
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All planted signals verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
