"""Who are we measuring?

This is the most important abstraction in the metrics layer, and it exists
because of something the dataset proved rather than something I assumed.

"Gold customers in the UK" is ambiguous. It can mean:

  * whoever is Gold *now*, measured over some past period, or
  * whoever was Gold *at some point in the past*.

In L-Mart those two readings differ by about 21 percentage points, because 45
of the highest-value Gold members were promoted to Platinum in February 2026.
Read it one way and engagement collapsed; read it the other and it declined
about half as much, with the rest being a change in who is called Gold.

So `tier_as_of` has NO DEFAULT. Asking for a tier without saying as-of-when is
an error, not a query. That forces the caller -- eventually the model -- to make
the choice explicitly, puts the choice in the trace where it can be inspected,
and lets the eval mark whether it chose correctly. A sensible default here would
have hidden the single most interesting decision in the whole investigation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from core.errors import ActionableError

# Mirrors lmart.customers.country_code and lmart.loyalty_tiers.tier_code.
# Held here so that a mistyped filter fails immediately with a clear message
# rather than silently returning an empty cohort, which reads like "no data"
# instead of "you asked for something that does not exist".
# tests/test_metric_values.py asserts these still match the database.
COUNTRIES = ("GB", "IN", "AE", "SG")
TIERS = ("BRONZE", "SILVER", "GOLD", "PLATINUM")

# Sentinel for "resolve the tier at the end of whatever period is being
# measured", i.e. a cohort whose membership moves between comparison periods.
PERIOD_END = "period_end"


class CohortError(ActionableError, ValueError):
    """The cohort could not be resolved as specified."""


@dataclass(frozen=True)
class CohortSpec:
    """A structured description of a population.

    countries / tiers are empty tuples meaning "no filter on this attribute".
    """

    countries: tuple[str, ...] = ()
    tiers: tuple[str, ...] = ()
    tier_as_of: date | str | None = None
    members_only: bool = False

    def validate(self) -> None:
        for country in self.countries:
            if country not in COUNTRIES:
                raise CohortError(
                    f"Unknown country {country!r}. Known: {', '.join(COUNTRIES)}")
        for tier in self.tiers:
            if tier not in TIERS:
                raise CohortError(
                    f"Unknown tier {tier!r}. Known: {', '.join(TIERS)}")

        if self.tiers and self.tier_as_of is None:
            raise CohortError(
                "Filtering by tier requires tier_as_of. Tier membership changes "
                "over time: pass a date to hold the cohort fixed at that point, "
                f"or {PERIOD_END!r} to resolve it separately for each period "
                "measured. In L-Mart the two answers differ by about 21 "
                "percentage points, so there is no safe default.")

        if self.tier_as_of is not None and not self.tiers:
            raise CohortError(
                "tier_as_of was given without a tier filter, which has no effect.")

        if (self.tier_as_of is not None
                and not isinstance(self.tier_as_of, date)
                and self.tier_as_of != PERIOD_END):
            raise CohortError(
                f"tier_as_of must be a date or {PERIOD_END!r}, "
                f"got {self.tier_as_of!r}")

    def resolve_as_of(self, period_end: date) -> date | None:
        """The concrete date at which tier membership is read.

        PERIOD_END resolves to the last day inside the period, not the exclusive
        end bound -- "their tier during the period we measured", not "their tier
        the moment after it closed".
        """
        if self.tier_as_of is None:
            return None
        if self.tier_as_of == PERIOD_END:
            return period_end - timedelta(days=1)
        return self.tier_as_of

    def describe(self, period_end: date) -> str:
        """Human-readable, for the trace and for citation in an answer."""
        parts = []
        if self.countries:
            parts.append("/".join(self.countries))
        if self.tiers:
            as_of = self.resolve_as_of(period_end)
            anchor = ("as of the end of each period measured"
                      if self.tier_as_of == PERIOD_END else f"as of {as_of}")
            parts.append(f"{'/'.join(self.tiers)} tier ({anchor})")
        elif self.members_only:
            parts.append("loyalty members")
        return " ".join(parts) if parts else "all customers"


def build_cohort_sql(spec: CohortSpec, period_end: date) -> tuple[str, dict]:
    """Return the cohort CTE body and its parameters.

    The result is always a single column, customer_id. Every metric joins to it
    by that column, so cohort resolution is written once here rather than being
    re-derived -- correctly or otherwise -- inside ten different metrics.
    """
    spec.validate()

    joins: list[str] = []
    conditions: list[str] = []
    params: dict = {}

    if spec.tiers or spec.members_only:
        joins.append(
            "join lmart.loyalty_accounts la on la.customer_id = cust.customer_id")

    if spec.tiers:
        as_of = spec.resolve_as_of(period_end)
        # The point-in-time join. effective_to is exclusive and null means
        # "still in effect", so this selects exactly one span per account.
        joins.append(
            "join lmart.tier_history th on th.account_id = la.account_id\n"
            "         and th.effective_from <= %(cohort_as_of)s\n"
            "         and (th.effective_to is null "
            "or th.effective_to > %(cohort_as_of)s)")
        conditions.append("th.tier_code = any(%(cohort_tiers)s)")
        params["cohort_as_of"] = as_of
        params["cohort_tiers"] = list(spec.tiers)

    if spec.countries:
        conditions.append("cust.country_code = any(%(cohort_countries)s)")
        params["cohort_countries"] = list(spec.countries)

    where = ("\n       where " + "\n         and ".join(conditions)) if conditions else ""
    join_sql = ("\n       " + "\n       ".join(joins)) if joins else ""

    # Aliased `cust`, not `c`: `c` is taken by lmart.campaigns in the outer
    # query of campaign_funnel. Scoping makes that safe but not readable.
    sql = (f"select distinct cust.customer_id\n"
           f"       from lmart.customers cust{join_sql}{where}")
    return sql, params
