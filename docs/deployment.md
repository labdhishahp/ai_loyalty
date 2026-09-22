# Deployment

Two Vercel projects against one Supabase database.

## Why two projects, not one

The Python API and the Next.js app have different runtimes, different build
pipelines and different dependency files. One project each keeps both builds
trivial, and the frontend already talks to the API through a server-side proxy
that takes a `BACKEND_URL` — so a remote API needs no code change at all.

The cost is two deployments to keep in step. That is a smaller cost than a build
configuration that has to satisfy two toolchains at once.

## 1. The API

Deployed from the repository root. `vercel.json` routes every `/api/*` request to
`api/index.py`, which exports the FastAPI app.

```bash
vercel login
vercel --prod
```

Environment variables (Project Settings → Environment Variables):

| Variable | Notes |
| -------- | ----- |
| `DATABASE_POOL_URL` | **Required.** Transaction pooler, port 6543. The database allows 60 connections and serverless scales past that. |
| `DATABASE_URL` | Direct connection. Used by migrations; harmless here. |
| `APP_API_KEY` | **Required in production.** The API refuses to start without it when `VERCEL` is set, because an unprotected endpoint that runs model calls spends someone else's quota. |
| `LLM_PROVIDER` | `coe` or `anthropic`. |
| `COE_BASE_URL`, `COE_API_KEY`, `COE_MODEL` | When using the gateway. |
| `ANTHROPIC_API_KEY` | Fallback, or when `LLM_PROVIDER=anthropic`. |
| `HF_TOKEN` | Embeddings for knowledge retrieval. |
| `AGENT_MAX_STEPS`, `AGENT_MAX_RUN_TOKENS`, `AGENT_MAX_RUN_COST_USD`, `AGENT_ENABLED` | Optional; code defaults apply. |

**`maxDuration` is 60 seconds and that is enough**, because a request executes
exactly one agent turn. The client repeats until the run finishes. A loop inside
one request would exceed any ceiling on a long investigation — the durable run
model is what makes a serverless deployment viable rather than a workaround.

## 2. The frontend

Deployed from `web/` (set it as the project's Root Directory).

| Variable | Notes |
| -------- | ----- |
| `BACKEND_URL` | The API deployment's origin, e.g. `https://lmart-api.vercel.app`. |
| `BACKEND_API_KEY` | Same value as `APP_API_KEY`. |

Neither is prefixed `NEXT_PUBLIC_`. That prefix compiles a value into the browser
bundle, and `BACKEND_API_KEY` is a secret — the proxy route handler exists
precisely so the browser never holds it.

## 3. Database

Migrations are applied from a machine with the **direct** connection string:

```bash
python db/migrate.py
python -m seed.load --reset      # only for a fresh database
python -m seed.verify
python -m knowledge.build_corpus && python -m knowledge.ingest --reset
```

Supabase's dashboard offers connection strings in a Prisma flavour that appends
`?pgbouncer=true`. libpq has no such parameter; `core/db.py` strips it, so either
form can be pasted.

## Not configured, deliberately

**No email provider.** Execution writes a real campaign and real per-recipient
send records; no message leaves the system. Every decision worth engineering is
upstream of the send.

**No cron.** Nothing in the system needs a schedule yet. Metric rollups would be
the first candidate, and only once query latency justifies them.
