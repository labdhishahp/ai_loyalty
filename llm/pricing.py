"""Cost estimation, where a price is actually known.

Returns None rather than zero for a self-hosted or unpriced gateway. Zero would
read as "this run was free", and a budget silently never binding is worse than
having no budget at all -- so the token ceiling, not cost, is the primary control.
"""

from __future__ import annotations

from .base import Usage

# USD per million tokens, (input, output).
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_usd(model: str, usage: Usage) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1e6
