# Planted truths — the answer key

L-Mart's data is synthetic, and the causal structure inside it was designed, not
discovered. This document records what is actually true, so that agent answers
can be graded against fact rather than plausibility.

**Keep this out of the agent's context.** It is the answer key.

Every number below was produced by `python -m seed.ablate`, which rebuilds the
dataset with one mechanism disabled at a time and measures how much of the
decline disappears. That is a counterfactual — impossible on real data, available
here only because we own the data-generating process. The attribution is
therefore measured, not asserted.

---

## The headline question

> *"Why has engagement among Gold customers in the UK dropped over the last three
> months?"*

Focus period **2026-06 → 2026-08**, against **2025-06 → 2025-08** year over year.

| Metric (GB Gold, per member per month) | 2025 | 2026 | Change |
| -------------------------------------- | ---- | ---- | ------ |
| Orders                                  | 1.627 | 1.019 | **−37.3%** |
| Net revenue                             | £161.93 | £98.17 | **−39.4%** |
| Active rate (≥1 order in month)         | 75.2% | 64.0% | −11.2pp |

The decline is real on both metrics. It is also **mostly not a behaviour change.**

---

## Measured attribution

Averaged over five seeds; `sd` is the spread across seeds on the orders metric.

| Cause | Orders/member | Revenue/member | sd |
| ----- | ------------- | -------------- | -- |
| **C3** — February tier review (composition) | **+18.4pp** | +17.6pp | 2.6 |
| **C1** — reactivation programme stopped | **+11.8pp** | +11.4pp | 2.1 |
| **C2** — GB Beauty stockout | **+5.5pp** | +6.9pp | 2.6 |
| **R3** — points expiry policy change | +0.4pp | −0.4pp | 2.6 |

Contributions do not sum to the headline: the causes interact, and the
composition effect changes *which people* the other causes are measured on.

---

## The real causes

### C3 — The February 2026 tier review *(largest, and not a behaviour change)*

On **2026-02-01**, 45 of the highest-value GB Gold members were promoted to
Platinum. They were the most frequent shoppers in the tier. Nobody changed how
they shop — the *set of people called Gold* changed, and Gold's averages fell.

- **How to find it:** `tier_history`. A spike of 45 `upgrade` rows dated
  2026-02-01. Then re-run the metric on a **fixed cohort** — whoever was Gold in
  January 2026, regardless of tier today.
- **The discriminating evidence:** the fixed cohort declines **−16.2%**, against
  **−37.3%** for "currently Gold". About **21pp of the headline is composition.**
- **Only reachable via `tier_history`.** An agent reading
  `loyalty_accounts.current_tier` cannot find this, and will attribute the whole
  decline to behaviour.

### C1 — The UK Gold Reactivation programme stopped *(largest actionable cause)*

A monthly email to lapsed GB Gold members ran for 21 waves, **2024-09 → 2026-05**,
then stopped when its budget was reallocated. Median audience 109 per wave, with
real incremental lift.

- **How to find it:** `campaigns`, filtered to `programme = 'UK Gold
  Reactivation'`. **This is an absence, not an event** — there are simply no rows
  after 2026-05-05. Nothing flags it; you have to look for what should be there.
- **The trap:** marketing did *not* stop. The **Global Rewards Digest** kept
  sending monthly through 2026-08. "Campaigns stopped" is wrong; "*this*
  reactivation programme stopped" is right.
- **The second trap:** the Digest shows a healthy conversion rate and has **zero
  incremental lift** — its conversions are orders that would have happened anyway.
  Conversion rate is not evidence of effectiveness.

### C2 — GB Beauty stockout *(smallest real cause)*

From **2026-04-01**, 65% of Beauty SKUs became unavailable in GB. GB Gold members
over-index on Beauty by 2.6×, so it hit them hardest.

- **How to find it:** revenue by category for GB. Beauty falls **−66.5%**
  (£19,957/month in Jan–Mar → £6,683/month in the focus period).
- Moves both metrics: lines drop out of baskets, and a basket that empties
  entirely becomes a trip that never happened.

---

## The red herrings

### R1 — GB total revenue is UP

GB net revenue rose **+6.1%** year over year (£709,559 → £752,750), because Silver
and Platinum grew. **An agent that checks only country-level totals will conclude
nothing is wrong.** The problem is only visible once you segment by tier.

### R2 — The global channel shift

App share rose from **31.1%** (2026-05) to **41.3%** (2026-08) as store share fell.
Real, dramatic, and perfectly correlated with the timing — but it applies to
**every country and every tier equally**, so it cannot explain a GB-Gold-specific
decline. Ruling it out requires a comparison against another cohort, not merely
observing that the dates line up.

### R3 — The points expiry policy change *(the hardest one)*

On **2026-02-01** L-Mart shortened points validity from 18 months to 12 and
applied it retroactively. **599,620 points expired in a single day** — 3.9× a
typical month, and the most dramatic single event anywhere in the dataset. It
lands four months before the decline, and hits long-tenured Gold savers hardest.

**It explains nothing.** Measured contribution +0.4pp, indistinguishable from
noise at sd 2.6.

This is deliberate and it was measured, not assumed. An agent that names it as
the cause has mistaken **magnitude for relevance** — the most common failure in
real analysis, and the one this dataset is built to punish.

### Also available to rule out

- **Store closures:** there are none. Every store has `closed_on` null.
- **Tenure:** 85% of customers predate the window, so the cohort is not diluted
  by recent joiners.
- **Seasonality:** real and substantial (December is 1.35×, February 0.86×).
  Period-over-period comparison against Mar–May is confounded; **year-over-year is
  the correct method**, and an agent that uses the wrong one should be marked down.

---

## Grading a good answer

A strong answer must:

1. Confirm the decline is real, **on both frequency and spend**.
2. Identify that **~21pp of ~37pp is composition**, not behaviour — and say so
   explicitly, using a fixed cohort.
3. Name the **reactivation programme stop** as the largest actionable cause, and
   distinguish it from "marketing stopped".
4. Name the **Beauty stockout** as a real but smaller contributor.
5. **Rule out** the app shift (affects all cohorts) and the points expiry (large
   but non-causal), with reasons rather than by silence.
6. Not be fooled by rising GB total revenue.
7. Use **year-over-year**, not period-over-period, and be able to say why.

Partial credit is meaningful here: naming C1 and C2 while missing C3 is a
plausible-sounding answer that is wrong about the majority of the effect. That is
exactly the failure mode worth measuring.

---

## Regenerating

```bash
python -m seed.generate   # ~1.5s, deterministic (RANDOM_SEED in seed/config.py)
python -m seed.verify     # asserts every signal above is present
python -m seed.ablate     # ~60s, re-measures the attribution table
```

If you change anything in `seed/config.py`, re-run all three — and update the
numbers in this document from the output, rather than the other way round.
