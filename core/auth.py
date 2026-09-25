"""Who is calling, and what are they allowed to do.

TWO KINDS OF CALLER, deliberately different:

  A PERSON presents a Supabase access token. Their identity is whatever Supabase
  says it is; their AUTHORITY comes from ops.user_roles, which this project owns.
  Identity and authority are separate on purpose -- being logged in is not the
  same as being allowed to spend money.

  A SERVICE presents the shared APP_API_KEY. It is how CI and the MCP server
  call the API, and it is deliberately weaker than any person: it may read and
  it may draft a proposal, but it may not approve or execute one. A campaign
  that reaches real customers requires a named human, and a shared secret names
  nobody.

TOKENS ARE VERIFIED, NOT DECODED. The project's Supabase instance signs with
ES256 and publishes the public key at a JWKS endpoint, so signature, expiry,
audience and issuer are all checked against the published key. Reading the
claims without verifying the signature would accept a token anyone could forge
in a text editor -- which is the single most common authentication mistake, and
the reason this uses a library rather than parsing JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from . import config
from .errors import ActionableError

# Capabilities, kept coarse. A scope should answer a question a reviewer would
# actually ask -- "can this caller spend money?" -- not mirror the route table.
READ = "read"          # investigate: every read tool
PROPOSE = "propose"    # draft a campaign; changes nothing customers can see
APPROVE = "approve"    # sign off a proposal
EXECUTE = "execute"    # send it

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "analyst": frozenset({READ, PROPOSE}),
    "approver": frozenset({READ, PROPOSE, APPROVE, EXECUTE}),
    # Weaker than any human, by design: a shared secret names nobody, so it may
    # not authorise anything that reaches a customer.
    "service": frozenset({READ, PROPOSE}),
}


class AuthError(ActionableError):
    """The caller could not be identified, or may not do this."""


@dataclass(frozen=True)
class Principal:
    """A caller, resolved. Replaces the hardcoded actor string."""

    actor: str                                  # email, or 'service'
    role: str                                   # analyst | approver | service
    user_id: str | None = None                  # Supabase uuid, None for service
    scopes: frozenset[str] = field(default_factory=frozenset)

    def can(self, scope: str) -> bool:
        return scope in self.scopes

    def require(self, scope: str) -> None:
        if not self.can(scope):
            raise AuthError(
                f"This action needs the '{scope}' permission. You are signed in "
                f"as {self.actor} with the '{self.role}' role, which has: "
                f"{', '.join(sorted(self.scopes)) or 'none'}.")


SERVICE_PRINCIPAL = Principal(actor="service", role="service",
                              scopes=ROLE_SCOPES["service"])


@lru_cache(maxsize=1)
def _jwk_client() -> PyJWKClient:
    """Fetches and caches the published signing keys.

    Cached because it is an HTTP call on a hot path; PyJWKClient handles key
    rotation by re-fetching when it sees an unknown key id.
    """
    base = config.require("SUPABASE_URL")
    return PyJWKClient(f"{base}/auth/v1/.well-known/jwks.json")


def verify_token(token: str) -> dict:
    """Return the verified claims, or raise. Never returns unverified claims."""
    try:
        key = _jwk_client().get_signing_key_from_jwt(token).key
        return jwt.decode(
            token, key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
            issuer=f"{config.require('SUPABASE_URL')}/auth/v1",
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Your session has expired. Sign in again.") from None
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"Invalid session token: {exc}") from None


ROLE_SQL = "select role, email from ops.user_roles where user_id = %s"


def principal_from_token(conn, token: str) -> Principal:
    """Verify a token, then look up what that person is allowed to do.

    A verified token proves identity and nothing else. Someone with a valid
    Supabase account but no row here has no role and cannot do anything -- which
    is the safe default for a system where signup is open.
    """
    claims = verify_token(token)
    user_id = claims.get("sub")
    email = claims.get("email") or user_id

    row = conn.execute(ROLE_SQL, (user_id,)).fetchone()
    if row is None:
        raise AuthError(
            f"{email} is signed in but has no role in this system. An "
            f"administrator must grant 'analyst' or 'approver'.")

    role = row[0]
    return Principal(actor=row[1] or email, role=role, user_id=user_id,
                     scopes=ROLE_SCOPES[role])
