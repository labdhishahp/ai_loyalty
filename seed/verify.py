"""Verify the loaded dataset in Postgres.

Two sections, and the split matters:

  A. STRUCTURAL INTEGRITY -- does the data obey its own rules?
     Do order headers equal the sum of their lines? Does any points balance ever
     go negative? Does every account have exactly one open tier row?
     These checks are only possible against the loaded database, and they are
     the reason this file stopped working on CSV files. A generator bug in the
     FIFO points logic, or a header/line arithmetic slip, is invisible in a flat
     file and obvious here.

  B. PLANTED SIGNALS -- is the intended causal structure present and findable?
     This is the first eval in the project, and it grades the DATASET, not the
     agent. A seed whose causes are too weak to detect, or whose red herrings
     are too weak to mislead, would make every downstream measurement
     meaningless: the agent could look right or wrong for reasons that have
     nothing to do with the agent.

ON THE RISK OF A TAUTOLOGY: this file writes its own SQL and will share no code
with the metrics layer, so a misunderstanding of the schema cannot pass both by
being made once. More importantly, the numbers it asserts against come from
independent sources -- the generator's own constants in seed/config.py, and the
counterfactual attribution measured by seed/ablate.py -- not from these queries.

Run:  python -m seed.verify
"""

from __future__ import annotations

import os
import sys
from datetime import date

import psycopg
from dotenv import load_dotenv

from . import config

FAILURES: list[str] = []

FOCUS_START = config.FOCUS_PERIOD_START                 # 2026-06-01
FOCUS_END = date(2026, 9, 1)                            # exclusive
PRIOR_START = date(2025, 6, 1)
PRIOR_END = date(2025, 9, 1)                            # exclusive
MONTHS = 3


def check(label: str, passed: bool, detail: str) -> None:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}\n         {detail}")
    if not passed:
        FAILURES.append(label)


def pct(new: float, old: float) -> float:
    return (new - old) / old * 100 if old else 0.0


# ---------------------------------------------------------------------------
# A. Structural integrity
# ---------------------------------------------------------------------------
# Each query returns a count of violations, which must be zero.

INTEGRITY = [
    ("Order headers equal the sum of their lines", """
        select count(*) from (
            select o.order_id
            from lmart.orders o
            join lmart.order_items oi on oi.order_id = o.order_id
            group by o.order_id, o.gross_amount_minor
            having o.gross_amount_minor <> sum(oi.line_amount_minor)
        ) violations
    """),
    ("net = gross - discount on every order", """
        select count(*) from lmart.orders
        where net_amount_minor <> gross_amount_minor - discount_minor
    """),
    ("No order exists without lines", """
        select count(*) from lmart.orders o
        where not exists (
            select 1 from lmart.order_items oi where oi.order_id = o.order_id)
    """),
    # The end-to-end test of the FIFO points-lot logic. If redemption ever spent
    # points that were not there, or expiry removed points twice, a running
    # balance goes negative somewhere.
    ("No points balance ever goes negative", """
        select count(*) from (
            select sum(points) over (
                partition by account_id order by occurred_at, entry_id) as balance
            from lmart.points_ledger
        ) running where balance < 0
    """),
    ("Every account has exactly one open tier_history row", """
        select count(*) from (
            select account_id, count(*) filter (where effective_to is null) as open_rows
            from lmart.tier_history group by account_id
        ) per_account where open_rows <> 1
    """),
    ("No account has overlapping tier_history spans", """
        select count(*)
        from lmart.tier_history a
        join lmart.tier_history b
          on a.account_id = b.account_id
         and a.tier_history_id < b.tier_history_id
        where a.effective_from < coalesce(b.effective_to, 'infinity'::date)
          and b.effective_from < coalesce(a.effective_to, 'infinity'::date)
    """),
    ("order_id is set on 'converted' events and only those", """
        select count(*) from lmart.campaign_events
        where (event_type = 'converted') <> (order_id is not null)
    """),
    ("Every conversion falls inside its campaign's attribution window", """
        select count(*)
        from lmart.campaign_events e
        join lmart.campaigns c on c.campaign_id = e.campaign_id
        join lmart.orders o on o.order_id = e.order_id
        where e.event_type = 'converted'
          and (o.ordered_at::date < c.sent_on
               or o.ordered_at::date > c.sent_on + interval '18 days')
    """),
]


# ---------------------------------------------------------------------------
# B. Planted signals
# ---------------------------------------------------------------------------

