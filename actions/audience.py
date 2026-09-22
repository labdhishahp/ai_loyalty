"""Resolving who a campaign would reach. Deterministic, never the model's job.

TWO THINGS ARE ENFORCED HERE RATHER THAN CHECKED LATER, because a filter applied
at send time is a filter that can be forgotten:

  CONSENT. Customers without marketing consent are excluded from the audience,
  not removed from it afterwards. Policy is explicit that this happens at
  resolution, and the difference matters: an audience that contains people who
  opted out is wrong even if nothing is ever sent to it, because the count is
  reported, approved, and acted on.

  GB DORMANCY. Consent is treated as stale in Great Britain after 24 months
  without a purchase. Excluded the same way, and counted, so a reviewer can see
  how many were dropped and why.

THE AUDIENCE IS A RULE, AND IT IS RESOLVED TWICE: once when the proposal is
validated, and again at execution. People opt out and tiers change in between,
so a list captured at proposal time would send to someone who withdrew consent
after it was approved. Same resolver both times.

Cohort resolution reuses metrics/cohort.py: "Gold in GB" must mean exactly the
same thing when targeting a campaign as when measuring the problem it addresses.
Two definitions of a cohort is how a campaign ends up aimed at a different group
from the one that was analysed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.errors import ActionableError
from metrics.cohort import PERIOD_END, CohortSpec, build_cohort_sql

from .rules import DORMANCY_MONTHS_GB


class AudienceError(ActionableError):
    """The audience cannot be resolved as specified."""


def data_as_of(conn) -> date:
    """The date the system treats as "now" for anything about recency.

    NOT the wall clock. L-Mart's transaction data has a fixed horizon, and the
    wall clock runs past it. Anchored to the clock, every customer eventually
    looks lapsed, "no order in 21 days" selects the entire base, and the
    dormancy rule starts excluding people for the passage of time rather than
    for anything they did. That failure is silent: the audience is merely larger
    than it should be, and nothing errors.

    Anchoring to the latest order in the data keeps every recency rule meaning
    what it says. It also makes audience resolution deterministic, so a test
    written today still passes next month.
    """
    return conn.execute(
        "select max(ordered_at)::date from lmart.orders").fetchone()[0]


@dataclass(frozen=True)
class AudienceSpec:
    countries: tuple[str, ...] = ()
    tiers: tuple[str, ...] = ()
    tier_as_of: str | None = None          # ISO date or 'period_end'
    lapsed_min_days: int | None = None     # no order for at least this long
    lapsed_max_days: int | None = None     # ...but not longer than this

    @classmethod
    def from_dict(cls, raw: dict) -> "AudienceSpec":
        return cls(countries=tuple(raw.get("countries") or ()),
                   tiers=tuple(raw.get("tiers") or ()),
                   tier_as_of=raw.get("tier_as_of"),
                   lapsed_min_days=raw.get("lapsed_min_days"),
                   lapsed_max_days=raw.get("lapsed_max_days"))

    def to_dict(self) -> dict:
        return {"countries": list(self.countries), "tiers": list(self.tiers),
                "tier_as_of": self.tier_as_of,
                "lapsed_min_days": self.lapsed_min_days,
                "lapsed_max_days": self.lapsed_max_days}

    def cohort(self) -> CohortSpec:
        as_of: date | str | None = None
        if self.tier_as_of:
            as_of = (PERIOD_END if self.tier_as_of == PERIOD_END
                     else date.fromisoformat(self.tier_as_of))
        return CohortSpec(countries=self.countries, tiers=self.tiers,
                          tier_as_of=as_of)

    def describe(self) -> str:
        parts = [self.cohort().describe(date.today())]
        if self.lapsed_min_days is not None:
            window = f"no order for {self.lapsed_min_days}+ days"
            if self.lapsed_max_days is not None:
                window = (f"no order for {self.lapsed_min_days}-"
                          f"{self.lapsed_max_days} days")
            parts.append(window)
        return ", ".join(parts)


@dataclass(frozen=True)
class Audience:
    customer_ids: list[int]
    size: int
    excluded_no_consent: int
    excluded_dormant_gb: int
    tiers_present: list[str]
    description: str


# One grouped aggregate, not a scalar subquery per customer. The first version
# ran `(select max(ordered_at) from orders where customer_id = c.customer_id)`
# once per row; for a 2,000-customer cohort over the network that took minutes
# rather than the second it should. Recency is needed for every member of the
# audience, so it is computed once for the whole cohort and joined.
RESOLVE_SQL = """
with cohort as ({cohort}),
     last_orders as (
        select o.customer_id, max(o.ordered_at)::date as last_order_on
        from lmart.orders o
        where o.customer_id in (select customer_id from cohort)
        group by o.customer_id
     )
select c.customer_id, la.current_tier, c.marketing_opt_in, c.country_code,
       lo.last_order_on
from cohort
join lmart.customers c on c.customer_id = cohort.customer_id
left join lmart.loyalty_accounts la on la.customer_id = c.customer_id
left join last_orders lo on lo.customer_id = c.customer_id
where true {lapse_filter}
"""


def resolve(conn, spec: AudienceSpec, as_of: date | None = None) -> Audience:
    today = as_of or data_as_of(conn)
    cohort_sql, params = build_cohort_sql(spec.cohort(), today)

    lapse = ""
    if spec.lapsed_min_days is not None:
        lapse += (" and (lo.last_order_on is null or lo.last_order_on <= "
                  "%(today)s::date - %(lapsed_min)s)")
        params["lapsed_min"] = spec.lapsed_min_days
    if spec.lapsed_max_days is not None:
        # A customer who has never ordered is not "lapsed by N days"; they are
        # unclassifiable on recency, so an upper bound excludes them.
        lapse += (" and lo.last_order_on is not null and lo.last_order_on >= "
                  "%(today)s::date - %(lapsed_max)s")
        params["lapsed_max"] = spec.lapsed_max_days
    params["today"] = today

    rows = conn.execute(
        RESOLVE_SQL.format(cohort=cohort_sql, lapse_filter=lapse), params
    ).fetchall()

    eligible: list[int] = []
    tiers: set[str] = set()
    no_consent = dormant = 0
    dormancy_cutoff = today.replace(
        year=today.year - DORMANCY_MONTHS_GB // 12)


    for customer_id, tier, opted_in, country, last_order in rows:
        if not opted_in:
            no_consent += 1
            continue
        if country == "GB" and (last_order is None or last_order < dormancy_cutoff):
            dormant += 1
            continue
        eligible.append(customer_id)
        tiers.add(tier or "NONE")

    return Audience(customer_ids=eligible, size=len(eligible),
                    excluded_no_consent=no_consent,
                    excluded_dormant_gb=dormant,
                    tiers_present=sorted(tiers),
                    description=spec.describe())
