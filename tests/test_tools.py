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
import tools.action_tools     # noqa: F401  -- registers the write tools
import tools.knowledge_tools  # noqa: F401  -- registers search_knowledge
from tools.registry import REGISTRY, ToolContext

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


def test_the_write_partition_holds_exactly_one_tool():
    """The model's entire write capability, as a checkable fact.

    It can record a draft proposal. It cannot approve, execute, or touch any
    L-Mart business table. submit_findings and preview_campaign are reads:
    one ends a run, the other validates a hypothetical without storing it.
    """
    assert {t.name for t in REGISTRY.writable()} == {"create_campaign_proposal"}
    assert {t.name for t in REGISTRY.readable()} == {
        "get_reference_data", "list_metrics", "get_metric", "list_campaigns",
        "search_customers", "get_customer_360", "submit_findings",
        "search_knowledge", "preview_campaign"}


def test_a_write_tool_is_refused_when_the_run_is_read_only(db_conn):
    """Defence in depth: a read-only run does not offer write tools at all, and
    dispatch refuses them even if one were somehow requested."""
    failure = REGISTRY.dispatch("create_campaign_proposal", {},
                                ToolContext(conn=db_conn, allow_writes=False))
    assert failure.ok is False
    assert failure.error == "not_permitted"


def test_unknown_tool_is_a_failure_not_an_exception(tool_ctx):
    failure = REGISTRY.dispatch("delete_everything", {}, tool_ctx)
    assert failure.ok is False
    assert failure.error == "unknown_tool"
    assert "get_metric" in failure.message          # tells it what does exist


def test_validation_failure_names_the_field(tool_ctx):
    failure = REGISTRY.dispatch("get_metric", {"metric": "orders_per_member"},
                                tool_ctx)
    assert failure.ok is False and failure.error == "validation"
    assert "period_start" in failure.message


def test_unexpected_argument_is_rejected(tool_ctx):
    failure = REGISTRY.dispatch(
        "get_metric", {**{k: str(v) for k, v in FOCUS.items()},
                       "metric": "total_revenue", "sql": "drop table"}, tool_ctx)
    assert failure.ok is False and failure.error == "validation"


def test_a_failing_handler_does_not_leak_the_query(tool_ctx):
    """A database error can quote the statement. The model must not learn the
    schema from an error message."""
    failure = REGISTRY.dispatch(
        "get_metric", {**{k: str(v) for k, v in FOCUS.items()},
                       "metric": "no_such_metric"}, tool_ctx)
    assert failure.ok is False
    assert "select" not in failure.message.lower()
    assert "lmart." not in failure.message


def test_model_view_hides_sql_and_trace_view_keeps_it(tool_ctx):
    result = REGISTRY.dispatch(
        "get_metric", {**{k: str(v) for k, v in FOCUS.items()},
                       "metric": "total_revenue", "countries": ["GB"]}, tool_ctx)
    assert result.ok is True
    assert "internals" not in result.for_model()
    assert "sql" not in str(result.for_model()).lower()
    assert "select" in result.for_trace()["internals"]["sql"].lower()


# ------------------------------------------------------------- behaviour

def test_reference_data_gives_the_codes_that_filters_need(tool_ctx):
    result = REGISTRY.dispatch("get_reference_data", {}, tool_ctx)
    assert result.ok
    assert "GB" in result.data["countries"]
    assert {t["tier_code"] for t in result.data["tiers"]} >= {"GOLD", "PLATINUM"}
    assert "BEAUTY" in result.data["categories"]
    programmes = {p["programme"] for p in result.data["campaign_programmes"]}
    assert "UK Gold Reactivation" in programmes


def test_get_metric_reports_cohort_size_next_to_the_value(tool_ctx):
    result = REGISTRY.dispatch("get_metric", {
        "metric": "orders_per_member", "period_start": "2026-06-01",
        "period_end": "2026-09-01", "countries": ["GB"], "tiers": ["GOLD"],
        "tier_as_of": "period_end"}, tool_ctx)
    assert result.ok
    assert result.meta["cohort_size"] > 100
    assert "customers" in result.summary


