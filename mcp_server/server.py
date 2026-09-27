"""An MCP server built over the existing tool registry.

WHAT MCP SOLVES HERE. Without it, every client that wants L-Mart's capabilities
needs custom integration code, and every capability needs adapting per client --
the M x N problem. MCP is one protocol both sides already speak, so a tool
written once is reachable from Claude Desktop, an internal service, or anything
else, without either side knowing about the other.

WHAT IT DOES NOT DO: it does not replace tool calling. The model still emits a
tool call and reads a result; MCP only changes where the definitions live and
how the call travels. Our own agent therefore does NOT go through it -- see
below.

ADAPTER, NOT A REWRITE. Every tool here is the registry's tool. The schema comes
from the same Pydantic model, the arguments are validated by the same code, and
execution goes through the same REGISTRY.dispatch() the agent uses. If the two
could drift, the adapter would be worse than useless: it would look like the
same capability while behaving differently.

WHY THE AGENT DOES NOT CONSUME ITS OWN TOOLS OVER MCP. The Messages API can call
a remote MCP server directly, which sounds tidy and would be a mistake. The
application would never see the individual tool calls, so ops.tool_calls -- the
trace that makes every number in an answer auditable, that the eval reads, and
that the UI renders -- would go blind. The trace is the spine of this project;
trading it for less dispatch code is a bad deal. In-process for the agent, MCP
for everyone else.

TOOLS AND RESOURCES ARE DIFFERENT PRIMITIVES, and both are exposed because the
difference is the point:

  a TOOL is an action a model chooses to take, with arguments it decides on --
  search_knowledge("discount ceiling for gold") runs a query;

  a RESOURCE is addressable content a client can list and read by URI --
  lmart://knowledge/campaign-governance-policy is a document you attach, with no
  model involved in choosing it.

Same corpus, different primitive. A tools-only server would not show you what
MCP adds over plain tool calling.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any

import anyio.to_thread
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer

from core import db
from core.auth import Principal
from tools.envelope import json_safe
from tools.registry import REGISTRY, ToolContext

# Registering the tools is a side effect of importing these modules, and it has
# to happen here. Relying on the agent having been imported first would make the
# MCP server's tool list depend on import order elsewhere -- and would drag the
# agent runtime into a process that has no agent in it.
import tools.action_tools     # noqa: E402,F401
import tools.catalog          # noqa: E402,F401
import tools.knowledge_tools  # noqa: E402,F401

from .auth import (SupabaseTokenVerifier, principal_from_access_token,
                    stdio_principal)

log = logging.getLogger("lmart.mcp")

# submit_findings ends an agent run. An MCP client has no run, so exposing it
# would offer a capability that cannot mean anything here.
NOT_EXPOSED = {"submit_findings"}

INSTRUCTIONS = """\
L-Mart loyalty operations: investigate customer behaviour, read company policy,
and draft campaign proposals.

Start with get_reference_data -- filter values are exact codes ('GB', not 'UK'),
and a wrong code returns an empty result that looks like 'nothing happened'.

Measurements are fixed and tested; you choose which one and what to put in it.
Where a cohort is defined by something that changes over time, such as loyalty
tier, you must say as of when.

Creating a campaign proposal produces a DRAFT for human approval. Nothing here
sends anything to a customer.
"""


def _signature_from_model(tool) -> tuple[list, dict]:
    """Mirror a Pydantic input model as a plain function signature.

    MCP derives a tool's schema from the callable's signature, and our schemas
    live in Pydantic models. Rather than restating each model as MCP parameters
    -- two definitions that would drift -- the signature is generated from the
    model, so the MCP schema and the registry schema are the same statement.
    """
    params, annotations = [], {}
    for name, field in tool.input_model.model_fields.items():
        default = (inspect.Parameter.empty if field.is_required()
                   else field.get_default(call_default_factory=True))
        params.append(inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY,
            default=default, annotation=field.annotation))
        annotations[name] = field.annotation
    return params, annotations


def _make_handler(tool_name: str, principal_for_call):
    """Wrap a registry tool as an MCP callable."""
    tool = REGISTRY.get(tool_name)
    params, annotations = _signature_from_model(tool)

    async def handler(**kwargs: Any) -> dict:
        principal: Principal = principal_for_call()
        # Arguments carrying an explicit null are dropped so Pydantic applies
        # the model's default rather than being handed None for a field that
        # does not accept it.
        arguments = {k: v for k, v in kwargs.items() if v is not None}

        # Every tool is database-bound and psycopg here is synchronous, so the
        # work goes to a worker thread. Running it inline would block the event
        # loop and serialise every concurrent MCP client behind whichever query
        # happens to be slowest.
        def run() -> Any:
            with db.pooled_connection() as conn:
                conn.execute("set time zone 'UTC'")
                return REGISTRY.dispatch(tool.name, arguments, ToolContext(
                    conn=conn,
                    actor=principal.actor,
                    # No run: an MCP call is not part of an investigation.
                    # Anything it writes is attributed to the caller.
                    run_id=None,
                    allow_writes=tool.mutates,
                    scopes=principal.scopes,
                ))

        outcome = await anyio.to_thread.run_sync(run)
        # MCP read calls are not written to ops.tool_calls -- that table records
        # steps of a run, and there is no run here. Logged instead so an
        # operator can still see who called what.
        log.info("mcp %s tool=%s ok=%s", principal.actor, tool.name, outcome.ok)
        return json_safe(outcome.for_model())

    handler.__signature__ = inspect.Signature(params)
    handler.__annotations__ = annotations
    handler.__name__ = tool.name
    handler.__doc__ = tool.description
    return handler


DOCUMENT_SQL = """
    select slug, title, doc_type, jurisdiction, effective_from, body
    from knowledge.documents
    where effective_to is null
    order by doc_type, slug
