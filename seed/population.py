"""Customers, loyalty accounts, and each customer's latent behaviour parameters.

The important idea here is `base_rate`: an intrinsic daily order probability
drawn per customer and held fixed for life. It is the customer's propensity to
shop, independent of tier, season, or campaign. Every planted cause in
simulate.py is a *multiplier* on it.

That separation is what makes the dataset analysable. When Gold engagement falls,
it fell because a multiplier changed or because the *set of people called Gold*
changed -- and those are exactly the two explanations the agent has to tell apart.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config

FIRST_NAMES = [
    "Aisha", "Rohan", "Emily", "Wei", "Fatima", "James", "Priya", "Daniel",
    "Mei", "Omar", "Sophie", "Arjun", "Hannah", "Yusuf", "Chloe", "Ananya",
    "Thomas", "Layla", "Nikhil", "Grace", "Hassan", "Olivia", "Karan", "Zara",
    "Ethan", "Ishaan", "Amelia", "Noor", "Lucas", "Divya", "Jack", "Sana",
]
LAST_NAMES = [
    "Sharma", "Khan", "Walker", "Tan", "Patel", "Clarke", "Lim", "Ahmed",
    "Wright", "Nair", "Hughes", "Al-Farsi", "Bennett", "Reddy", "Foster", "Chen",
    "Morgan", "Iyer", "Baker", "Rahman", "Turner", "Menon", "Ellis", "Hassan",
]


@dataclass
class Customer:
    customer_id: int
    external_ref: str
    full_name: str
    email: str
    country_code: str
    city: str
    signed_up_on: date
    marketing_opt_in: bool

    # Loyalty. account_id is None for customers who never enrolled.
    account_id: int | None
    enrolled_on: date | None
    initial_tier: str | None

    # Latent behaviour
    base_rate: float
    # Frozen at enrolment and NEVER changed when the tier label changes.
    # This is what makes the February tier review a pure composition effect:
    # the set of people called Gold changes, but nobody shops differently.
    rate_multiplier: float
    category_weights: dict[str, float]

    # Mutable simulation state, reset per run by simulate.py
    current_tier: str | None = None
    last_order_on: date | None = None
    campaign_lift_until: date | None = None
    points_lots: list = field(default_factory=list)   # FIFO [expiry_date, remaining]


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    total = sum(weights.values())
    r = rng.random() * total
    upto = 0.0
    for key, weight in weights.items():
        upto += weight
        if r <= upto:
            return key
    return next(reversed(weights))


def build(rng: random.Random) -> list[Customer]:
    customers: list[Customer] = []
    account_id = 1
    # Most customers predate the window (see PRE_WINDOW_SIGNUP_SHARE); a minority
    # join during it, so tenure remains a legitimate alternative explanation the
    # agent may need to consider and rule out.

    for customer_id in range(1, config.CUSTOMER_COUNT + 1):
        country = _weighted_choice(rng, config.COUNTRIES)
        city = rng.choice(config.CITIES[country])
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)

        if rng.random() < config.PRE_WINDOW_SIGNUP_SHARE:
            signed_up_on = config.HISTORY_START - timedelta(days=rng.randrange(1, 900))
        else:
            signed_up_on = config.HISTORY_START + timedelta(
                days=rng.randrange((config.HISTORY_END - config.HISTORY_START).days))

        enrolled = rng.random() < config.LOYALTY_ENROLMENT_RATE
        tier = _weighted_choice(rng, config.TIER_MIX) if enrolled else None

        base_rate = rng.lognormvariate(config.BASE_RATE_MU, config.BASE_RATE_SIGMA)

        # Per-customer category affinity: start from the global weights, then
        # double one randomly chosen favourite. Without this every basket looks
        # the same and category-level analysis has nothing to find.
        weights = dict(config.CATEGORY_WEIGHTS)
        weights[rng.choice(config.CATEGORIES)] *= 2.0

        # C2 mechanism: GB Gold members over-index on Beauty. This is what makes
        # the GB Beauty stockout hit *this cohort specifically* rather than
        # depressing every cohort equally.
        if country == config.BEAUTY_STOCKOUT_COUNTRY and tier == "GOLD":
            weights["BEAUTY"] *= config.GOLD_GB_BEAUTY_AFFINITY

        customers.append(Customer(
            customer_id=customer_id,
            external_ref=f"LM-{customer_id:07d}",
            full_name=f"{first} {last}",
            email=f"{first.lower()}.{last.lower().replace(' ', '').replace('-', '')}{customer_id}@example.com",
            country_code=country,
            city=city,
            signed_up_on=signed_up_on,
            marketing_opt_in=rng.random() < config.MARKETING_OPT_IN_RATE,
            account_id=account_id if enrolled else None,
            # Enrolment is at or after signup, and never after the window ends.
            enrolled_on=min(
                signed_up_on + timedelta(days=rng.randrange(0, 200)),
                config.HISTORY_END,
            ) if enrolled else None,
            initial_tier=tier,
            base_rate=min(base_rate, config.MAX_DAILY_ORDER_PROB),
            rate_multiplier=(config.TIER_ORDER_RATE[tier] if tier else 0.75),
            category_weights=weights,
        ))
        if enrolled:
            account_id += 1

    return customers


def select_tier_review_promotions(customers: list[Customer]) -> set[int]:
    """C3: which GB Gold members get promoted to Platinum in the Feb 2026 review.

    Selected by intrinsic base_rate rather than by realised spend. Those are
    near-equivalent (spend is driven by base_rate) but base_rate is available
    before the simulation runs, which avoids a two-pass generate/measure/regenerate
    cycle for no analytical gain.

    Only accounts enrolled by the review date are eligible -- see below.

    Deterministic: sorted by base_rate then customer_id, so ties never depend on
    dict or set iteration order.
    """
    eligible = [
        c for c in customers
        if c.country_code == "GB" and c.initial_tier == "GOLD"
        and c.account_id is not None
        # A review cannot promote someone who has not joined yet. Without this,
        # an account enrolling in August 2026 was still promoted in the February
        # review, and simulate.py closed its enrolment span on the review date
        # -- producing a span that ends six months before it starts. Six such
        # rows existed; point-in-time queries simply never matched them, so the
        # members vanished from every cohort instead of erroring.
        and c.enrolled_on <= config.TIER_REVIEW_DATE
    ]
    eligible.sort(key=lambda c: (-c.base_rate, c.customer_id))
    n = int(len(eligible) * config.TIER_REVIEW_PROMOTE_FRACTION)
    return {c.customer_id for c in eligible[:n]}
