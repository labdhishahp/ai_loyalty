-- 0002_operations_schema.sql
--
-- How the AI employee works, recorded as data.
--
-- WHY THESE TABLES EXIST NOW AND NOT EARLIER: nothing consumed them until there
-- was a loop to record. They arrive with their first consumer.
--
-- WHY THE TRACE IS THE EXECUTION SUBSTRATE, NOT LOGGING. The agent loop is a
-- state machine persisted here and advanced one turn per HTTP request, rather
-- than a while-loop living inside one request. Three things fall out of that,
-- none of which is available from logs:
--   * it fits a serverless execution ceiling -- a long investigation is many
--     short requests instead of one that gets cut off;
--   * a run can be inspected, replayed and resumed after a crash;
--   * evaluation reads rows instead of scraping output.

create schema if not exists ops;


-- One investigation. parent_run_id is present from the first version: a
-- multi-agent fan-out is a run with a parent, so the option costs one column
-- now instead of a migration later.
create table ops.agent_runs (
    run_id          uuid primary key,
    parent_run_id   uuid        null references ops.agent_runs(run_id),
    question        text        not null,
    status          text        not null check (status in
                        ('pending','running','completed','failed',
                         'budget_exceeded','cancelled')),

    -- Which provider and model answered. Recorded per run so a Qwen-vs-Claude
    -- comparison on identical questions is a query, not an experiment rerun.
    provider        text        not null,
    model           text        not null,

    -- Budgets, copied onto the run at creation. Stored rather than read from the
    -- environment at each turn so a run's limits cannot change underneath it,
    -- and so an old run explains its own outcome.
    max_steps       int         not null,
    max_tokens      int         not null,
    max_cost_usd    numeric(10,4)   null,

    steps_used      int         not null default 0,
    input_tokens    bigint      not null default 0,
    output_tokens   bigint      not null default 0,
    cost_usd        numeric(10,4) not null default 0,

    -- Who asked. Unauthenticated until Milestone 4; present now so that
    -- authorization is threaded through from the start rather than retrofitted.
    actor           text        not null default 'anonymous',

    final_answer    jsonb           null,
    error           text            null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    completed_at    timestamptz     null
);
create index agent_runs_status_idx on ops.agent_runs (status, created_at desc);
create index agent_runs_parent_idx on ops.agent_runs (parent_run_id);
comment on table ops.agent_runs is
    'One investigation. The loop advances this row one turn at a time.';


-- One model turn. assistant_payload keeps the provider''s own content blocks so
-- the conversation can be replayed exactly -- that is what preserves reasoning
-- blocks across turns for providers that emit them.
create table ops.agent_steps (
    step_id         bigint generated always as identity primary key,
    run_id          uuid        not null references ops.agent_runs(run_id) on delete cascade,
    step_no         int         not null,
    assistant_text  text            null,
    assistant_payload jsonb         null,
    stop_reason     text            null,
    input_tokens    int         not null default 0,
    output_tokens   int         not null default 0,
    duration_ms     int             null,
    created_at      timestamptz not null default now(),
    unique (run_id, step_no)
);
create index agent_steps_run_idx on ops.agent_steps (run_id, step_no);


-- One tool invocation. `result` holds the TRACE view, SQL included, so any
-- number in an answer can be audited back to the statement that produced it.
-- The model only ever saw the model view of the same object.
create table ops.tool_calls (
    call_id         text        primary key,
    run_id          uuid        not null references ops.agent_runs(run_id) on delete cascade,
    step_no         int         not null,
    tool            text        not null,
    arguments       jsonb       not null,
    ok              boolean     not null,
    result          jsonb           null,
    error           text            null,
    duration_ms     int             null,
    created_at      timestamptz not null default now()
);
create index tool_calls_run_idx on ops.tool_calls (run_id, created_at);
comment on table ops.tool_calls is
    'Every tool invocation, with the trace view of its result. call_id is what '
    'an answer cites, so a claim can be followed back to its evidence.';