# "GB Gold" resolved from tier_history at a point in time -- never from
# loyalty_accounts.current_tier. Which of the two you use is worth ~21
# percentage points, which is the whole lesson of this dataset.
COHORT_METRICS = """
    with cohort as (
        select distinct c.customer_id
        from lmart.customers c
        join lmart.loyalty_accounts la on la.customer_id = c.customer_id
        join lmart.tier_history th on th.account_id = la.account_id
        where c.country_code = 'GB'
          and th.tier_code = 'GOLD'
          and th.effective_from <= %(as_of)s
          and (th.effective_to is null or th.effective_to > %(as_of)s)
    )
    select
        (select count(*) from cohort)                as members,
        count(o.order_id)                            as orders,
        coalesce(sum(o.net_amount_minor), 0)         as revenue
    from cohort
    left join lmart.orders o
           on o.customer_id = cohort.customer_id
          and o.ordered_at >= %(start)s
          and o.ordered_at <  %(end)s
"""


def cohort_metrics(cur, as_of: date, start: date, end: date):
    """Return (members, orders per member per month, revenue per member per month)."""
    cur.execute(COHORT_METRICS, {"as_of": as_of, "start": start, "end": end})
    members, orders, revenue = cur.fetchone()
    if not members:
        return 0, 0.0, 0.0
    denominator = members * MONTHS
    return members, orders / denominator, float(revenue) / denominator


