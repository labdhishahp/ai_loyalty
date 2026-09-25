"""Who an MCP caller is, on each transport.

THE TWO TRANSPORTS HAVE GENUINELY DIFFERENT TRUST BOUNDARIES, and pretending
otherwise would be the mistake.

  stdio   The client launched this process, on this machine, as this user. The
          OS already decided who you are; there is no network and no token to
          check. Demanding one would be security theatre. Identity comes from
          configuration and defaults to READ ONLY, so a local session cannot
          draft a campaign unless somebody deliberately raised it.

  http    Anyone who can reach the port can speak to it. The token is the only
          thing standing between a caller and the database, so it is verified
          exactly as the REST API verifies it -- same JWKS, same role lookup,
          same scopes. There is no MCP-specific notion of permission.

This is the security consequence of MCP that is easy to miss: moving a tool
behind a protocol moves the trust boundary. In-process, the agent's identity was
implicit -- it was simply whoever started the run. Over a network it must be
carried, verified, and refused.
"""

from __future__ import annotations

import anyio.to_thread
from mcp.server.auth.provider import AccessToken, TokenVerifier

from core import config, db
from core.auth import ROLE_SCOPES, AuthError, Principal, principal_from_token


def stdio_principal() -> Principal:
    """Identity for a local stdio session.

    Read-only by default. Raising it is a deliberate act -- MCP_ROLE=approver on
    a laptop is a decision someone made, not something that happened by accident.
    """
    actor = config.get("MCP_ACTOR", "mcp-stdio")
    role = config.get("MCP_ROLE", "analyst")
    if role not in ROLE_SCOPES:
        raise AuthError(f"MCP_ROLE must be one of: {', '.join(ROLE_SCOPES)}")
    # A local session gets read only unless MCP_ALLOW_WRITES says otherwise.
    scopes = (ROLE_SCOPES[role] if config.get_bool("MCP_ALLOW_WRITES", False)
              else frozenset({"read"}))
    return Principal(actor=actor, role=role, scopes=scopes)


class SupabaseTokenVerifier(TokenVerifier):
    """Verifies an MCP bearer token using the project's existing auth.

    Deliberately thin. Every rule about who may do what already lives in
    core/auth.py, and a second implementation here would be a second place for
    the answer to drift.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        # The service key is a valid bearer credential for machine callers, and
        # is weaker than any person: read and propose, never approve or execute.
        service_key = config.app_api_key()
        if service_key and token == service_key:
            return AccessToken(token=token, client_id="service",
                               scopes=sorted(ROLE_SCOPES["service"]),
                               subject="service",
                               claims={"actor": "service", "role": "service"})

        # Verifying a user token fetches the signing keys over HTTP and queries
        # Postgres, both with blocking libraries. Doing that directly in an
        # async handler stalls the whole event loop, which showed up as the
        # request timing out rather than as anything resembling its cause: the
        # service-key path short-circuits before either call and worked fine,
        # so only real tokens hung. Offloaded to a worker thread.
        def resolve() -> Principal:
            with db.pooled_connection() as conn:
                return principal_from_token(conn, token)

        try:
            principal = await anyio.to_thread.run_sync(resolve)
        except AuthError:
            # Returning None rather than raising: the protocol expects an
            # authentication failure, not a server error.
            return None
        return AccessToken(
            token=token, client_id=principal.actor,
            scopes=sorted(principal.scopes), subject=principal.user_id,
            claims={"actor": principal.actor, "role": principal.role})


def principal_from_access_token(access: AccessToken | None) -> Principal:
    """Turn the verified token back into the Principal the registry expects."""
    if access is None:
        # No token reached the tool. Refusing everything is the only safe
        # reading -- an unauthenticated caller is not a read-only caller.
        return Principal(actor="anonymous", role="none", scopes=frozenset())
    claims = access.claims or {}
    return Principal(actor=claims.get("actor", access.client_id or "unknown"),
                     role=claims.get("role", "unknown"),
                     user_id=access.subject,
                     scopes=frozenset(access.scopes or ()))
