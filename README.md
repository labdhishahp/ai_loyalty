# L-Mart AI Loyalty Operations Employee

An AI analyst for a fictional omnichannel retailer's loyalty operations. It takes
a business objective in natural language, investigates enterprise data through a
controlled set of capabilities, reasons over the evidence, and — eventually —
files structured, validated proposals that a human approves before anything runs.

The question it is built to answer:

> *"Why has engagement among Gold customers in the UK dropped over the last three
> months?"*

and then, once that is answered well:

> *"Create a campaign to re-engage them."*

The governing principle:

> The LLM chooses **which questions to ask** and **how to interpret the answers**.
> It never computes a number, never authors SQL, never decides whether something
> is allowed, and never writes to the business database.

See [docs/architecture.md](docs/architecture.md) for the design and the reasoning
behind each decision.

## Status

**Milestone 1, slice 2 — the metrics layer.** Complete.

- 11 named, versioned metric definitions with hand-written SQL
- `CohortSpec`: point-in-time cohort resolution with a **required** `tier_as_of`
- An engine that validates requests and assembles SQL, separated from execution
  so the rules are testable with no database
- 43 tests: 21 offline (cohort rules, SQL assembly), 22 against Postgres
  reproducing every number in the answer key

**Milestone 1, slice 1 — the data foundation.** Complete.

- 12-table business schema for customers, loyalty, transactions, points and campaigns
- A deterministic generator producing ~118k orders and ~266k order lines over 24 months,
  loaded straight into Supabase Postgres
- Four causal mechanisms and three red herrings planted in that data
- A verifier running in SQL against the loaded database, covering both
  structural integrity and every planted signal
- An ablation study that **measures** each cause's contribution, so the eval
  answer key is evidence rather than assertion

Not built yet: metrics layer, tool registry, agent runtime, operations tables.
Those land in the slices that need them.

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"


cp .env.example .env          # add your Supabase connection string
python db/migrate.py          # create the lmart schema
```

## Configuration

`cp .env.example .env` and fill in what the current milestone needs. Every
variable is documented in the template with what it is and when it is first
required; the table below is the summary.

| Variable | Needed from | Why |
| -------- | ----------- | --- |
| `DATABASE_URL` | now | Direct connection (5432). Migrations, COPY and tests need a real session. |
| `DATABASE_POOL_URL` | slice 1.5 | Transaction pooler (6543). The database allows 60 connections and serverless scales past that. |
| `LLM_PROVIDER` | slice 1.5 | `coe` or `anthropic`. The agent, trace and eval never learn which answered. |
| `COE_BASE_URL` / `COE_API_KEY` / `COE_MODEL` | slice 1.5 | OpenAI-compatible gateway serving Qwen. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | optional | Only when `LLM_PROVIDER=anthropic`. |
| `AGENT_MAX_STEPS` / `AGENT_MAX_RUN_TOKENS` / `AGENT_MAX_RUN_COST_USD` / `AGENT_ENABLED` | optional | Bounded turns, token ceiling, spend cap, kill switch. Defaults live in code. |
| `HF_TOKEN` | Milestone 2 | Embeddings via `BAAI/bge-small-en-v1.5` (384-d). |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | optional | Dimension is derived from the model in code so the two cannot drift. |
| `APP_API_KEY` | slice 1.6 | Shared secret. A deployed API that runs model calls must not be open. |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | Milestone 4 | Supabase Auth for the approval workflow. |
| `NEXT_PUBLIC_*` | slice 1.6 | The only values that reach a browser. The anon key is public by design; the service role key must never appear here. |

A **blank** value means unset, everywhere — `core/config.py` enforces that, so an
unfilled placeholder does not shadow a code default.

## Working with the dataset

```bash
python -m seed.load --reset   # generate in memory, COPY into Postgres
python -m seed.verify         # integrity + planted signals, in SQL
python -m seed.generate       # optional: build only, print counts, no database
python -m seed.ablate         # ~60s: re-measure each cause's contribution
pytest                        # database tests skip automatically without .env
```

## Asking the data a question

```bash
python -m metrics list

# the headline, on the cohort as it stands in each period
python -m metrics compare orders_per_member --country GB --tier GOLD --as-of period_end
#   -> 1.615 -> 1.012  (-37.3%),  cohort 259 -> 241

# the same question, cohort pinned before the February tier review
python -m metrics compare orders_per_member --country GB --tier GOLD --as-of 2026-01-15
#   -> 1.592 -> 1.333  (-16.2%),  cohort 266 -> 266
```

One parameter apart, twenty-one points apart. `tier_as_of` therefore has no
default: asking for a tier without saying as-of-when raises an error rather than
quietly picking a reading. That decision has to be made by whoever asks the
question, has to be visible in the trace, and has to be markable by the eval.

**Postgres is the source of truth.** The generator builds the dataset in memory
and streams it straight into Supabase — there is no intermediate file, so there
is never a question of which copy is current.

`seed/verify.py` checks two things against the loaded database: that the data
obeys its own rules (order headers equal the sum of their lines, no points
balance ever goes negative, no overlapping tier spans), and that every planted
causal signal is present and findable. The first group is only possible against
a real database, and it is why verification lives there rather than in files.

`seed/ablate.py` is the deliberate exception: it runs 25 full simulations to
measure each cause counterfactually, and routing those through the network would
turn a one-minute study into many minutes for no benefit. Ablation is a question
about the *generator*, not about storage.

Generation is deterministic: the same `RANDOM_SEED` produces byte-identical
output. That is a correctness requirement, not a convenience — evals compare agent
runs against a fixed answer key, so a dataset that moved between runs would make
score changes uninterpretable.

**`docs/planted-truths.md` is the answer key. Keep it out of the agent's context.**

## Layout

```
db/migrations/   numbered SQL, applied once, never edited after the fact
db/migrate.py    the runner (no ORM: the metrics layer will be hand-written SQL)
seed/config.py   every tunable parameter, and every planted cause, in one file
seed/simulate.py the chronological day-by-day simulation
seed/generate.py the in-memory pipeline (library + a no-database smoke test)
seed/load.py     generate and COPY straight into Postgres
seed/verify.py   integrity and signal checks, in SQL against the database
seed/ablate.py   counterfactual attribution, in memory, for the answer key
metrics/cohort.py   who is being measured (the tier_as_of decision lives here)
metrics/catalog.py  the 11 metric definitions, each small enough to read
metrics/engine.py   request validation and SQL assembly, separate from execution
docs/            architecture and the answer key
```
