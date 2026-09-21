"""Generate the dataset and load it straight into Postgres.

Postgres is the source of truth for L-Mart. There is no intermediate file: the
generator builds the dataset in memory and this streams it into the database in
one command, so there is never a "which copy is current?" question to answer.

WHY COPY RATHER THAN INSERT: ~390,000 rows across twelve tables. Row-at-a-time
inserts over a network round trip would take many minutes; COPY takes seconds.

WHY CSV TEXT RATHER THAN psycopg's typed write_row: COPY in CSV format lets
*Postgres* parse every value according to the column's declared type. That means
the load is also a type check -- a date that isn't a date, or a number that
overflows, fails here rather than surfacing as a strange metric later. Rows carry
None for nullable columns; csv.writer renders that as an empty field, and
`null ''` maps it back to SQL NULL.

WHY THE DIRECT CONNECTION (port 5432) rather than Supabase's transaction pooler:
COPY needs a real session. The pooler exists for the many short-lived connections
the API layer will open later -- a different problem with a different answer.

Run:  python -m seed.load            (refuses to overwrite existing data)
      python -m seed.load --reset    (truncates first)
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

from . import generate, simulate

BATCH_ROWS = 10_000


def build_plan(customers, ref, tables):
    """(table, columns, rows) for every table, in foreign-key order.

    Returned rather than inlined so that tests/test_row_shapes.py checks the same
    plan this loader uses. A column list that drifts out of step with its row
    tuples would otherwise fail only at COPY time, against a real database.

    points_ledger and campaign_events both reference orders, so orders must
    already be present when they load.
    """
    return [
        ("loyalty_tiers", generate.TIER_COLUMNS,
         generate.dict_rows(ref.tiers, generate.TIER_COLUMNS)),
        ("stores", generate.STORE_COLUMNS,
         generate.dict_rows(ref.stores, generate.STORE_COLUMNS)),
        ("categories", generate.CATEGORY_COLUMNS,
         generate.dict_rows(ref.categories, generate.CATEGORY_COLUMNS)),
        ("products", generate.PRODUCT_COLUMNS,
         generate.dict_rows(ref.products, generate.PRODUCT_COLUMNS)),
        ("customers", generate.CUSTOMER_COLUMNS,
         generate.customer_rows(customers)),
        ("loyalty_accounts", generate.LOYALTY_ACCOUNT_COLUMNS,
         generate.loyalty_account_rows(customers)),
        ("tier_history", simulate.TIER_HISTORY_COLUMNS, tables.tier_history),
        ("orders", simulate.ORDER_COLUMNS, tables.orders),
        ("order_items", simulate.ORDER_ITEM_COLUMNS, tables.order_items),
        ("points_ledger", simulate.POINTS_COLUMNS, tables.points_ledger),
        ("campaigns", simulate.CAMPAIGN_COLUMNS, tables.campaigns),
        ("campaign_events", simulate.CAMPAIGN_EVENT_COLUMNS, tables.campaign_events),
    ]


def copy_rows(conn, table: str, columns, rows) -> int:
    """Stream rows into lmart.<table> via COPY, buffering to bound memory."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    statement = (f"copy lmart.{table} ({', '.join(columns)}) from stdin "
                 f"with (format csv, null '')")
    count = 0
    with conn.cursor().copy(statement) as copy:
        for row in rows:
            writer.writerow(row)
            count += 1
            if count % BATCH_ROWS == 0:
                copy.write(buffer.getvalue())
                buffer.seek(0)
                buffer.truncate(0)
        copy.write(buffer.getvalue())
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="truncate all lmart tables before loading")
    args = parser.parse_args()

    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set. Copy .env.example to .env first.",
              file=sys.stderr)
        return 1

    print("Building dataset in memory...")
    started = time.time()
    customers, ref, tables = generate.build()

    plan = build_plan(customers, ref, tables)

    print(f"Loading into Postgres ({time.time() - started:.1f}s to build)...")
    with psycopg.connect(url, autocommit=False) as conn:
        existing = conn.execute("select count(*) from lmart.customers").fetchone()[0]
        if existing and not args.reset:
            print(f"lmart.customers already holds {existing:,} rows. "
                  f"Re-run with --reset to replace them.", file=sys.stderr)
            return 1

        if args.reset:
            # One statement, so the foreign-key graph never has to be satisfied
            # part-way through the truncate.
            conn.execute("truncate " + ", ".join(f"lmart.{t}" for t, _, _ in plan)
                         + " cascade")
            print("  truncated existing data")

        for table, columns, rows in plan:
            loaded = copy_rows(conn, table, columns, rows)
            print(f"  {loaded:>9,}  {table}")

        # Committed only after every table has loaded. A failure part-way leaves
        # the database empty rather than half-populated -- which matters because
        # the next thing anyone does is trust these numbers.
        conn.commit()

    print(f"\nLoaded in {time.time() - started:.1f}s")
    print("Next: python -m seed.verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
