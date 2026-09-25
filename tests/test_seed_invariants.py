"""Invariants the generated tier history must hold.

Checked on the in-memory build rather than the loaded database: no credentials,
about a second, and a violation is attributed to the generator rather than to
whatever loaded it.

The invariant these exist for is the one that failed silently. A tier span
ending before it starts overlaps nothing, so the existing overlap check passed
it. The point-in-time join in metrics/cohort.py requires

    effective_from <= as_of  AND  (effective_to is null OR effective_to > as_of)

which an inverted span can never satisfy, so those members simply disappeared
from every cohort. No error, no double count -- just quietly absent people.
"""

from __future__ import annotations

from datetime import date

import pytest

from seed import config, generate
from seed.simulate import TIER_HISTORY_COLUMNS


@pytest.fixture(scope="module")
def built():
    customers, _, tables = generate.build(quiet=True)
    index = {name: i for i, name in enumerate(TIER_HISTORY_COLUMNS)}
    spans = [{name: row[i] for name, i in index.items()}
             for row in tables.tier_history]
    enrolled = {c.account_id: c.enrolled_on
                for c in customers if c.account_id is not None}
    return spans, enrolled


def _date(value):
    return date.fromisoformat(value) if value else None


def test_every_span_ends_after_it_starts(built):
    """The defect. A closed span must cover a non-empty interval -- a tier held
    for no time, or ending before it began, is not a tier anyone held."""
    spans, _ = built
    inverted = [s for s in spans
                if s["effective_to"]
                and _date(s["effective_to"]) <= _date(s["effective_from"])]
    assert inverted == [], f"{len(inverted)} inverted or zero-length span(s)"


def test_no_span_starts_before_its_account_existed(built):
    """The root cause, stated directly: a review promoted members who had not
    yet enrolled, which is what produced the inverted spans."""
    spans, enrolled = built
    early = [s for s in spans
             if _date(s["effective_from"]) < enrolled[s["account_id"]]]
    assert early == [], f"{len(early)} span(s) predate enrolment"


def test_each_account_has_exactly_one_open_span(built):
    spans, enrolled = built
    open_per_account: dict[int, int] = {}
    for s in spans:
        if s["effective_to"] is None:
            open_per_account[s["account_id"]] = \
                open_per_account.get(s["account_id"], 0) + 1
    assert set(open_per_account) == set(enrolled)
    assert all(n == 1 for n in open_per_account.values())


def test_spans_for_one_account_do_not_overlap(built):
    spans, _ = built
    by_account: dict[int, list] = {}
    for s in spans:
        by_account.setdefault(s["account_id"], []).append(s)
    for account_id, rows in by_account.items():
        rows.sort(key=lambda r: _date(r["effective_from"]))
        for earlier, later in zip(rows, rows[1:]):
            assert earlier["effective_to"] is not None, account_id
            assert _date(earlier["effective_to"]) <= _date(later["effective_from"]), \
                account_id


def test_the_tier_review_still_promotes_a_meaningful_cohort(built):
    """The fix removes ineligible members; it must not remove the effect.

    The composition effect is the largest single factor in the planted answer,
    so a review that promoted almost nobody would quietly gut the dataset.
    """
    spans, _ = built
    promotions = [s for s in spans
                  if s["reason"] == "upgrade"
                  and _date(s["effective_from"]) == config.TIER_REVIEW_DATE]
    assert len(promotions) >= 30
    assert all(s["tier_code"] == "PLATINUM" for s in promotions)
