"""Metric request validation and SQL assembly. No database required.

build_sql() is a pure function of the request, which is what makes these rules
cheap to test. Only the numbers themselves need Postgres.
"""

from __future__ import annotations

from datetime import date

import pytest

from metrics import catalog
from metrics.cohort import CohortSpec, PERIOD_END
from metrics.engine import MONTH, MetricRequest, MetricRequestError, build_sql

START, END = date(2026, 6, 1), date(2026, 9, 1)


def request(metric="total_revenue", **kwargs):
    kwargs.setdefault("period_start", START)
    kwargs.setdefault("period_end", END)
    return MetricRequest(metric=metric, **kwargs)


def test_unknown_metric_lists_what_is_available():
    with pytest.raises(catalog.UnknownMetric, match="Available:"):
        build_sql(request("engagement"))


def test_period_must_run_forwards():
    with pytest.raises(MetricRequestError, match="must be before"):
        build_sql(request(period_start=END, period_end=START))


def test_period_independent_metric_cannot_be_split_by_month():
    with pytest.raises(MetricRequestError, match="does not vary over time"):
        build_sql(request("cohort_size", granularity=MONTH))


def test_unsupported_filter_is_rejected_rather_than_ignored():
    with pytest.raises(MetricRequestError, match="does not support filter"):
        build_sql(request("total_revenue", filters={"programme": "anything"}))


def test_supported_filter_becomes_a_bound_parameter():
    sql, params = build_sql(request(
        "campaign_funnel", filters={"programme": "UK Gold Reactivation"}))
    assert "c.programme = %(filter_programme)s" in sql
    assert params["filter_programme"] == "UK Gold Reactivation"
    # The value is bound, never interpolated into the statement text.
    assert "UK Gold Reactivation" not in sql


def test_monthly_series_divides_by_one_month_not_the_whole_period():
    """A monthly bucket is a per-month rate. Dividing each bucket by the
    period's three months would understate every point by 3x."""
    _, monthly = build_sql(request("orders_per_member", granularity=MONTH))
    _, total = build_sql(request("orders_per_member"))
    assert monthly["months"] == 1.0
    assert total["months"] == pytest.approx(3.02, abs=0.01)


def test_monthly_series_groups_and_orders_by_its_dimensions():
    sql, _ = build_sql(request("revenue_by_category", granularity=MONTH))
    assert "date_trunc('month', o.ordered_at)::date as period" in sql
    assert "cat.code as category" in sql
    assert "group by 1, 2" in sql
    assert "order by 1, 2" in sql


def test_totals_have_no_grouping():
    sql, _ = build_sql(request("total_revenue"))
    assert "group by" not in sql


def test_every_metric_in_the_catalogue_builds():
    """Catches a metric added with a typo in its SQL template or a dimension
    referencing an alias its from_sql never defines."""
    for definition in catalog.DEFINITIONS:
        kwargs = {"filters": {name: "x" for name in definition.filter_names}}
        sql, params = build_sql(request(definition.name, **kwargs))
        assert sql.startswith("with cohort as (")
        assert " as value" in sql
        if definition.supports_month:
            build_sql(request(definition.name, granularity=MONTH, **kwargs))


def test_per_member_metrics_take_their_denominator_from_the_cohort():
    """Not from the join. That is what makes an inner join safe: members with
    no orders still count in the denominator."""
    for name in ("orders_per_member", "revenue_per_member", "active_rate"):
        sql, _ = build_sql(request(name))
        assert "(select count(*) from cohort)" in sql
