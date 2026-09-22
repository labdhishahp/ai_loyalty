"""The capabilities the model may use.

Six tools, chosen so that each answers a question the others cannot:

  get_reference_data  what values actually exist. Without it the model guesses
                      "UK" when the data says "GB", gets an empty result, and
                      concludes there is no data -- a silent, confident wrong
                      answer. One call removes a whole class of failure.
  list_metrics        discovery. The eleven metric descriptions are long and
                      useful; inlining them into every tool definition would
                      spend that context on every single turn instead of once.
  get_metric          the workhorse. The model picks a metric and parameterises
                      it; it never writes SQL.
  list_campaigns      a metric cannot show an ABSENCE. "Which programmes ran,
                      and when did each last send" is the only way to notice a
                      programme that stopped, and nothing flags that.
  search_customers    find people by name, email or cohort.
  get_customer_360    one customer in full, when an aggregate needs a witness.

Descriptions are written for a model choosing between them under context
pressure: what the tool answers, and when NOT to reach for it.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from metrics import catalog as metric_catalog
from metrics.cohort import PERIOD_END, CohortSpec, build_cohort_sql
from metrics.engine import MetricRequest, execute

from .envelope import ToolResult
from .registry import tool


class _Input(BaseModel):
    """extra='forbid' makes Pydantic emit additionalProperties:false, which is
    what providers' strict tool modes require to guarantee valid arguments."""
    model_config = ConfigDict(extra="forbid")


