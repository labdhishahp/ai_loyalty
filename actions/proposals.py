"""Creating, validating and reading campaign proposals."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core import rate_limit
from core.errors import ActionableError

from .audience import AudienceSpec, resolve
from .policy_engine import Validation, validate


class ProposalError(ActionableError):
    """The proposal could not be created or found."""


# Exactly the fields that determine what will happen. Anything outside this set
# -- the rationale, the evidence list -- can be edited after approval without
# changing what executes, so including it would invalidate approvals for
# cosmetic edits.
HASHED_FIELDS = ("name", "channel", "offer_type", "offer_value",
                 "audience_spec", "holdout_pct")


def content_hash(proposal: dict) -> str:
    payload = {field: proposal[field] for field in HASHED_FIELDS}
    # sort_keys so an identical proposal always hashes identically regardless of
    # dict ordering.
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def create(conn, *, run_id: str | None, name: str, objective: str,
           programme: str, channel: str, offer_type: str, offer_value: float,
           audience: dict, holdout_pct: int, rationale: str,
           evidence_call_ids: list[str], created_by: str = "agent") -> dict:
    """Write a draft proposal and validate it immediately.

    Validation happens on creation rather than on request: a draft nobody
    validated looks the same as one that passed, and the difference matters to
    whoever opens the approval screen.
    """
    # Before resolving the audience, which is the expensive part.
    rate_limit.check(conn, "proposal", created_by)

    spec = AudienceSpec.from_dict(audience)
    resolved = resolve(conn, spec)
    result = validate(conn, offer_type=offer_type, offer_value=offer_value,
                      holdout_pct=holdout_pct, audience=resolved, spec=spec)

    proposal_id = str(uuid.uuid4())
    row = {"name": name, "channel": channel, "offer_type": offer_type,
           "offer_value": offer_value, "audience_spec": spec.to_dict(),
           "holdout_pct": holdout_pct}

    conn.execute("""
        insert into ops.campaign_proposals
            (proposal_id, run_id, status, name, objective, programme, channel,
             offer_type, offer_value, audience_spec, holdout_pct, rationale,
             evidence_call_ids, audience_size, validation, content_hash,
             created_by)
        values (%s,%s,'draft',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (proposal_id, run_id, name, objective, programme, channel, offer_type,
          offer_value, Jsonb(spec.to_dict()), holdout_pct, rationale,
          evidence_call_ids, resolved.size, Jsonb(result.to_dict()),
          content_hash(row), created_by))
    audit(conn, created_by, "proposal.created", "proposal", proposal_id,
          {"name": name, "audience_size": resolved.size, "valid": result.ok})
    conn.commit()
    return load(conn, proposal_id)


def load(conn, proposal_id: str) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "select * from ops.campaign_proposals where proposal_id = %s",
            (proposal_id,)).fetchone()
    if row is None:
        raise ProposalError(f"No proposal {proposal_id}.")
    return row


def revalidate(conn, proposal_id: str) -> tuple[dict, Validation]:
    """Re-run every check against the current state of the world.

    Not a cached result: people opt out and tiers change between proposal and
    execution, so a validation from an hour ago is a statement about an hour ago.
    """
    proposal = load(conn, proposal_id)
    spec = AudienceSpec.from_dict(proposal["audience_spec"])
    resolved = resolve(conn, spec)
    result = validate(conn, offer_type=proposal["offer_type"],
                      offer_value=float(proposal["offer_value"]),
                      holdout_pct=proposal["holdout_pct"],
                      audience=resolved, spec=spec)
    conn.execute("""update ops.campaign_proposals
                       set validation=%s, audience_size=%s, updated_at=now()
                     where proposal_id=%s""",
                 (Jsonb(result.to_dict()), resolved.size, proposal_id))
    conn.commit()
    return load(conn, proposal_id), result


def audit(conn, actor: str, action: str, subject_type: str, subject_id: str,
          detail: dict | None = None) -> None:
    conn.execute("""insert into ops.audit_log
                        (actor, action, subject_type, subject_id, detail)
                    values (%s,%s,%s,%s,%s)""",
                 (actor, action, subject_type, subject_id,
                  Jsonb(detail) if detail else None))
