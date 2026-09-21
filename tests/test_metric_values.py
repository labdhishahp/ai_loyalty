"""Do the metrics reproduce the known answers? Requires Postgres.

Every expected value here traces to docs/planted-truths.md, which was measured
by seed/ablate.py rather than asserted. These are therefore not regression tests
against whatever the code currently returns -- they check the metrics against
facts established independently of them.

Assertions are stated as ranges and relationships rather than exact floats. The
claims that matter ("the decline is real", "most of it is composition", "the
stockout is visible in one category") should survive a change to the seed size
or a tweak to a generator constant; an exact float would not, and would train
everyone to re-baseline instead of to think.
"""

from __future__ import annotations

from datetime import date

import pytest

from metrics import catalog
from metrics.cohort import COUNTRIES, PERIOD_END, TIERS, CohortSpec
from metrics.engine import MONTH, MetricRequest, execute, scalar

FOCUS = (date(2026, 6, 1), date(2026, 9, 1))
PRIOR = (date(2025, 6, 1), date(2025, 9, 1))
EARLY_2026 = (date(2026, 1, 1), date(2026, 4, 1))
WHOLE_HISTORY = (date(2024, 9, 1), date(2026, 9, 1))

GB = ("GB",)
GOLD = ("GOLD",)
# The date the answer key uses to pin the cohort before the February tier review.
BEFORE_TIER_REVIEW = date(2026, 1, 15)


def run(conn, metric, period, cohort=None, **kwargs):
    return execute(conn, MetricRequest(
        metric=metric, period_start=period[0], period_end=period[1],
        cohort=cohort or CohortSpec(), **kwargs))


def change(new: float, old: float) -> float:
    return (new - old) / old * 100


# ---------------------------------------------------------------------------
# The reference constants are a cache of what is in the database. Prove it.
# ---------------------------------------------------------------------------

def test_reference_constants_match_the_database(conn):
    tiers = {r[0] for r in conn.execute(
        "select tier_code from lmart.loyalty_tiers").fetchall()}
    countries = {r[0] for r in conn.execute(
        "select distinct country_code from lmart.customers").fetchall()}
    assert tiers == set(TIERS)
    assert countries == set(COUNTRIES)


# ---------------------------------------------------------------------------
# The headline, on both metrics
# ---------------------------------------------------------------------------

def test_gb_gold_declined_on_frequency_and_on_spend(conn):
    moving = CohortSpec(countries=GB, tiers=GOLD, tier_as_of=PERIOD_END)
    for metric in ("orders_per_member", "revenue_per_member"):
        now = run(conn, metric, FOCUS, moving)
        then = run(conn, metric, PRIOR, moving)
        drop = change(scalar(now), scalar(then))
        print(f"\n  {metric}: {scalar(then):.3f} -> {scalar(now):.3f} ({drop:+.1f}%)")
        assert drop <= -20, f"{metric} fell only {drop:.1f}%"


def test_the_gold_cohort_itself_shrank(conn):
    """Composition's first fingerprint, visible before any behaviour metric."""
    moving = CohortSpec(countries=GB, tiers=GOLD, tier_as_of=PERIOD_END)
    now = run(conn, "cohort_size", FOCUS, moving)
    then = run(conn, "cohort_size", PRIOR, moving)
    print(f"\n  GB Gold cohort: {then.cohort_size} -> {now.cohort_size}")
    assert now.cohort_size < then.cohort_size


# ---------------------------------------------------------------------------
# C3: the composition effect -- the same query, one parameter different
# ---------------------------------------------------------------------------

def test_a_fixed_cohort_declines_much_less_than_a_moving_one(conn):
    moving = CohortSpec(countries=GB, tiers=GOLD, tier_as_of=PERIOD_END)
    fixed = CohortSpec(countries=GB, tiers=GOLD, tier_as_of=BEFORE_TIER_REVIEW)

    moving_drop = change(scalar(run(conn, "orders_per_member", FOCUS, moving)),
                         scalar(run(conn, "orders_per_member", PRIOR, moving)))
    fixed_drop = change(scalar(run(conn, "orders_per_member", FOCUS, fixed)),
                        scalar(run(conn, "orders_per_member", PRIOR, fixed)))
    composition = fixed_drop - moving_drop

    print(f"\n  moving cohort {moving_drop:+.1f}%  fixed cohort {fixed_drop:+.1f}%"
          f"  -> {composition:+.1f}pp is composition")
    assert composition >= 8, "the composition effect has gone missing"
    assert fixed_drop <= -8, "no real behaviour change is left to explain"


