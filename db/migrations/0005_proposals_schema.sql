-- 0005_proposals_schema.sql
--
-- A campaign proposal: the agent's entire write capability.
--
-- WHAT THE MODEL CAN DO IS FILL IN THIS FORM. A row here is inert -- it sends
-- nothing, charges nothing and changes no business data. Execution is a
-- separate, non-AI code path that runs only after a human approves.
--
-- WHY THE AUDIENCE IS A RULE, NOT A LIST. Storing resolved customer ids would
-- freeze an audience that should be recomputed at execution time: people opt
-- out, tiers change, and a list captured at proposal time would email someone
-- who withdrew consent in between. The rule is stored; the list is derived,
-- twice, and both times by the same deterministic resolver.

create table ops.campaign_proposals (
    proposal_id     uuid primary key,
    -- Which investigation produced it. Null for a proposal a human drafted.
    run_id          uuid            null references ops.agent_runs(run_id),

    status          text        not null check (status in
                        ('draft','rejected','approved','executed','cancelled')),

    name            text        not null,
    objective       text        not null,
    programme       text        not null,
    channel         text        not null check (channel in ('email','push','sms')),

    -- One offer, never two. Stacking a multiplier on a discount is prohibited
    -- by policy, and a single typed offer makes that structurally impossible
    -- rather than something the validator has to catch.
    offer_type      text        not null check (offer_type in
                        ('points_multiplier','percentage_discount')),
    offer_value     numeric(6,2) not null check (offer_value > 0),

    audience_spec   jsonb       not null,
    holdout_pct     int         not null default 0 check (holdout_pct between 0 and 50),

    rationale       text        not null,
    -- call_ids from the run that justify this. A proposal with no evidence is a
    -- guess, and the approval screen shows this so a reviewer can follow it back.
    evidence_call_ids text[]    not null default '{}',

    audience_size   int             null,
    validation      jsonb           null,

    -- sha256 of the fields that define what will happen. Approval binds to this
    -- value, so editing a proposal after approval invalidates it rather than
    -- silently executing something nobody agreed to.
    content_hash    text            null,

    created_by      text        not null default 'agent',
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index campaign_proposals_status_idx on ops.campaign_proposals (status, created_at desc);
create index campaign_proposals_run_idx on ops.campaign_proposals (run_id);
comment on table ops.campaign_proposals is
    'Proposed campaigns. Inert until approved and executed by a separate path.';
