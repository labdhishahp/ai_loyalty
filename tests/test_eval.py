"""The grader.

The defect these exist for: criteria used to be a flat module-level tuple that
the grader applied to every completed run, so a run that asked a different
question was marked down for omitting causes it was never asked about. The score
looked like a measurement and was not one, and it made "we have no rubric for
this" indistinguishable from "this answer was poor".

No database, no model. The grader is a pure function of an answer and a rubric,
which is what makes it cheap to trust.
"""

from __future__ import annotations

import pytest

from eval.answer_key import (GOLD_QUESTIONS, HEADLINE, PROPOSAL,
                             GoldQuestion, match)
from eval.grade import grade

HEADLINE_QUESTION = ("Why has engagement among Gold customers in the UK "
                     "dropped over the last three months?")
PROPOSAL_QUESTION = (
    "The UK Gold Reactivation email programme stopped after its May 2026 wave. "
    "Check what our playbooks and campaign policy say, then propose a campaign "
    "to re-engage lapsed GB Gold members.")

# An answer that satisfies every criterion, so a shortfall in a test is a
# shortfall in the grader rather than in the fixture.
PERFECT = {
    "headline": "It fell, and roughly half of that is composition.",
    "verdict": "confirmed",
    "metrics_used": ["orders_per_member", "revenue_per_member"],
    "comparison_basis": "year over year, because the business is seasonal",
    "cohort_basis": "held fixed as of 2026-01-15, and also re-resolved per period",
    "causes": [
        {"name": "February tier review", "explanation": "A promotion moved the "
         "highest-value members out, so composition changed.",
         "evidence_call_ids": ["toolu_a"], "importance": "largest",
         "confidence": "high"},
        {"name": "UK Gold Reactivation stopped", "explanation": "The "
         "reactivation programme's last wave was May 2026.",
         "evidence_call_ids": ["toolu_b"], "importance": "significant",
         "confidence": "high"},
        {"name": "GB Beauty stockout", "explanation": "Beauty availability "
         "collapsed from April.", "evidence_call_ids": ["toolu_c"],
         "importance": "minor", "confidence": "medium"},
    ],
    "ruled_out": [
        {"name": "Points expiry", "why_not": "The expiry was large but shows no "
         "measurable effect.", "evidence_call_ids": ["toolu_d"]},
        {"name": "App channel shift", "why_not": "The app shift affects every "
         "country and tier equally.", "evidence_call_ids": ["toolu_e"]},
    ],
    "limitations": "Could not separate pricing effects from availability effects.",
    "recommended_next": "Reinstate the programme with a holdout.",
}

WEAK = {
    "headline": "Nothing much happened.",
    "verdict": "inconclusive",
    "metrics_used": ["orders_per_member"],
    "comparison_basis": "compared against the previous quarter",
    "cohort_basis": "Gold customers",
    "causes": [],
    "ruled_out": [],
    "limitations": "",
    "recommended_next": "",
}


# ------------------------------------------------------------- matching

def test_the_headline_question_matches_its_rubric():
    assert match(HEADLINE_QUESTION) is HEADLINE


def test_a_different_question_about_the_same_segment_gets_its_own_rubric():
    """The defect, pinned. Both questions mention GB Gold; only one asks why
    engagement fell, and they have different right answers.

    When this was first fixed the proposal question had no rubric at all, so
    the assertion was that it scored nothing. It has one now, so the property
    worth holding is stronger: it must get ITS rubric, never the headline's.
    """
    assert match(PROPOSAL_QUESTION) is PROPOSAL
    assert match(PROPOSAL_QUESTION) is not HEADLINE


@pytest.mark.parametrize("question", [
    "How many customers do we have in Singapore?",
    "What is the maximum discount for Gold members?",
    "Propose a campaign for lapsed Gold members in the UK.",
    "",
])
def test_unrelated_questions_are_not_scored(question):
    assert match(question) is None


def test_matching_ignores_case_and_spacing():
    assert match("  WHY  has ENGAGEMENT among gold customers FALLEN?  ") is HEADLINE


