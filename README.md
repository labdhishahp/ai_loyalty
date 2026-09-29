# L-Mart AI Loyalty Operations

An AI analyst for a fictional omnichannel retailer's loyalty operations. It takes
a business objective in natural language, investigates enterprise data through a
controlled set of capabilities, reads the relevant policies, reasons over the
evidence, and files structured proposals that a human approves before anything
executes.

The question it is built to answer:

> *"Why has engagement among Gold customers in the UK dropped over the last three
> months?"*

and then:

> *"Create a campaign to re-engage them."*

The governing principle:

> The LLM chooses **which questions to ask** and **how to interpret the answers**.
> It never computes a number, never authors SQL, never decides whether something
> is allowed, and never writes to the business database.

See [docs/architecture.md](docs/architecture.md) for the reasoning behind each
decision, and [docs/deployment.md](docs/deployment.md) for running it on Vercel.

## What works

| | |
| --- | --- |
| **Data** | 12-table business schema, ~119k orders over 24 months, four planted causes and three red herrings, with each cause's contribution **measured** by counterfactual ablation |
| **Metrics** | 11 versioned definitions over hand-written SQL, with `CohortSpec` point-in-time cohort resolution |
| **Tools** | 10 capabilities behind one registry: nine read, one write |
| **Agent** | A durable run, one turn per HTTP request, with step / token / cost budgets and a kill switch |
| **Knowledge** | 22 documents, hybrid retrieval (metadata filter + full-text + pgvector), recall@3 = 0.955 |
| **Actions** | Proposals validated by a deterministic policy engine against rules held in one place |
| **Approval** | Hash-bound human approval, re-validation at execution, idempotent execution, append-only audit |
| **UI** | Investigation, live trace, evidence you can click, approval review, audit |
| **Tests** | 226, every one of them with no model calls at all |

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill in what the current milestone needs
python db/migrate.py
python -m seed.load --reset     # generate and COPY ~390k rows
python -m seed.verify           # integrity + every planted signal
python -m knowledge.build_corpus && python -m knowledge.ingest --reset
```

```bash
# API
uvicorn api.app:app --port 8000
# UI, in web/
cp .env.example .env.local && npm install && npm run dev
```

## Running it

```bash
python -m agent ask "Why has engagement among Gold customers in the UK dropped?"
python -m agent show <run_id>

python -m metrics compare orders_per_member --country GB --tier GOLD --as-of period_end
python -m eval.run_retrieval    # retrieval quality, no model calls
python -m eval.grade            # score completed runs, no model calls
pytest                          # database tests skip without .env
```

## The idea the project is built around

`seed/ablate.py` rebuilds the dataset with one mechanism disabled at a time and
measures how much of the decline disappears. That is a counterfactual — impossible
on real data, available here only because we own the data-generating process — and
it means the eval's answer key is evidence rather than assertion.

It also corrected the design twice. The tier review turned out to dominate
(+18.7pp) rather than the campaign stop (+12.4pp), and the points expiry measured
within noise (+2.3pp on orders, −3.7pp on spend, sd ~2.8), so it was reclassified
from a cause into the dataset's hardest red herring rather than tuned up into one.

## Where it currently stands

One investigation, scored against the answer key:

```
claude-sonnet-5, 6 steps, $0.289          SCORE 14/22 (64%)

  PASS  Confirms the decline; measures both frequency and spend
  PASS  Compares year-over-year rather than period-over-period
  PASS  Finds the stopped reactivation programme, and cites the budget memo
        recording the decision
  PASS  Every cause cites the evidence behind it
  MISS  Rules OUT the composition effect — which is the largest single cause
  MISS  Misses the GB Beauty availability gap
  MISS  Never mentions the points expiry or the channel shift
```

That is the honest baseline, and it is the point of having one. The agent checked
cohort size month by month through 2026 and correctly found it stable — but only
from March, after the February tier review it was looking for. A real analytical
error, caught by a dataset designed to catch it.

## Layout

```
seed/         deterministic generator, ablation study, SQL verifier
metrics/      11 metric definitions, CohortSpec, engine, CLI
tools/        the registry: the only way the model touches anything
llm/          provider boundary — Anthropic, OpenAI-compatible, and a scripted fake
agent/        system prompt, structured findings, the durable run loop
knowledge/    corpus, embeddings, hybrid retrieval
actions/      audience resolution, policy engine, proposals, approval, execution
api/          FastAPI surface
web/          Next.js UI
eval/         retrieval eval, answer key, grader
docs/         architecture, planted truths, deployment, MCP
```
