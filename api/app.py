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
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agent import runtime
from tools.registry import REGISTRY
from core import config, db
from core.auth import (APPROVE, EXECUTE, PROPOSE, READ, SERVICE_PRINCIPAL,
                       AuthError, Principal, principal_from_token)
from core.errors import ActionableError
from core.rate_limit import RateLimited
from core import rate_limit
from mcp_server.server import build_http

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lmart.api")

IS_SERVERLESS = config.is_serverless()

# ---------------------------------------------------------------------------
# MCP, mounted alongside the REST API rather than run as a separate service.
#
# Same process, same database pool, same auth: an MCP caller is verified by the
# code that verifies a REST caller, so there is one answer to "who may do what"
# rather than two that can drift. A separate deployment would double the
# configuration and the surface for no benefit at this size.
#
# The agent does NOT route through this. It dispatches in-process, so every tool
# call lands in ops.tool_calls; going through MCP would hide those calls from
# the application and blind the trace the whole project is built around.
# ---------------------------------------------------------------------------
# An operator's kill switch. Changing it redeploys on Vercel either way, but it
# means turning MCP off is a setting rather than a code change at a bad moment.
MCP_HTTP_ENABLED = config.get_bool("MCP_HTTP_ENABLED", True)

# STATELESS ON SERVERLESS, AND THIS IS THE WHOLE DIFFERENCE THAT MATTERS.
#
# Streamable HTTP normally keeps per-client session state in memory: a client
# calls initialize, gets an Mcp-Session-Id back, and sends it with every later
# request. That works for one long-lived process and cannot work here. Vercel
# scales the function to many instances with no affinity between them, so the
# instance that issued a session id is usually not the instance the next
# request reaches -- and the second request fails with a session it has never
# heard of. Recycling instances between requests produces the same failure.
#
# stateless_http builds a fresh transport per request and tracks no session, so
# any instance can serve any request. json_response goes with it: SSE exists to
# stream over a held-open connection, which is exactly what a function that may
# be frozen after each response cannot promise.
#
# A laptop keeps the stateful, streaming default, because there the session IS
# one process and streaming is the better experience.
mcp_server = build_http()
mcp_app = mcp_server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=IS_SERVERLESS,
    json_response=IS_SERVERLESS,
)

# Whether the session manager's task group is actually running. Even in
# stateless mode `run()` must have been entered -- it owns the task group every
# request is dispatched through -- and it is entered from the ASGI lifespan.
# Whether a given serverless adapter runs lifespan at all is a property of the
# platform, not of this code, so it is checked rather than assumed.
_mcp_running = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _mcp_running
    if not MCP_HTTP_ENABLED:
        yield
        return
    # The streamable-HTTP session manager owns the task group requests are
    # dispatched through and must be running for the transport to work.
    # Mounting the app without this yields a route that accepts requests and
    # then fails on the first one.
    async with mcp_server.session_manager.run():
        _mcp_running = True
        try:
            yield
        finally:
            _mcp_running = False


async def mcp_endpoint(scope, receive, send) -> None:
    """The MCP transport, behind a check that it was actually started.

    Without this, a host that does not run ASGI lifespan gives a request that
    hangs or dies inside the SDK with nothing pointing at the cause. A 503
    naming the reason is the difference between a five-minute diagnosis and an
    afternoon.
    """
    if not MCP_HTTP_ENABLED:
        response = JSONResponse(
            {"detail": "MCP over HTTP is disabled here (MCP_HTTP_ENABLED)."},
            status_code=404)
        await response(scope, receive, send)
        return
    if not _mcp_running:
        response = JSONResponse(
            {"detail": "The MCP session manager is not running: this host did "
                       "not execute the ASGI lifespan. The REST API is "
                       "unaffected."},
            status_code=503)
        await response(scope, receive, send)
        return
    await mcp_app(scope, receive, send)


app = FastAPI(title="L-Mart AI Loyalty Operations", version="0.1.0",
              lifespan=lifespan)
app.mount("/mcp", mcp_endpoint)

# CORS is for development only, and that is a statement about the architecture
# rather than a shortcut. In production the browser never calls this API: it
# calls the Next.js route handler at /api/proxy/*, which holds the shared secret
# and forwards server-side. So there is no production origin to allow, and
# adding one would mean the browser had started talking to the API directly --
# which is the thing the proxy exists to prevent.
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


def require_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    """Identify the caller. Two accepted credentials, deliberately unequal.

    A Supabase bearer token identifies a PERSON, whose authority comes from
    ops.user_roles. The shared key identifies a SERVICE -- CI, the MCP server --
    and is weaker than any person: read and propose only. A campaign that
    reaches real customers requires a named human, and a shared secret names
    nobody.

    A bearer token is preferred when both are present, so a signed-in browser is
    never silently downgraded to the service identity.
    """
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        with connection() as conn:
            try:
                return principal_from_token(conn, token)
            except AuthError as exc:
                raise HTTPException(401, str(exc)) from exc

    expected = config.app_api_key()
    if expected is None:
        if IS_SERVERLESS:
            # Deployed and unprotected is not a state worth tolerating quietly.
            raise HTTPException(500, "APP_API_KEY is not configured.")
        return SERVICE_PRINCIPAL
    if x_api_key and x_api_key == expected:
        return SERVICE_PRINCIPAL
    raise HTTPException(401, "Sign in, or present a valid X-API-Key.")


