-- Rate limiting counts rows this schema already writes (see core/rate_limit.py):
-- there is no counter table. That choice is only sound if the count is cheap,
-- and the existing indexes are on (status, created_at) -- useless for a lookup
-- keyed by actor. Without these, every run creation would scan the whole runs
-- table, so the limiter would get more expensive exactly as it became more
-- necessary.
--
-- created_at leads the actor column so that the range predicate -- the
-- selective half, since the window is one hour out of the table's whole
-- history -- is the one the index seeks on.

create index if not exists agent_runs_actor_recent_idx
    on ops.agent_runs (created_at desc, actor);

create index if not exists campaign_proposals_creator_recent_idx
    on ops.campaign_proposals (created_at desc, created_by);
