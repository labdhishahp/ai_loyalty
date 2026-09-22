"""The tool layer: what the model may call, and what happens when it calls wrongly.

The offline tests cover the contract -- schemas, validation, the read/write
split. They are the ones that matter most, because the model's output is
untrusted input and dispatch() is the trust boundary.
"""

from __future__ import annotations

from datetime import date

import pytest

import agent.findings  # noqa: F401  -- registers submit_findings
import tools.catalog   # noqa: F401  -- registers the read tools
from tools.registry import REGISTRY

# A valid payload for submit_findings, which unlike the read tools cannot be
# called with no arguments.
VALID_FINDINGS = {
    "headline": "h", "verdict": "inconclusive", "metrics_used": ["x"],
    "comparison_basis": "y", "cohort_basis": "z",
    "causes": [{"name": "c", "explanation": "e", "evidence_call_ids": ["a"],
                "importance": "minor", "confidence": "low"}],
    "ruled_out": [{"name": "r", "why_not": "w", "evidence_call_ids": []}],
    "limitations": "l", "recommended_next": "n",
}

FOCUS = {"period_start": date(2026, 6, 1), "period_end": date(2026, 9, 1)}


# --------------------------------------------------------------- contract

def test_every_tool_exposes_a_strict_flat_schema():
    for spec in REGISTRY.schemas():
        schema = spec["input_schema"]
        assert schema["additionalProperties"] is False, spec["name"]
        assert schema["type"] == "object"
        # No $ref anywhere: provider strict modes differ in whether they
        # resolve them, so the schema is inlined into a self-contained document.
        assert "$defs" not in schema, f"{spec['name']} still has $defs"
        assert "$ref" not in str(schema), f"{spec['name']} still has a $ref"


def test_every_tool_description_is_substantial():
    """Descriptions are the highest-leverage prompt text in the system. A
    one-liner here is a silent quality regression."""
    for spec in REGISTRY.schemas():
        assert len(spec["description"]) > 120, spec["name"]


def test_the_write_partition_is_empty():
    """Checkable fact, not a promise: nothing the model can call changes
    business data. submit_findings ends a run; it writes no L-Mart records."""
    assert REGISTRY.writable() == []
    assert {t.name for t in REGISTRY.readable()} == {
        "get_reference_data", "list_metrics", "get_metric", "list_campaigns",
        "search_customers", "get_customer_360", "submit_findings"}


def test_unknown_tool_is_a_failure_not_an_exception(db_conn):
    failure = REGISTRY.dispatch("delete_everything", {}, db_conn)
    assert failure.ok is False
    assert failure.error == "unknown_tool"
    assert "get_metric" in failure.message          # tells it what does exist


def test_validation_failure_names_the_field(db_conn):
    failure = REGISTRY.dispatch("get_metric", {"metric": "orders_per_member"},
                                db_conn)
    assert failure.ok is False and failure.error == "validation"
    assert "period_start" in failure.message


def test_unexpected_argument_is_rejected(db_conn):
    failure = REGISTRY.dispatch(
        "get_metric", {**{k: str(v) for k, v in FOCUS.items()},
                       "metric": "total_revenue", "sql": "drop table"}, db_conn)
    assert failure.ok is False and failure.error == "validation"


def test_a_failing_handler_does_not_leak_the_query(db_conn):
    """A database error can quote the statement. The model must not learn the
    schema from an error message."""
    failure = REGISTRY.dispatch(
        "get_metric", {**{k: str(v) for k, v in FOCUS.items()},
                       "metric": "no_such_metric"}, db_conn)
    assert failure.ok is False
    assert "select" not in failure.message.lower()
    assert "lmart." not in failure.message


