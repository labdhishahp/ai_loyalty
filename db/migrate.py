"""Apply numbered SQL migrations, exactly once each, in filename order.

WHY NOT ALEMBIC: Alembic's value is autogenerating diffs from ORM models. We
deliberately have no ORM -- the metrics layer *is* hand-written SQL, and an ORM
would sit between us and the queries we are trying to make precise. What is
actually needed is "run these .sql files once, in order, and remember which
ran". That is this file, and it is small enough to read in one sitting.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

# Lives in `public` rather than `lmart` on purpose: it is infrastructure
# bookkeeping, not business data, and it must survive a drop of the lmart schema.
LEDGER_DDL = """
create table if not exists public.schema_migrations (
    filename    text primary key,
    sha256      text        not null,
    applied_at  timestamptz not null default now()
)
"""


def main() -> int:
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set. Copy .env.example to .env first.", file=sys.stderr)
        return 1

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("No migrations found.", file=sys.stderr)
        return 1

    with psycopg.connect(url, autocommit=False) as conn:
        conn.execute(LEDGER_DDL)
        applied = {
            row[0]: row[1]
            for row in conn.execute("select filename, sha256 from public.schema_migrations")
        }

        for path in files:
            body = path.read_text()
            digest = hashlib.sha256(body.encode()).hexdigest()

            if path.name in applied:
                # A changed migration means someone edited applied history. That
                # is a real bug (environments will have diverged), so fail loudly
                # rather than silently skipping or re-running.
                if applied[path.name] != digest:
                    print(
                        f"ERROR: {path.name} was already applied but its contents changed.\n"
                        f"Write a new migration instead of editing an applied one.",
                        file=sys.stderr,
                    )
                    return 1
                print(f"  skip  {path.name}")
                continue

            print(f"  apply {path.name}")
            # Each migration runs in its own transaction: a failure leaves the
            # database at the last good migration rather than half-applied.
            conn.execute(body)
            conn.execute(
                "insert into public.schema_migrations (filename, sha256) values (%s, %s)",
                (path.name, digest),
            )
            conn.commit()

    print("Migrations up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