def main() -> int:
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set. Copy .env.example to .env first.",
              file=sys.stderr)
        return 1

    with psycopg.connect(url) as conn:
        # Every timestamp is stored as timestamptz and every boundary below is a
        # bare date. Pinning the session to UTC makes that cast deterministic
        # rather than dependent on whatever the server's default happens to be.
        conn.execute("set time zone 'UTC'")
        cur = conn.cursor()

        if cur.execute("select count(*) from lmart.orders").fetchone()[0] == 0:
            print("No data loaded. Run: python -m seed.load --reset", file=sys.stderr)
            return 1

        print("\nL-Mart dataset verification")
        print(f"  focus period      {FOCUS_START} .. {FOCUS_END} (exclusive)")
        print(f"  YoY comparison    {PRIOR_START} .. {PRIOR_END} (exclusive)")

        print("\nA. Structural integrity")
        for label, sql in INTEGRITY:
            violations = cur.execute(sql).fetchone()[0]
            check(label, violations == 0, f"{violations} violation(s)")

        print("\nB. Planted signals")

        # --- headline ------------------------------------------------------
        members_now, orders_now, revenue_now = cohort_metrics(
            cur, FOCUS_START, FOCUS_START, FOCUS_END)
        members_then, orders_then, revenue_then = cohort_metrics(
            cur, PRIOR_START, PRIOR_START, PRIOR_END)
        orders_drop = pct(orders_now, orders_then)
        revenue_drop = pct(revenue_now, revenue_then)

        check(
            "HEADLINE  GB Gold order frequency down year-over-year",
            orders_drop <= -20,
            f"orders/member/month {orders_then:.3f} -> {orders_now:.3f} "
            f"({orders_drop:+.1f}%), cohort {members_then} -> {members_now}",
        )
        # Checked separately because "engagement" is not one number, and the
        # planted causes do not all land on the same one.
        check(
            "HEADLINE  GB Gold spend per member down year-over-year",
            revenue_drop <= -20,
            f"net revenue/member/month GBP {revenue_then / 100:,.2f} -> "
            f"{revenue_now / 100:,.2f} ({revenue_drop:+.1f}%)",
        )

        # --- R1: country totals are UP, masking the problem ----------------
        gb_revenue = """
            select coalesce(sum(o.net_amount_minor), 0)
            from lmart.orders o
            join lmart.customers c on c.customer_id = o.customer_id
            where c.country_code = 'GB'
              and o.ordered_at >= %s and o.ordered_at < %s
        """
        gb_now = cur.execute(gb_revenue, (FOCUS_START, FOCUS_END)).fetchone()[0]
        gb_then = cur.execute(gb_revenue, (PRIOR_START, PRIOR_END)).fetchone()[0]
        check(
            "R1  GB total revenue is UP while GB Gold is down (masking)",
            gb_now > gb_then,
            f"GB net revenue GBP {gb_then / 100:,.0f} -> {gb_now / 100:,.0f} "
            f"({pct(float(gb_now), float(gb_then)):+.1f}%)",
        )

        # --- C1: the reactivation programme stopped ------------------------
        waves = cur.execute("""
            select to_char(c.sent_on, 'YYYY-MM') as month,
                   count(*) filter (where e.event_type = 'sent') as audience
            from lmart.campaigns c
            left join lmart.campaign_events e on e.campaign_id = c.campaign_id
            where c.programme = %s
            group by c.campaign_id, c.sent_on
            order by c.sent_on
        """, (config.CAMPAIGN_PROGRAMME,)).fetchall()
        wave_months = [row[0] for row in waves]
        audiences = sorted(row[1] for row in waves)
        in_focus = [m for m in wave_months if m >= FOCUS_START.strftime("%Y-%m")]
        check(
            "C1  UK Gold Reactivation ran monthly then stopped before the focus period",
            len(waves) >= 18 and not in_focus,
            f"{len(waves)} waves, {wave_months[0]} .. {wave_months[-1]}, "
            f"0 in focus period; audience per wave min {audiences[0]} / "
            f"median {audiences[len(audiences) // 2]} / max {audiences[-1]}",
        )

        digest_months = [r[0] for r in cur.execute("""
            select distinct to_char(sent_on, 'YYYY-MM')
            from lmart.campaigns where programme = %s order by 1
        """, (config.DIGEST_PROGRAMME,)).fetchall()]
        focus_months = ["2026-06", "2026-07", "2026-08"]
        check(
            "C1b Control programme kept running through the focus period",
            all(m in digest_months for m in focus_months),
            f"{config.DIGEST_PROGRAMME}: {len(digest_months)} waves, "
            f"last {digest_months[-1]}",
        )

        # --- C2: the GB Beauty stockout ------------------------------------
        beauty = dict(cur.execute("""
            select to_char(o.ordered_at, 'YYYY-MM') as month,
                   sum(oi.line_amount_minor)
            from lmart.orders o
            join lmart.customers c  on c.customer_id = o.customer_id
            join lmart.order_items oi on oi.order_id = o.order_id
            join lmart.products p   on p.product_id = oi.product_id
            join lmart.categories cat on cat.category_id = p.category_id
            where c.country_code = %s and cat.code = 'BEAUTY'
            group by 1
        """, (config.BEAUTY_STOCKOUT_COUNTRY,)).fetchall())
        before = [float(beauty.get(m, 0)) for m in ("2026-01", "2026-02", "2026-03")]
        after = [float(beauty.get(m, 0)) for m in focus_months]
        beauty_change = pct(sum(after) / 3, sum(before) / 3)
        check(
            "C2  GB Beauty revenue collapsed from the stockout date",
            beauty_change <= -40,
            f"GBP/month {sum(before) / 300:,.0f} (Jan-Mar) -> "
            f"{sum(after) / 300:,.0f} (focus) ({beauty_change:+.1f}%)",
        )

        # --- C3: composition effect ----------------------------------------
        # Same query, one parameter different: resolve the cohort as it stood in
        # January 2026 instead of today. That single change is the discriminator.
        _, fixed_now, _ = cohort_metrics(cur, date(2026, 1, 15), FOCUS_START, FOCUS_END)
        _, fixed_then, _ = cohort_metrics(cur, date(2026, 1, 15), PRIOR_START, PRIOR_END)
        fixed_drop = pct(fixed_now, fixed_then)
        promotions = cur.execute("""
            select count(*) from lmart.tier_history
            where reason = 'upgrade' and effective_from = %s
        """, (config.TIER_REVIEW_DATE,)).fetchone()[0]
        check(
            "C3  Composition effect present, but a real behaviour change remains",
            fixed_drop > orders_drop + 8 and fixed_drop <= -8,
            f"fixed Jan-2026 Gold cohort {fixed_drop:+.1f}% vs currently-Gold "
            f"{orders_drop:+.1f}%: {fixed_drop - orders_drop:+.1f}pp is composition, "
            f"{fixed_drop:+.1f}pp is genuine; {promotions} promotions on "
            f"{config.TIER_REVIEW_DATE}",
        )

        # --- R3: the points expiry spike -----------------------------------
        expiries = dict(cur.execute("""
            select to_char(occurred_at, 'YYYY-MM'), -sum(points)
            from lmart.points_ledger where entry_type = 'expire' group by 1
        """).fetchall())
        policy_month = config.POINTS_POLICY_CHANGE_DATE.strftime("%Y-%m")
        others = sorted(float(v) for m, v in expiries.items() if m != policy_month)
        typical = others[len(others) // 2] if others else 0.0
        spike = float(expiries.get(policy_month, 0))
        peak = max(expiries, key=lambda m: expiries[m]) if expiries else None
        check(
            "R3  Points expiry is a discrete spike on the policy change date",
            peak == policy_month and typical > 0 and spike >= 3 * typical,
            f"{spike:,.0f} points expired in {peak} vs a typical month of "
            f"{typical:,.0f} ({spike / typical:.1f}x)" if typical else "no baseline",
        )

        # --- R2: the global channel shift ----------------------------------
        shares = dict(cur.execute("""
            select to_char(ordered_at, 'YYYY-MM'),
                   count(*) filter (where channel = 'app')::numeric / count(*)
            from lmart.orders group by 1
        """).fetchall())
        check(
            "R2  Global app share rose during the focus period (affects all cohorts)",
            float(shares["2026-08"]) - float(shares["2026-05"]) > 0.05,
            f"app share 2026-05 {float(shares['2026-05']):.1%} -> "
            f"2026-08 {float(shares['2026-08']):.1%}",
        )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("Structural integrity holds and all planted signals verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
