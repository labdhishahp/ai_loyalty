-- One column so that two requests cannot advance the same run at once.
--
-- Nothing stopped them before: a double-clicked button, a client retrying
-- after a platform timeout, or two open tabs would each call the model --
-- paying twice -- and then collide on agent_steps' unique (run_id, step_no),
-- so the loser raised a database error only after the expensive part was done.
--
-- WHY NOT REUSE status. 'running' already means "this investigation is under
-- way", which is true between turns as well as during one. Claiming on it
-- would refuse the next legitimate turn of every multi-step run. The two facts
-- are genuinely different -- the run is in progress, versus a turn is in
-- flight right now -- so they get their own columns.
--
-- WHY A TIMESTAMP AND NOT A BOOLEAN. A serverless function can be killed
-- without warning, and a boolean left true would wedge the run forever with
-- nothing able to clear it. A timestamp is a lease: another request may take
-- over once it is older than any live turn could be (agent.runtime
-- LEASE_SECONDS, 90s against a 60s maxDuration), so a killed turn recovers on
-- its own instead of needing an operator.

alter table ops.agent_runs
    add column if not exists turn_claimed_at timestamptz null;

comment on column ops.agent_runs.turn_claimed_at is
    'When the in-flight turn was claimed; null when no turn is running. A '
    'lease rather than a lock, so a killed serverless function does not wedge '
    'the run. See LEASE_SECONDS in agent/runtime.py.';
