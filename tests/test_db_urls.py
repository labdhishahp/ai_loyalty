"""Connection-string handling. No database needed.

Supabase's dashboard offers its connection strings in a Prisma flavour, which
appends `?pgbouncer=true`. libpq has no such parameter, so psycopg refuses the
whole URI with "invalid URI query parameter" -- a failure with no obvious
connection to where the string came from. Stripping it in code means whoever
pastes from that tab does not have to know.
"""

from __future__ import annotations

from core.db import sanitise


def test_prisma_pgbouncer_flag_is_removed():
    url = "postgresql://u:p@host.pooler.supabase.com:6543/postgres?pgbouncer=true"
    assert sanitise(url) == "postgresql://u:p@host.pooler.supabase.com:6543/postgres"


def test_other_driver_specific_parameters_are_removed():
    url = ("postgresql://u:p@host:6543/postgres"
           "?pgbouncer=true&connection_limit=1&schema=public")
    assert "?" not in sanitise(url)


def test_genuine_libpq_parameters_survive():
    """sslmode is libpq's own and must not be collateral damage."""
    url = "postgresql://u:p@host:5432/postgres?sslmode=require&pgbouncer=true"
    result = sanitise(url)
    assert "sslmode=require" in result
    assert "pgbouncer" not in result


def test_a_url_without_a_query_string_is_untouched():
    url = "postgresql://u:p@db.project.supabase.co:5432/postgres"
    assert sanitise(url) == url