def _rows(conn, sql: str, params: tuple | dict = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------

class ReferenceInput(_Input):
    pass


@tool("get_reference_data",
      "Lists every value that filters accept: country codes, loyalty tiers, "
      "product categories, sales channels, campaign programme names, and the "
      "date range the data covers. Call this FIRST, before filtering by "
      "anything. Filter values are exact codes, not names -- the United Kingdom "
      "is 'GB', not 'UK'. A wrong code returns an empty result that looks "
      "exactly like 'nothing happened', so guessing produces confident wrong "
      "answers.",
      ReferenceInput)
def get_reference_data(_: ReferenceInput, conn) -> ToolResult:
    data = {
        "countries": [r["country_code"] for r in _rows(
            conn, "select distinct country_code from lmart.customers order by 1")],
        "tiers": _rows(conn, "select tier_code, name, rank, "
                             "annual_spend_threshold_minor, points_earn_multiplier "
                             "from lmart.loyalty_tiers order by rank"),
        "categories": [r["code"] for r in _rows(
            conn, "select code from lmart.categories order by code")],
        "channels": [r["channel"] for r in _rows(
            conn, "select distinct channel from lmart.orders order by 1")],
        "campaign_programmes": _rows(
            conn, "select programme, count(*) as waves, min(sent_on) as first_wave, "
                  "max(sent_on) as last_wave from lmart.campaigns group by programme "
                  "order by programme"),
        "data_range": _rows(
            conn, "select min(ordered_at)::date as first_order, "
                  "max(ordered_at)::date as last_order from lmart.orders")[0],
        "currency": "GBP; all monetary values are integer pence (minor units)",
    }
    return ToolResult(
        tool="get_reference_data", call_id="", data=data,
        summary=(f"{len(data['countries'])} countries, {len(data['tiers'])} tiers, "
                 f"{len(data['categories'])} categories, "
                 f"{len(data['campaign_programmes'])} campaign programmes; "
                 f"orders from {data['data_range']['first_order']} to "
                 f"{data['data_range']['last_order']}"))


# ---------------------------------------------------------------------------

class ListMetricsInput(_Input):
    pass


@tool("list_metrics",
      "Lists every metric that can be computed, with what each measures, its "
      "unit, how it is split, and which filters it accepts. Call this before "
      "get_metric unless you already know the exact metric name.",
      ListMetricsInput)
def list_metrics(_: ListMetricsInput, conn) -> ToolResult:
    entries = metric_catalog.catalogue()
    return ToolResult(tool="list_metrics", call_id="", data=entries,
                      summary=f"{len(entries)} metrics available: "
                              f"{', '.join(e['name'] for e in entries)}")


# ---------------------------------------------------------------------------

class GetMetricInput(_Input):
    metric: str = Field(description="Metric name from list_metrics.")
    period_start: date = Field(description="First day of the period, inclusive.")
    period_end: date = Field(description="Day AFTER the period, exclusive. "
                                         "For June-August 2026 use 2026-09-01.")
    granularity: Literal["total", "month"] = Field(
        default="total",
        description="'total' for one number over the period; 'month' for a "
                    "monthly series. A month with no matching rows is absent "
                    "from the series rather than shown as zero.")
    countries: list[str] = Field(
        default_factory=list,
        description="Country codes to include. Empty means all countries.")
    tiers: list[str] = Field(
        default_factory=list,
        description="Loyalty tier codes to include. Empty means all customers, "
                    "members and non-members alike.")
    tier_as_of: str | None = Field(
        default=None,
        description="REQUIRED whenever tiers is set, and there is no default. "
                    "Tier membership changes over time, so 'Gold customers' is "
                    "ambiguous. Pass an ISO date (e.g. '2026-01-15') to hold the "
                    "cohort fixed at that moment, so the same people are measured "
                    "in every period. Pass 'period_end' to re-resolve the cohort "
                    "for each period, which is how a dashboard usually reads it. "
                    "Comparing the two tells you whether a change in a per-member "
                    "average came from behaviour or from a change in who is in "
                    "the tier.")
    programme: str | None = Field(
        default=None,
        description="Only for campaign_funnel: restrict to one campaign "
                    "programme, exactly as named by get_reference_data.")


@tool("get_metric",
      "Computes a named business metric over a period for a cohort. The metric "
      "definitions are fixed and tested, so two calls with the same arguments "
      "always agree. Returns the resolved cohort and its SIZE alongside the "
      "value: a per-member average can move because behaviour changed or because "
      "the cohort's membership changed, and only both numbers together "
      "distinguish them.",
      GetMetricInput)
def get_metric(inp: GetMetricInput, conn) -> ToolResult:
    as_of: date | str | None = None
    if inp.tier_as_of:
        as_of = (PERIOD_END if inp.tier_as_of == PERIOD_END
                 else date.fromisoformat(inp.tier_as_of))

    result = execute(conn, MetricRequest(
        metric=inp.metric,
        period_start=inp.period_start,
        period_end=inp.period_end,
        cohort=CohortSpec(countries=tuple(inp.countries), tiers=tuple(inp.tiers),
                          tier_as_of=as_of),
        granularity=inp.granularity,
        filters={"programme": inp.programme} if inp.programme else {},
    ))

    if result.granularity == "total" and not result.dimensions and result.rows:
        value = result.rows[0]["value"]
        headline = f"{value:,.4g} {result.unit}"
    else:
        headline = f"{len(result.rows)} rows"

    return ToolResult(
        tool="get_metric", call_id="", data=result.rows,
        summary=(f"{result.metric} for {result.cohort} "
                 f"({result.cohort_size:,} customers), "
                 f"{result.period_start} to {result.period_end}: {headline}"),
        meta={"metric": result.metric, "unit": result.unit,
              "cohort": result.cohort, "cohort_size": result.cohort_size,
              "period_start": str(result.period_start),
              "period_end": str(result.period_end),
              "granularity": result.granularity, "filters": result.filters},
        # SQL is trace-only. The model must never see the query: the metrics
        # layer exists so it cannot reason about joins it may not write.
        internals={"sql": result.sql, "params": {k: str(v) for k, v
                                                 in result.params.items()}})


# ---------------------------------------------------------------------------

class ListCampaignsInput(_Input):
    programme: str | None = Field(
        default=None,
        description="Omit for a summary of every programme. Give a programme "
                    "name to list its individual send waves.")


@tool("list_campaigns",
      "Lists campaign programmes and when each one sent. Omit programme for a "
      "summary showing every programme's first wave, last wave and wave count; "
      "give a programme to see its individual waves. Use this to find out "
      "whether something STOPPED: a programme that was switched off leaves no "
      "record of being switched off, only an absence of waves after a date. No "
      "metric can show you an absence, so this is the only way to see it.",
      ListCampaignsInput)
def list_campaigns(inp: ListCampaignsInput, conn) -> ToolResult:
    if inp.programme is None:
        rows = _rows(conn, """
            select c.programme, count(distinct c.campaign_id) as waves,
                   min(c.sent_on) as first_wave, max(c.sent_on) as last_wave,
                   count(*) filter (where e.event_type = 'sent') as total_sent
            from lmart.campaigns c
            left join lmart.campaign_events e on e.campaign_id = c.campaign_id
            group by c.programme order by c.programme
        """)
        return ToolResult(
            tool="list_campaigns", call_id="", data=rows,
            summary="; ".join(f"{r['programme']}: {r['waves']} waves, "
                              f"{r['first_wave']} to {r['last_wave']}"
                              for r in rows) or "no campaigns")

    rows = _rows(conn, """
        select c.campaign_id, c.code, c.name, c.sent_on, c.objective,
               c.target_description, c.status,
               count(*) filter (where e.event_type = 'sent')      as sent,
               count(*) filter (where e.event_type = 'opened')    as opened,
               count(*) filter (where e.event_type = 'clicked')   as clicked,
               count(*) filter (where e.event_type = 'converted') as converted
        from lmart.campaigns c
        left join lmart.campaign_events e on e.campaign_id = c.campaign_id
        where c.programme = %s
        group by c.campaign_id, c.code, c.name, c.sent_on, c.objective,
                 c.target_description, c.status
        order by c.sent_on
    """, (inp.programme,))
    if not rows:
        return ToolResult(tool="list_campaigns", call_id="", data=[],
                          summary=f"No programme named {inp.programme!r}. "
                                  f"Call list_campaigns without a programme to "
                                  f"see the names that exist.")
    return ToolResult(
        tool="list_campaigns", call_id="", data=rows,
        summary=(f"{inp.programme}: {len(rows)} waves from {rows[0]['sent_on']} "
                 f"to {rows[-1]['sent_on']}; last wave sent {rows[-1]['sent']} "
                 f"and converted {rows[-1]['converted']}"))


# ---------------------------------------------------------------------------

class SearchCustomersInput(_Input):
    query: str | None = Field(
        default=None, description="Matches name or email, case-insensitive.")
    countries: list[str] = Field(default_factory=list)
    tiers: list[str] = Field(default_factory=list)
    tier_as_of: str | None = Field(
        default=None, description="Required when tiers is set. ISO date or "
                                  "'period_end'. See get_metric.")
    limit: int = Field(default=20, ge=1, le=100)


@tool("search_customers",
      "Finds customers by name or email, optionally narrowed to a country and "
      "loyalty tier. Returns identifiers and basic attributes, not behaviour -- "
      "use get_customer_360 for one customer's history.",
      SearchCustomersInput)
def search_customers(inp: SearchCustomersInput, conn) -> ToolResult:
    as_of: date | str | None = None
    if inp.tier_as_of:
        as_of = (PERIOD_END if inp.tier_as_of == PERIOD_END
                 else date.fromisoformat(inp.tier_as_of))
    spec = CohortSpec(countries=tuple(inp.countries), tiers=tuple(inp.tiers),
                      tier_as_of=as_of)
    # Reuses the cohort resolver rather than re-deriving tier logic: "who is
    # Gold" must mean the same thing in a search as in a metric.
    cohort_sql, params = build_cohort_sql(spec, date.today())

    text_filter = ""
    if inp.query:
        text_filter = ("and (c.full_name ilike %(q)s or c.email ilike %(q)s)")
        params["q"] = f"%{inp.query}%"
    params["limit"] = inp.limit

    rows = _rows(conn, f"""
        with cohort as ({cohort_sql})
        select c.customer_id, c.external_ref, c.full_name, c.email,
               c.country_code, c.city, c.signed_up_on, c.marketing_opt_in,
               la.current_tier
        from cohort
        join lmart.customers c on c.customer_id = cohort.customer_id
        left join lmart.loyalty_accounts la on la.customer_id = c.customer_id
        where true {text_filter}
        order by c.customer_id
        limit %(limit)s
    """, params)
    return ToolResult(tool="search_customers", call_id="", data=rows,
                      meta={"cohort": spec.describe(date.today()),
                            "limit": inp.limit},
                      summary=f"{len(rows)} customer(s) matched"
                              + (" (limit reached; narrow the search)"
                                 if len(rows) == inp.limit else ""))


# ---------------------------------------------------------------------------

class Customer360Input(_Input):
    customer_id: int = Field(description="From search_customers.")


@tool("get_customer_360",
      "Everything about one customer: profile, current and past loyalty tiers, "
      "points balance and recent movements, order history summary, recent "
      "orders, and campaign engagement. Use it to check whether an aggregate "
      "finding holds for an individual, or to investigate one account.",
      Customer360Input)
def get_customer_360(inp: Customer360Input, conn) -> ToolResult:
    profile = _rows(conn, """
        select c.customer_id, c.external_ref, c.full_name, c.email,
               c.country_code, c.city, c.signed_up_on, c.marketing_opt_in,
               la.account_id, la.enrolled_on, la.current_tier, la.status
        from lmart.customers c
        left join lmart.loyalty_accounts la on la.customer_id = c.customer_id
        where c.customer_id = %s
    """, (inp.customer_id,))
    if not profile:
        return ToolResult(tool="get_customer_360", call_id="", data={},
                          summary=f"No customer with id {inp.customer_id}.")
    profile = profile[0]

    data = {
        "profile": profile,
        "tier_history": _rows(conn, """
            select tier_code, effective_from, effective_to, reason
            from lmart.tier_history th
            join lmart.loyalty_accounts la on la.account_id = th.account_id
            where la.customer_id = %s order by effective_from
        """, (inp.customer_id,)),
        "orders_summary": _rows(conn, """
            select count(*) as orders, coalesce(sum(net_amount_minor), 0) as net_minor,
                   min(ordered_at)::date as first_order,
                   max(ordered_at)::date as last_order
            from lmart.orders where customer_id = %s
        """, (inp.customer_id,))[0],
        "recent_orders": _rows(conn, """
            select order_id, ordered_at, channel, net_amount_minor, discount_minor
            from lmart.orders where customer_id = %s
            order by ordered_at desc limit 10
        """, (inp.customer_id,)),
        "points": _rows(conn, """
            select coalesce(sum(points), 0) as balance,
                   coalesce(sum(points) filter (where entry_type = 'expire'), 0) as expired
            from lmart.points_ledger pl
            join lmart.loyalty_accounts la on la.account_id = pl.account_id
            where la.customer_id = %s
        """, (inp.customer_id,))[0],
        "campaign_engagement": _rows(conn, """
            select c.programme, e.event_type, count(*) as events
            from lmart.campaign_events e
            join lmart.campaigns c on c.campaign_id = e.campaign_id
            where e.customer_id = %s group by 1, 2 order by 1, 2
        """, (inp.customer_id,)),
    }
    summary = (f"{profile['full_name']} ({profile['country_code']}, "
               f"tier {profile['current_tier'] or 'not enrolled'}): "
               f"{data['orders_summary']['orders']} orders, "
               f"points balance {data['points']['balance']:,}")
    return ToolResult(tool="get_customer_360", call_id="", data=data,
                      summary=summary)
