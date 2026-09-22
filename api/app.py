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
