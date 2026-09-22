"""The HTTP surface.

ONE TURN PER REQUEST. /runs/{id}/advance executes a single agent turn and
returns. The client repeats until the run is terminal. That shape is forced by
serverless -- a function has a wall-clock ceiling and a twelve-turn
investigation will exceed it -- and it is also what makes the run resumable, so
the constraint and the good design happen to agree.

CONNECTIONS COME FROM THE POOLER. Every request handler opens a short-lived
transaction-pooled connection. The database allows 60 connections and serverless
scales to many instances; direct connections would exhaust it under trivial load.

AUTH IS A SHARED SECRET, for now. Until Milestone 4 there is no identity, and a
public endpoint that runs model calls is an invitation to spend someone else's
gateway quota. Locally the key may be unset; a deployed instance refuses to
start without one, because the failure mode of forgetting it is silent.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent import runtime
from core import config, db
from core.errors import ActionableError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lmart.api")

IS_SERVERLESS = bool(config.get("VERCEL") or config.get("VERCEL_ENV"))

app = FastAPI(title="L-Mart AI Loyalty Operations", version="0.1.0")

# The browser calls this API directly in development. In production both deploy
# under one origin, so the permissive list is scoped to localhost only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


@contextmanager
def connection():
    with db.pooled_connection() as conn:
        conn.execute("set time zone 'UTC'")
        yield conn


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> str:
    expected = config.app_api_key()
    if expected is None:
        if IS_SERVERLESS:
            # Deployed and unprotected is not a state worth tolerating quietly.
            raise HTTPException(500, "APP_API_KEY is not configured.")
        return "local"
    if x_api_key != expected:
        raise HTTPException(401, "Invalid or missing X-API-Key.")
    return "operator"


class AskRequest(BaseModel):
    question: str = Field(min_length=8, max_length=2000)


class RunSummary(BaseModel):
    run_id: str
    question: str
    status: str
    provider: str
    model: str
    steps_used: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    created_at: str
    completed_at: str | None


@app.get("/api/health")
def health() -> dict:
    """Cheap and dependency-aware: reports what is configured without calling
    any of it, so a health check never costs a model request."""
    from llm import factory
    return {
        "ok": True,
        "llm_provider_configured": config.get("LLM_PROVIDER", "anthropic"),
        "coe_gateway_configured": factory.coe_is_configured(),
        "agent_enabled": config.get_bool("AGENT_ENABLED", True),
        "auth_required": config.app_api_key() is not None,
    }


@app.post("/api/runs", status_code=201)
def create_run(body: AskRequest, actor: str = Depends(require_api_key)) -> dict:
    with connection() as conn:
        try:
            run_id = runtime.create_run(conn, body.question, actor=actor)
        except runtime.AgentDisabled as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"run_id": run_id, "status": "pending"}


@app.post("/api/runs/{run_id}/advance")
def advance(run_id: str, _: str = Depends(require_api_key)) -> dict:
    with connection() as conn:
        try:
            run = runtime.advance(conn, run_id)
        except KeyError as exc:
            raise HTTPException(404, "No such run.") from exc
        except ActionableError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _serialise(run)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, _: str = Depends(require_api_key)) -> dict:
    with connection() as conn:
        run = runtime.load_run(conn, run_id)
        if run is None:
            raise HTTPException(404, "No such run.")
        return _serialise(run)


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, _: str = Depends(require_api_key)) -> dict:
    with connection() as conn:
        runtime.cancel(conn, run_id)
        run = runtime.load_run(conn, run_id)
        if run is None:
            raise HTTPException(404, "No such run.")
        return _serialise(run)


@app.get("/api/runs")
def list_runs(limit: int = Query(25, ge=1, le=100),
              _: str = Depends(require_api_key)) -> dict:
    from psycopg.rows import dict_row
    with connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute("""
                select run_id, question, status, provider, model, steps_used,
                       input_tokens, output_tokens, cost_usd,
                       created_at, completed_at
                from ops.agent_runs order by created_at desc limit %s
            """, (limit,)).fetchall()
    return {"runs": [_scalars(r) for r in rows]}


def _scalars(row: dict) -> dict:
    """Make a row JSON-safe without dragging the whole trace through."""
    out = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif key == "cost_usd":
            out[key] = float(value)
        else:
            out[key] = str(value) if key in ("run_id", "parent_run_id") else value
    return out


def _serialise(run: dict) -> dict:
    """The full trace, as the UI consumes it.

    The SQL inside each tool result is deliberately kept: this endpoint serves
    the operator's trace viewer, which exists so any number in an answer can be
    followed back to the statement that produced it. The MODEL never saw it --
    that separation happens in the tool envelope, not here.
    """
    return {
        **_scalars({k: v for k, v in run.items()
                    if k not in ("steps", "tool_calls")}),
        "steps": [_scalars(s) for s in run["steps"]],
        "tool_calls": [_scalars(c) for c in run["tool_calls"]],
    }


# ---------------------------------------------------------------------------
# Proposals, approval and execution (Milestone 3 and 4)
# ---------------------------------------------------------------------------
# These endpoints are the human half of the system. The agent can create a
# draft; only a person reaches anything below.

from actions import approval as approval_actions       # noqa: E402
from actions import proposals as proposal_actions      # noqa: E402


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = Field(default=None, max_length=2000)


class ExecuteRequest(BaseModel):
    # Supplied by the client so a retry after a timeout is provably the same
    # request. Generated server-side it would be a new key every attempt, which
    # is exactly the failure idempotency exists to prevent.
    idempotency_key: str = Field(min_length=8, max_length=128)


@app.get("/api/proposals")
def list_proposals(limit: int = Query(25, ge=1, le=100),
                   status: str | None = None,
                   _: str = Depends(require_api_key)) -> dict:
    from psycopg.rows import dict_row
    with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("""
            select proposal_id, run_id, status, name, programme, channel,
                   offer_type, offer_value, audience_size, created_by,
                   created_at, updated_at,
                   (validation->>'ok')::boolean as valid
            from ops.campaign_proposals
            where (%s::text is null or status = %s)
            order by created_at desc limit %s
        """, (status, status, limit)).fetchall()
    return {"proposals": [_scalars(r) for r in rows]}


@app.get("/api/proposals/{proposal_id}")
def get_proposal(proposal_id: str, _: str = Depends(require_api_key)) -> dict:
    from psycopg.rows import dict_row
    with connection() as conn:
        try:
            proposal = proposal_actions.load(conn, proposal_id)
        except proposal_actions.ProposalError as exc:
            raise HTTPException(404, str(exc)) from exc
        with conn.cursor(row_factory=dict_row) as cur:
            approvals = cur.execute(
                "select * from ops.approvals where proposal_id=%s "
                "order by created_at desc", (proposal_id,)).fetchall()
            executions = cur.execute(
                "select * from ops.campaign_executions where proposal_id=%s "
                "order by started_at desc", (proposal_id,)).fetchall()
    return {**_scalars(proposal),
            "approvals": [_scalars(a) for a in approvals],
            "executions": [_scalars(e) for e in executions]}


@app.post("/api/proposals/{proposal_id}/revalidate")
def revalidate_proposal(proposal_id: str,
                        _: str = Depends(require_api_key)) -> dict:
    """Re-run the checks against the world as it is now.

    Exposed because the approval screen must not show a stale verdict: people
    opt out and tiers change, and an approver deciding on yesterday's validation
    is deciding on yesterday.
    """
    with connection() as conn:
        try:
            proposal, validation = proposal_actions.revalidate(conn, proposal_id)
        except proposal_actions.ProposalError as exc:
            raise HTTPException(404, str(exc)) from exc
    return {**_scalars(proposal), "validation": validation.to_dict()}


@app.post("/api/proposals/{proposal_id}/decision")
def decide_proposal(proposal_id: str, body: DecisionRequest,
                    actor: str = Depends(require_api_key)) -> dict:
    with connection() as conn:
        try:
            outcome = approval_actions.decide(
                conn, proposal_id, decision=body.decision, actor=actor,
                note=body.note)
        except proposal_actions.ProposalError as exc:
            raise HTTPException(404, str(exc)) from exc
        except approval_actions.ApprovalError as exc:
            # 409: the request is well-formed, the state forbids it.
            raise HTTPException(409, str(exc)) from exc
    return {"approval_id": outcome["approval_id"],
            "decision": outcome["decision"],
            "proposal": _scalars(outcome["proposal"])}


@app.post("/api/proposals/{proposal_id}/execute")
def execute_proposal(proposal_id: str, body: ExecuteRequest,
                     actor: str = Depends(require_api_key)) -> dict:
    with connection() as conn:
        try:
            return approval_actions.execute(
                conn, proposal_id, idempotency_key=body.idempotency_key,
                actor=actor)
        except proposal_actions.ProposalError as exc:
            raise HTTPException(404, str(exc)) from exc
        except approval_actions.ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc


@app.get("/api/audit")
def audit_log(limit: int = Query(50, ge=1, le=200),
              subject_id: str | None = None,
              _: str = Depends(require_api_key)) -> dict:
    from psycopg.rows import dict_row
    with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("""
            select * from ops.audit_log
            where (%s::text is null or subject_id = %s)
            order by occurred_at desc limit %s
        """, (subject_id, subject_id, limit)).fetchall()
    return {"entries": [_scalars(r) for r in rows]}
