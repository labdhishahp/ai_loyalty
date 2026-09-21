"""Opening database connections, with the two Supabase constraints applied once.

There are exactly two ways this project talks to Postgres, and using the wrong
one is a mistake that works locally and fails under load. So they are named
functions rather than a URL and a comment.

  direct_connection()   migrations, the COPY load, the test suite, the CLIs.
                        Needs a real session: COPY and DDL cannot go through a
                        transaction pooler.

  pooled_connection()   anything serving an HTTP request. This database allows
                        60 connections; serverless functions scale to many
                        short-lived instances and would exhaust that.

TWO THINGS THE POOLER REQUIRES, both handled here so no caller has to remember:

1. Driver-specific query parameters are stripped. Supabase's dashboard offers
   its connection strings in a Prisma flavour, which appends `?pgbouncer=true`.
   libpq has no such parameter and psycopg refuses the URI outright with
   "invalid URI query parameter". The flag is meaningful to Prisma and noise to
   everyone else, so it is removed rather than demanded of whoever pastes it.

2. Prepared statements are disabled. In transaction pooling mode a connection is
   handed to a different client between statements, so a statement prepared on
   one is not there for the next. psycopg3 silently starts preparing after the
   fifth identical execution, which makes this fail only once a query becomes
   common -- the worst possible time to discover it.
"""

from __future__ import annotations

import urllib.parse

import psycopg

from . import config

# Parameters that belong to an ORM's own connection layer rather than to libpq.
# Removed rather than allowlisting libpq's own set, which is long and versioned.
DRIVER_SPECIFIC_PARAMS = frozenset({
    "pgbouncer",          # Prisma: "this is a pooler, skip prepared statements"
    "schema",             # Prisma: default search_path
    "connection_limit",   # Prisma pool sizing
    "pool_timeout",       # Prisma pool sizing
    "statement_cache_size",  # asyncpg
})


def sanitise(url: str) -> str:
    """Drop query parameters libpq would reject. Everything else is preserved."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in DRIVER_SPECIFIC_PARAMS]
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(kept)))


def direct_connection(**kwargs):
    """A real session. Use for DDL, COPY, migrations and tests."""
    return psycopg.connect(sanitise(config.database_url()), **kwargs)


def pooled_connection(**kwargs):
    """A transaction-pooled connection. Use for anything serving a request."""
    kwargs.setdefault("prepare_threshold", None)   # see note 2 above
    return psycopg.connect(sanitise(config.pool_url()), **kwargs)
