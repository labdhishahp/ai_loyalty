"""Per-actor rate limiting, counted in Postgres.

WHY POSTGRES AND NOT MEMORY. The usual limiter keeps a counter in the process.
That is wrong here for a specific reason: this API is written for Vercel, where
each request may land on a different instance and instances are created and
destroyed freely. A per-process counter would let a caller exceed the limit by
exactly the number of warm instances, and would reset to zero every cold start.
The limit has to live somewhere all instances share, and the only such place
this system already has is the database.

WHY NO COUNTER TABLE. A limiter normally needs its own store. This one does not,
because the two things worth limiting already leave a durable row behind with
the actor and the timestamp on it:

    run         ops.agent_runs      -- every row is a run that will spend money
    proposal    ops.campaign_proposals

So the limit is a count over rows that had to be written anyway. That removes a
table, removes the write on the hot path, and removes the classic limiter bug
where the counter and the thing it counts drift apart. It also means a run that
failed still counts against you, which is correct: it consumed the tokens.

WHAT IS LIMITED, AND WHAT IS NOT. Only run creation and proposal creation.
Those are the two actions that cost real money or change state. Reads are
cheap, authenticated, and bounded by the connection pooler, and limiting them
would require the counter table this design just avoided -- machinery bought
for a threat that does not exist at this size. Advancing a run is not limited
either, because it cannot run away: a run carries its own max_steps and
max_cost_usd, so the per-run budget already caps it. Limiting run creation
therefore caps total spend, which is the property actually wanted.

THE TRADEOFF, STATED PLAINLY. This is a fixed lookback window, not a sliding
one and not a token bucket. A caller can use the whole hour's allowance in a
few seconds, and the window is a trailing interval rather than a bucket that
resets on the hour, so there is no thundering-herd boundary. What it does not
do is smooth bursts. Smoothing would need a bucket with a refill rate and
somewhere to keep it -- again, the table this avoids. The purpose here is to
bound cost and blast radius, not to shape traffic.

RACES. Two simultaneous requests can both read a count below the limit and both
proceed, so the effective ceiling is limit + concurrency, not limit. Closing
that needs a lock or a serializable transaction on every request. For a limiter
whose job is "stop a loop from spending a thousand dollars overnight", being
off by the number of concurrent requests is not worth a lock on the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import config
from core.errors import ActionableError


class RateLimited(ActionableError):
    """The caller has exceeded their allowance for this action.

    ActionableError because the message is written for whoever hit it: it says
    what the limit is and when to try again, and contains no internal detail.
    """

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Limit:
    """One limited action, and where its evidence already lives."""
    table: str          # trusted: named here, never taken from a caller
    actor_column: str
    default_per_hour: int
    env_var: str
    noun: str


LIMITS: dict[str, Limit] = {
    "run": Limit(
        table="ops.agent_runs", actor_column="actor",
        # Twenty runs an hour, each capped at its own step and cost budget, is
        # a ceiling of a few dollars an hour per actor. High enough that no one
        # doing real work will meet it; low enough that a client stuck in a
        # retry loop is a nuisance rather than an invoice.
        default_per_hour=20, env_var="RATE_LIMIT_RUNS_PER_HOUR",
        noun="investigation"),
    "proposal": Limit(
        table="ops.campaign_proposals", actor_column="created_by",
        # Lower, because each proposal resolves an audience and runs policy
        # validation, and because a human has to read every one of these. Ten
        # an hour is already more than an approver can meaningfully review.
        default_per_hour=10, env_var="RATE_LIMIT_PROPOSALS_PER_HOUR",
        noun="campaign proposal"),
}

WINDOW_SECONDS = 3600


def limit_for(action: str) -> int:
    """The configured ceiling. Zero or less disables the limit.

    Read per call rather than at import, so that a deployment can change it
    without a redeploy and so that tests can set it without reloading modules.
    """
    spec = LIMITS[action]
    return config.get_int(spec.env_var, spec.default_per_hour)


def count_recent(conn, action: str, actor: str) -> int:
    """How many of `action` this actor has taken inside the window."""
    spec = LIMITS[action]
    # The table and column come from LIMITS above, never from a caller, so the
    # interpolation is of literals this module owns. The actor is bound.
    with conn.cursor() as cur:
        cur.execute(
            f"select count(*) from {spec.table} "
            f"where {spec.actor_column} = %s "
            f"  and created_at > now() - interval '{WINDOW_SECONDS} seconds'",
            (actor,))
        return int(cur.fetchone()[0])


def check(conn, action: str, actor: str) -> None:
    """Raise RateLimited if `actor` has no allowance left for `action`.

    Called before doing the work, not after, so a refused request costs one
    indexed count and nothing else.
    """
    allowed = limit_for(action)
    if allowed <= 0:
        return
    used = count_recent(conn, action, actor)
    if used < allowed:
        return
    spec = LIMITS[action]
    raise RateLimited(
        f"Rate limit reached: {allowed} {spec.noun}s per hour. "
        f"You have started {used} in the last hour. Try again later.",
        retry_after=WINDOW_SECONDS)
