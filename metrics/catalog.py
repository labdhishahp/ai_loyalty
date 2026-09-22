"""The metric catalogue: every number this system knows how to compute.

WHY A CATALOGUE RATHER THAN GENERATED SQL. A business metric is a *definition*,
not a query. "Engagement" has to mean the same thing every time it is asked for,
or two answers cannot be compared and no eval score means anything. Hand-written
SQL also gets unit tests; generated SQL gets nothing.

Specifically, an LLM writing SQL against this schema would sooner or later join
lmart.orders to lmart.order_items and count a three-item order as three orders.
Nothing errors. The number is simply wrong, and it looks entirely reasonable.

So the model chooses WHICH metric and WHAT to plug into it. The SQL is ours.

Each definition is deliberately small enough to read in one go:

  from_sql        FROM/JOIN chain. `cohort` is a CTE of customer_id supplied by
                  the engine; `{period}` is replaced with the period predicate.
  value_sql       the aggregate being measured.
  period_column   which column the period filters on (None = period-independent).
  dimensions      fixed per metric. The only caller-chosen axis is total vs
                  monthly -- a general dimension picker would be a query builder,
                  and query builders are where correctness goes to die.
  filters         an allowlist of extra predicates the caller may supply.

INNER JOINS THROUGHOUT. Per-member metrics take their denominator from a
subquery over `cohort`, not from the join, so an inner join gives an identical
answer to a left join while keeping monthly grouping simple. The visible
consequence: a month with no matching rows is ABSENT from a monthly series
rather than present as zero. For campaign_funnel that is exactly right -- the
absence of waves after May 2026 is the signal, not a gap in the data.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.errors import ActionableError


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    description: str
    unit: str
    from_sql: str
    value_sql: str
    period_column: str | None
    dimensions: tuple[tuple[str, str], ...] = ()      # (alias, SQL expression)
    filters: tuple[tuple[str, str], ...] = ()         # (name, SQL predicate)
    supports_month: bool = True
    note: str = ""

    @property
    def dimension_names(self) -> tuple[str, ...]:
        return tuple(alias for alias, _ in self.dimensions)

    @property
    def filter_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.filters)


# Reused join chains. Written once so that a mistake cannot be made in one
# metric and not another.
ORDERS = ("cohort\n"
          "     join lmart.orders o on o.customer_id = cohort.customer_id "
          "and {period}")

ORDER_LINES = (ORDERS + "\n"
               "     join lmart.order_items oi on oi.order_id = o.order_id\n"
               "     join lmart.products p on p.product_id = oi.product_id")

# Per-member metrics divide by the cohort size and by %(months)s. The engine
# passes the period's length in months for a total, and 1.0 for a monthly
# series, so each monthly bucket is a per-month rate rather than a period rate.
PER_MEMBER = "/ nullif((select count(*) from cohort), 0) / %(months)s"

DEFINITIONS: tuple[MetricDefinition, ...] = (

    MetricDefinition(
        name="cohort_size",
        description=(
            "How many customers match the cohort definition. Worth checking "
            "before anything else: if a cohort's size changed between two "
            "periods, per-member averages moved partly because the membership "
            "moved, not because behaviour did."),
        unit="customers",
        from_sql="cohort",
        value_sql="count(*)::numeric",
        period_column=None,
        supports_month=False,
    ),

    MetricDefinition(
        name="active_rate",
        description=(
            "Share of the cohort that placed at least one order in the period. "
            "Measures breadth of engagement -- how many people came back at "
            "all -- as opposed to how often or how much they spent."),
        unit="ratio",
        from_sql=ORDERS,
        value_sql=("count(distinct o.customer_id)::numeric "
                   "/ nullif((select count(*) from cohort), 0)"),
        period_column="o.ordered_at",
    ),

    MetricDefinition(
        name="orders_per_member",
        description=(
            "Orders per cohort member per month. The main frequency measure: "
            "how often people shop. Members who did not order at all are "
            "included in the denominator."),
        unit="orders per member per month",
        from_sql=ORDERS,
        value_sql=f"count(o.order_id)::numeric {PER_MEMBER}",
        period_column="o.ordered_at",
    ),

    MetricDefinition(
        name="revenue_per_member",
        description=(
            "Net revenue per cohort member per month, in GBP pence. The main "
            "spend measure. Read alongside orders_per_member: a cause can move "
            "one and barely move the other, so a decline described with only "
            "one of them is an incomplete answer."),
        unit="GBP pence per member per month",
        from_sql=ORDERS,
        value_sql=f"sum(o.net_amount_minor)::numeric {PER_MEMBER}",
        period_column="o.ordered_at",
    ),

    MetricDefinition(
        name="avg_order_value",
        description=(
            "Average net value of an order, in GBP pence. Per ORDER, not per "
            "member -- it says nothing about how often people shop."),
        unit="GBP pence",
        from_sql=ORDERS,
        value_sql=("sum(o.net_amount_minor)::numeric "
                   "/ nullif(count(o.order_id), 0)"),
        period_column="o.ordered_at",
    ),

    MetricDefinition(
        name="total_revenue",
        description=(
            "Total net revenue for the cohort, in GBP pence. An absolute total, "
            "so it moves with cohort size as well as with behaviour."),
        unit="GBP pence",
        from_sql=ORDERS,
        value_sql="sum(o.net_amount_minor)::numeric",
        period_column="o.ordered_at",
    ),

    MetricDefinition(
        name="revenue_by_category",
        description=(
            "Net revenue split by product category, in GBP pence. Use it to "
            "find whether a change is concentrated in part of the range rather "
            "than spread across everything."),
        unit="GBP pence",
        from_sql=ORDER_LINES + "\n"
                 "     join lmart.categories cat on cat.category_id = p.category_id",
        value_sql="sum(oi.line_amount_minor)::numeric",
        period_column="o.ordered_at",
        dimensions=(("category", "cat.code"),),
    ),

    MetricDefinition(
        name="orders_by_channel",
        description=(
            "Order counts split by channel (store, web, app). Use it to check "
            "whether shopping moved between channels rather than stopping. A "
            "channel shift that affects every cohort equally cannot explain a "
            "change specific to one of them."),
        unit="orders",
        from_sql=ORDERS,
        value_sql="count(*)::numeric",
        period_column="o.ordered_at",
        dimensions=(("channel", "o.channel"),),
    ),

    MetricDefinition(
        name="points_expired",
        description=(
            "Loyalty points that expired, by month. A large expiry is visible "
            "and dramatic; whether it changed anyone's behaviour is a separate "
            "question that this metric does not answer."),
        unit="points",
        from_sql=("cohort\n"
                  "     join lmart.loyalty_accounts la "
                  "on la.customer_id = cohort.customer_id\n"
                  "     join lmart.points_ledger pl on pl.account_id = la.account_id\n"
                  "         and pl.entry_type = 'expire' and {period}"),
        value_sql="-sum(pl.points)::numeric",
        period_column="pl.occurred_at",
    ),

    MetricDefinition(
        name="campaign_funnel",
        description=(
            "Campaign events (sent, delivered, opened, clicked, converted, "
            "unsubscribed) for waves sent in the period, optionally filtered to "
            "one programme. Grouped by month this also shows WHEN a programme "
            "was sending: a programme that stopped has no rows after its last "
            "wave, and that absence is the only record of it stopping. A high "
            "conversion rate is not by itself evidence that a campaign caused "
            "anything -- some of those orders would have happened regardless."),
        unit="events",
        from_sql=("lmart.campaigns c\n"
                  "     join lmart.campaign_events e on e.campaign_id = c.campaign_id\n"
                  "     join cohort on cohort.customer_id = e.customer_id"),
        value_sql="count(*)::numeric",
        period_column="c.sent_on",
        dimensions=(("event_type", "e.event_type"),),
        filters=(("programme", "c.programme = %(filter_programme)s"),),
    ),

    MetricDefinition(
        name="tier_changes",
        description=(
            "Loyalty tier movements in the period, split by reason and by the "
            "tier moved to. A cluster of upgrades on one date means the "
            "membership of the tier below it changed on that date, which will "
            "move that tier's per-member averages without anyone's behaviour "
            "changing."),
        unit="tier changes",
        from_sql=("cohort\n"
                  "     join lmart.loyalty_accounts la "
                  "on la.customer_id = cohort.customer_id\n"
                  "     join lmart.tier_history th on th.account_id = la.account_id\n"
                  "         and {period}"),
        value_sql="count(*)::numeric",
        period_column="th.effective_from",
        dimensions=(("reason", "th.reason"), ("to_tier", "th.tier_code")),
    ),
)

BY_NAME = {definition.name: definition for definition in DEFINITIONS}


class UnknownMetric(ActionableError):
    """Asked for a metric that does not exist."""


def get(name: str) -> MetricDefinition:
    try:
        return BY_NAME[name]
    except KeyError:
        raise UnknownMetric(
            f"Unknown metric {name!r}. Available: "
            f"{', '.join(sorted(BY_NAME))}") from None


def catalogue() -> list[dict]:
    """A description of every metric, for discovery.

    This is what a `list_metrics` tool will return in the next slice, so the
    descriptions above are written for a reader deciding which metric to use --
    not as internal notes.
    """
    return [
        {
            "name": d.name,
            "description": d.description,
            "unit": d.unit,
            "dimensions": list(d.dimension_names),
            "filters": list(d.filter_names),
            "supports_monthly": d.supports_month,
        }
        for d in DEFINITIONS
    ]
