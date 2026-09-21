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

**Milestone 1, slice 1 — the data foundation.** Complete.

- 12-table business schema for customers, loyalty, transactions, points and campaigns
- A deterministic generator producing ~118k orders and ~266k order lines over 24 months
- Four causal mechanisms and three red herrings planted in that data
- A verifier that asserts every planted signal is present and findable
- An ablation study that **measures** each cause's contribution, so the eval
  answer key is evidence rather than assertion

Not built yet: metrics layer, tool registry, agent runtime, operations tables.
Those land in the slices that need them.

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env          # add your Supabase connection string
python db/migrate.py          # create the lmart schema
```

## Working with the dataset

```bash
python -m seed.generate       # ~1.5s  -> CSVs in seed/out/
python -m seed.verify         # ~2s    assert every planted signal is present
python -m seed.load --reset   # ~10s   COPY into Postgres
python -m seed.ablate         # ~60s   re-measure each cause's contribution
```

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
seed/verify.py   grades the dataset, independently of the schema
seed/ablate.py   counterfactual attribution for the answer key
docs/            architecture and the answer key
```
