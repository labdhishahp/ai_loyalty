"""The business rules, as constants. The single source of truth.

WHY THIS FILE EXISTS SEPARATELY. These numbers appear in three places: the
policy documents the agent reads and cites, the validator that enforces them,
and the tests. If the corpus said 15% while the validator enforced 12%, the
agent would confidently cite a rule the system does not apply -- which is worse
than having no policy at all, because it looks correct.

So the rules live here, the corpus generator imports them to write the prose,
and the policy engine imports them to enforce it. Changing a ceiling changes
both, in one edit, and they cannot drift.
"""

from __future__ import annotations

# Maximum percentage discount by tier. Where an audience spans several tiers the
# ceiling of the LOWEST tier applies -- the offer reaches everyone in it.
DISCOUNT_CEILING_PCT = {"BRONZE": 10, "SILVER": 12, "GOLD": 15, "PLATINUM": 20}
TIER_ORDER = ("BRONZE", "SILVER", "GOLD", "PLATINUM")

POINTS_MULTIPLIER_CEILING = 3.0

# Rolling seven-day cap across ALL programmes and channels.
CONTACT_CAP_PER_WEEK = 2
CONTACT_WINDOW_DAYS = 7

MIN_AUDIENCE = 50            # below this, results cannot be measured
MAX_AUDIENCE = 50_000
SENIOR_APPROVAL_ABOVE = 10_000

# A holdout is required once the audience is large enough for it to be
# meaningful; below that it would just shrink an already-small send.
HOLDOUT_REQUIRED_ABOVE = 200
MIN_HOLDOUT_PCT = 5

# GB treats consent as stale after this long without a purchase.
DORMANCY_MONTHS_GB = 24
