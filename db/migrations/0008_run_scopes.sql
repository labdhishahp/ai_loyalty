-- 0008_run_scopes.sql
--
-- The permissions a run executes under, frozen at creation.
--
-- Not re-derived on each turn from the caller's current role: an investigation
-- that began under one set of permissions should finish under them. Otherwise a
-- role change mid-run would silently alter what an in-flight agent may do, and
-- the trace would no longer explain its own behaviour.

alter table ops.agent_runs
    add column scopes text[] not null default array['read'];
