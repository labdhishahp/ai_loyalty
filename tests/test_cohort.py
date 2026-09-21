"""Rules about who is being measured. No database required.

The rule under test is the one the dataset forced on us: a tier filter without
an as-of date is not under-specified, it is ambiguous by ~21 percentage points.
"""

from __future__ import annotations

from datetime import date

import pytest

from metrics.cohort import (COUNTRIES, PERIOD_END, CohortError, CohortSpec,
                            build_cohort_sql)

PERIOD_CLOSE = date(2026, 9, 1)


def test_tier_without_as_of_is_rejected():
    with pytest.raises(CohortError, match="requires tier_as_of"):
        CohortSpec(tiers=("GOLD",)).validate()


def test_as_of_without_tier_is_rejected():
    # Silently ignoring it would let a caller believe a cohort was pinned in
    # time when it was not.
    with pytest.raises(CohortError, match="without a tier filter"):
        CohortSpec(tier_as_of=date(2026, 1, 15)).validate()


def test_unknown_values_are_rejected():
    with pytest.raises(CohortError, match="Unknown tier"):
        CohortSpec(tiers=("DIAMOND",), tier_as_of=PERIOD_END).validate()
    with pytest.raises(CohortError, match="Unknown country"):
        CohortSpec(countries=("US",)).validate()


def test_period_end_resolves_to_the_last_day_inside_the_period():
    spec = CohortSpec(tiers=("GOLD",), tier_as_of=PERIOD_END)
    assert spec.resolve_as_of(PERIOD_CLOSE) == date(2026, 8, 31)


def test_a_fixed_date_ignores_the_period():
    spec = CohortSpec(tiers=("GOLD",), tier_as_of=date(2026, 1, 15))
    assert spec.resolve_as_of(PERIOD_CLOSE) == date(2026, 1, 15)
    assert spec.resolve_as_of(date(2025, 9, 1)) == date(2026, 1, 15)


def test_tier_filter_reads_tier_history_not_the_cached_current_tier():
    """The whole point: loyalty_accounts.current_tier cannot answer a
    historical question, so the cohort must join through tier_history."""
    sql, params = build_cohort_sql(
        CohortSpec(countries=("GB",), tiers=("GOLD",),
                   tier_as_of=date(2026, 1, 15)), PERIOD_CLOSE)
    assert "lmart.tier_history" in sql
    assert "current_tier" not in sql
    assert "effective_from <= %(cohort_as_of)s" in sql
    assert "effective_to is null or th.effective_to > %(cohort_as_of)s" in sql
    assert params["cohort_as_of"] == date(2026, 1, 15)


def test_no_filters_means_every_customer():
    sql, params = build_cohort_sql(CohortSpec(), PERIOD_CLOSE)
    assert "where" not in sql
    assert "join" not in sql
    assert params == {}


def test_describe_names_the_anchor_so_it_can_be_cited():
    fixed = CohortSpec(countries=("GB",), tiers=("GOLD",), tier_as_of=date(2026, 1, 15))
    moving = CohortSpec(countries=("GB",), tiers=("GOLD",), tier_as_of=PERIOD_END)
    assert fixed.describe(PERIOD_CLOSE) == "GB GOLD tier (as of 2026-01-15)"
    assert "each period" in moving.describe(PERIOD_CLOSE)
    assert CohortSpec().describe(PERIOD_CLOSE) == "all customers"
