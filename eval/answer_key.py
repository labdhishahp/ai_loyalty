"""What a good answer to the headline question contains.

Derived from docs/planted-truths.md, whose numbers were MEASURED by
seed/ablate.py rather than asserted. Each criterion is one thing a competent
analyst would do, scored independently so partial credit is meaningful --
naming the campaign stop while missing the composition effect is a
plausible-sounding answer that is wrong about the largest single factor, and a
single pass/fail would hide exactly that.

MATCHING IS KEYWORD-BASED AND DELIBERATELY TRANSPARENT. An LLM judge would be
another model's opinion about a model, non-deterministic, and would cost money
on every eval run. Keywords are crude, and they are auditable: when a criterion
scores wrong you can see precisely why. The structured answer format is what
makes this workable -- causes, ruled_out and the basis fields are separate, so a
criterion checks the field it is actually about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Criterion:
    key: str
    description: str
    weight: int
    check: Callable[[dict], bool]


def _text(*values) -> str:
    return " ".join(str(v) for v in values).lower()


def _causes(answer: dict) -> str:
    return _text(*[f"{c['name']} {c['explanation']}" for c in answer["causes"]])


def _ruled_out(answer: dict) -> str:
    return _text(*[f"{r['name']} {r['why_not']}" for r in answer["ruled_out"]])


def _any(haystack: str, *needles: str) -> bool:
    return any(n in haystack for n in needles)


CRITERIA: tuple[Criterion, ...] = (
    Criterion(
        "decline_is_real", "Confirms the decline rather than disputing it", 1,
        lambda a: a["verdict"] in ("confirmed", "partly_confirmed")),

    Criterion(
        "both_metrics", "Measures frequency AND spend, not just one", 2,
        lambda a: _any(_text(a["metrics_used"]), "orders_per_member")
        and _any(_text(a["metrics_used"]), "revenue_per_member")),

    Criterion(
        "year_over_year", "Compares year-over-year, not period-over-period", 2,
        lambda a: _any(_text(a["comparison_basis"]),
                       "year-over-year", "year over year", "yoy",
                       "same period a year", "same months a year")),

    # The largest single factor, worth ~18pp of a ~37pp decline, and reachable
    # only through tier_history.
    Criterion(
        "composition_effect",
        "Identifies the February tier review as a cause of the fall in "
        "per-member averages", 3,
        lambda a: _any(_causes(a) + _text(a["cohort_basis"]),
                       "tier review", "promot", "composition")
        and not _any(_ruled_out(a), "composition", "tier review")),

    Criterion(
        "fixed_cohort",
        "Measures a cohort fixed before the tier review, not only the current "
        "one", 2,
        lambda a: _any(_text(a["cohort_basis"]), "fixed", "held", "as of",
                       "point in time")),

    Criterion(
        "campaign_stop", "Identifies the stopped reactivation programme", 3,
        lambda a: _any(_causes(a), "reactivation", "campaign", "programme")),

    Criterion(
        "campaign_specificity",
        "Says THIS programme stopped, not that marketing stopped", 1,
        lambda a: _any(_causes(a), "uk gold reactivation", "reactivation")),

    Criterion(
        "beauty_stockout", "Identifies the GB Beauty availability gap", 2,
        lambda a: _any(_causes(a), "beauty", "stockout", "availability",
                       "out of stock")),

    Criterion(
        "ruled_out_points_expiry",
        "Dismisses the points expiry with a reason rather than by silence", 2,
        lambda a: _any(_ruled_out(a), "points", "expir")),

    Criterion(
        "ruled_out_channel_shift",
        "Dismisses the app/channel shift by showing it affects every cohort", 1,
        lambda a: _any(_ruled_out(a), "app", "channel")),

    Criterion(
        "evidence_cited", "Every cause cites at least one call_id", 2,
        lambda a: bool(a["causes"])
        and all(c["evidence_call_ids"] for c in a["causes"])),

    Criterion(
        "states_limits", "States what it could not determine", 1,
        lambda a: len(a.get("limitations", "")) > 40),
)

TOTAL_WEIGHT = sum(c.weight for c in CRITERIA)
