#!/usr/bin/env python3
"""D4a.1 OpenViking admin helper -- account + user-key provisioning.

OpenViking's ``api_key`` auth mode uses a TWO-LAYER key model:

* a **root key** (from ``ov.conf``) may only call account-administration and
  selected system routes;
* a **user/admin key** (created through the Admin API) is what tenant-scoped
  DATA APIs (``/api/v1/resources``, ``/api/v1/search/find``, ``/api/v1/fs``,
  ``/api/v1/content``) require.

The D4a adapter sends ONE key as ``X-API-Key``. So the application must be given
a USER key, and the root key stays server-side. This helper creates (idempotently)
the application account and its admin user, and prints the user key.

It prints ONLY the user key on stdout; nothing else. The caller stores it in the
0600 env file. No secret is ever logged by the server config or this script.

    python tools/openviking_admin.py --base-url http://127.0.0.1:1933 \
        --root-key "$OPENVIKING_ROOT_KEY" \
        --account website-builder --admin-user wb-admin
"""

from __future__ import annotations

import argparse
import sys


def ensure_account(base_url: str, root_key: str, account_id: str, admin_user_id: str) -> str:
    import httpx

    c = httpx.Client(base_url=base_url.rstrip("/"), headers={"X-API-Key": root_key}, timeout=60)
    accounts = c.get("/api/v1/admin/accounts")
    accounts.raise_for_status()
    existing = {a.get("account_id") for a in (accounts.json().get("result") or [])}
    if account_id not in existing:
        r = c.post(
            "/api/v1/admin/accounts",
            json={"account_id": account_id, "admin_user_id": admin_user_id},
        )
        r.raise_for_status()
        return r.json()["result"]["user_key"]
    # Account exists: find the admin user's key.
    users = c.get(f"/api/v1/admin/accounts/{account_id}/users")
    users.raise_for_status()
    for user in users.json().get("result") or []:
        if user.get("user_id") == admin_user_id and user.get("api_key"):
            return user["api_key"]
    # Admin user missing: register it.
    r = c.post(
        f"/api/v1/admin/accounts/{account_id}/users",
        json={"user_id": admin_user_id, "role": "admin"},
    )
    r.raise_for_status()
    return r.json()["result"]["user_key"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ensure an OpenViking account + user key")
    parser.add_argument("--base-url", default="http://127.0.0.1:1933")
    parser.add_argument("--root-key", required=True)
    parser.add_argument("--account", default="website-builder")
    parser.add_argument("--admin-user", default="wb-admin")
    args = parser.parse_args(argv)
    try:
        key = ensure_account(args.base_url, args.root_key, args.account, args.admin_user)
    except Exception as exc:  # noqa: BLE001 - report a class name only, never a value
        print(f"admin provisioning failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    sys.stdout.write(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
