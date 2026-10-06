# Deployment

One Vercel project, one production URL, one Supabase database.

## How one project serves both halves

Vercel reads a single Root Directory (`.`) and does two independent things with
it: it detects Next.js from `package.json` and builds the frontend, and it turns
`api/*.py` into Python serverless functions. So both halves ship from one build.

```
/                 Next.js page          (app/page.tsx)
/runs/<id>        Next.js page
/bff/*            Next.js route handler — holds the API key, forwards server-side
/api/*            Python function       (vercel.json rewrite -> api/index)
/mcp, /mcp/*      Python function       (same)
```

This is why the Next.js app lives at the repository root rather than in `web/`:
Vercel looks for the framework and for `api/` in the *same* folder, so a frontend
one level down is never built. It is also why the proxy sits at `/bff` and not
under `app/api/` — the Python function already owns `/api/*`, and two owners for
one prefix is a precedence question nobody should have to answer at deploy time.

`vercel.json` pins `"framework": "nextjs"` so the setting lives in the repository
rather than only in dashboard state.

## 1. Deploying

Deployed from the repository root. `vercel.json` routes every `/api/*` and
`/mcp/*` request to `api/index.py`, which exports the FastAPI app.

```bash
vercel login
vercel --prod
```

Environment variables (Project Settings → Environment Variables):

| Variable | Notes |
| -------- | ----- |
| `SUPABASE_URL` | **Required to import the app, not just to serve a request.** `api/app.py` builds the MCP server at module scope, which calls `require("SUPABASE_URL")`. Missing, the function fails to import and *every* route returns 500 — including `/api/health`. |
| `DATABASE_POOL_URL` | **Required.** Transaction pooler, port 6543. The database allows 60 connections and serverless scales past that. On Vercel the code now *refuses* to fall back to the direct connection, because that fallback works in a smoke test and exhausts the limit under load. |
| `DATABASE_URL` | Direct connection. Used by migrations; harmless here. |
| `APP_API_KEY` | **Required in production.** Without it, a deployed API refuses every authenticated request with a 500 rather than serving one unauthenticated. `/api/health` stays reachable on purpose, so a misconfigured deployment can still be diagnosed. |
| `ANTHROPIC_API_KEY` | Required to run an investigation. |
| `HF_TOKEN` | Required for knowledge retrieval (embeddings). |
| `PUBLIC_BASE_URL` | This deployment's own origin, e.g. `https://lmart-api.vercel.app`. Used for the MCP resource-server URL advertised to clients. Defaults to `http://localhost:8000`, which is wrong everywhere except a laptop. |
| `LLM_PROVIDER` | `anthropic` (default) or `openai_compatible`. |
| `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY`, `OPENAI_COMPAT_MODEL` | Only when `LLM_PROVIDER=openai_compatible`. This path has never been exercised against a real gateway — see `llm/openai_compatible.py`. |
| `ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT` | Optional; code defaults apply. |
| `AGENT_MAX_STEPS`, `AGENT_MAX_RUN_TOKENS`, `AGENT_MAX_RUN_COST_USD`, `AGENT_ENABLED` | Optional; code defaults apply. |
| `LLM_TIMEOUT_SECONDS` | Optional, default 50. Must stay below `maxDuration`. |
| `EMBEDDING_TIMEOUT_SECONDS`, `EMBEDDING_BUDGET_SECONDS` | Optional, default 15 and 30. Must stay below `maxDuration`. |
| `MCP_HTTP_ENABLED` | Optional, default true. `false` returns 404 from `/mcp` and leaves the REST API alone. |
| `RATE_LIMIT_RUNS_PER_HOUR`, `RATE_LIMIT_PROPOSALS_PER_HOUR` | Optional; default 20 and 10 per actor. `0` disables. |

`SUPABASE_SERVICE_ROLE_KEY` is deliberately **not** in this table. The only
thing that reads it is `scripts/users.py`, run from a laptop. Giving a
deployment a key that bypasses row level security, for work it never does, is
how blast radius grows quietly.

### One turn at a time

`POST /runs/{id}/advance` claims the run before it spends anything. Two
requests reaching the same run — a double-clicked button, a client retrying
after a platform timeout, two open tabs — used to *both* call the model and
then collide on the step table, so the loser failed only after the expensive
part was done. A single conditional `UPDATE` now decides it: Postgres
serialises the row, the loser matches no rows and returns **409** without
calling anything.

The claim is a lease (`ops.agent_runs.turn_claimed_at`, 90s against a 60s
`maxDuration`), not a lock. A serverless function can be killed without warning
and has no `finally` to run, so a lock would wedge the run permanently — a
worse failure than the double-spend it prevents. A claim older than any live
turn could be is taken over automatically.

Clients should treat 409 as "retry shortly", not as an error to show the user.

### MCP over HTTP

Mounted at `/mcp`, and **stateless when deployed**. Streamable HTTP normally
keeps per-client session state in memory: the client calls `initialize`, gets an
`Mcp-Session-Id`, and sends it with every later request. Vercel scales the
function to many instances with no affinity between them, so the instance that
issued a session id is usually not the one the next request reaches. On
`VERCEL`, `api/app.py` therefore selects `stateless_http` and `json_response` —
a fresh transport per request, no session to lose, no held-open stream a frozen
function cannot promise. A laptop keeps the stateful, streaming default.

Even stateless, the SDK's session manager must have been started from the ASGI
lifespan. Whether a given host runs lifespan is a property of the platform, so
`/mcp` checks rather than assumes: if it was never started the endpoint answers
**503 naming the reason**, and the REST API is unaffected. `MCP_HTTP_ENABLED=false`
turns the endpoint off entirely (404) without touching anything else.

