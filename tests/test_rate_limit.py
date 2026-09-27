"""The rate limiter, including the part that only matters on serverless.

Every test here runs inside a transaction that is rolled back, so the rows it
counts are rows it created itself. That is not merely tidy: the limiter counts
real runs and real proposals, so a test that inserted without rolling back would
consume the limit of whichever actor it used and make the NEXT test fail for a
reason that has nothing to do with what it asserts.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api.app import app
from core import config, rate_limit
from core.rate_limit import RateLimited

HEADERS = {"X-API-Key": config.app_api_key() or "local"}


def make_run(conn, actor: str, *, age: str = "0 seconds") -> str:
    """Insert one run attributable to `actor`, optionally backdated."""
    run_id = str(uuid.uuid4())
    conn.execute("""
        insert into ops.agent_runs
            (run_id, question, status, provider, model,
             max_steps, max_tokens, actor, created_at)
        values (%s, 'why did it drop?', 'completed', 'fake', 'fake-1',
                18, 400000, %s, now() - %s::interval)
    """, (run_id, actor, age))
    return run_id


@pytest.fixture
def actor() -> str:
    """A fresh identity per test, so no test can be affected by another's rows
    or by anything a real investigation left behind."""
    return f"ratelimit-{uuid.uuid4()}@test.invalid"


def test_an_actor_with_no_history_is_not_limited(own_conn, actor):
    rate_limit.check(own_conn, "run", actor)          # must not raise


def test_counting_is_per_actor_not_global(own_conn, actor, monkeypatch):
    """The limit belongs to a caller. One busy analyst must not lock out
    everyone else, which is what a global counter would do."""
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "1")
    make_run(own_conn, actor)
    with pytest.raises(RateLimited):
        rate_limit.check(own_conn, "run", actor)
    rate_limit.check(own_conn, "run", f"someone-else-{uuid.uuid4()}@test.invalid")


def test_the_window_is_a_trailing_hour(own_conn, actor, monkeypatch):
    """Runs older than the window do not count, so an actor who was busy
    yesterday starts today with a full allowance."""
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "1")
    make_run(own_conn, actor, age="2 hours")
    assert rate_limit.count_recent(own_conn, "run", actor) == 0
    rate_limit.check(own_conn, "run", actor)          # must not raise


def test_the_limit_admits_exactly_its_allowance(own_conn, actor, monkeypatch):
    """Off-by-one matters: a limit of 3 must allow the third run and refuse the
    fourth, not refuse the third."""
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "3")
    for _ in range(2):
        make_run(own_conn, actor)
        rate_limit.check(own_conn, "run", actor)
    make_run(own_conn, actor)                          # now at 3
    with pytest.raises(RateLimited):
        rate_limit.check(own_conn, "run", actor)


def test_a_failed_run_still_counts(own_conn, actor, monkeypatch):
    """It spent the tokens. Excusing failures would let a client that crashes
    every run retry without limit -- exactly the case worth limiting."""
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "1")
    own_conn.execute("""
        insert into ops.agent_runs
            (run_id, question, status, provider, model,
             max_steps, max_tokens, actor)
        values (%s, 'why did it drop?', 'failed', 'fake', 'fake-1',
                18, 400000, %s)
    """, (str(uuid.uuid4()), actor))
    with pytest.raises(RateLimited):
        rate_limit.check(own_conn, "run", actor)


def test_zero_disables_the_limit(own_conn, actor, monkeypatch):
    """An operator needs a way to turn this off without editing code -- during
    a backfill, or when the limiter itself is the problem."""
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "0")
    for _ in range(5):
        make_run(own_conn, actor)
    rate_limit.check(own_conn, "run", actor)          # must not raise


def test_the_message_says_what_the_limit_is_and_carries_a_retry_delay(
        own_conn, actor, monkeypatch):
    """RateLimited is an ActionableError, so this text reaches the caller --
    and the agent, if a write tool trips it. It has to be useful and must not
    leak internals."""
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "1")
    make_run(own_conn, actor)
    with pytest.raises(RateLimited) as exc:
        rate_limit.check(own_conn, "run", actor)
    message = str(exc.value)
    assert "1 investigations per hour" in message
    assert "ops.agent_runs" not in message and "select" not in message.lower()
    assert exc.value.retry_after == rate_limit.WINDOW_SECONDS


def test_runs_and_proposals_are_counted_separately(own_conn, actor, monkeypatch):
    """Two different resources with two different costs. Exhausting one must
    not block the other."""
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "1")
    make_run(own_conn, actor)
    with pytest.raises(RateLimited):
        rate_limit.check(own_conn, "run", actor)
    rate_limit.check(own_conn, "proposal", actor)     # must not raise


def test_limits_are_read_per_call_not_frozen_at_import(monkeypatch):
    """So a deployment can change the ceiling without a redeploy, and so a test
    can change it without reloading the module."""
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "7")
    assert rate_limit.limit_for("run") == 7
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "9")
    assert rate_limit.limit_for("run") == 9


def test_the_api_answers_429_with_retry_after(monkeypatch, own_conn):
    """A refused request must tell a well-behaved client when to come back.
    Dropping the connection instead is what turns a retry loop into an outage.

    The allowance is filled by seeding rows rather than by creating runs
    through the API, for a reason worth stating: runtime.create_run commits, so
    a run made here would outlive the test's transaction and count against the
    service identity's real allowance afterwards. A request that is REFUSED
    writes nothing, so this test leaves the database exactly as it found it.
    """
    from contextlib import contextmanager

    @contextmanager
    def one_connection():
        yield own_conn

    monkeypatch.setattr("api.app.connection", one_connection)
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "1")
    make_run(own_conn, "service")

    client = TestClient(app)
    response = client.post("/api/runs", json={"question": "why did it drop?"},
                           headers=HEADERS)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(rate_limit.WINDOW_SECONDS)
    assert "per hour" in response.json()["detail"]
    own_conn.rollback()


def test_the_limit_is_checked_before_the_run_row_is_written(monkeypatch,
                                                            own_conn):
    """Order matters. Checking afterwards would mean every refused request
    still created the run it was refused for, so the limit could never be
    enforced at all."""
    from contextlib import contextmanager

    @contextmanager
    def one_connection():
        yield own_conn

    monkeypatch.setattr("api.app.connection", one_connection)
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_HOUR", "1")
    seeded = make_run(own_conn, "service")

    TestClient(app).post("/api/runs", json={"question": "why did it drop?"},
                         headers=HEADERS)

    remaining = own_conn.execute(
        "select run_id from ops.agent_runs where actor = 'service' "
        "and created_at > now() - interval '1 hour'").fetchall()
    assert [str(r[0]) for r in remaining] == [seeded]
    own_conn.rollback()
