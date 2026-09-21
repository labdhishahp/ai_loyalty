"""The chronological simulation: one pass over every day in the history window.

WHY A DAY-BY-DAY LOOP rather than generating each table independently:
the planted causes are stateful and causally ordered. A campaign send targets
members who have *not ordered in 30 days*, which depends on orders already
generated. Campaign lift then changes the orders that follow. Points expire 18
months after they were earned, and a large expiry changes later behaviour. Tier
promotion in Feb 2026 changes the tier multiplier from that day onward.

Generating tables independently would require circular passes and would make it
very easy to produce data whose parts quietly contradict each other. A single
forward pass with per-customer state is both simpler to reason about and
guarantees internal consistency: every row is produced from the state that
actually existed at that moment.

Everything the agent is meant to discover is a MULTIPLIER on a customer's
intrinsic base_rate, applied here. Nothing else changes behaviour.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config
from .population import Customer
from .reference import Reference

# Column orders. Defined once so the CSV header, the row tuples, and the
# Postgres COPY column list can never drift apart.
ORDER_COLUMNS = (
    "order_id", "customer_id", "store_id", "channel", "ordered_at",
    "gross_amount_minor", "discount_minor", "net_amount_minor",
)
ORDER_ITEM_COLUMNS = (
    "order_item_id", "order_id", "product_id", "quantity",
    "unit_price_minor", "line_amount_minor",
)
POINTS_COLUMNS = (
    "entry_id", "account_id", "occurred_at", "entry_type", "points",
    "order_id", "reason",
)
CAMPAIGN_COLUMNS = (
    "campaign_id", "code", "name", "programme", "objective", "channel",
    "target_description", "status", "sent_on",
)
CAMPAIGN_EVENT_COLUMNS = (
    "event_id", "campaign_id", "customer_id", "event_type", "occurred_at", "order_id",
)
TIER_HISTORY_COLUMNS = (
    "tier_history_id", "account_id", "tier_code", "effective_from",
    "effective_to", "reason",
)

BASKET_SIZE_WEIGHTS = {1: 0.35, 2: 0.28, 3: 0.18, 4: 0.12, 5: 0.07}

# Behaviour multipliers for customers who are not (yet) loyalty members.
NON_MEMBER_RATE = 0.75
PRE_ENROLMENT_RATE = 0.85

# Annual real price drift, applied backwards from the end of the window. This is
# why order_items stores the price charged rather than joining to products.
ANNUAL_PRICE_DRIFT = 0.03


@dataclass
class Tables:
    orders: list[tuple] = field(default_factory=list)
    order_items: list[tuple] = field(default_factory=list)
    points_ledger: list[tuple] = field(default_factory=list)
    campaigns: list[tuple] = field(default_factory=list)
    campaign_events: list[tuple] = field(default_factory=list)
    tier_history: list[tuple] = field(default_factory=list)


def _weighted_choice(rng: random.Random, weights: dict) -> object:
    total = sum(weights.values())
    r = rng.random() * total
    upto = 0.0
    for key, weight in weights.items():
        upto += weight
        if r <= upto:
            return key
    return next(reversed(weights))


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp for short months; points expiry on the 31st of a 30-day month is not
    # a business rule worth modelling precisely.
    day = min(d.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30,
                      31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _channel_mix_for(day: date) -> dict[str, float]:
    """R2 (red herring): a global app relaunch moves share from store to app.

    Applies to every country and tier equally -- which is precisely why it cannot
    explain a GB-Gold-specific decline. An agent must compare cohorts to rule it
    out, not merely observe that the timing lines up.
    """
    mix = dict(config.CHANNEL_MIX_BASE)
    if day < config.APP_SHIFT_START:
        return mix
    span = (config.APP_SHIFT_END - config.APP_SHIFT_START).days
    progress = min(1.0, (day - config.APP_SHIFT_START).days / span) if span else 1.0
    moved = config.APP_SHIFT_MAGNITUDE * progress
    mix["store"] -= moved
    mix["app"] += moved
    return mix


def run(customers: list[Customer], ref: Reference,
        promoted_ids: set[int], rng: random.Random) -> Tables:
    t = Tables()

    accounts = [c for c in customers if c.account_id is not None]
    price_end = config.HISTORY_END

    # --- initial tier history -------------------------------------------------
    # One open-ended row per member at enrolment. Promotions close it later.
    tier_history_id = 1
    open_tier_row: dict[int, int] = {}      # account_id -> index into t.tier_history
    for c in accounts:
        c.current_tier = c.initial_tier
        t.tier_history.append((
            tier_history_id, c.account_id, c.initial_tier,
            c.enrolled_on.isoformat(), None, "enrolment",
        ))
        open_tier_row[c.account_id] = len(t.tier_history) - 1
        tier_history_id += 1

    order_id = 1
    order_item_id = 1
    entry_id = 1
    campaign_id = 1
    event_id = 1

    # Campaign sends the customer has received but not yet converted on:
    # customer_id -> (campaign_id, expiry_date)
    pending_conversion: dict[int, tuple[int, date]] = {}

    day = config.HISTORY_START
    total_months = _months_between(config.HISTORY_START, config.HISTORY_END)

    while day <= config.HISTORY_END:
        # ------------------------------------------------------------------
        # C3: the February 2026 tier review.
        # The highest-propensity GB Gold members move to Platinum. Nobody's
        # behaviour changes -- but the *set of people called Gold* does, and
        # Gold's averages fall as a result. This is a composition effect, and
        # it is only detectable through tier_history.
        # ------------------------------------------------------------------
        if day == config.TIER_REVIEW_DATE:
            for c in accounts:
                if c.customer_id in promoted_ids:
                    idx = open_tier_row[c.account_id]
                    row = t.tier_history[idx]
                    t.tier_history[idx] = row[:4] + (day.isoformat(),) + row[5:]
                    t.tier_history.append((
                        tier_history_id, c.account_id, "PLATINUM",
                        day.isoformat(), None, "upgrade",
                    ))
                    open_tier_row[c.account_id] = len(t.tier_history) - 1
                    tier_history_id += 1
                    c.current_tier = "PLATINUM"

        # ------------------------------------------------------------------
        # C4: points expiry, processed at the start of each month.
        # Points expire 18 months after being earned, consumed FIFO by
        # redemptions. Losing a balance you were saving toward something is a
        # real reason to disengage, so a large expiry applies a penalty.
        # ------------------------------------------------------------------
        # The retroactive half of the policy change: outstanding balances are
        # re-dated to the shorter validity, so anything already past 12 months
        # expires in a single sweep on this date. That one-off spike is the
        # discoverable event -- a gradual rule produces only a trend.
        if day == config.POINTS_POLICY_CHANGE_DATE:
            for c in accounts:
                for lot in c.points_lots:
                    lot[1] = _add_months(lot[0], config.POINTS_EXPIRY_MONTHS_AFTER)

        if day.day == 1 or day == config.POINTS_POLICY_CHANGE_DATE:
            policy_sweep = day == config.POINTS_POLICY_CHANGE_DATE
            for c in accounts:
                if not c.points_lots:
                    continue
                expired = 0
                remaining_lots = []
                for lot in c.points_lots:
                    if lot[1] <= day and lot[2] > 0:
                        expired += lot[2]
                    elif lot[2] > 0:
                        remaining_lots.append(lot)
                c.points_lots = remaining_lots
                if expired > 0:
                    t.points_ledger.append((
                        entry_id, c.account_id, f"{day.isoformat()} 00:05:00+00",
                        "expire", -expired, None,
                        "Expired under the 2026 points validity policy change"
                        if policy_sweep
                        else f"Points expired after "
                             f"{config.POINTS_EXPIRY_MONTHS_AFTER} months",
                    ))
                    entry_id += 1

        # ------------------------------------------------------------------
        # C1: the UK Gold Reactivation programme.
        # A monthly email to lapsed GB Gold members. It ran for 21 months and
        # then stopped. Because each wave is its own campaign row, the stoppage
        # is visible as an ABSENCE of rows after 2026-05-05 -- there is nothing
        # to notice unless you look for what should be there and isn't.
        # ------------------------------------------------------------------
        if (day.day == config.CAMPAIGN_SEND_DAY
                and config.CAMPAIGN_FIRST_SEND <= day <= config.CAMPAIGN_LAST_SEND):
            audience = [
                c for c in accounts
                if c.country_code == "GB"
                and c.current_tier == "GOLD"
                and c.marketing_opt_in
                and (c.last_order_on is None
                     or (day - c.last_order_on).days >= config.CAMPAIGN_LAPSED_DAYS)
            ]
            if audience:
                t.campaigns.append((
                    campaign_id,
                    f"UKGOLD-REACT-{day:%Y%m}",
                    f"UK Gold Reactivation - {day:%B %Y}",
                    config.CAMPAIGN_PROGRAMME,
                    "reactivation",
                    "email",
                    f"GB customers currently in the Gold tier with no order in the "
                    f"last {config.CAMPAIGN_LAPSED_DAYS} days and marketing consent",
                    "completed",
                    day.isoformat(),
                ))
                sent_at = f"{day.isoformat()} 09:00:00+00"
                for c in audience:
                    t.campaign_events.append(
                        (event_id, campaign_id, c.customer_id, "sent", sent_at, None))
                    event_id += 1
                    if rng.random() >= config.CAMPAIGN_DELIVERY_RATE:
                        continue
                    t.campaign_events.append(
                        (event_id, campaign_id, c.customer_id, "delivered", sent_at, None))
                    event_id += 1

                    # Lift is applied to everyone who received the email, whether
                    # or not they opened it -- the open event is a measurement,
                    # not the mechanism.
                    c.campaign_lift_until = day + timedelta(
                        days=config.CAMPAIGN_LIFT_WINDOW_DAYS)
                    pending_conversion[c.customer_id] = (
                        campaign_id, day + timedelta(days=config.CAMPAIGN_LIFT_WINDOW_DAYS))

                    if rng.random() < config.CAMPAIGN_OPEN_RATE:
                        opened_at = f"{day.isoformat()} {rng.randrange(9, 22):02d}:00:00+00"
                        t.campaign_events.append(
                            (event_id, campaign_id, c.customer_id, "opened", opened_at, None))
                        event_id += 1
                        if rng.random() < config.CAMPAIGN_CLICK_RATE:
                            t.campaign_events.append(
                                (event_id, campaign_id, c.customer_id, "clicked", opened_at, None))
                            event_id += 1
                    if rng.random() < config.CAMPAIGN_UNSUB_RATE:
                        t.campaign_events.append(
                            (event_id, campaign_id, c.customer_id, "unsubscribed", sent_at, None))
                        event_id += 1
                        c.marketing_opt_in = False
                campaign_id += 1

        # ------------------------------------------------------------------
        # Daily demand
        # ------------------------------------------------------------------
        season = config.SEASONALITY[day.month]
        months_elapsed = _months_between(config.HISTORY_START, day)
        # R1 (masking): Silver and Platinum grow steadily, so GB total revenue is
        # UP over the focus period even as GB Gold falls. An agent that checks
        # only country-level totals will conclude, wrongly, that nothing is wrong.
        non_gold_growth = (1 + config.NON_GOLD_GROWTH_PER_MONTH) ** months_elapsed
        channel_mix = _channel_mix_for(day)
        years_before_end = (price_end - day).days / 365.25
        price_factor = (1 - ANNUAL_PRICE_DRIFT) ** years_before_end
        gb_stockout_active = day >= config.BEAUTY_STOCKOUT_FROM

        for c in customers:
            if c.account_id is None:
                tier_mult = NON_MEMBER_RATE
            elif c.enrolled_on and day < c.enrolled_on:
                tier_mult = PRE_ENROLMENT_RATE
            else:
                # c.rate_multiplier, NOT config.TIER_ORDER_RATE[c.current_tier].
                # Promotion must not make anyone shop more, or the February tier
                # review would be a behaviour change rather than a pure
                # composition effect -- and the cohort analysis it exists to
                # teach would have nothing to reveal.
                tier_mult = c.rate_multiplier

            p = c.base_rate * tier_mult * season

            # Keyed on the INITIAL tier for the same reason: the growth trend
            # belongs to a customer segment, not to a label that moves.
            if c.initial_tier in ("SILVER", "PLATINUM") or c.account_id is None:
                p *= non_gold_growth
            if c.campaign_lift_until and day <= c.campaign_lift_until:
                p *= config.CAMPAIGN_LIFT

            if rng.random() >= min(p, config.MAX_DAILY_ORDER_PROB):
                continue

            # ----- basket ---------------------------------------------------
            channel = _weighted_choice(rng, channel_mix)
            store_id = None
            if channel == "store":
                store_id = rng.choice(ref.stores_by_country[c.country_code])["store_id"]

            n_lines = _weighted_choice(rng, BASKET_SIZE_WEIGHTS)
            lines: list[tuple] = []
            gross = 0
            for _ in range(n_lines):
                category = _weighted_choice(rng, c.category_weights)
                product = rng.choice(ref.products_by_category[category])
                # C2: the GB Beauty stockout. The line is simply not bought.
                # Baskets shrink, and a basket that empties entirely becomes a
                # trip that never happened -- so this reduces both average
                # basket value AND purchase frequency, which is what a real
                # supply failure does.
                if (gb_stockout_active
                        and c.country_code == config.BEAUTY_STOCKOUT_COUNTRY
                        and product["product_id"] in ref.stocked_out_product_ids):
                    continue
                quantity = 1 if rng.random() < 0.78 else rng.randrange(2, 4)
                unit_price = max(1, int(round(product["list_price_minor"] * price_factor)))
                line_amount = unit_price * quantity
                lines.append((order_item_id, order_id, product["product_id"],
                              quantity, unit_price, line_amount))
                order_item_id += 1
                gross += line_amount

            if not lines:
                continue

            # ----- redemption ----------------------------------------------
            discount = 0
            redeemed = 0
            balance = sum(lot[2] for lot in c.points_lots)
            if (c.account_id is not None
                    and balance >= config.REDEMPTION_BLOCK_POINTS
                    and gross > config.REDEMPTION_BLOCK_VALUE_MINOR
                    and rng.random() < config.REDEMPTION_PROBABILITY):
                redeemed = config.REDEMPTION_BLOCK_POINTS
                discount = config.REDEMPTION_BLOCK_VALUE_MINOR

            net = gross - discount
            ordered_at = f"{day.isoformat()} {rng.randrange(7, 23):02d}:{rng.randrange(0, 60):02d}:00+00"
            t.orders.append((order_id, c.customer_id, store_id, channel, ordered_at,
                             gross, discount, net))
            t.order_items.extend(lines)

            # ----- points ---------------------------------------------------
            if c.account_id is not None and c.enrolled_on and day >= c.enrolled_on:
                if redeemed:
                    # FIFO: oldest points are spent first, which is what makes
                    # the expiry wave (C4) land on people who were *saving*.
                    to_spend = redeemed
                    for lot in c.points_lots:
                        if to_spend <= 0:
                            break
                        take = min(lot[2], to_spend)
                        lot[2] -= take
                        to_spend -= take
                    c.points_lots = [l for l in c.points_lots if l[2] > 0]
                    t.points_ledger.append((
                        entry_id, c.account_id, ordered_at, "redeem", -redeemed,
                        order_id, "Redeemed against order",
                    ))
                    entry_id += 1

                multiplier = float(
                    next(x["points_earn_multiplier"] for x in ref.tiers
                         if x["tier_code"] == c.current_tier))
                earned = int((net // config.POINTS_PER_MINOR) * multiplier)
                if earned > 0:
                    t.points_ledger.append((
                        entry_id, c.account_id, ordered_at, "earn", earned,
                        order_id, "Earned on order",
                    ))
                    entry_id += 1
                    validity = (config.POINTS_EXPIRY_MONTHS_AFTER
                                if day >= config.POINTS_POLICY_CHANGE_DATE
                                else config.POINTS_EXPIRY_MONTHS)
                    # [earn_date, expiry_date, remaining] -- the earn date is
                    # retained so the retroactive policy change can re-date the lot.
                    c.points_lots.append([day, _add_months(day, validity), earned])

            # ----- campaign conversion --------------------------------------
            pending = pending_conversion.get(c.customer_id)
            if pending and day <= pending[1]:
                t.campaign_events.append(
                    (event_id, pending[0], c.customer_id, "converted", ordered_at, order_id))
                event_id += 1
                del pending_conversion[c.customer_id]

            c.last_order_on = day
            order_id += 1

        day += timedelta(days=1)

    # Any campaign whose programme is still sending at the end of the window
    # would be 'active'; this one stopped, so every wave is 'completed'.
    return t


def add_background_campaigns(t: Tables, customers: list[Customer],
                             rng: random.Random) -> None:
    """The Global Rewards Digest: a monthly batch email that never stopped.

    Generated as a second pass rather than inside the day loop because it has NO
    behavioural lift -- it does not change what orders exist, so it does not need
    to participate in the forward simulation. Its 'converted' events are orders
    that happened within the attribution window anyway.

    That is deliberate. It gives the dataset a campaign with a healthy-looking
    conversion rate and zero incremental effect, next to a campaign with real
    lift that was switched off. Telling those two apart is the job.
    """
    orders_by_customer: dict[int, list[tuple[date, int]]] = {}
    for row in t.orders:
        order_id, customer_id = row[0], row[1]
        ordered_on = date.fromisoformat(row[4][:10])
        orders_by_customer.setdefault(customer_id, []).append((ordered_on, order_id))
    for entries in orders_by_customer.values():
        entries.sort()

    members = [c for c in customers if c.account_id is not None]
    campaign_id = max((row[0] for row in t.campaigns), default=0) + 1
    event_id = max((row[0] for row in t.campaign_events), default=0) + 1

    day = date(config.HISTORY_START.year, config.HISTORY_START.month,
               config.DIGEST_SEND_DAY)
    while day <= config.HISTORY_END:
        if day < config.HISTORY_START:
            day = _add_months(day, 1)
            continue

        audience = [
            c for c in members
            if c.marketing_opt_in and c.enrolled_on and c.enrolled_on <= day
        ]
        if not audience:
            day = _add_months(day, 1)
            continue

        t.campaigns.append((
            campaign_id,
            f"DIGEST-{day:%Y%m}",
            f"Global Rewards Digest - {day:%B %Y}",
            config.DIGEST_PROGRAMME,
            "engagement",
            "email",
            "All loyalty members with marketing consent, all markets",
            "completed",
            day.isoformat(),
        ))
        sent_at = f"{day.isoformat()} 10:00:00+00"
        window_end = day + timedelta(days=config.DIGEST_ATTRIBUTION_WINDOW_DAYS)

        for c in audience:
            t.campaign_events.append(
                (event_id, campaign_id, c.customer_id, "sent", sent_at, None))
            event_id += 1
            if rng.random() >= config.DIGEST_DELIVERY_RATE:
                continue
            t.campaign_events.append(
                (event_id, campaign_id, c.customer_id, "delivered", sent_at, None))
            event_id += 1
            if rng.random() < config.DIGEST_OPEN_RATE:
                t.campaign_events.append(
                    (event_id, campaign_id, c.customer_id, "opened", sent_at, None))
                event_id += 1
                if rng.random() < config.DIGEST_CLICK_RATE:
                    t.campaign_events.append(
                        (event_id, campaign_id, c.customer_id, "clicked", sent_at, None))
                    event_id += 1

            for ordered_on, order_id in orders_by_customer.get(c.customer_id, ()):
                if day <= ordered_on <= window_end:
                    t.campaign_events.append((
                        event_id, campaign_id, c.customer_id, "converted",
                        f"{ordered_on.isoformat()} 12:00:00+00", order_id))
                    event_id += 1
                    break

        campaign_id += 1
        day = _add_months(day, 1)
