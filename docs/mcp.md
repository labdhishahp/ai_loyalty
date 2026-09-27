# MCP

## What it solves here

Without MCP, every client that wants L-Mart's capabilities needs its own
integration code, and every capability needs adapting per client — the M×N
problem. MCP is one protocol both sides already speak, so a tool written once is
reachable from Claude Desktop, an internal service, or anything else, and
neither side needs to know about the other.

**It does not replace tool calling.** The model still emits a tool call and reads
a result. MCP changes only *where the definitions live* and *how the call
travels*. Adopting it changed nothing in `agent/runtime.py`.

## Where the server sits

```
Claude Desktop / Inspector          any remote MCP client
        │ stdio                              │ Streamable HTTP
        ▼                                    ▼
   mcp_server/server.py ── built from REGISTRY ──┐
        │                                        │
        ▼                                        │
   ToolContext(actor, scopes, run_id=None) ◄─────┘
        │
        ▼
   REGISTRY.dispatch()   ← the same dispatcher the agent uses
        │
        ▼
   metrics / knowledge / actions / Postgres
```

`mcp_server/server.py` is an **adapter**. Every tool is the registry's tool, the
schema is generated from the same Pydantic model, arguments are validated by the
same code, and execution goes through the same `REGISTRY.dispatch()`. If the two
could drift, the adapter would be worse than useless — it would look like the
same capability while behaving differently.

## Why the agent does not use it

The Messages API can call a remote MCP server directly. That sounds tidy and
would be a mistake here: the application would never see the individual tool
calls, so `ops.tool_calls` would go blind — and that table is what makes every
number in an answer auditable, what the eval reads, and what the trace viewer
renders.

**In-process for the agent, MCP for everyone else.** A test asserts it
(`test_the_agent_does_not_route_through_mcp`) rather than leaving it to a comment.

## Tools and resources are different primitives

Both are exposed, because the difference is the point:

| | Tool | Resource |
|---|---|---|
| What | An action a model chooses to take | Content a client addresses by URI |
| Example | `search_knowledge("discount ceiling")` | `lmart://knowledge/campaign-governance-policy` |
| Who decides | The model, mid-answer | The client, before asking anything |
| In Claude Desktop | Something the model calls | Something you attach |

Same corpus, different primitive. A tools-only server would not show you what MCP
adds over plain tool calling.

**9 tools** — the registry's, minus `submit_findings`, which ends an agent run
and would be meaningless to a client that has no run.

**Resources** — `lmart://knowledge` (an index) and `lmart://knowledge/{slug}`
(any current document). Superseded policy is not served: the safest route to a
withdrawn rule should not be asking for it by name.

## Identity — and the thing worth understanding

Moving a tool behind a protocol **moves the trust boundary**. In-process, the
agent's identity was implicit: whoever started the run. Over a network it has to
be carried, verified, and refused.

| | stdio | Streamable HTTP |
|---|---|---|
| Trust boundary | The transport — you launched it, on your machine | The token — anyone can reach the port |
| Identity | `MCP_ACTOR` / `MCP_ROLE`, **read-only by default** | Supabase JWT, or the service key |
| Verification | None needed | `core/auth.py`, the same code the REST API uses |

There is no MCP-specific notion of permission: `ROLE_SCOPES` decides and the
registry enforces, the same code on every route.

### How writes are authorised over MCP, precisely

In-process, `dispatch()` puts two independent gates in front of a write tool:

| Gate | Belongs to | Question it answers |
|---|---|---|
| `allow_writes` | the **run** | Was this investigation started with writing enabled? |
| `scopes` | the **caller** | May this person or service write at all? |

**Only the second gate does any work over MCP.** An MCP call is not part of a
run — there is no investigation to have been started read-only — so the adapter
passes `allow_writes=tool.mutates` (`mcp_server/server.py`), which satisfies the
run gate by construction. Its refusal, *"this tool changes data and this run is
read-only"*, can never fire on an MCP call.

That is not a hole, and it is worth being clear about why. `allow_writes` was
never an authorization control; it is a per-run *narrowing* of authority the
caller already holds, which is why `POST /api/runs` refuses `allow_writes: true`
to anyone without `propose`. The control that decides whether a write is
permitted is `scopes`, and it applies identically here: an MCP caller holding
only `read` is refused `create_campaign_proposal` at dispatch, with the same
message a run would get.

The per-run narrowing still exists over MCP — it is just expressed where a
runless protocol can express it, on the principal:

* **stdio** starts at `{read}` and stays there unless `MCP_ALLOW_WRITES=true`.
  A laptop session cannot write by accident.
* **HTTP** takes scopes from `ops.user_roles`, so an analyst (`{read, propose}`)
  can draft a proposal and an approver can do more, exactly as over REST.

### What genuinely differs

Not authority, but **provenance**. A proposal created through MCP has
`run_id = NULL` and is attributed to the caller rather than to an investigation,
so it carries no `evidence_call_ids` linking it back to measured numbers. It is
still an inert draft that a human with `approve` and `execute` must sign off,
and the content hash still binds that approval — but a reviewer opening it will
find a proposal with no trace behind it. Worth knowing before trusting one.

## Running it

**stdio**, for local clients:

```bash
python -m mcp_server
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "lmart": {
      "command": "/absolute/path/to/ai_loyalty/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/absolute/path/to/ai_loyalty",
      "env": { "MCP_ACTOR": "your-name-laptop", "MCP_ROLE": "analyst" }
    }
  }
}
```

**HTTP**, mounted in the API at `/mcp`:

```bash
uvicorn api.app:app --port 8000
# then connect a client to http://127.0.0.1:8000/mcp/
# with Authorization: Bearer <supabase token | APP_API_KEY>
```

## One implementation note worth keeping

Every handler runs its database work in a worker thread. psycopg here is
synchronous, and calling it inline from an async handler stalls the event loop.
That surfaced as a *request timeout* with no obvious cause — and only for real
user tokens, because the service-key path short-circuits before touching the
database or fetching signing keys. Concurrency: five simultaneous clients now
complete in about four seconds.
