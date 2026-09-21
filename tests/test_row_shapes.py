"""Catch load-breaking mistakes without needing a database.

seed/load.py streams rows into Postgres as CSV text. Two classes of mistake fail
only at COPY time, against a real database, with an error that points at a byte
offset rather than at the code:

  1. A column list that drifts out of step with its row tuples.
  2. A genuinely empty string in the data. COPY is configured with `null ''`,
     so an empty text value would arrive as SQL NULL -- silently, and only
     visibly later as a missing name or a broken join.

Both are cheap to check here, in about a second, with no credentials.
"""

from __future__ import annotations

import csv
import io
import itertools

import pytest

from seed import generate, load

SAMPLE_ROWS = 5_000


@pytest.fixture(scope="module")
def plan():
    customers, ref, tables = generate.build(quiet=True)
    # Row sources are generators; materialise them so the tests can iterate twice.
    return [(table, columns, list(rows))
            for table, columns, rows in load.build_plan(customers, ref, tables)]


def test_every_row_matches_its_column_list(plan):
    for table, columns, rows in plan:
        assert rows, f"{table} produced no rows"
        widths = {len(row) for row in rows}
        assert widths == {len(columns)}, (
            f"{table}: declares {len(columns)} columns but rows have "
            f"widths {sorted(widths)}")


def test_no_empty_strings_would_be_read_as_null(plan):
    for table, columns, rows in plan:
        for row in itertools.islice(rows, SAMPLE_ROWS):
            for column, value in zip(columns, row):
                assert value != "", (
                    f"{table}.{column} is an empty string, which COPY's "
                    f"`null ''` would load as NULL. Use None for a real null.")


def test_none_renders_as_an_empty_csv_field():
    """The assumption `null ''` depends on. Asserted rather than believed."""
    buffer = io.StringIO()
    csv.writer(buffer).writerow((1, None, "x"))
    assert buffer.getvalue().strip() == "1,,x"
