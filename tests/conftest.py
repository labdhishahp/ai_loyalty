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
