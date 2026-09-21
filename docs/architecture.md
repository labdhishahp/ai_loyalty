# L-Mart AI Loyalty Operations Employee — Architecture

An AI analyst for a fictional omnichannel retailer's loyalty operations. It takes
a business objective in natural language, investigates enterprise data through a
controlled set of capabilities, reasons over the evidence, and — eventually —
files structured, validated proposals that a human approves before anything
executes.

Not a chatbot over a database. The distinction is not cosmetic; it drives every
decision below.

## The governing principle

> The LLM chooses **which questions to ask** and **how to interpret the answers**.
> It never computes a number, never authors SQL, never decides whether something
> is allowed, and never writes to the business database.

Two subsystems with a hard boundary between them:

|                | Investigation (read)          | Operation (write)                          |
| -------------- | ----------------------------- | ------------------------------------------ |
| Failure cost   | A wrong answer                 | A wrong campaign sent to 40,000 people     |
| Reversibility  | Free — ask again               | Expensive or impossible                    |
| Who decides    | The model may roam             | Model proposes → code validates → human approves |

Almost every "the AI agent did something bad" story is a system that blurred
these two.

## Target architecture

```
  Client (Next.js on Vercel)
    ask · watch the run trace · review and approve
                  │
  API (FastAPI, Python, Vercel functions)
    POST /runs                  create a run
    POST /runs/{id}/advance     execute ONE agent turn, persist, return
    GET  /runs/{id}             the full trace
    POST /proposals/{id}/approve
         │                                   │
  Agent runtime                      Action pipeline (no LLM)
    Claude Opus 5                      proposal → validate → approve → execute
    step + cost budget                 → audit log
    persists every step
         │                                   │
  ── Tool layer ── the only way the model touches anything ──
    registry: name → {JSON Schema, validator, handler, authz, mutates?}
         │                     │                    │
  Metrics layer         Knowledge retrieval     Policy engine
  (versioned SQL)       (hybrid search)         (rules as data)
         └─────────────────────┴────────────────────┘
                          Supabase / Postgres
       business tables · knowledge · operations tables (runs, steps, proposals)
```

## Decisions and why

**A metrics layer, not text-to-SQL.** Business metrics are *definitions*, not
queries. If the model re-derives "active customer" each time, numbers are not
reproducible and evals are meaningless. An LLM writing SQL against a 12-table
schema will also produce plausible, wrong joins — fan-out on `order_items`
double-counts revenue and nothing errors. The model picks a named metric and
supplies dimensions and filters; the SQL is ours, versioned and unit-tested.
This is what makes "how do you stop it hallucinating numbers?" answerable with
*it structurally cannot — it never sees the SQL.*

**Business rules in the database and in validators, never in the prompt.** A
prompt is a suggestion with ~95% compliance. A validator is a guarantee. Rules
must be right every time, be auditable afterwards, and return the same result on
the same inputs; a prompt gives none of those three and a function gives all of
them for free.

**Durable runs.** The agent loop is a state machine persisted in Postgres,
advanced one turn per HTTP call — not a `while` loop in memory. This fits
serverless execution limits, and it makes the trace the execution substrate
rather than logging bolted on afterwards. Observability, resumability, and eval
replay all fall out of it.

**The agent does not consume its own tools over MCP.** The Messages API can call
a remote MCP server directly, but then the application never sees the individual
tool calls and the trace goes blind. The trace is the spine of this project. One
registry, two transports: in-process dispatch for the agent, MCP for external
clients.

## Sequencing

Capabilities are ordered by dependency and **diagnosability**, not difficulty:
a capability enters as soon as an eval exists that can isolate its contribution.
Every capability ships its *seam* in Milestone 1 even when its implementation
doesn't, so nothing later requires a rewrite.

| # | Milestone | Adds |
| - | --------- | ---- |
| **1** | Investigation spine | Schema, seeded data, metrics layer, tool registry, durable runs, agent loop, trace UI, gold eval |
| 1b | MCP transport | MCP server over the same registry; Claude Desktop as an interactive tool-test surface |
| 2 | Knowledge | Document corpus, hybrid retrieval (metadata filter + pgvector + full-text), retrieval eval |
| 3 | Proposals | `campaign_proposals`, policy engine, validation, audience resolution |
| 4 | Approval & execution | Auth, per-tool authorization, signed approvals, audit log, idempotent execution |
| 5 | Multi-agent fan-out | Parallel hypothesis sub-runs — **shipped only if it beats the single loop on the eval** |
| 6 | Production | Regression evals in CI, cost dashboards, failure taxonomy |

### Seams present from Milestone 1

| Capability   | Seam |
| ------------ | ---- |
| MCP          | Registry stores schema/validator/handler/authz separately from dispatch |
| Knowledge    | Trace evidence rows carry a `source_type` |
| Actions      | Registry partitioned read/write; the write partition is empty and visibly so |
| Multi-agent  | `parent_run_id` on runs; the loop takes `(tools, seed_context, budget)` as parameters |

## Deliberately not used

| Technology | Why not |
| ---------- | ------- |
| LangChain / LangGraph | Abstracts the exact layer this project exists to understand. A durable state machine in Postgres is less code and fully legible. |
| A dedicated vector DB | pgvector lives in the Postgres we already run, and keeps knowledge joinable to business data. |
| An ORM | The metrics layer *is* hand-written SQL. An ORM would sit between us and the queries we are trying to make precise. |
| Redis / queues / Celery | Postgres (`LISTEN/NOTIFY`, `FOR UPDATE SKIP LOCKED`) covers this scale. |
| Fine-tuning | No training data, a moving schema, and prompt + tools + retrieval will outperform it. |

## Current state

**Milestone 1, slice 1 complete:** business schema, deterministic seed generator
loading directly into Supabase Postgres, verification in SQL covering both
structural integrity and the planted signals, and a measured causal attribution
for the answer key. See [planted-truths.md](planted-truths.md).

**Postgres is the source of truth from this slice onward.** An earlier version
kept generated CSVs as the primary dataset and verified against those files. That
had a blind spot exactly where the risk was: CSV checks pass on data the schema
might not even accept, so column types, `CHECK` constraints, foreign keys and
NULL handling went untested. Verifying in SQL also exercises the same joins,
indexes and fan-out hazards the metrics layer is about to depend on, and enables
integrity checks that flat files cannot express.

**Milestone 1, slice 2 complete:** the metrics layer. 11 versioned metric
definitions over hand-written SQL, with `CohortSpec` as its central abstraction.

`CohortSpec` is where slice 1.1's finding became architecture. "Gold customers
in the UK" is ambiguous, and in this data the ambiguity is worth ~21 percentage
points, so `tier_as_of` is required rather than defaulted: a fixed date holds the
cohort still, `"period_end"` re-resolves it per period. The model will have to
choose, the choice lands in the trace, and the eval can mark it.

The engine separates SQL assembly from execution, so the rules that matter are
testable without a database. The metrics reproduce every number in the answer key
via SQL written independently of `seed/verify.py` -- two implementations agreeing
to the decimal, which is a real cross-check rather than a regression baseline.

Not yet built: tool registry, agent runtime, operations tables.
Operations tables are deliberately absent — no consumer exists yet, and they will
land in the slice that uses them.
