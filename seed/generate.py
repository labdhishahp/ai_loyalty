"""Generate the L-Mart dataset as CSV files.

WHY CSV AS AN INTERMEDIATE rather than inserting straight into Postgres:

1. It lets `seed/verify.py` check the planted signals with no database and no
   credentials, so the generate -> verify loop is seconds long.
2. Verification against files is a genuine check. Verifying with SQL against the
   loaded schema would test the same joins the metrics layer will later use,
   which risks a tautology: a shared misunderstanding would pass both.
3. COPY from a file is the fastest way to load Postgres, and the files are
   inspectable when something looks wrong.

Run:  python -m seed.generate
"""

from __future__ import annotations

import csv
import random
import time

from . import config, population, reference, simulate

CUSTOMER_COLUMNS = (
    "customer_id", "external_ref", "full_name", "email", "country_code",
    "city", "signed_up_on", "marketing_opt_in",
)
LOYALTY_ACCOUNT_COLUMNS = (
    "account_id", "customer_id", "enrolled_on", "current_tier", "status",
)
TIER_COLUMNS = (
    "tier_code", "name", "rank", "annual_spend_threshold_minor",
    "points_earn_multiplier",
)
STORE_COLUMNS = ("store_id", "code", "name", "country_code", "city",
                 "opened_on", "closed_on")
CATEGORY_COLUMNS = ("category_id", "code", "name")
PRODUCT_COLUMNS = ("product_id", "sku", "name", "category_id", "list_price_minor")


def _write(name: str, columns: tuple, rows) -> int:
    path = config.OUT_DIR / f"{name}.csv"
    count = 0
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(row if isinstance(row, (tuple, list))
                            else [row[c] for c in columns])
            count += 1
    return count


def build(seed: int | None = None, quiet: bool = False):
    """Run the whole pipeline in memory and return (customers, reference, tables).

    Separated from main() so that seed/ablate.py can rebuild the dataset with one
    causal mechanism disabled, without writing CSVs.

    ONE generator is threaded through every stage. Using several independently
    seeded generators would make the output depend on the order stages run in,
    which is exactly the fragility a fixed seed exists to remove.
    """
    rng = random.Random(config.RANDOM_SEED if seed is None else seed)
    ref = reference.build(rng)
    customers = population.build(rng)
    promoted = population.select_tier_review_promotions(customers)
    if not quiet:
        print(f"  tier review: promoting {len(promoted)} GB Gold members to "
              f"Platinum on {config.TIER_REVIEW_DATE}")
        print("  simulating...")
    tables = simulate.run(customers, ref, promoted, rng)
    simulate.add_background_campaigns(tables, customers, rng)
    return customers, ref, tables


def main() -> int:
    started = time.time()
    config.OUT_DIR.mkdir(exist_ok=True)

    print(f"L-Mart seed  (RANDOM_SEED={config.RANDOM_SEED}, "
          f"{config.CUSTOMER_COUNT:,} customers, "
          f"{config.HISTORY_START} -> {config.HISTORY_END})")

    customers, ref, tables = build()

    counts = {}
    counts["stores"] = _write("stores", STORE_COLUMNS, ref.stores)
    counts["categories"] = _write("categories", CATEGORY_COLUMNS, ref.categories)
    counts["products"] = _write("products", PRODUCT_COLUMNS, ref.products)
    counts["loyalty_tiers"] = _write("loyalty_tiers", TIER_COLUMNS, ref.tiers)

    counts["customers"] = _write("customers", CUSTOMER_COLUMNS, (
        (c.customer_id, c.external_ref, c.full_name, c.email, c.country_code,
         c.city, c.signed_up_on.isoformat(), "true" if c.marketing_opt_in else "false")
        for c in customers
    ))
    counts["loyalty_accounts"] = _write("loyalty_accounts", LOYALTY_ACCOUNT_COLUMNS, (
        (c.account_id, c.customer_id, c.enrolled_on.isoformat(), c.current_tier, "active")
        for c in customers if c.account_id is not None
    ))

    counts["tier_history"] = _write(
        "tier_history", simulate.TIER_HISTORY_COLUMNS, tables.tier_history)
    counts["orders"] = _write("orders", simulate.ORDER_COLUMNS, tables.orders)
    counts["order_items"] = _write(
        "order_items", simulate.ORDER_ITEM_COLUMNS, tables.order_items)
    counts["points_ledger"] = _write(
        "points_ledger", simulate.POINTS_COLUMNS, tables.points_ledger)
    counts["campaigns"] = _write("campaigns", simulate.CAMPAIGN_COLUMNS, tables.campaigns)
    counts["campaign_events"] = _write(
        "campaign_events", simulate.CAMPAIGN_EVENT_COLUMNS, tables.campaign_events)

    print()
    for name, count in counts.items():
        print(f"  {count:>9,}  {name}")
    print(f"\nWrote {config.OUT_DIR} in {time.time() - started:.1f}s")
    print("Next: python -m seed.verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
