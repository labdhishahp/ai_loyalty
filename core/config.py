"""Reading configuration from the environment, consistently.

WHY THIS EXISTS RATHER THAN os.environ EVERYWHERE.

A .env file with `COE_API_KEY=` (blank, waiting to be filled in) sets the
variable to the empty string, not to nothing. `os.environ.get("X", default)`
then returns "" and the default never applies. That failure is quiet and
confusing: a setting appears configured, behaves as unset, and the error
surfaces somewhere unrelated.

So: an empty value means UNSET, everywhere, once.

Also central because the project has several packages -- seed, metrics, tools,
agent, api -- that all read the same variables. Having each parse them its own
way is how DATABASE_URL ends up meaning two different things.
"""

from __future__ import annotations

import os
import pathlib

from dotenv import load_dotenv

# Anchored to the repo root rather than the working directory. dotenv's search
# walks up from the *caller's* frame, which fails outright when code is run from
# stdin and silently finds the wrong file when run from a subdirectory.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


def get(name: str, default: str | None = None) -> str | None:
    """Read a setting. Blank and whitespace-only are treated as unset."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def require(name: str, hint: str = "") -> str:
    value = get(name)
    if value is None:
        suffix = f" {hint}" if hint else ""
        raise ConfigError(
            f"{name} is not set. Add it to .env -- see .env.example for what it "
            f"is and where to find the value.{suffix}")
    return value


def get_int(name: str, default: int) -> int:
    value = get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from None


def get_float(name: str, default: float) -> float:
    value = get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}") from None


def get_bool(name: str, default: bool) -> bool:
    value = get(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# Two URLs, deliberately. Migrations, the COPY load and the test suite need a
# real session on the direct connection. The API runs on serverless functions
# that scale to many short-lived instances against a 60-connection limit, so it
# must go through the transaction pooler. Using the wrong one is the kind of
# mistake that works locally and fails under load, so they are named separately
# rather than being one setting with a comment.

def database_url() -> str:
    """Direct connection (port 5432). Sessions, COPY, migrations, tests."""
    return require("DATABASE_URL")


def is_serverless() -> bool:
    """True when running on Vercel. Both variables are set by the platform.

    Read through this rather than checked inline, because several rules differ
    between a laptop and a deployment and they should all agree on what a
    deployment is.
    """
    return bool(get("VERCEL") or get("VERCEL_ENV"))


def pool_url() -> str:
    """Transaction pooler (port 6543). Everything serving HTTP requests.

    Falls back to the direct connection on a laptop, so local development works
    before the pooler URL is filled in.

    ON A DEPLOYMENT THAT FALLBACK IS REFUSED. It used to apply everywhere, with
    a comment saying a real deployment would set the pooler URL -- which is a
    hope, not a mechanism. The failure it allowed is the worst shape available:
    direct connections work perfectly for one developer clicking around, and
    exhaust the 60-connection limit only once real traffic scales the function
    out. That passes every smoke test and fails in production.
    """
    pooled = get("DATABASE_POOL_URL")
    if pooled:
        return pooled
    if is_serverless():
        raise ConfigError(
            "DATABASE_POOL_URL is not set. A deployed function must use the "
            "transaction pooler (port 6543): serverless scales to many "
            "instances and direct connections would exhaust the database's "
            "60-connection limit under load. Copy the pooler URL from "
            "Supabase -> Project Settings -> Database -> Connection pooling.")
    return database_url()


def app_api_key() -> str | None:
    """Shared secret for the API.

    Absent is tolerated on a laptop. On a deployment the API refuses every
    request rather than serving one unauthenticated -- see require_principal in
    api/app.py, which is where the refusal belongs: /api/health stays reachable
    so a misconfigured deployment can still be diagnosed.
    """
    return get("APP_API_KEY")
