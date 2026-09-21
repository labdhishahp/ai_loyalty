"""Reference data: stores, categories, products, loyalty tiers.

Hand-shaped rather than randomly generated. These tables are small, they are the
vocabulary every question is phrased in, and readable names ("Beauty", "Gold")
make generated data inspectable by eye. Randomising them would buy nothing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class Reference:
    tiers: list[dict]
    stores: list[dict]
    categories: list[dict]
    products: list[dict]
    category_id_by_code: dict[str, int]
    products_by_category: dict[str, list[dict]]
    stocked_out_product_ids: set[int]
    stores_by_country: dict[str, list[dict]]


TIERS = [
    # tier_code, name, rank, annual spend threshold (pence), points multiplier
    ("BRONZE", "Bronze", 1, 0, 1.00),
    ("SILVER", "Silver", 2, 50_000, 1.00),
    ("GOLD", "Gold", 3, 150_000, 1.50),
    ("PLATINUM", "Platinum", 4, 400_000, 2.00),
]

# (country, city, count) -- roughly proportional to customer distribution.
STORE_PLAN = [
    ("GB", "London", 6), ("GB", "Manchester", 3), ("GB", "Birmingham", 2),
    ("GB", "Leeds", 2), ("GB", "Glasgow", 2),
    ("IN", "Mumbai", 4), ("IN", "Bengaluru", 3), ("IN", "Delhi", 3),
    ("IN", "Hyderabad", 2), ("IN", "Pune", 2),
    ("AE", "Dubai", 3), ("AE", "Abu Dhabi", 2), ("AE", "Sharjah", 1),
    ("SG", "Singapore", 4),
]

CATEGORY_NAMES = {
    "GROCERY": "Grocery & Fresh",
    "BEAUTY": "Beauty & Personal Care",
    "ELECTRONICS": "Electronics",
    "APPAREL": "Apparel & Footwear",
    "HOME": "Home & Living",
    "BABY": "Baby & Kids",
}

# Products per category, and the price band (in pence) they are drawn from.
# Bands differ so that basket value varies realistically by what was bought.
CATEGORY_PRODUCT_PLAN = {
    "GROCERY": (60, 120, 1_200),
    "BEAUTY": (48, 450, 4_500),
    "ELECTRONICS": (30, 2_500, 60_000),
    "APPAREL": (50, 900, 9_000),
    "HOME": (40, 600, 12_000),
    "BABY": (25, 300, 4_000),
}

PRODUCT_WORDS = {
    "GROCERY": ["Oat Milk", "Sourdough Loaf", "Free Range Eggs", "Basmati Rice",
                "Olive Oil", "Greek Yoghurt", "Ground Coffee", "Chicken Breast"],
    "BEAUTY": ["Hydrating Serum", "Vitamin C Cream", "Shampoo", "Body Lotion",
               "Lip Balm", "Face Cleanser", "Sunscreen SPF50", "Hair Oil"],
    "ELECTRONICS": ["Wireless Earbuds", "Smart Speaker", "USB-C Charger", "Tablet",
                    "Bluetooth Keyboard", "Action Camera", "Power Bank", "Smart Bulb"],
    "APPAREL": ["Cotton T-Shirt", "Denim Jeans", "Running Shoes", "Wool Jumper",
                "Rain Jacket", "Chino Trousers", "Ankle Boots", "Linen Shirt"],
    "HOME": ["Cotton Bedding Set", "Ceramic Mug", "Table Lamp", "Cast Iron Pan",
             "Storage Basket", "Wall Clock", "Cushion Cover", "Cutlery Set"],
    "BABY": ["Nappies Pack", "Baby Wipes", "Cotton Babygrow", "Feeding Bottle",
             "Soft Toy", "Pram Blanket", "Baby Shampoo", "Teething Ring"],
}

VARIANTS = ["Classic", "Everyday", "Premium", "Essential", "Signature", "Compact",
            "Value", "Deluxe", "Original", "Pro"]


def build(rng: random.Random) -> Reference:
    tiers = [
        {
            "tier_code": code, "name": name, "rank": rank,
            "annual_spend_threshold_minor": threshold,
            "points_earn_multiplier": f"{mult:.2f}",
        }
        for code, name, rank, threshold, mult in TIERS
    ]

    stores: list[dict] = []
    store_id = 1
    for country, city, count in STORE_PLAN:
        for n in range(count):
            stores.append({
                "store_id": store_id,
                "code": f"{country}-{city[:3].upper()}-{n + 1:02d}",
                "name": f"L-Mart {city} {n + 1}",
                "country_code": country,
                "city": city,
                # All stores predate the dataset, so store openings never confound
                # a trend. Store *closure* is left available as a hypothesis the
                # agent can check and correctly rule out (there are none).
                "opened_on": "2019-03-01",
                "closed_on": None,
            })
            store_id += 1

    categories: list[dict] = []
    category_id_by_code: dict[str, int] = {}
    for i, code in enumerate(config.CATEGORIES, start=1):
        categories.append({"category_id": i, "code": code, "name": CATEGORY_NAMES[code]})
        category_id_by_code[code] = i

    products: list[dict] = []
    products_by_category: dict[str, list[dict]] = {c: [] for c in config.CATEGORIES}
    product_id = 1
    for code in config.CATEGORIES:
        count, low, high = CATEGORY_PRODUCT_PLAN[code]
        for n in range(count):
            word = PRODUCT_WORDS[code][n % len(PRODUCT_WORDS[code])]
            variant = VARIANTS[(n // len(PRODUCT_WORDS[code])) % len(VARIANTS)]
            # Log-uniform price draw: most items cheap, a few expensive, which is
            # what real catalogues look like and what makes basket value skew.
            price = int(round(low * (high / low) ** rng.random()))
            product = {
                "product_id": product_id,
                "sku": f"{code[:3]}-{n + 1:04d}",
                "name": f"{variant} {word}",
                "category_id": category_id_by_code[code],
                "list_price_minor": price,
            }
            products.append(product)
            products_by_category[code].append(product)
            product_id += 1

    # C2: the specific Beauty SKUs that go out of stock in GB from April 2026.
    # Chosen once, deterministically, so the same SKUs are affected every run.
    beauty = products_by_category["BEAUTY"]
    n_out = int(len(beauty) * config.BEAUTY_STOCKOUT_SKU_FRACTION)
    stocked_out = {p["product_id"] for p in rng.sample(beauty, n_out)}

    stores_by_country: dict[str, list[dict]] = {}
    for store in stores:
        stores_by_country.setdefault(store["country_code"], []).append(store)

    return Reference(
        tiers=tiers,
        stores=stores,
        categories=categories,
        products=products,
        category_id_by_code=category_id_by_code,
        products_by_category=products_by_category,
        stocked_out_product_ids=stocked_out,
        stores_by_country=stores_by_country,
    )
