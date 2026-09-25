"""Manage the people who may use L-Mart.

Identity lives in Supabase Auth; authority lives in ops.user_roles. Creating a
person therefore takes two steps, and doing either by hand is how they drift
apart -- an account with no role that cannot do anything, or a role row pointing
at a user that no longer exists.

Uses the SERVICE ROLE key, which bypasses row level security and can create
accounts. Server-side only; never reaches a browser.

  python -m scripts.users list
  python -m scripts.users create <email> <analyst|approver> [--password P]
  python -m scripts.users grant  <email> <analyst|approver>
  python -m scripts.users revoke <email>
  python -m scripts.users delete <email>
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request

from core import config, db

ROLES = ("analyst", "approver")


def _admin(path: str, method: str = "GET", body: dict | None = None):
    base = config.require("SUPABASE_URL")
    key = config.require("SUPABASE_SERVICE_ROLE_KEY")
    request = urllib.request.Request(
        f"{base}/auth/v1/admin{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        raise SystemExit(f"Supabase admin API returned {exc.code}: {detail}")


def find_user(email: str) -> dict | None:
    users = _admin(f"/users?per_page=200").get("users", [])
    return next((u for u in users if u.get("email") == email), None)


def create(email: str, role: str, password: str | None = None) -> None:
    if role not in ROLES:
        raise SystemExit(f"role must be one of: {', '.join(ROLES)}")
    password = password or secrets.token_urlsafe(18)

    existing = find_user(email)
    if existing:
        print(f"  account already exists: {email} ({existing['id']})")
        user = existing
    else:
        # email_confirm bypasses the verification email. These are operator
        # accounts created by an administrator, not public signups, so there is
        # no inbox to confirm from.
        user = _admin("/users", "POST",
                      {"email": email, "password": password,
                       "email_confirm": True})
        print(f"  created account: {email} ({user['id']})")
        print(f"  password: {password}")

    grant(email, role, user_id=user["id"])


def grant(email: str, role: str, user_id: str | None = None) -> None:
    if role not in ROLES:
        raise SystemExit(f"role must be one of: {', '.join(ROLES)}")
    if user_id is None:
        user = find_user(email)
        if not user:
            raise SystemExit(f"no Supabase account for {email}")
        user_id = user["id"]

    with db.direct_connection() as conn:
        conn.execute("""
            insert into ops.user_roles (user_id, email, role)
            values (%s, %s, %s)
            on conflict (user_id) do update
                set role = excluded.role, email = excluded.email,
                    updated_at = now()
        """, (user_id, email, role))
        # Role changes are consequential, so they are auditable like anything
        # else that decides what may happen.
        conn.execute("""insert into ops.audit_log
                            (actor, action, subject_type, subject_id, detail)
                        values ('admin','role.granted','user',%s,%s)""",
                     (user_id, json.dumps({"email": email, "role": role})))
        conn.commit()
    print(f"  granted {role} to {email}")


def revoke(email: str) -> None:
    with db.direct_connection() as conn:
        deleted = conn.execute(
            "delete from ops.user_roles where email = %s", (email,)).rowcount
        conn.execute("""insert into ops.audit_log
                            (actor, action, subject_type, subject_id, detail)
                        values ('admin','role.revoked','user',%s,%s)""",
                     (email, json.dumps({"email": email})))
        conn.commit()
    print(f"  revoked {deleted} role row(s) for {email}; the account remains")


def delete(email: str) -> None:
    user = find_user(email)
    revoke(email)
    if user:
        _admin(f"/users/{user['id']}", "DELETE")
        print(f"  deleted account {email}")


def listing() -> None:
    accounts = {u["email"]: u for u in _admin("/users?per_page=200").get("users", [])}
    with db.direct_connection() as conn:
        roles = {r[0]: (r[1], r[2]) for r in conn.execute(
            "select email, role, user_id from ops.user_roles").fetchall()}

    print(f"  {'email':<34} {'role':<10} account   confirmed")
    for email in sorted(set(accounts) | set(roles)):
        role = roles.get(email, ("(none)",))[0]
        account = accounts.get(email)
        print(f"  {email:<34} {role:<10} "
              f"{'yes' if account else 'MISSING':<9} "
              f"{'yes' if account and account.get('email_confirmed_at') else 'no'}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.users")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    c = sub.add_parser("create"); c.add_argument("email"); c.add_argument("role")
    c.add_argument("--password")
    g = sub.add_parser("grant"); g.add_argument("email"); g.add_argument("role")
    r = sub.add_parser("revoke"); r.add_argument("email")
    d = sub.add_parser("delete"); d.add_argument("email")
    args = parser.parse_args()

    if args.command == "list":
        listing()
    elif args.command == "create":
        create(args.email, args.role, args.password)
    elif args.command == "grant":
        grant(args.email, args.role)
    elif args.command == "revoke":
        revoke(args.email)
    elif args.command == "delete":
        delete(args.email)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
