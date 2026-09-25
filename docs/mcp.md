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

There is no MCP-specific notion of permission. `ROLE_SCOPES` decides, the
registry enforces, and a write tool is refused identically whichever door the
caller came through.

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
