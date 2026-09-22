"""The agent loop, tested without calling a model.

Everything here is deterministic behaviour of the loop itself -- budgets, repeat
suppression, conversation reconstruction across requests, how a tool failure
reaches the model. A live model could not test these reliably even if cost were
free: you cannot make a real model repeat a call on the turn your test needs it to.

These do use the database, because the loop's state IS the database. That is the
point of the design, not an accident of testing.
"""

from __future__ import annotations

import json
import uuid

import pytest

from agent import runtime
from agent.findings import SUBMIT_FINDINGS
from llm import fake
from llm.base import AssistantMessage, ToolResultsMessage, UserMessage

QUESTION = "Why did engagement fall?"

FINDINGS = {
    "headline": "It fell because of one thing.",
    "verdict": "confirmed",
    "metrics_used": ["orders_per_member"],
    "comparison_basis": "year over year",
    "cohort_basis": "fixed at 2026-01-15",
    "causes": [{"name": "A cause", "explanation": "Because.",
                "evidence_call_ids": ["toolu_x"], "importance": "largest",
                "confidence": "high"}],
    "ruled_out": [{"name": "A red herring", "why_not": "Affects everyone.",
                   "evidence_call_ids": []}],
    "limitations": "None.",
    "recommended_next": "Do the thing.",
}


@pytest.fixture
def scripted(monkeypatch):
    """Install a scripted provider in place of whatever LLM_PROVIDER says."""
    holder = {}

    def install(*completions):
        provider = fake.ScriptedProvider(script=list(completions))
        monkeypatch.setattr(runtime.factory, "create",
                            lambda name=None: provider)
        holder["provider"] = provider
        return provider

    install.__dict__["holder"] = holder
    return install


@pytest.fixture
def run_conn(conn):
    """Each test gets its own runs; clean them up so reruns are independent."""
    created: list[str] = []
    original = runtime.create_run

    def tracked(c, question, **kwargs):
        run_id = original(c, question, **kwargs)
        created.append(run_id)
        return run_id

    runtime.create_run = tracked
    yield conn
    runtime.create_run = original
    if created:
        conn.execute("delete from ops.agent_runs where run_id = any(%s)",
                     (created,))
        conn.commit()


# ---------------------------------------------------------------- happy path

def test_a_run_completes_and_stores_structured_findings(run_conn, scripted):
    scripted(
        fake.calls(fake.call("list_metrics", {}), text="Looking at what exists."),
        fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)),
    )
    run_id = runtime.create_run(run_conn, QUESTION, actor="test")
    run = runtime.run_to_completion(run_conn, run_id)

    assert run["status"] == "completed"
    assert run["final_answer"]["verdict"] == "confirmed"
    assert run["steps_used"] == 2
    assert len(run["tool_calls"]) == 2
    assert run["completed_at"] is not None


def test_the_trace_records_arguments_and_the_model_facing_result(run_conn, scripted):
    scripted(
        fake.calls(fake.call("get_metric", {
            "metric": "total_revenue", "period_start": "2026-06-01",
            "period_end": "2026-09-01", "countries": ["GB"]})),
        fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)),
    )
    run_id = runtime.create_run(run_conn, QUESTION)
    run = runtime.run_to_completion(run_conn, run_id)

    metric_call = next(c for c in run["tool_calls"] if c["tool"] == "get_metric")
    assert metric_call["ok"]
    assert metric_call["arguments"]["countries"] == ["GB"]
    # The trace keeps the SQL so a number can be audited back to its statement.
    assert "sql" in metric_call["result"]["internals"]


# ------------------------------------------------------------------ budgets

def test_step_budget_stops_the_run_before_spending_another_turn(run_conn, scripted):
    provider = scripted(*[fake.calls(fake.call("list_metrics", {"n": i}))
                          for i in range(10)])
    run_id = runtime.create_run(run_conn, QUESTION)
    run_conn.execute("update ops.agent_runs set max_steps=3 where run_id=%s",
                     (run_id,))
    run_conn.commit()

    run = runtime.run_to_completion(run_conn, run_id)
    assert run["status"] == "budget_exceeded"
    assert run["steps_used"] == 3
    # Checked BEFORE the request, so the fourth turn was never bought.
    assert len(provider.requests) == 3


def test_token_budget_stops_the_run(run_conn, scripted):
    scripted(*[fake.calls(fake.call("list_metrics", {"n": i}),
                          usage=fake.Usage(60_000, 500)) for i in range(6)])
    run_id = runtime.create_run(run_conn, QUESTION)
    run_conn.execute("update ops.agent_runs set max_tokens=100000 where run_id=%s",
                     (run_id,))
    run_conn.commit()

    run = runtime.run_to_completion(run_conn, run_id)
    assert run["status"] == "budget_exceeded"
    assert "Token limit" in run["error"]


