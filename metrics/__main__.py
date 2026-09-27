"""Run a metric from the command line.

Exists so metrics can be exercised by hand before there is a tool layer or a UI
in front of them. That matters more than it sounds: the next slice turns these
into capabilities a model chooses between, and the fastest way to find out
whether a metric is well-named and well-described is to try answering a real
question with it yourself.

  python -m metrics list
  python -m metrics run orders_per_member --start 2026-06-01 --end 2026-09-01 \
      --country GB --tier GOLD --as-of 2026-01-15
  python -m metrics run campaign_funnel --start 2024-09-01 --end 2026-09-01 \
      --monthly --filter programme="UK Gold Reactivation"
  python -m metrics compare orders_per_member --country GB --tier GOLD --as-of period_end
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import psycopg
from dotenv import load_dotenv

from . import catalog
from .cohort import PERIOD_END, CohortSpec
from .engine import MONTH, TOTAL, MetricRequest, execute

# The question this project exists to answer, as default periods.
FOCUS = (date(2026, 6, 1), date(2026, 9, 1))
PRIOR = (date(2025, 6, 1), date(2025, 9, 1))


def connect():
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set. Copy .env.example to .env first.",
              file=sys.stderr)
        raise SystemExit(1)
    conn = psycopg.connect(url)
    conn.execute("set time zone 'UTC'")
    return conn


def parse_as_of(value: str | None):
    if value is None:
        return None
    if value == PERIOD_END:
        return PERIOD_END
    return date.fromisoformat(value)


def cohort_from(args) -> CohortSpec:
    return CohortSpec(
        countries=tuple(args.country or ()),
        tiers=tuple(args.tier or ()),
        tier_as_of=parse_as_of(args.as_of),
    )


def show(result) -> None:
    print(f"\n{result.metric}  [{result.unit}]")
    print(f"  cohort   {result.cohort}  ({result.cohort_size:,} customers)")
    print(f"  period   {result.period_start} .. {result.period_end} (exclusive)"
          f"  granularity={result.granularity}")
    if result.filters:
        print(f"  filters  {result.filters}")
    print()
    if not result.rows:
        print("  (no rows)")
        return
    columns = [c for c in result.rows[0] if c != "value"]
    width = {c: max(len(c), max(len(str(r[c])) for r in result.rows))
             for c in columns}
    header = "  ".join(c.ljust(width[c]) for c in columns)
    print(f"  {header}{'  ' if columns else ''}{'value':>16}")
    for row in result.rows:
        cells = "  ".join(str(row[c]).ljust(width[c]) for c in columns)
        print(f"  {cells}{'  ' if columns else ''}{row['value']:>16,.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m metrics")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show the metric catalogue")

    def add_common(p):
        p.add_argument("metric")
        p.add_argument("--country", action="append", help="repeatable, e.g. GB")
        p.add_argument("--tier", action="append", help="repeatable, e.g. GOLD")
        p.add_argument("--as-of", help=f"a date, or {PERIOD_END!r}")
        p.add_argument("--monthly", action="store_true")
        p.add_argument("--filter", action="append", default=[],
                       metavar="NAME=VALUE")
        p.add_argument("--sql", action="store_true", help="print the SQL that ran")

    run_parser = sub.add_parser("run", help="run a metric over one period")
    add_common(run_parser)
    run_parser.add_argument("--start", type=date.fromisoformat, default=FOCUS[0])
    run_parser.add_argument("--end", type=date.fromisoformat, default=FOCUS[1],
                            help="exclusive")

    compare_parser = sub.add_parser(
        "compare", help="run a metric over the focus period and the year before")
    add_common(compare_parser)

    args = parser.parse_args()

    if args.command == "list":
        for entry in catalog.catalogue():
            print(f"\n{entry['name']}  [{entry['unit']}]")
            print(f"  {entry['description']}")
            details = []
            if entry["dimensions"]:
                details.append(f"split by {', '.join(entry['dimensions'])}")
            if entry["filters"]:
                details.append(f"filters: {', '.join(entry['filters'])}")
            if entry["supports_monthly"]:
                details.append("supports --monthly")
            if details:
                print(f"  ({'; '.join(details)})")
        return 0

    filters = dict(pair.split("=", 1) for pair in args.filter)
    cohort = cohort_from(args)
    granularity = MONTH if args.monthly else TOTAL

    with connect() as conn:
        if args.command == "run":
            result = execute(conn, MetricRequest(
                args.metric, args.start, args.end, cohort, granularity, filters))
            show(result)
            if args.sql:
                print(f"\n{result.sql}\n\nparams: {result.params}")
            return 0

        # compare: the same metric over two periods, which is the shape of
        # nearly every real question about a change.
        results = []
        for label, (start, end) in (("prior year", PRIOR), ("focus", FOCUS)):
            result = execute(conn, MetricRequest(
                args.metric, start, end, cohort, granularity, filters))
            show(result)
            results.append((label, result))

        if all(len(r.rows) == 1 and not r.dimensions for _, r in results):
            old = results[0][1].rows[0]["value"]
            new = results[1][1].rows[0]["value"]
            delta = (new - old) / old * 100 if old else 0.0
            print(f"\n  year-over-year: {old:,.3f} -> {new:,.3f}  ({delta:+.1f}%)")
            print(f"  cohort size:    {results[0][1].cohort_size:,} -> "
                  f"{results[1][1].cohort_size:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
