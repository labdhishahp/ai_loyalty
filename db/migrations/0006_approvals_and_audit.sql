-- 0006_approvals_and_audit.sql
--
-- Human approval, execution, and an audit trail.
--
-- WHY APPROVAL BINDS TO A CONTENT HASH. "Approved" has to be a statement about
-- a specific thing. Without a hash, approving a proposal and then editing it
-- executes something nobody agreed to, and the record would still say approved.
-- The hash is taken over exactly the fields that determine what happens.

-- Whether a run may call write tools at all. Default false: an investigation
-- has no business proposing anything, and making the capability opt-in per run
-- means a read-only run structurally cannot write, rather than choosing not to.
alter table ops.agent_runs
    add column allow_writes boolean not null default false;

create table ops.approvals (
    approval_id     uuid primary key,
    proposal_id     uuid        not null references ops.campaign_proposals(proposal_id),
    decision        text        not null check (decision in ('approved','rejected')),

    -- Who, and what they were looking at. approved_hash is compared against the
    -- proposal's current hash at execution; a mismatch means it changed after
    -- the decision and the approval no longer applies.
    actor           text        not null,
    approved_hash   text        not null,
    note            text            null,

    -- Recorded at decision time, not recomputed later: this is what the
    -- approver actually saw.
    audience_size   int         not null,
    validation      jsonb       not null,

    created_at      timestamptz not null default now()
);
create index approvals_proposal_idx on ops.approvals (proposal_id, created_at desc);
comment on table ops.approvals is
    'Signed decisions. Append-only: a changed mind is a new row, never an edit.';


-- What actually happened, once approved. Separate from approvals because a
-- decision and its consequences are different facts with different lifetimes.
create table ops.campaign_executions (
    execution_id    uuid primary key,
    proposal_id     uuid        not null references ops.campaign_proposals(proposal_id),
    approval_id     uuid        not null references ops.approvals(approval_id),

    -- Supplied by the caller and unique, so a retry after a timeout cannot
    -- send the same campaign twice. This is the difference between an
    -- at-least-once and an exactly-once write.
    idempotency_key text        not null unique,

    status          text        not null check (status in
                        ('running','completed','failed')),
    campaign_id     bigint          null references lmart.campaigns(campaign_id),
    recipients      int         not null default 0,
    holdout         int         not null default 0,
    error           text            null,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz     null
);
create index campaign_executions_proposal_idx on ops.campaign_executions (proposal_id);


-- Append-only record of every consequential act. Deliberately not a log file:
-- it is queried by the UI, joined to runs and proposals, and must survive a
-- redeploy.
create table ops.audit_log (
    audit_id        bigint generated always as identity primary key,
    occurred_at     timestamptz not null default now(),
    actor           text        not null,
    action          text        not null,
    subject_type    text        not null,
    subject_id      text        not null,
    detail          jsonb           null
);
create index audit_log_subject_idx on ops.audit_log (subject_type, subject_id, occurred_at desc);
create index audit_log_time_idx on ops.audit_log (occurred_at desc);
comment on table ops.audit_log is
    'Append-only. Never updated, never deleted.';