def test_the_kill_switch_stops_a_run_in_flight(run_conn, scripted, monkeypatch):
    scripted(fake.calls(fake.call("list_metrics", {})),
             fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)))
    run_id = runtime.create_run(run_conn, QUESTION)
    runtime.advance(run_conn, run_id)

    monkeypatch.setenv("AGENT_ENABLED", "false")
    run = runtime.advance(run_conn, run_id)
    assert run["status"] == "cancelled"
    assert "AGENT_ENABLED" in run["error"]


# -------------------------------------------------------- repeat suppression

def test_an_identical_call_is_answered_with_a_pointer_not_re_executed(
        run_conn, scripted):
    same = {"metric": "total_revenue", "period_start": "2026-06-01",
            "period_end": "2026-09-01"}
    scripted(
        fake.calls(fake.call("get_metric", same, call_id="first")),
        fake.calls(fake.call("get_metric", same, call_id="second")),
        fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)),
    )
    run_id = runtime.create_run(run_conn, QUESTION)
    run = runtime.run_to_completion(run_conn, run_id)

    second = next(c for c in run["tool_calls"] if c["call_id"] == "second")
    assert second["result"]["repeat_of"] == "first"
    assert second["duration_ms"] == 0          # nothing was executed
    assert "already ran this exact call" in second["result"]["summary"]


def test_different_arguments_are_not_treated_as_repeats(run_conn, scripted):
    scripted(
        fake.calls(fake.call("get_metric", {"metric": "total_revenue",
                                            "period_start": "2026-06-01",
                                            "period_end": "2026-09-01"})),
        fake.calls(fake.call("get_metric", {"metric": "total_revenue",
                                            "period_start": "2025-06-01",
                                            "period_end": "2025-09-01"})),
        fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)),
    )
    run_id = runtime.create_run(run_conn, QUESTION)
    run = runtime.run_to_completion(run_conn, run_id)
    assert all("repeat_of" not in (c["result"] or {})
               for c in run["tool_calls"] if c["tool"] == "get_metric")


# ------------------------------------------------- conversation reconstruction

def test_the_conversation_is_rebuilt_from_the_database_between_turns(
        run_conn, scripted):
    """Nothing survives in memory between advances -- that is what makes a run
    resumable, and what makes the serverless deployment possible at all."""
    provider = scripted(
        fake.calls(fake.call("list_metrics", {}), text="First thought."),
        fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)),
    )
    run_id = runtime.create_run(run_conn, QUESTION)
    runtime.advance(run_conn, run_id)
    runtime.advance(run_conn, run_id)

    second = provider.requests[1].messages
    assert isinstance(second[0], UserMessage) and second[0].text == QUESTION
    assert isinstance(second[1], AssistantMessage)
    assert second[1].text == "First thought."
    assert second[1].tool_calls[0].name == "list_metrics"
    assert isinstance(second[2], ToolResultsMessage)
    assert second[2].results[0].is_error is False


def test_a_tool_failure_reaches_the_model_as_a_recoverable_error(
        run_conn, scripted):
    provider = scripted(
        # tier filter without tier_as_of: the guard rail worth 21 points
        fake.calls(fake.call("get_metric", {
            "metric": "orders_per_member", "period_start": "2026-06-01",
            "period_end": "2026-09-01", "tiers": ["GOLD"]})),
        fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)),
    )
    run_id = runtime.create_run(run_conn, QUESTION)
    runtime.advance(run_conn, run_id)
    run = runtime.advance(run_conn, run_id)

    failed = next(c for c in run["tool_calls"] if not c["ok"])
    assert "tier_as_of" in failed["error"]
    # ...and the model was actually shown it, flagged as an error.
    outcome = provider.requests[1].messages[2].results[0]
    assert outcome.is_error is True
    assert "tier_as_of" in outcome.content


def test_ending_without_findings_is_nudged_once_then_failed(run_conn, scripted):
    scripted(fake.says("I think it was the weather."),
             fake.says("Still the weather."))
    run_id = runtime.create_run(run_conn, QUESTION)
    run = runtime.run_to_completion(run_conn, run_id)

    assert run["status"] == "failed"
    assert "submit_findings" in run["error"]
    assert run["steps"][1]["user_message"] is not None       # was nudged


# ------------------------------------------------------------------ metadata

def test_the_run_records_which_provider_and_model_answered(run_conn, scripted):
    scripted(fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)))
    run_id = runtime.create_run(run_conn, QUESTION)
    run = runtime.run_to_completion(run_conn, run_id)
    assert run["provider"] == "fake" and run["model"] == "scripted"


def test_every_tool_is_offered_to_the_model(run_conn, scripted):
    provider = scripted(fake.calls(fake.call(SUBMIT_FINDINGS, FINDINGS)))
    run_id = runtime.create_run(run_conn, QUESTION)
    runtime.run_to_completion(run_conn, run_id)
    offered = {t.name for t in provider.requests[0].tools}
    assert offered == {"get_reference_data", "list_metrics", "get_metric",
                       "list_campaigns", "search_customers",
                       "get_customer_360", SUBMIT_FINDINGS}
