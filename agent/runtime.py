"""The agent loop, as a state machine in Postgres.

ONE TURN PER CALL. advance() executes a single model turn plus its tool calls,
persists everything, and returns. It does not loop. Three reasons, in order of
how much they matter:

  1. A serverless function has a wall-clock ceiling. A twelve-turn investigation
     inside one request gets cut off; twelve short requests do not.
  2. The trace becomes the execution substrate rather than logging bolted on.
     Debugging is a query. Replay is reading rows.
  3. A crashed or cancelled run resumes from its last completed turn instead of
     starting over.

The conversation is REBUILT from the database on every advance. Nothing lives in
process memory between calls -- which is the discipline serverless forces, and
which happens to be exactly what makes a run inspectable.

BUDGETS ARE COPIED ONTO THE RUN at creation rather than read from the environment
each turn, so a run's limits cannot change underneath it and an old run explains
its own outcome.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core import config
from core.errors import ActionableError
from llm import factory, pricing
from llm.base import (LLMError, Message, ToolOutcome, ToolResultsMessage,
                      ToolSpec, Usage, UserMessage)
from tools.registry import REGISTRY, ToolContext

from .findings import SUBMIT_FINDINGS
from .prompt import SYSTEM_PROMPT

import agent.findings          # noqa: F401  registers submit_findings
import tools.catalog           # noqa: F401  registers the read tools
import tools.knowledge_tools   # noqa: F401  registers search_knowledge
import tools.action_tools      # noqa: F401  registers the write tools

TERMINAL = ("completed", "failed", "budget_exceeded", "cancelled")
MAX_NUDGES = 1

NUDGE = ("You ended your turn without calling submit_findings. Either continue "
         "investigating with the tools, or call submit_findings now with what "
         "you have, including what remains uncertain.")


@dataclass(frozen=True)
class Budgets:
    max_steps: int
    max_tokens: int
    max_cost_usd: float | None

    @classmethod
    def from_config(cls) -> "Budgets":
        return cls(
            max_steps=config.get_int("AGENT_MAX_STEPS", 20),
            # Cumulative across turns, and every turn resends the conversation,
            # so this grows roughly quadratically in steps. Measured: a 14-step
            # investigation reached ~390k before caching. 1.5M leaves room for a
            # thorough run; the cost cap is the binding limit in practice.
            max_tokens=config.get_int("AGENT_MAX_RUN_TOKENS", 1_500_000),
            # Tightened from 5.00 after the first real run spent $2.23 and
            # produced no answer. With caching a complete investigation should
            # land well under this; a run that hits it is a bug, not a big
            # question.
            max_cost_usd=config.get_float("AGENT_MAX_RUN_COST_USD", 1.0),
        )


class AgentDisabled(RuntimeError):
    """The kill switch is off."""


def _require_enabled() -> None:
    if not config.get_bool("AGENT_ENABLED", True):
        raise AgentDisabled(
            "AGENT_ENABLED is false; no new agent work will start.")


# --------------------------------------------------------------------------
# Creating and reading runs
# --------------------------------------------------------------------------

def create_run(conn, question: str, actor: str = "anonymous",
               provider_name: str | None = None,
               allow_writes: bool = False,
               scopes: frozenset[str] | None = None) -> str:
    _require_enabled()
    provider = factory.create(provider_name)
    budgets = Budgets.from_config()
    run_id = str(uuid.uuid4())
    conn.execute("""
        insert into ops.agent_runs
            (run_id, question, status, provider, model,
             max_steps, max_tokens, max_cost_usd, actor, allow_writes, scopes)
        values (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
    """, (run_id, question, provider.name, provider.model,
          budgets.max_steps, budgets.max_tokens, budgets.max_cost_usd, actor,
          allow_writes, sorted(scopes or {"read"})))
    conn.commit()
    return run_id


def load_run(conn, run_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        run = cur.execute("select * from ops.agent_runs where run_id = %s",
                          (run_id,)).fetchone()
        if run is None:
            return None
        run["steps"] = cur.execute(
            "select * from ops.agent_steps where run_id = %s order by step_no",
            (run_id,)).fetchall()
        run["tool_calls"] = cur.execute(
            "select * from ops.tool_calls where run_id = %s order by created_at",
            (run_id,)).fetchall()
    return run


def cancel(conn, run_id: str) -> None:
    # `status not in %s` does NOT work: psycopg renders a Python tuple as a
    # composite value, so Postgres sees `status not in $2` and rejects it.
    # `<> all(array)` is the parameterised form of "differs from every element".
    conn.execute("""update ops.agent_runs
                       set status='cancelled', updated_at=now(), completed_at=now()
                     where run_id = %s and status <> all(%s)""",
                 (run_id, list(TERMINAL)))
    conn.commit()


# --------------------------------------------------------------------------
# Rebuilding the conversation
# --------------------------------------------------------------------------

def _rebuild(run: dict) -> list[Message]:
    """Reconstruct the neutral message list from persisted rows.

    The provider's own content blocks are replayed verbatim where we have them,
    which is what preserves reasoning blocks across turns for providers that
    emit them.
    """
    from llm.base import AssistantMessage, ToolCall

    calls_by_step: dict[int, list[dict]] = {}
    for call in run["tool_calls"]:
        calls_by_step.setdefault(call["step_no"], []).append(call)

    messages: list[Message] = [UserMessage(run["question"])]
    for step in run["steps"]:
        if step["step_no"] > 0 and step["user_message"]:
            messages.append(UserMessage(step["user_message"]))

        step_calls = calls_by_step.get(step["step_no"], [])
        messages.append(AssistantMessage(
            text=step["assistant_text"],
            tool_calls=tuple(ToolCall(c["call_id"], c["tool"], c["arguments"])
                             for c in step_calls),
            provider_payload=step["assistant_payload"],
        ))
        if step_calls:
            messages.append(ToolResultsMessage(tuple(
                ToolOutcome(
                    call_id=c["call_id"],
                    content=json.dumps(c["result"] if c["ok"]
                                       else {"ok": False, "error": c["error"]},
                                       default=str)[:8000],
                    is_error=not c["ok"])
                for c in step_calls)))
    return messages


def _tool_specs(allow_writes: bool) -> list[ToolSpec]:
    """Write tools are not merely refused when a run is read-only -- they are
    not offered. A model cannot misuse a capability it was never shown, and the
    trace then makes plain which runs could write at all."""
    return [ToolSpec(**spec) for spec in REGISTRY.schemas(include_writes=allow_writes)]


# --------------------------------------------------------------------------
# Advancing
# --------------------------------------------------------------------------

def _finish(conn, run_id: str, status: str, error: str | None = None,
            answer: dict | None = None) -> None:
    conn.execute("""
        update ops.agent_runs
           set status=%s, error=%s, final_answer=%s,
               updated_at=now(), completed_at=now()
         where run_id=%s
    """, (status, error, Jsonb(answer) if answer else None, run_id))
    conn.commit()


def advance(conn, run_id: str) -> dict:
    """Execute one model turn. Returns the run as it now stands."""
    run = load_run(conn, run_id)
    if run is None:
        raise KeyError(f"No run {run_id}")
    if run["status"] in TERMINAL:
        return run

    try:
        _require_enabled()
    except AgentDisabled as exc:
        _finish(conn, run_id, "cancelled", str(exc))
        return load_run(conn, run_id)

    # Budgets are checked BEFORE spending, so an exhausted run stops without one
    # more request rather than after it.
    step_no = len(run["steps"])
    used_tokens = run["input_tokens"] + run["output_tokens"]
    if step_no >= run["max_steps"]:
        _finish(conn, run_id, "budget_exceeded",
                f"Step limit reached ({run['max_steps']}) without findings.")
        return load_run(conn, run_id)
    if used_tokens >= run["max_tokens"]:
        _finish(conn, run_id, "budget_exceeded",
                f"Token limit reached ({run['max_tokens']}).")
        return load_run(conn, run_id)
    if run["max_cost_usd"] and float(run["cost_usd"]) >= float(run["max_cost_usd"]):
        _finish(conn, run_id, "budget_exceeded",
                f"Cost limit reached (${run['max_cost_usd']}).")
        return load_run(conn, run_id)

    conn.execute("update ops.agent_runs set status='running', updated_at=now() "
                 "where run_id=%s", (run_id,))
    conn.commit()

    # A nudge is a user turn injected when the model stopped without finishing.
    nudge = None
    if step_no > 0:
        last = run["steps"][-1]
        had_calls = any(c["step_no"] == last["step_no"] for c in run["tool_calls"])
        if not had_calls and last["stop_reason"] == "end_turn":
            nudges_so_far = sum(1 for s in run["steps"]
                                if s["step_no"] > 0 and s["user_message"])
            if nudges_so_far >= MAX_NUDGES:
                _finish(conn, run_id, "failed",
                        "Stopped without calling submit_findings.")
                return load_run(conn, run_id)
            nudge = NUDGE

    messages = _rebuild(run)
    if nudge:
        messages.append(UserMessage(nudge))

    provider = factory.create(run["provider"])
    started = time.time()
    try:
        completion = provider.complete(
            system=SYSTEM_PROMPT, messages=messages,
            tools=_tool_specs(run["allow_writes"]), max_tokens=16000)
    except LLMError as exc:
        _finish(conn, run_id, "failed", str(exc))
        return load_run(conn, run_id)
    duration_ms = int((time.time() - started) * 1000)

    cost = pricing.estimate_usd(run["model"], completion.usage)
    conn.execute("""
        insert into ops.agent_steps
            (run_id, step_no, user_message, assistant_text, assistant_payload,
             stop_reason, input_tokens, output_tokens, duration_ms)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (run_id, step_no, nudge, completion.text,
          Jsonb(completion.provider_payload) if completion.provider_payload else None,
          completion.stop_reason, completion.usage.input_tokens,
          completion.usage.output_tokens, duration_ms))
    conn.execute("""
        update ops.agent_runs
           set steps_used = steps_used + 1,
               input_tokens = input_tokens + %s,
               output_tokens = output_tokens + %s,
               cost_usd = cost_usd + %s,
               updated_at = now()
         where run_id = %s
    """, (completion.usage.input_tokens, completion.usage.output_tokens,
          cost or 0, run_id))
    conn.commit()

    # --- execute the tool calls ------------------------------------------
    # A model that has lost the thread re-asks the same question. Observed on
    # the first real run: identical points_expired calls on three consecutive
    # steps, spending a third of the token budget to learn nothing. Returning a
    # pointer to the earlier answer is cheaper than the answer, and it tells the
    # model plainly that it is going in circles -- which is information it can
    # act on, unlike a silent duplicate.
    seen = {(c["tool"], json.dumps(c["arguments"], sort_keys=True, default=str)):
            c["call_id"] for c in run["tool_calls"] if c["ok"]}

    findings: dict | None = None
    for call in completion.tool_calls:
        signature = (call.name, json.dumps(call.arguments, sort_keys=True,
                                           default=str))
        if signature in seen and call.name != SUBMIT_FINDINGS:
            previous = seen[signature]
            conn.execute("""
                insert into ops.tool_calls
                    (call_id, run_id, step_no, tool, arguments, ok, result,
                     duration_ms)
                values (%s,%s,%s,%s,%s,true,%s,0)
            """, (call.id, run_id, step_no, call.name, Jsonb(call.arguments),
                  Jsonb({"ok": True, "call_id": call.id,
                         "repeat_of": previous,
                         "summary": f"You already ran this exact call. Its "
                                    f"result is in {previous}; cite that "
                                    f"call_id. Ask something different or "
                                    f"submit your findings.",
                         "data": None, "used": {}})))
            continue

        call_started = time.time()
        outcome = REGISTRY.dispatch(call.name, call.arguments, ToolContext(
            conn=conn, actor=run["actor"], run_id=run_id,
            allow_writes=run["allow_writes"],
            # Scopes are stored on the run, not re-derived here: the permissions
            # that applied when the investigation started are the ones it runs
            # under, even if the person's role changes mid-flight.
            scopes=frozenset(run["scopes"] or ["read"])))
        elapsed = int((time.time() - call_started) * 1000)

        # The call_id in the trace must be the PROVIDER's id: that is what the
        # next request references, and what the model cites in its answer.
        conn.execute("""
            insert into ops.tool_calls
                (call_id, run_id, step_no, tool, arguments, ok, result, error,
                 duration_ms)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (call.id, run_id, step_no, call.name, Jsonb(call.arguments),
              outcome.ok,
              Jsonb({**outcome.for_trace(), "call_id": call.id})
              if outcome.ok else None,
              None if outcome.ok else outcome.message, elapsed))

        if outcome.ok and call.name == SUBMIT_FINDINGS:
            findings = outcome.data
    conn.commit()

    if findings is not None:
        _finish(conn, run_id, "completed", answer=findings)
    elif completion.stop_reason == "refusal":
        _finish(conn, run_id, "failed", "The model declined the request.")

    return load_run(conn, run_id)


def run_to_completion(conn, run_id: str, max_advances: int = 40,
                      on_step=None) -> dict:
    """Drive a run to a terminal state. For the CLI and the eval harness.

    The HTTP API calls advance() once per request instead; this is the same
    state machine, driven from one process.
    """
    run = load_run(conn, run_id)
    for _ in range(max_advances):
        if run["status"] in TERMINAL:
            break
        run = advance(conn, run_id)
        if on_step:
            on_step(run)
    return run
