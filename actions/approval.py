"""Human approval and execution. No model is involved in either.

THE TRUST BOUNDARY IS HERE. Everything upstream -- the investigation, the
retrieval, the proposal -- is advisory. This module is where something becomes
true about the world, and it is ordinary deterministic code with a person in the
middle of it.

FOUR GUARANTEES, each one addressing a specific way this goes wrong:

  1. APPROVAL BINDS TO A CONTENT HASH. "Approved" must be a statement about a
     specific thing. Without the hash, approving a proposal and then editing the
     discount executes something nobody agreed to, and the record still reads
     "approved".

  2. VALIDATION RUNS AGAIN AT EXECUTION. Not the stored result -- the checks, re-
     run, now. People opt out and tiers change between approval and execution,
     so a validation from yesterday is a statement about yesterday. The first
     run helps the model; this one is the control.

  3. EXECUTION IS IDEMPOTENT. The caller supplies a key, unique in the database.
     A retry after a timeout returns the original execution instead of sending
     the campaign a second time. Without this, "did that request go through?"
     has no safe answer.

  4. EVERYTHING IS AUDITED. Append-only, queryable, joined to the run that
     proposed it.

SENDS ARE SIMULATED. Execution writes a real campaign and real per-recipient
send records; no message leaves the system. Integrating a delivery provider adds
operational surface and teaches nothing about agent design -- every interesting
decision is upstream of the send.
"""

from __future__ import annotations

import uuid
from datetime import date

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core.errors import ActionableError

from . import proposals
from .audience import AudienceSpec, resolve
from .policy_engine import validate


class ApprovalError(ActionableError):
    """The decision or execution cannot proceed."""


