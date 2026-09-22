"""Shared fixtures.

Tests that need Postgres are skipped when DATABASE_URL is absent, so the offline
suite still runs on a fresh clone with no credentials. That is a deliberate
split: the rules of the metrics layer are testable without a database, and only
the numbers themselves are not.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

load_dotenv()


@pytest.fixture(scope="session")
def conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; skipping tests that need Postgres")
    import psycopg
    with psycopg.connect(url) as connection:
        # Every metric compares a timestamptz column against a bare date. Pinning
        # the session to UTC makes that cast deterministic rather than dependent
        # on the server's default.
        connection.execute("set time zone 'UTC'")
        yield connection


@pytest.fixture
def own_conn():
    """A connection of this test's own, rolled back and closed afterwards.

    The API handlers open a connection per request and discard it, so a failure
    inside one cannot affect the next. Sharing the session connection in tests
    loses that property: one aborted transaction leaves the connection in a
    failed state and every later test using it errors with InFailedSqlTransaction
    for reasons that have nothing to do with what they are testing.
    """
    import os
    import psycopg
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    with psycopg.connect(url) as connection:
        connection.execute("set time zone 'UTC'")
        yield connection
        connection.rollback()


@pytest.fixture
def db_conn(conn):
    """A connection that is rolled back after each test.

    Tool dispatch sets `statement_timeout` with SET LOCAL and rolls back on
    handler failure, so tests must not share transaction state.
    """
    yield conn
    conn.rollback()


# A valid submit_findings payload, shared by the runtime and API tests.
# Defined here rather than in one test module and imported by the other: test
# modules are not a package, so a relative import between them fails at
# collection, and making them one would be plumbing for its own sake.
SAMPLE_FINDINGS = {
    "headline": "It fell because of one thing.",
    "verdict": "confirmed",
    "metrics_used": ["orders_per_member"],
    "comparison_basis": "year over year",
    "cohort_basis": "fixed at 2026-01-15",
    "causes": [{"name": "A cause", "explanation": "Because.",
                "evidence_call_ids": ["toolu_x"], "importance": "largest",
                "confidence": "high"}],
    "ruled_out": [{"name": "A red herring", "why_not": "Affects everyone.",
                   "evidence_call_ids": []}],
    "limitations": "None.",
    "recommended_next": "Do the thing.",
}


@pytest.fixture
def sample_findings():
    return dict(SAMPLE_FINDINGS)