This is the one part of the deployment that cannot be confirmed without
deploying — see the first-deploy checks below.

### The 60-second budget

**`maxDuration` is 60 seconds**, because a request executes exactly one agent
turn. The client repeats until the run finishes. A loop inside one request would
exceed any ceiling on a long investigation — the durable run model is what makes
a serverless deployment viable rather than a workaround.

Every outbound call is now bounded *below* that ceiling, so a slow dependency
produces a recorded failure instead of a function the platform kills silently:

| Call | Was | Now |
| ---- | --- | --- |
| Model request | SDK default 600s, 2 retries | `LLM_TIMEOUT_SECONDS`, default **50s**, no retries |
| Embeddings | 60s × 3 attempts + backoff ≈ **195s** | whole-call deadline, default **30s** |
| Tool SQL | `statement_timeout`, 30s | unchanged |

The model budget is set against measured turns rather than guessed: across the
30 steps recorded in `ops.agent_steps` the mean is 11.1s, the 95th percentile
24.6s and the slowest 37.0s.

**The honest limit:** a single turn combining a near-worst-case model call with
a cold embedding model can still exceed 60s in total. Each part fails cleanly on
its own budget, but the *sum* is not guaranteed to fit. If that shows up in
practice, raise `maxDuration` (up to 300s on Pro) rather than shrinking the
individual budgets, which would start failing turns that were going to succeed.

## 2. Frontend variables

Same project, same Environment Variables screen.

| Variable | Notes |
| -------- | ----- |
| `BACKEND_API_KEY` | Same value as `APP_API_KEY`. Used by the `/bff` handler. |
| `NEXT_PUBLIC_SUPABASE_URL` | **Read at build time.** |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | **Read at build time.** |
| `BACKEND_URL` | **Optional.** The `/bff` handler defaults to this deployment's own origin via `VERCEL_URL`, because the API *is* this deployment. Set it only to point at an API running elsewhere, e.g. a local uvicorn during `next dev`. |

`BACKEND_API_KEY` is not prefixed `NEXT_PUBLIC_`. That prefix compiles a value
into the browser bundle, and this is a secret — the `/bff` route handler exists
precisely so the browser never holds it.

**The `NEXT_PUBLIC_` pair is read at build time, not at runtime.** Next.js
compiles them into the bundle, so setting them after a deploy changes nothing
until the next build: the app would ship with sign-in permanently disabled, and
the symptom looks like a bug in the auth code rather than a missing variable.
`next.config.ts` refuses a production build without them, which is the last
moment that is cheap to fix.

### Why the browser never needs CORS

The browser calls `/api/proxy/*` on the **frontend's** origin. That route
handler holds `BACKEND_API_KEY` and forwards server-side to `BACKEND_URL`. So
the two projects being on different origins costs nothing, the secret never
reaches the browser, and the API's CORS list stays scoped to localhost for
development. If a production origin ever needs adding to that list, something
has started calling the API directly and the proxy has been bypassed.

## 3. Database

Migrations are applied from a machine with the **direct** connection string:

```bash
python db/migrate.py             # includes 0009 rate-limit indexes, 0010 turn claim
python -m seed.load --reset      # only for a fresh database
python -m seed.verify
python -m knowledge.build_corpus && python -m knowledge.ingest --reset
```

Supabase's dashboard offers connection strings in a Prisma flavour that appends
`?pgbouncer=true`. libpq has no such parameter; `core/db.py` strips it, so either
form can be pasted.

## 4. Checks on the first deploy

These are the things no local test can settle, because they are properties of
the platform rather than of this repository.

| Check | Expect | If it fails |
| ----- | ------ | ----------- |
| `GET /api/health` | 200 | The function did not import. Almost always a missing `SUPABASE_URL` or a dependency, both of which have tests, so suspect the environment first. |
| `GET /api/me` with `X-API-Key` | `role: service` | `APP_API_KEY` mismatch. |
| `GET /api/me` with a Supabase bearer token | the signed-in role | JWKS fetch, or no row in `ops.user_roles`. |
| One `POST /api/runs` → repeated `/advance` to terminal | completes | Watch for a turn killed at 60s: `LLM_TIMEOUT_SECONDS` should fire first. |
| A question that searches the corpus | cites a document | Embeddings; check `HF_TOKEN` and the budget. |
| Double-click advance | one 200, one 409 | The claim did not take. |
| Connections during parallel requests | well under 60 | `DATABASE_POOL_URL` not actually the pooler. |
| Trip the rate limiter | 429 with `Retry-After` | — |
| Frontend → proxy → API | renders a run | `BACKEND_URL` or `BACKEND_API_KEY`. |
| `POST /mcp/` `initialize`, then a follow-up call | both succeed | **The one genuine unknown.** A 503 naming the lifespan means the host does not run ASGI lifespan; set `MCP_HTTP_ENABLED=false` and MCP stays available over stdio, with the REST API unaffected. |

Cold start is worth timing once. The function imports `anthropic`, `openai`,
`mcp` and `fastapi`, which is most of its startup cost. `openai` is only needed
for the OpenAI-compatible provider, which has never run against a real gateway; dropping it
would be the first thing to try if cold starts are a problem.

## Not configured, deliberately

**No email provider.** Execution writes a real campaign and real per-recipient
send records; no message leaves the system. Every decision worth engineering is
upstream of the send.

**No cron.** Nothing in the system needs a schedule yet. Metric rollups would be
the first candidate, and only once query latency justifies them.