"""


def build(principal_for_call=None, *, name: str = "lmart",
          server: MCPServer | None = None) -> MCPServer:
    """Construct the server. `principal_for_call` decides identity per call.

    `server` lets a caller supply a pre-configured MCPServer -- the HTTP variant
    needs auth settings at construction time -- so both transports are furnished
    with the same tools and resources from one place.
    """
    if principal_for_call is None:
        # Over HTTP the verified token is in the request context; over stdio it
        # comes from configuration. Resolving it per call rather than per server
        # is what lets one server instance serve many callers.
        def principal_for_call() -> Principal:
            access = get_access_token()
            if access is not None:
                return principal_from_access_token(access)
            return stdio_principal()

    if server is None:
        server = MCPServer(name=name, version="0.1.0", instructions=INSTRUCTIONS)

    for spec in REGISTRY.schemas(include_writes=True):
        if spec["name"] in NOT_EXPOSED:
            continue
        server.add_tool(_make_handler(spec["name"], principal_for_call),
                        name=spec["name"], description=spec["description"])

    # --- resources ------------------------------------------------------
    # Current policy only: a superseded document has effective_to set and must
    # not be readable as though it still applied.
    @server.resource("lmart://knowledge/{slug}",
                     name="L-Mart knowledge document",
                     description="A current policy, playbook, guideline, "
                                 "post-mortem or memo, by slug.",
                     mime_type="text/markdown")
    async def knowledge_document(slug: str) -> str:
        def fetch():
            with db.pooled_connection() as conn:
                return conn.execute(
                    "select title, doc_type, jurisdiction, effective_from, "
                    "body from knowledge.documents "
                    "where slug = %s and effective_to is null",
                    (slug,)).fetchone()

        row = await anyio.to_thread.run_sync(fetch)
        if row is None:
            raise ValueError(f"No current document '{slug}'.")
        title, doc_type, jurisdiction, effective_from, body = row
        return (f"# {title}\n\n*{doc_type} · {jurisdiction} · effective "
                f"{effective_from}*\n\n{body}")

    @server.resource("lmart://knowledge",
                     name="L-Mart knowledge index",
                     description="Every current document, with its URI.",
                     mime_type="application/json")
    async def knowledge_index() -> str:
        def fetch():
            with db.pooled_connection() as conn:
                return conn.execute(DOCUMENT_SQL).fetchall()

        rows = await anyio.to_thread.run_sync(fetch)
        return json.dumps([
            {"uri": f"lmart://knowledge/{slug}", "slug": slug, "title": title,
             "doc_type": doc_type, "jurisdiction": jurisdiction,
             "effective_from": str(effective_from)}
            for slug, title, doc_type, jurisdiction, effective_from, _ in rows
        ], indent=2)

    return server


def build_http() -> MCPServer:
    """The server as mounted in the API, authenticated by bearer token.

    Identical tools and resources to the stdio server -- the only difference is
    where identity comes from. Over a network the token IS the trust boundary,
    so it is verified on every request by the same code the REST API uses.

    validate_token_resource is off because the tokens are Supabase session
    tokens, which carry no RFC 8707 resource indicator. Requiring one would
    reject every legitimate token; the audience and issuer are already checked
    during verification.
    """
    from mcp.server.auth.settings import AuthSettings

    from core import config

    base = config.require("SUPABASE_URL")
    public = config.get("PUBLIC_BASE_URL", "http://localhost:8000")

    bare = MCPServer(
        name="lmart", version="0.1.0", instructions=INSTRUCTIONS,
        token_verifier=SupabaseTokenVerifier(),
        auth=AuthSettings(
            issuer_url=f"{base}/auth/v1",
            resource_server_url=f"{public}/mcp",
            validate_token_resource=False,
        ),
    )
    # Identity per request comes from the verified token in the auth context.
    return build(server=bare)
