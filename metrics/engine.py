"""Validate a metric request, assemble its SQL, run it, return a typed result.

SQL ASSEMBLY IS SEPARATED FROM EXECUTION on purpose. build_sql() is a pure
function of the request, so the rules that actually matter -- you cannot filter
by tier without saying as-of-when, you cannot ask a period-independent metric
for a monthly series, you cannot pass a filter a metric does not support -- are
testable in a second with no database and no credentials. Only the numbers
themselves need Postgres.

WHAT GOES BACK TO THE CALLER. A MetricResult echoes the request in full: the
metric, the period, the resolved cohort description, and the cohort's size.
That last one matters. A per-member average can move because behaviour changed
or because the membership changed, and the only way to tell them apart is to see
both numbers. It costs one extra round trip and is worth it.

The result also carries the SQL that produced it, for the trace. The tool layer
will strip that before anything reaches the model: it is there so a human can
audit a number, not so a model can learn to write queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from psycopg.rows import dict_row

from . import catalog
from .cohort import CohortSpec, build_cohort_sql

TOTAL = "total"
MONTH = "month"
GRANULARITIES = (TOTAL, MONTH)

# Average days per month. Per-member metrics are expressed per member per month
# so that periods of different lengths are comparable; a ratio between two equal
# periods is unaffected by the exact constant.
DAYS_PER_MONTH = 30.44


class MetricRequestError(ValueError):
    """The request cannot be answered as specified."""


@dataclass(frozen=True)
class MetricRequest:
    metric: str
    period_start: date
    period_end: date                      # exclusive
    cohort: CohortSpec = field(default_factory=CohortSpec)
    granularity: str = TOTAL
    filters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricResult:
    metric: str
    description: str
    unit: str
    period_start: date
    period_end: date
    granularity: str
    cohort: str
    cohort_size: int
    filters: dict
    dimensions: tuple[str, ...]
    rows: list[dict]
    sql: str                              # for the trace, not for the model
    params: dict


def _validate(request: MetricRequest) -> catalog.MetricDefinition:
    definition = catalog.get(request.metric)

    if request.period_start >= request.period_end:
        raise MetricRequestError(
            f"period_start ({request.period_start}) must be before period_end "
            f"({request.period_end}); period_end is exclusive.")

    if request.granularity not in GRANULARITIES:
        raise MetricRequestError(
            f"Unknown granularity {request.granularity!r}. "
            f"Use one of: {', '.join(GRANULARITIES)}")

    if request.granularity == MONTH and not definition.supports_month:
        raise MetricRequestError(
            f"{definition.name} does not vary over time, so it cannot be "
            f"broken down by month.")

    unknown = set(request.filters) - set(definition.filter_names)
    if unknown:
        supported = ", ".join(definition.filter_names) or "none"
        raise MetricRequestError(
            f"{definition.name} does not support filter(s) "
            f"{', '.join(sorted(unknown))}. Supported: {supported}")

    request.cohort.validate()
    return definition


def build_sql(request: MetricRequest) -> tuple[str, dict]:
    """Assemble the SQL and parameters for a request. Pure; no database needed."""
    definition = _validate(request)

    cohort_sql, params = build_cohort_sql(request.cohort, request.period_end)
    params["start"] = request.period_start
    params["end"] = request.period_end

    # Per-member metrics divide by this. A monthly series wants a per-month rate
    # in each bucket, so its divisor is one month rather than the whole period.
    days = (request.period_end - request.period_start).days
    params["months"] = (1.0 if request.granularity == MONTH
                        else max(days / DAYS_PER_MONTH, 1e-9))

    period_predicate = "true"
    if definition.period_column:
        period_predicate = (f"{definition.period_column} >= %(start)s "
                            f"and {definition.period_column} < %(end)s")

    from_sql = definition.from_sql.format(period=period_predicate)
    # A metric whose from_sql has no {period} slot never had the predicate
    # applied, so it goes in the WHERE clause instead.
    conditions: list[str] = []
    if definition.period_column and "{period}" not in definition.from_sql:
        conditions.append(period_predicate)

    for name, predicate in definition.filters:
        if name in request.filters:
            conditions.append(predicate)
            params[f"filter_{name}"] = request.filters[name]

    selects: list[str] = []
    if request.granularity == MONTH:
        selects.append(
            f"date_trunc('month', {definition.period_column})::date as period")
    for alias, expression in definition.dimensions:
        selects.append(f"{expression} as {alias}")

    group_ordinals = list(range(1, len(selects) + 1))
    selects.append(f"{definition.value_sql} as value")

    where = ("\nwhere " + "\n  and ".join(conditions)) if conditions else ""
    grouping = ""
    if group_ordinals:
        ordinals = ", ".join(str(n) for n in group_ordinals)
        grouping = f"\ngroup by {ordinals}\norder by {ordinals}"

    # Bound to a name rather than inlined: f-string expressions cannot contain
    # a backslash before Python 3.12, and this project targets 3.11.
    select_list = ",\n       ".join(selects)
    sql = (f"with cohort as (\n    {cohort_sql}\n)\n"
           f"select {select_list}\n"
           f"from {from_sql}{where}{grouping}")
    return sql, params


COHORT_SIZE_SQL = "with cohort as (\n    {cohort}\n)\nselect count(*) from cohort"


def execute(conn, request: MetricRequest) -> MetricResult:
    """Run a metric request against Postgres."""
    definition = _validate(request)
    sql, params = build_sql(request)

    cohort_sql, cohort_params = build_cohort_sql(request.cohort, request.period_end)
    with conn.cursor() as cur:
        cohort_size = cur.execute(
            COHORT_SIZE_SQL.format(cohort=cohort_sql), cohort_params).fetchone()[0]

    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(sql, params).fetchall()

    # Postgres numerics arrive as Decimal. Convert so results are JSON-safe --
    # they are about to become tool output.
    for row in rows:
        for key, value in row.items():
            if isinstance(value, Decimal):
                row[key] = float(value)

    return MetricResult(
        metric=definition.name,
        description=definition.description,
        unit=definition.unit,
        period_start=request.period_start,
        period_end=request.period_end,
        granularity=request.granularity,
        cohort=request.cohort.describe(request.period_end),
        cohort_size=cohort_size,
        filters=dict(request.filters),
        dimensions=definition.dimension_names,
        rows=rows,
        sql=sql,
        params=params,
    )


def scalar(result: MetricResult) -> float | None:
    """The single value of a metric with no dimensions and no monthly split."""
    if result.dimensions or result.granularity == MONTH:
        raise MetricRequestError(
            f"{result.metric} returned {len(result.rows)} rows; it has no single "
            f"value. Read result.rows.")
    if not result.rows:
        return None
    return result.rows[0]["value"]
