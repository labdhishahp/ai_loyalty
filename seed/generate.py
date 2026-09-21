"""Build the L-Mart dataset in memory.

This module holds the pipeline; it does not persist anything. Postgres is the
project's source of truth, so persistence lives in seed/load.py.

The CLI here is a smoke test: it builds the dataset and prints row counts
without needing database credentials. Useful for checking that a change to
seed/config.py still produces a sane dataset before spending a network round
trip on loading it.

Run:  python -m seed.generate
"""

from __future__ import annotations

import random
import time

from . import config, population, reference, simulate

# Column orders for the tables built here from dataclasses rather than tuples.
# The rest live next to the rows they describe, in seed/simulate.py.
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


def build(seed: int | None = None, quiet: bool = False):
    """Run the whole pipeline in memory and return (customers, reference, tables).

    Separated from main() so that seed/load.py can load it straight into Postgres
    and seed/ablate.py can rebuild it with one causal mechanism disabled.

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


def customer_rows(customers):
    return ((c.customer_id, c.external_ref, c.full_name, c.email, c.country_code,
             c.city, c.signed_up_on.isoformat(), c.marketing_opt_in)
            for c in customers)


def loyalty_account_rows(customers):
    return ((c.account_id, c.customer_id, c.enrolled_on.isoformat(),
             c.current_tier, "active")
            for c in customers if c.account_id is not None)


def dict_rows(records, columns):
    return ([record[column] for column in columns] for record in records)


def main() -> int:
    started = time.time()
    print(f"L-Mart seed  (RANDOM_SEED={config.RANDOM_SEED}, "
          f"{config.CUSTOMER_COUNT:,} customers, "
          f"{config.HISTORY_START} -> {config.HISTORY_END})")

    customers, ref, tables = build()

    counts = {
        "stores": len(ref.stores),
        "categories": len(ref.categories),
        "products": len(ref.products),
        "loyalty_tiers": len(ref.tiers),
        "customers": len(customers),
        "loyalty_accounts": sum(1 for c in customers if c.account_id is not None),
        "tier_history": len(tables.tier_history),
        "orders": len(tables.orders),
        "order_items": len(tables.order_items),
        "points_ledger": len(tables.points_ledger),
        "campaigns": len(tables.campaigns),
        "campaign_events": len(tables.campaign_events),
    }
    print()
    for name, count in counts.items():
        print(f"  {count:>9,}  {name}")
    print(f"\nBuilt in {time.time() - started:.1f}s (nothing written)")
    print("Next: python -m seed.load --reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