def test_the_tier_review_shows_up_as_a_cluster_of_upgrades(conn):
    result = run(conn, "tier_changes", EARLY_2026,
                 CohortSpec(countries=GB), granularity=MONTH)
    upgrades = [r for r in result.rows
                if r["reason"] == "upgrade" and r["to_tier"] == "PLATINUM"]
    february = [r for r in upgrades if r["period"] == date(2026, 2, 1)]
    print(f"\n  GB upgrades to Platinum by month: "
          f"{[(str(r['period']), r['value']) for r in upgrades]}")
    assert february, "no Platinum upgrades in February 2026"
    assert february[0]["value"] >= max(
        (r["value"] for r in upgrades if r["period"] != date(2026, 2, 1)), default=0)


# ---------------------------------------------------------------------------
# C1: the programme that stopped, and the one that did not
# ---------------------------------------------------------------------------

def test_the_reactivation_programme_has_no_waves_in_the_focus_period(conn):
    result = run(conn, "campaign_funnel", WHOLE_HISTORY, CohortSpec(),
                 granularity=MONTH, filters={"programme": "UK Gold Reactivation"})
    months = sorted({r["period"] for r in result.rows})
    in_focus = [m for m in months if m >= date(2026, 6, 1)]
    print(f"\n  reactivation waves: {months[0]} .. {months[-1]} ({len(months)} months)")
    assert len(months) >= 18
    assert not in_focus, "the programme did not stop"


def test_the_control_programme_kept_running(conn):
    """So that 'marketing stopped' is a wrong answer, not a vague one."""
    result = run(conn, "campaign_funnel", WHOLE_HISTORY, CohortSpec(),
                 granularity=MONTH, filters={"programme": "Global Rewards Digest"})
    months = {r["period"] for r in result.rows}
    assert {date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)} <= months


# ---------------------------------------------------------------------------
# C2 and the red herrings
# ---------------------------------------------------------------------------

def test_gb_beauty_revenue_collapsed(conn):
    def beauty(period):
        rows = run(conn, "revenue_by_category", period, CohortSpec(countries=GB)).rows
        return next(r["value"] for r in rows if r["category"] == "BEAUTY")

    drop = change(beauty(FOCUS), beauty(EARLY_2026))
    print(f"\n  GB Beauty revenue {drop:+.1f}% vs Jan-Mar")
    assert drop <= -40


def test_gb_total_revenue_rose_while_gb_gold_fell(conn):
    """R1: the masking effect. A country-level check alone concludes nothing
    is wrong."""
    now = scalar(run(conn, "total_revenue", FOCUS, CohortSpec(countries=GB)))
    then = scalar(run(conn, "total_revenue", PRIOR, CohortSpec(countries=GB)))
    print(f"\n  GB total revenue {change(now, then):+.1f}%")
    assert now > then


def test_app_share_rose_globally(conn):
    """R2: real, well-timed, and irrelevant -- it moves every cohort equally."""
    def app_share(period):
        rows = run(conn, "orders_by_channel", period).rows
        total = sum(r["value"] for r in rows)
        return next(r["value"] for r in rows if r["channel"] == "app") / total

    print(f"\n  app share {app_share(PRIOR):.1%} -> {app_share(FOCUS):.1%}")
    assert app_share(FOCUS) - app_share(PRIOR) > 0.05


def test_points_expiry_is_a_spike_not_a_trend(conn):
    """R3: the most dramatic event in the data, and it explains nothing."""
    rows = run(conn, "points_expired", WHOLE_HISTORY, CohortSpec(),
               granularity=MONTH).rows
    by_month = {r["period"]: r["value"] for r in rows}
    spike = by_month[date(2026, 2, 1)]
    others = sorted(v for m, v in by_month.items() if m != date(2026, 2, 1))
    typical = others[len(others) // 2]
    print(f"\n  points expired Feb 2026: {spike:,.0f} vs typical {typical:,.0f} "
          f"({spike / typical:.1f}x)")
    assert spike == max(by_month.values())
    assert spike >= 3 * typical


# ---------------------------------------------------------------------------
# Every metric runs against the real schema
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("definition", catalog.DEFINITIONS, ids=lambda d: d.name)
def test_every_metric_executes(conn, definition):
    """Catches SQL that assembles cleanly but references a column that does not
    exist, or an ambiguous alias -- neither of which build_sql() can see."""
    filters = {name: "UK Gold Reactivation" for name in definition.filter_names}
    result = run(conn, definition.name, FOCUS, CohortSpec(countries=GB),
                 filters=filters)
    assert result.cohort_size > 0
    if definition.supports_month:
        run(conn, definition.name, FOCUS, CohortSpec(countries=GB),
            granularity=MONTH, filters=filters)
