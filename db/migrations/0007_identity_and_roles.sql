-- 0007_identity_and_roles.sql
--
-- Who may do what.
--
-- WHY ROLES LIVE IN A TABLE rather than in Supabase user metadata: a role is a
-- business fact about a person, it needs to be joinable and auditable, and
-- changing one should be an ordinary database write rather than an Auth admin
-- call. Identity comes from Supabase; authority comes from here.
--
-- TWO ROLES, because separation of duties is the entire point of the approval
-- workflow. An analyst who can also approve their own proposal is not a control.

create table ops.user_roles (
    user_id     uuid primary key,          -- Supabase auth.users.id
    email       text        not null,      -- denormalised so the audit log reads
    role        text        not null check (role in ('analyst','approver')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index user_roles_email_idx on ops.user_roles (email);
comment on table ops.user_roles is
    'Authority. Identity is Supabase''s; this decides what that identity may do.';


-- ---------------------------------------------------------------------------
-- Row level security, as DEFENCE IN DEPTH and nothing more.
--
-- Be precise about what this does and does not do. The API connects as the
-- database owner, which bypasses RLS entirely, so these policies do NOT enforce
-- the analyst/approver split -- api/auth.py does that, and it is the real
-- control.
--
-- What they do is close a different hole: Supabase exposes schemas through
-- PostgREST, and a misconfiguration there would otherwise serve agent traces,
-- proposals and the audit log to anyone holding the anon key. Enabling RLS with
-- no permissive policy denies those roles by default.
-- ---------------------------------------------------------------------------

alter table ops.agent_runs          enable row level security;
alter table ops.agent_steps         enable row level security;
alter table ops.tool_calls          enable row level security;
alter table ops.campaign_proposals  enable row level security;
alter table ops.approvals           enable row level security;
alter table ops.campaign_executions enable row level security;
alter table ops.audit_log           enable row level security;
alter table ops.user_roles          enable row level security;

revoke all on all tables in schema ops from anon, authenticated;