def test_every_gold_question_matches_its_own_canonical_text():
    """A rubric that cannot recognise the question it was written for would
    silently score nothing."""
    for gold in GOLD_QUESTIONS:
        assert gold.matches(gold.question), gold.key


# -------------------------------------------------------------- grading

def test_a_complete_answer_earns_every_point():
    earned, results = grade(PERFECT, HEADLINE)
    missed = [c.key for c, passed in results if not passed]
    assert missed == [], f"criteria not met by a deliberately complete answer: {missed}"
    assert earned == HEADLINE.total_weight


def test_a_weak_answer_earns_little():
    earned, _ = grade(WEAK, HEADLINE)
    assert 0 <= earned <= 4


def test_scoring_uses_the_rubric_it_is_given_not_a_global_one():
    """The structural fix: criteria arrive as an argument, so an answer cannot
    be scored against a question it was not asked."""
    single = GoldQuestion(
        key="single", question="x", identifies=(("x",),),
        criteria=(HEADLINE.criteria[0],))
    earned, results = grade(PERFECT, single)
    assert len(results) == 1
    assert earned == single.total_weight < HEADLINE.total_weight


def test_a_malformed_answer_scores_zero_rather_than_crashing():
    """A run can fail in ways that leave the answer incomplete. Grading must
    report that, not raise."""
    earned, results = grade({}, HEADLINE)
    assert earned == 0
    assert all(not passed for _, passed in results)


def test_partial_credit_distinguishes_a_plausible_wrong_answer():
    """Naming the campaign stop while ruling OUT the composition effect is the
    failure mode the weighting exists to expose, so it must not score full."""
    plausible = {**PERFECT,
                 "causes": [PERFECT["causes"][1]],
                 "ruled_out": PERFECT["ruled_out"] + [
                     {"name": "Composition", "why_not": "Cohort size was stable.",
                      "evidence_call_ids": []}]}
    earned, _ = grade(plausible, HEADLINE)
    assert 0 < earned < HEADLINE.total_weight


# --------------------------------------------------------------- rubric

@pytest.mark.parametrize("gold", GOLD_QUESTIONS, ids=lambda g: g.key)
def test_every_rubric_is_internally_consistent(gold):
    keys = [c.key for c in gold.criteria]
    assert len(keys) == len(set(keys)), "duplicate criterion keys"
    assert all(c.weight > 0 for c in gold.criteria)
    assert gold.total_weight == sum(c.weight for c in gold.criteria)


def test_no_two_rubrics_can_claim_the_same_question():
    """The whole grader defect in one property. Two rubrics that both match a
    question would make the score depend on declaration order, which is how a
    confidently wrong number gets produced."""
    for gold in GOLD_QUESTIONS:
        claimants = [g.key for g in GOLD_QUESTIONS if g.matches(gold.question)]
        assert claimants == [gold.key], f"{gold.key} is ambiguous: {claimants}"


def test_the_proposal_rubric_does_not_grade_the_investigation_question():
    """A proposal is not a diagnosis. Marking it down for failing to name the
    beauty stockout would be the original defect wearing a new rubric."""
    subjects = " ".join(c.key + c.description for c in PROPOSAL.criteria).lower()
    for absent in ("beauty", "stockout", "points expiry", "year-over-year",
                   "channel shift"):
        assert absent not in subjects


def test_the_proposal_rubric_rewards_the_playbook_not_a_vocabulary_match():
    """An answer that echoes the question back must not score. The question
    itself contains "propose", "lapsed", "GB Gold" and "campaign"."""
    echo = {
        "headline": "I will propose a campaign for lapsed GB Gold members.",
        "verdict": "confirmed", "metrics_used": [], "comparison_basis": "",
        "cohort_basis": "lapsed GB Gold members", "causes": [],
        "ruled_out": [], "limitations": "", "recommended_next": "",
    }
    earned, _ = grade(echo, PROPOSAL)
    assert earned == 0, "echoing the question scored points"