def test_model_view_hides_sql_and_trace_view_keeps_it(db_conn):
    result = REGISTRY.dispatch(
        "get_metric", {**{k: str(v) for k, v in FOCUS.items()},
                       "metric": "total_revenue", "countries": ["GB"]}, db_conn)
    assert result.ok is True
    assert "internals" not in result.for_model()
    assert "sql" not in str(result.for_model()).lower()
    assert "select" in result.for_trace()["internals"]["sql"].lower()


# ------------------------------------------------------------- behaviour

def test_reference_data_gives_the_codes_that_filters_need(db_conn):
    result = REGISTRY.dispatch("get_reference_data", {}, db_conn)
    assert result.ok
    assert "GB" in result.data["countries"]
    assert {t["tier_code"] for t in result.data["tiers"]} >= {"GOLD", "PLATINUM"}
    assert "BEAUTY" in result.data["categories"]
    programmes = {p["programme"] for p in result.data["campaign_programmes"]}
    assert "UK Gold Reactivation" in programmes


def test_get_metric_reports_cohort_size_next_to_the_value(db_conn):
    result = REGISTRY.dispatch("get_metric", {
        "metric": "orders_per_member", "period_start": "2026-06-01",
        "period_end": "2026-09-01", "countries": ["GB"], "tiers": ["GOLD"],
        "tier_as_of": "period_end"}, db_conn)
    assert result.ok
    assert result.meta["cohort_size"] > 100
    assert "customers" in result.summary


def test_tier_without_as_of_fails_with_an_explanation(db_conn):
    """The single most important guard rail in the system, reachable by the
    model exactly as a person would hit it."""
    failure = REGISTRY.dispatch("get_metric", {
        "metric": "orders_per_member", "period_start": "2026-06-01",
        "period_end": "2026-09-01", "tiers": ["GOLD"]}, db_conn)
    assert failure.ok is False
    assert "tier_as_of" in failure.message


def test_list_campaigns_reveals_the_programme_that_stopped(db_conn):
    result = REGISTRY.dispatch("list_campaigns", {}, db_conn)
    assert result.ok
    by_name = {r["programme"]: r for r in result.data}
    assert by_name["UK Gold Reactivation"]["last_wave"] < date(2026, 6, 1)
    assert by_name["Global Rewards Digest"]["last_wave"] >= date(2026, 8, 1)


def test_list_campaigns_for_an_unknown_programme_says_how_to_recover(db_conn):
    result = REGISTRY.dispatch("list_campaigns", {"programme": "Nope"}, db_conn)
    assert result.data == []
    assert "list_campaigns without a programme" in result.summary


def test_search_and_then_360(db_conn):
    found = REGISTRY.dispatch("search_customers", {
        "countries": ["GB"], "tiers": ["GOLD"], "tier_as_of": "2026-01-15",
        "limit": 5}, db_conn)
    assert found.ok and found.data
    customer_id = found.data[0]["customer_id"]

    detail = REGISTRY.dispatch("get_customer_360",
                               {"customer_id": customer_id}, db_conn)
    assert detail.ok
    assert detail.data["profile"]["country_code"] == "GB"
    assert detail.data["tier_history"]
    assert "orders" in detail.data["orders_summary"]


def test_missing_customer_is_reported_not_crashed(db_conn):
    result = REGISTRY.dispatch("get_customer_360", {"customer_id": 99999999},
                               db_conn)
    assert result.ok and result.data == {}
    assert "No customer" in result.summary


@pytest.mark.parametrize("name", sorted(t.name for t in REGISTRY.readable()))
def test_every_tool_runs_against_the_real_schema(db_conn, name):
    """Catches SQL that only fails when executed -- a wrong column, an
    ambiguous alias -- which no schema check can see."""
    minimal = {
        "get_metric": {"metric": "total_revenue", "period_start": "2026-06-01",
                       "period_end": "2026-09-01"},
        "get_customer_360": {"customer_id": 1},
        "search_customers": {"limit": 1},
        "submit_findings": VALID_FINDINGS,
    }.get(name, {})
    assert REGISTRY.dispatch(name, minimal, db_conn).ok