def decide(conn, proposal_id: str, *, decision: str, actor: str,
           note: str | None = None) -> dict:
    """Record an approval or rejection against the proposal as it stands now."""
    if decision not in ("approved", "rejected"):
        raise ApprovalError("decision must be 'approved' or 'rejected'.")

    proposal, validation = proposals.revalidate(conn, proposal_id)

    if proposal["status"] == "executed":
        raise ApprovalError("This proposal has already been executed.")

    if decision == "approved" and not validation.ok:
        # Refused rather than warned: an approval that records a failing
        # proposal as approved is a record that will later be read as
        # authorisation.
        raise ApprovalError(
            "This proposal fails validation and cannot be approved: "
            + "; ".join(c.detail for c in validation.failures))

    approval_id = str(uuid.uuid4())
    conn.execute("""
        insert into ops.approvals
            (approval_id, proposal_id, decision, actor, approved_hash, note,
             audience_size, validation)
        values (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (approval_id, proposal_id, decision, actor, proposal["content_hash"],
          note, proposal["audience_size"] or 0,
          Jsonb(validation.to_dict())))
    conn.execute("""update ops.campaign_proposals set status=%s, updated_at=now()
                    where proposal_id=%s""", (decision, proposal_id))
    proposals.audit(conn, actor, f"proposal.{decision}", "proposal", proposal_id,
                    {"approval_id": approval_id,
                     "audience_size": proposal["audience_size"],
                     "note": note})
    conn.commit()
    return {"approval_id": approval_id, "decision": decision,
            "proposal": proposals.load(conn, proposal_id)}


def latest_approval(conn, proposal_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("""
            select * from ops.approvals
            where proposal_id = %s and decision = 'approved'
            order by created_at desc limit 1
        """, (proposal_id,)).fetchone()


def execute(conn, proposal_id: str, *, idempotency_key: str,
            actor: str) -> dict:
    """Send an approved campaign. Deterministic, re-validated, idempotent."""
    with conn.cursor(row_factory=dict_row) as cur:
        existing = cur.execute(
            "select * from ops.campaign_executions where idempotency_key = %s",
            (idempotency_key,)).fetchone()
    if existing:
        # The whole point of the key: a retry is not a second send.
        return {"execution_id": str(existing["execution_id"]),
                "status": existing["status"],
                "recipients": existing["recipients"],
                "holdout": existing["holdout"], "replayed": True}

    proposal = proposals.load(conn, proposal_id)
    if proposal["status"] != "approved":
        raise ApprovalError(
            f"Proposal is '{proposal['status']}', not 'approved'.")

    approval = latest_approval(conn, proposal_id)
    if approval is None:
        raise ApprovalError("No approval recorded for this proposal.")

    if approval["approved_hash"] != proposal["content_hash"]:
        raise ApprovalError(
            "This proposal changed after it was approved, so the approval no "
            "longer applies to what would be sent. It must be approved again.")

    spec = AudienceSpec.from_dict(proposal["audience_spec"])
    resolved = resolve(conn, spec)
    revalidated = validate(conn, offer_type=proposal["offer_type"],
                           offer_value=float(proposal["offer_value"]),
                           holdout_pct=proposal["holdout_pct"],
                           audience=resolved, spec=spec)
    if not revalidated.ok:
        raise ApprovalError(
            "Validation no longer passes, so execution is refused: "
            + "; ".join(c.detail for c in revalidated.failures))

    execution_id = str(uuid.uuid4())
    conn.execute("""
        insert into ops.campaign_executions
            (execution_id, proposal_id, approval_id, idempotency_key, status)
        values (%s,%s,%s,%s,'running')
    """, (execution_id, proposal_id, approval["approval_id"], idempotency_key))

    try:
        # Holdout is deterministic: the lowest customer ids. A random split
        # would make the campaign unreproducible and the measurement arguable.
        held = (resolved.size * proposal["holdout_pct"]) // 100
        ordered = sorted(resolved.customer_ids)
        holdout, recipients = ordered[:held], ordered[held:]

        campaign_id = conn.execute("""
            insert into lmart.campaigns
                (campaign_id, code, name, programme, objective, channel,
                 target_description, status, sent_on)
            values ((select coalesce(max(campaign_id),0)+1 from lmart.campaigns),
                    %s,%s,%s,%s,%s,%s,'completed',%s)
            returning campaign_id
        """, (f"PROP-{execution_id[:8].upper()}", proposal["name"],
              proposal["programme"], proposal["objective"], proposal["channel"],
              resolved.description, date.today())).fetchone()[0]

        # Computed BEFORE the COPY block, not inside it. A connection in COPY
        # mode cannot serve another statement: the server waits for copy data
        # while the client waits for a query result, and the session hangs until
        # something kills it. Found exactly that way -- a test run left a COPY
        # open for ten minutes.
        next_id = conn.execute(
            "select coalesce(max(event_id),0)+1 from lmart.campaign_events"
        ).fetchone()[0]
        sent_at = f"{date.today()} 09:00:00+00"
        with conn.cursor() as cur:
            with cur.copy("copy lmart.campaign_events "
                          "(event_id, campaign_id, customer_id, event_type, "
                          "occurred_at) from stdin") as copy:
                for offset, customer_id in enumerate(recipients):
                    copy.write_row((next_id + offset, campaign_id, customer_id,
                                    "sent", sent_at))

        conn.execute("""update ops.campaign_executions
                           set status='completed', campaign_id=%s, recipients=%s,
                               holdout=%s, finished_at=now()
                         where execution_id=%s""",
                     (campaign_id, len(recipients), len(holdout), execution_id))
        conn.execute("""update ops.campaign_proposals set status='executed',
                            updated_at=now() where proposal_id=%s""",
                     (proposal_id,))
        proposals.audit(conn, actor, "campaign.executed", "proposal",
                        proposal_id,
                        {"execution_id": execution_id, "campaign_id": campaign_id,
                         "recipients": len(recipients), "holdout": len(holdout)})
        conn.commit()
    except Exception as exc:                                # noqa: BLE001
        conn.rollback()
        conn.execute("""update ops.campaign_executions
                           set status='failed', error=%s, finished_at=now()
                         where execution_id=%s""", (str(exc)[:500], execution_id))
        proposals.audit(conn, actor, "campaign.execution_failed", "proposal",
                        proposal_id, {"execution_id": execution_id,
                                      "error": str(exc)[:500]})
        conn.commit()
        raise ApprovalError(f"Execution failed: {exc}") from exc

    return {"execution_id": execution_id, "status": "completed",
            "campaign_id": campaign_id, "recipients": len(recipients),
            "holdout": len(holdout), "replayed": False}