def test_tier_without_as_of_fails_with_an_explanation(tool_ctx):
    """The single most important guard rail in the system, reachable by the
    model exactly as a person would hit it."""
    failure = REGISTRY.dispatch("get_metric", {
        "metric": "orders_per_member", "period_start": "2026-06-01",
        "period_end": "2026-09-01", "tiers": ["GOLD"]}, tool_ctx)
    assert failure.ok is False
    assert "tier_as_of" in failure.message


def test_list_campaigns_reveals_the_programme_that_stopped(tool_ctx):
    result = REGISTRY.dispatch("list_campaigns", {}, tool_ctx)
    assert result.ok
    by_name = {r["programme"]: r for r in result.data}
    assert by_name["UK Gold Reactivation"]["last_wave"] < date(2026, 6, 1)
    assert by_name["Global Rewards Digest"]["last_wave"] >= date(2026, 8, 1)


def test_list_campaigns_for_an_unknown_programme_says_how_to_recover(tool_ctx):
    result = REGISTRY.dispatch("list_campaigns", {"programme": "Nope"}, tool_ctx)
    assert result.data == []
    assert "list_campaigns without a programme" in result.summary


def test_search_and_then_360(tool_ctx):
    found = REGISTRY.dispatch("search_customers", {
        "countries": ["GB"], "tiers": ["GOLD"], "tier_as_of": "2026-01-15",
        "limit": 5}, tool_ctx)
    assert found.ok and found.data
    customer_id = found.data[0]["customer_id"]

    detail = REGISTRY.dispatch("get_customer_360",
                               {"customer_id": customer_id}, tool_ctx)
    assert detail.ok
    assert detail.data["profile"]["country_code"] == "GB"
    assert detail.data["tier_history"]
    assert "orders" in detail.data["orders_summary"]


def test_missing_customer_is_reported_not_crashed(tool_ctx):
    result = REGISTRY.dispatch("get_customer_360", {"customer_id": 99999999},
                               tool_ctx)
    assert result.ok and result.data == {}
    assert "No customer" in result.summary


@pytest.mark.parametrize("name", sorted(t.name for t in REGISTRY.readable()))
def test_every_tool_runs_against_the_real_schema(tool_ctx, name):
    """Catches SQL that only fails when executed -- a wrong column, an
    ambiguous alias -- which no schema check can see."""
    minimal = {
        "get_metric": {"metric": "total_revenue", "period_start": "2026-06-01",
                       "period_end": "2026-09-01"},
        "get_customer_360": {"customer_id": 1},
        "search_customers": {"limit": 1},
        "submit_findings": VALID_FINDINGS,
        "search_knowledge": {"query": "discount ceiling for gold"},
        "preview_campaign": {"channel": "email",
                             "offer_type": "points_multiplier",
                             "offer_value": 2.0, "countries": ["GB"],
                             "tiers": ["GOLD"], "tier_as_of": "2026-01-15",
                             "holdout_pct": 10},
    }.get(name, {})
    assert REGISTRY.dispatch(name, minimal, tool_ctx).ok


# --------------------------------------------------- caller permissions

def test_a_tool_is_refused_when_the_caller_lacks_its_scope(db_conn):
    """The run may permit writes and the caller still may not.

    Two independent gates: allow_writes is a property of the RUN, scopes are a
    property of the CALLER. Collapsing them would make "this run may write" and
    "this person may write" the same statement, and they are not.
    """
    from core.auth import READ
    read_only_caller = ToolContext(conn=db_conn, actor="viewer@example.com",
                                   allow_writes=True,          # run permits it
                                   scopes=frozenset({READ}))   # caller does not
    failure = REGISTRY.dispatch("create_campaign_proposal", {}, read_only_caller)
    assert failure.ok is False
    assert failure.error == "not_permitted"
    assert "propose" in failure.message
    assert "viewer@example.com" in failure.message


def test_a_caller_with_the_scope_gets_past_the_permission_check(db_conn):
    """Reaches validation instead of being refused -- proving the gate opened,
    without creating anything."""
    from core.auth import PROPOSE, READ
    proposer = ToolContext(conn=db_conn, actor="analyst@example.com",
                           allow_writes=True,
                           scopes=frozenset({READ, PROPOSE}))
    failure = REGISTRY.dispatch("create_campaign_proposal", {}, proposer)
    assert failure.error == "validation"        # not 'not_permitted'
