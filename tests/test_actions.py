"""Proposals, validation, approval and execution.

These are the tests that matter most in the project. Everything upstream is
advisory; this is where something becomes true about the world, and each test
below pins one guarantee that the design depends on.

No model is involved anywhere here -- that is the point. The safety of the
system does not rest on anything a model does or does not do.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from actions import approval, proposals
from actions.audience import AudienceSpec, resolve
from actions.policy_engine import validate
from actions.rules import DISCOUNT_CEILING_PCT, POINTS_MULTIPLIER_CEILING

GB_GOLD = {"countries": ["GB"], "tiers": ["GOLD"], "tier_as_of": "2026-01-15"}


@pytest.fixture
def clean(own_conn):
    """Remove only what THIS test created.

    An earlier version deleted every row in campaign_proposals, approvals,
    campaign_executions and audit_log. That is a cleanup that can reach data the
    test did not create, and it did: running the suite destroyed a proposal an
    actual investigation had produced. A test should never be able to delete
    something it did not make.

    So the ids present beforehand are recorded, and only the difference is
    removed. This holds however a row was created -- through a helper, a tool,
    or the API.
    """
    def ids(table, column):
        return {r[0] for r in own_conn.execute(
            f"select {column} from {table}").fetchall()}

    before = {
        "proposals": ids("ops.campaign_proposals", "proposal_id"),
        "approvals": ids("ops.approvals", "approval_id"),
        "executions": ids("ops.campaign_executions", "execution_id"),
        "audit": ids("ops.audit_log", "audit_id"),
    }
    yield own_conn

    own_conn.rollback()
    new_executions = ids("ops.campaign_executions", "execution_id") - before["executions"]
    campaigns = [r[0] for r in own_conn.execute(
        "select campaign_id from ops.campaign_executions "
        "where execution_id = any(%s) and campaign_id is not null",
        (list(new_executions),)).fetchall()] if new_executions else []

    # Referencing rows first: campaign_executions points at lmart.campaigns.
    for table, column, keep in (
            ("ops.campaign_executions", "execution_id", before["executions"]),
            ("ops.approvals", "approval_id", before["approvals"]),
            ("ops.campaign_proposals", "proposal_id", before["proposals"]),
            ("ops.audit_log", "audit_id", before["audit"])):
        current = ids(table, column)
        created = list(current - keep)
        if created:
            own_conn.execute(
                f"delete from {table} where {column} = any(%s)", (created,))
    if campaigns:
        own_conn.execute(
            "delete from lmart.campaign_events where campaign_id = any(%s)",
            (campaigns,))
        own_conn.execute("delete from lmart.campaigns where campaign_id = any(%s)",
                         (campaigns,))
    own_conn.commit()


def draft(conn, **overrides) -> dict:
    kwargs = dict(
        run_id=None, name="UK Gold Reactivation - October 2026",
        objective="Return lapsed GB Gold members to the store.",
        programme="UK Gold Reactivation", channel="email",
        offer_type="points_multiplier", offer_value=2.0,
        audience={**GB_GOLD, "lapsed_min_days": 21},
        holdout_pct=10, rationale="The programme that drove this stopped.",
        evidence_call_ids=["toolu_abc"])
    kwargs.update(overrides)
    return proposals.create(conn, **kwargs)


# ------------------------------------------------------------- audience

def test_customers_without_consent_are_excluded_not_filtered_later(clean):
    audience = resolve(clean, AudienceSpec.from_dict(GB_GOLD))
    assert audience.excluded_no_consent > 0
    ids = tuple(audience.customer_ids)
    opted_out = clean.execute(
        "select count(*) from lmart.customers where customer_id = any(%s) "
        "and not marketing_opt_in", (list(ids),)).fetchone()[0]
    assert opted_out == 0


def test_the_audience_is_the_same_cohort_the_metrics_measure(clean):
    """If targeting and measurement disagreed about who 'GB Gold' is, a campaign
    would be aimed at a different group from the one that was analysed."""
    audience = resolve(clean, AudienceSpec.from_dict(GB_GOLD))
    from metrics.cohort import build_cohort_sql
    sql, params = build_cohort_sql(
        AudienceSpec.from_dict(GB_GOLD).cohort(), date.today())
    cohort_ids = {r[0] for r in clean.execute(sql, params).fetchall()}
    assert set(audience.customer_ids) <= cohort_ids


def test_a_lapse_window_narrows_the_audience(clean):
    everyone = resolve(clean, AudienceSpec.from_dict(GB_GOLD))
    lapsed = resolve(clean, AudienceSpec.from_dict(
        {**GB_GOLD, "lapsed_min_days": 21}))
    assert 0 < lapsed.size < everyone.size


# -------------------------------------------------------- policy engine

def test_the_discount_ceiling_follows_the_lowest_tier_present(clean):
    """An offer reaches everyone in the audience, so the ceiling of the lowest
    tier in it applies -- not the tier that was asked for."""
    spec = AudienceSpec.from_dict({"countries": ["GB"]})   # all tiers
    audience = resolve(clean, spec)
    result = validate(clean, offer_type="percentage_discount",
                      offer_value=DISCOUNT_CEILING_PCT["GOLD"],
                      holdout_pct=10, audience=audience, spec=spec)
    failure = next(c for c in result.checks if c.rule == "discount_ceiling")
    assert failure.severity == "fail"
    assert "BRONZE" in failure.detail


def test_a_multiplier_above_the_ceiling_fails_with_the_limit_named(clean):
    spec = AudienceSpec.from_dict(GB_GOLD)
    audience = resolve(clean, spec)
    result = validate(clean, offer_type="points_multiplier",
                      offer_value=POINTS_MULTIPLIER_CEILING + 1,
                      holdout_pct=10, audience=audience, spec=spec)
    assert not result.ok
    assert str(POINTS_MULTIPLIER_CEILING) in result.failures[0].detail


def test_a_tiny_audience_fails_because_it_cannot_be_measured(clean):
    spec = AudienceSpec.from_dict(
        {**GB_GOLD, "lapsed_min_days": 3000})       # nobody
    audience = resolve(clean, spec)
    result = validate(clean, offer_type="points_multiplier", offer_value=2.0,
                      holdout_pct=10, audience=audience, spec=spec)
    assert not result.ok
    assert any(c.rule == "audience_minimum" for c in result.failures)


def test_every_check_names_the_policy_a_reader_can_go_and_read(clean):
    spec = AudienceSpec.from_dict(GB_GOLD)
    audience = resolve(clean, spec)
    result = validate(clean, offer_type="points_multiplier", offer_value=2.0,
                      holdout_pct=10, audience=audience, spec=spec)
    assert all(c.policy for c in result.checks)


# ------------------------------------------------------------- proposals

def test_a_proposal_is_validated_on_creation_not_on_request(clean):
    """A draft nobody validated looks identical to one that passed."""
    proposal = draft(clean)
    assert proposal["status"] == "draft"
    assert proposal["validation"]["ok"] is True
    assert proposal["audience_size"] > 0
    assert proposal["content_hash"]


def test_the_content_hash_covers_what_happens_and_not_the_prose(clean):
    a = draft(clean)
    b = draft(clean, rationale="Completely different wording.")
    c = draft(clean, offer_value=2.5)
    assert a["content_hash"] == b["content_hash"]   # prose does not change it
    assert a["content_hash"] != c["content_hash"]   # the offer does


# -------------------------------------------------------------- approval

def test_a_failing_proposal_cannot_be_approved(clean):
    proposal = draft(clean, offer_value=POINTS_MULTIPLIER_CEILING + 5)
    assert proposal["validation"]["ok"] is False
    with pytest.raises(approval.ApprovalError, match="fails validation"):
        approval.decide(clean, str(proposal["proposal_id"]),
                        decision="approved", actor="approver")


def test_editing_after_approval_invalidates_it(clean):
    """The guarantee the content hash exists for."""
    proposal = draft(clean)
    proposal_id = str(proposal["proposal_id"])
    approval.decide(clean, proposal_id, decision="approved", actor="approver")

    # Someone edits the offer after sign-off.
    clean.execute("""update ops.campaign_proposals
                       set offer_value = 3.0, content_hash = %s
                     where proposal_id = %s""",
                  ("tampered-hash", proposal_id))
    clean.commit()

    with pytest.raises(approval.ApprovalError, match="changed after it was approved"):
        approval.execute(clean, proposal_id,
                         idempotency_key=str(uuid.uuid4()), actor="approver")


def test_rejection_is_recorded_and_blocks_execution(clean):
    proposal = draft(clean)
    proposal_id = str(proposal["proposal_id"])
    approval.decide(clean, proposal_id, decision="rejected", actor="approver",
                    note="Wait for stock to recover.")
    assert proposals.load(clean, proposal_id)["status"] == "rejected"
    with pytest.raises(approval.ApprovalError, match="not 'approved'"):
        approval.execute(clean, proposal_id,
                         idempotency_key=str(uuid.uuid4()), actor="approver")


# ------------------------------------------------------------- execution

def test_execution_sends_to_the_audience_minus_the_holdout(clean):
    proposal = draft(clean)
    proposal_id = str(proposal["proposal_id"])
    approval.decide(clean, proposal_id, decision="approved", actor="approver")
    outcome = approval.execute(clean, proposal_id,
                               idempotency_key=str(uuid.uuid4()),
                               actor="approver")

    assert outcome["status"] == "completed"
    assert outcome["holdout"] > 0
    assert outcome["recipients"] + outcome["holdout"] == proposal["audience_size"]

    sent = clean.execute(
        "select count(*) from lmart.campaign_events where campaign_id=%s "
        "and event_type='sent'", (outcome["campaign_id"],)).fetchone()[0]
    assert sent == outcome["recipients"]
    assert proposals.load(clean, proposal_id)["status"] == "executed"


def test_a_repeated_execution_replays_instead_of_sending_twice(clean):
    proposal = draft(clean)
    proposal_id = str(proposal["proposal_id"])
    approval.decide(clean, proposal_id, decision="approved", actor="approver")
    key = str(uuid.uuid4())

    first = approval.execute(clean, proposal_id, idempotency_key=key,
                             actor="approver")
    second = approval.execute(clean, proposal_id, idempotency_key=key,
                              actor="approver")

    assert second["replayed"] is True
    assert second["execution_id"] == first["execution_id"]
    campaigns = clean.execute(
        "select count(*) from ops.campaign_executions").fetchone()[0]
    assert campaigns == 1


def test_every_consequential_act_is_audited(clean):
    proposal = draft(clean)
    proposal_id = str(proposal["proposal_id"])
    approval.decide(clean, proposal_id, decision="approved", actor="approver")
    approval.execute(clean, proposal_id, idempotency_key=str(uuid.uuid4()),
                     actor="approver")

    actions = [r[0] for r in clean.execute(
        "select action from ops.audit_log where subject_id=%s order by occurred_at",
        (proposal_id,)).fetchall()]
    assert actions == ["proposal.created", "proposal.approved",
                       "campaign.executed"]
