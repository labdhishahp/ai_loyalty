"""Identity and authorization.

TOKENS ARE MINTED LOCALLY with a throwaway EC key and the published key lookup
is stubbed. That is not a shortcut around the real thing -- it exercises the
actual verification path (signature, expiry, audience, issuer) while being
deterministic, free, and dependent on no external account. A test that asked
Supabase for a token would prove Supabase works, not that we check it properly.

The case that matters most is `test_a_forged_token_is_rejected`: signed by a
different key, everything else identical. Decoding claims without verifying the
signature would accept it, and that is the most common authentication mistake
there is.
"""

from __future__ import annotations

import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from core import auth
from core.auth import (APPROVE, EXECUTE, PROPOSE, READ, AuthError, Principal,
                       principal_from_token, verify_token)

ISSUER = "https://project.supabase.co/auth/v1"
USER_ID = "11111111-2222-3333-4444-555555555555"


def _keypair():
    return ec.generate_private_key(ec.SECP256R1())


def _token(private_key, *, sub=USER_ID, email="analyst@example.com",
           aud="authenticated", issuer=ISSUER, expires_in=3600):
    now = dt.datetime.now(dt.timezone.utc)
    return jwt.encode(
        {"sub": sub, "email": email, "aud": aud, "iss": issuer,
         "iat": now, "exp": now + dt.timedelta(seconds=expires_in)},
        private_key, algorithm="ES256")


@pytest.fixture
def signing(monkeypatch):
    """Stub the published-key lookup with a throwaway key we control."""
    key = _keypair()

    class _Key:
        def __init__(self, public):
            self.key = public

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key(key.public_key())

    monkeypatch.setattr(auth, "_jwk_client", lambda: _Client())
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    return key


# ------------------------------------------------------------ verification

def test_a_valid_token_verifies(signing):
    claims = verify_token(_token(signing))
    assert claims["sub"] == USER_ID
    assert claims["email"] == "analyst@example.com"


def test_a_forged_token_is_rejected(signing):
    """Signed by a different key. Everything else is identical, so only the
    signature check can catch it."""
    with pytest.raises(AuthError, match="Invalid session token"):
        verify_token(_token(_keypair()))


def test_an_expired_token_is_rejected(signing):
    with pytest.raises(AuthError, match="expired"):
        verify_token(_token(signing, expires_in=-60))


def test_a_token_for_another_audience_is_rejected(signing):
    with pytest.raises(AuthError, match="Invalid session token"):
        verify_token(_token(signing, aud="some-other-service"))


def test_a_token_from_another_issuer_is_rejected(signing):
    with pytest.raises(AuthError, match="Invalid session token"):
        verify_token(_token(signing, issuer="https://evil.example.com/auth/v1"))


# ----------------------------------------------------- identity vs authority

@pytest.fixture
def roles(own_conn):
    """Two people with roles, removed afterwards."""
    rows = [(USER_ID, "analyst@example.com", "analyst"),
            ("99999999-8888-7777-6666-555555555555", "approver@example.com",
             "approver")]
    for user_id, email, role in rows:
        own_conn.execute(
            "insert into ops.user_roles (user_id, email, role) values (%s,%s,%s) "
            "on conflict (user_id) do update set role = excluded.role",
            (user_id, email, role))
    own_conn.commit()
    yield own_conn
    own_conn.execute("delete from ops.user_roles where user_id = any(%s)",
                     ([r[0] for r in rows],))
    own_conn.commit()


def test_a_verified_token_without_a_role_grants_nothing(signing, roles):
    """Being signed in is not the same as being allowed. Signup is open, so the
    safe default for an unknown account is no authority at all."""
    stranger = _token(signing, sub="00000000-0000-0000-0000-000000000000",
                      email="stranger@example.com")
    with pytest.raises(AuthError, match="no role in this system"):
        principal_from_token(roles, stranger)


def test_an_analyst_may_propose_but_not_approve(signing, roles):
    principal = principal_from_token(roles, _token(signing))
    assert principal.role == "analyst"
    assert principal.actor == "analyst@example.com"
    assert principal.can(READ) and principal.can(PROPOSE)
    assert not principal.can(APPROVE) and not principal.can(EXECUTE)


def test_an_approver_may_do_everything(signing, roles):
    token = _token(signing, sub="99999999-8888-7777-6666-555555555555",
                   email="approver@example.com")
    principal = principal_from_token(roles, token)
    assert principal.role == "approver"
    assert all(principal.can(s) for s in (READ, PROPOSE, APPROVE, EXECUTE))


def test_the_service_credential_is_weaker_than_any_person():
    """A shared secret names nobody, so it may not authorise anything that
    reaches a customer."""
    service = auth.SERVICE_PRINCIPAL
    assert service.can(READ) and service.can(PROPOSE)
    assert not service.can(APPROVE) and not service.can(EXECUTE)


def test_refusal_explains_what_is_missing():
    analyst = Principal("a@example.com", "analyst", scopes=auth.ROLE_SCOPES["analyst"])
    with pytest.raises(AuthError) as excinfo:
        analyst.require(APPROVE)
    message = str(excinfo.value)
    assert "approve" in message and "analyst" in message and "a@example.com" in message