def requires(scope: str):
    """Dependency factory: refuse the request unless the caller holds `scope`."""
    def dependency(principal: Principal = Depends(require_principal)) -> Principal:
        try:
            principal.require(scope)
        except AuthError as exc:
            # 403, not 401: we know who you are, you simply may not do this.
            raise HTTPException(403, str(exc)) from exc
        return principal
    return dependency


class AskRequest(BaseModel):
    question: str = Field(min_length=8, max_length=2000)
    # Opt-in per run. A run that only needs to investigate is never offered the
    # write tool, so it structurally cannot propose anything rather than merely
    # choosing not to.
    allow_writes: bool = False


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
        "openai_compatible_configured": factory.openai_compatible_is_configured(),
        "agent_enabled": config.get_bool("AGENT_ENABLED", True),
        "auth_required": config.app_api_key() is not None,
        # Whether identity is configured, not what it points at. This endpoint
        # has no auth -- on purpose, so a misconfigured deployment can still be
        # diagnosed -- so it reports booleans rather than values.
        "supabase_configured": config.get("SUPABASE_URL") is not None,
        "mcp_endpoint": "/mcp",
        "mcp_tools": len(REGISTRY.schemas(include_writes=True)) - 1,  # no submit_findings
    }


@app.get("/api/me")
def me(principal: Principal = Depends(require_principal)) -> dict:
    """Who the caller is and what they may do.

    The UI needs this to decide what to render. Hiding a button is a courtesy,
    not a control -- every scope is enforced again server-side -- but showing an
    approver's buttons to an analyst who will only be refused is a bad interface.
    """
    return {"actor": principal.actor, "role": principal.role,
            "scopes": sorted(principal.scopes)}


@app.post("/api/runs", status_code=201)
def create_run(body: AskRequest,
               principal: Principal = Depends(require_principal)) -> dict:
    with connection() as conn:
        try:
            # Before anything that costs money. A client stuck in a retry loop
            # is the realistic failure here, not an attacker.
            rate_limit.check(conn, "run", principal.actor)
            # A run may only be granted permissions its creator already holds.
            if body.allow_writes:
                principal.require(PROPOSE)
            run_id = runtime.create_run(
                conn, body.question, actor=principal.actor,
                allow_writes=body.allow_writes, scopes=principal.scopes)
        except RateLimited as exc:
            # 429 with Retry-After: a well-behaved client backs off instead of
            # hammering, which is the whole point of answering rather than
            # dropping the connection.
            raise HTTPException(429, str(exc),
                                headers={"Retry-After": str(exc.retry_after)}) from exc
        except runtime.AgentDisabled as exc:
            raise HTTPException(503, str(exc)) from exc
        except AuthError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"run_id": run_id, "status": "pending"}


@app.post("/api/runs/{run_id}/advance")
def advance(run_id: str, _: Principal = Depends(require_principal)) -> dict:
    with connection() as conn:
        try:
            run = runtime.advance(conn, run_id)
        except KeyError as exc:
            raise HTTPException(404, "No such run.") from exc
        except runtime.RunBusy as exc:
            # 409, not 400: the request is well formed and would be valid at
            # another moment. A double-clicked button should not look like a
            # malformed request, and a client can safely poll and retry.
            raise HTTPException(409, str(exc)) from exc
        except ActionableError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _serialise(run)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, _: Principal = Depends(require_principal)) -> dict:
    with connection() as conn:
        run = runtime.load_run(conn, run_id)
        if run is None:
            raise HTTPException(404, "No such run.")
        return _serialise(run)


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, _: Principal = Depends(require_principal)) -> dict:
    with connection() as conn:
        runtime.cancel(conn, run_id)
        run = runtime.load_run(conn, run_id)
        if run is None:
            raise HTTPException(404, "No such run.")
        return _serialise(run)


@app.get("/api/runs")
def list_runs(limit: int = Query(25, ge=1, le=100),
              _: Principal = Depends(require_principal)) -> dict:
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
                   _: Principal = Depends(require_principal)) -> dict:
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
def get_proposal(proposal_id: str,
                 _: Principal = Depends(require_principal)) -> dict:
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
                        _: Principal = Depends(require_principal)) -> dict:
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
                    principal: Principal = Depends(requires(APPROVE))) -> dict:
    with connection() as conn:
        try:
            outcome = approval_actions.decide(
                conn, proposal_id, decision=body.decision, actor=principal.actor,
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
                     principal: Principal = Depends(requires(EXECUTE))) -> dict:
    with connection() as conn:
        try:
            return approval_actions.execute(
                conn, proposal_id, idempotency_key=body.idempotency_key,
                actor=principal.actor)
        except proposal_actions.ProposalError as exc:
            raise HTTPException(404, str(exc)) from exc
        except approval_actions.ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc


@app.get("/api/audit")
def audit_log(limit: int = Query(50, ge=1, le=200),
              subject_id: str | None = None,
              _: Principal = Depends(require_principal)) -> dict:
    from psycopg.rows import dict_row
    with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("""
            select * from ops.audit_log
            where (%s::text is null or subject_id = %s)
            order by occurred_at desc limit %s
        """, (subject_id, subject_id, limit)).fetchall()
    return {"entries": [_scalars(r) for r in rows]}
