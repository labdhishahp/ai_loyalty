"""COPY the generated CSVs into Postgres.

COPY rather than INSERT: ~380,000 rows across twelve tables. Row-at-a-time
inserts over a network round trip would take minutes; COPY takes seconds.

Uses the DIRECT connection (port 5432), not Supabase's transaction pooler.
COPY needs a real session, and the pooler is for the many short-lived connections
the API layer will open later -- a different problem with a different answer.

Run:  python -m seed.load          (refuses to overwrite existing data)
      python -m seed.load --reset  (truncates first)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

from . import config

# Load order is foreign-key order. points_ledger and campaign_events both
# reference orders, so orders must already be present when they load.
TABLES = [
    "loyalty_tiers",
    "stores",
    "categories",
    "products",
    "customers",
    "loyalty_accounts",
    "tier_history",
    "orders",
    "order_items",
    "points_ledger",
    "campaigns",
    "campaign_events",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="truncate all lmart tables before loading")
    args = parser.parse_args()

    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set. Copy .env.example to .env first.", file=sys.stderr)
        return 1

    missing = [t for t in TABLES if not (config.OUT_DIR / f"{t}.csv").exists()]
    if missing:
        print(f"Missing CSVs: {', '.join(missing)}. Run: python -m seed.generate",
              file=sys.stderr)
        return 1

    started = time.time()
    with psycopg.connect(url, autocommit=False) as conn:
        existing = conn.execute("select count(*) from lmart.customers").fetchone()[0]
        if existing and not args.reset:
            print(f"lmart.customers already holds {existing:,} rows. "
                  f"Re-run with --reset to replace them.", file=sys.stderr)
            return 1

        if args.reset:
            # One statement so the FK graph never has to be satisfied mid-way.
            conn.execute(
                "truncate " + ", ".join(f"lmart.{t}" for t in TABLES) + " cascade")
            print("  truncated existing data")

        for table in TABLES:
            path = config.OUT_DIR / f"{table}.csv"
            with path.open() as handle:
                header = handle.readline().strip().split(",")
                columns = ", ".join(header)
                # NULL '' maps the empty CSV field to SQL NULL, which is how the
                # generator writes optional values (closed_on, store_id, order_id).
                statement = (f"copy lmart.{table} ({columns}) from stdin "
                             f"with (format csv, null '')")
                with conn.cursor().copy(statement) as copy:
                    while chunk := handle.read(1 << 20):
                        copy.write(chunk)
            count = conn.execute(f"select count(*) from lmart.{table}").fetchone()[0]
            print(f"  {count:>9,}  {table}")

        conn.commit()

    print(f"\nLoaded in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
