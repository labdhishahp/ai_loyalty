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
pip install -e .

cp .env.example .env          # add your Supabase connection string
python db/migrate.py          # create the lmart schema
```

## Working with the dataset

```bash
python -m seed.load --reset   # generate in memory, COPY into Postgres
python -m seed.verify         # integrity + planted signals, in SQL
python -m seed.generate       # optional: build only, print counts, no database
python -m seed.ablate         # ~60s: re-measure each cause's contribution
```

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
docs/            architecture and the answer key
```
