#!/usr/bin/env python3
"""Narrow (or restore) the chat Cloudflare Access app to the /login path.

Guest mode puts the public site in front of Cloudflare Access: only
``<host>/login`` stays behind SSO, everything else is public and irc-lens
enforces the tier itself. Narrowing the EXISTING app's destination (rather
than creating a second app) keeps its AUD, so the lens's pinned
``auth.cloudflare.aud`` keeps verifying the JWTs.

The CF_Authorization cookie must stay host-wide so approved users are
recognized on public paths: ``path_cookie_attribute`` is pinned to False
(Cloudflare only scopes the cookie to the app path when it is True).
``same_site_cookie_attribute`` is pinned to "lax" so the SSO redirect back
from the team domain still carries the cookie.

Dry-run by default. Credentials come from the environment (inject them,
never print them)::

    grant run --inject CF_TOK=CLOUDFLARE_API_TOKEN \\
              --inject CF_ACC=CLOUDFLARE_ACCOUNT_ID -- \\
        python3 scripts/cf_access_login_path.py --host chat.culture.dev [--apply | --restore --apply]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

API = "https://api.cloudflare.com/client/v4/accounts/{acc}/access/apps"
LOGIN_PATH = "login"

# Fields the Access "update application" PUT accepts that we carry over
# unchanged from the live app, so a narrow/restore never drops settings.
_CARRIED = (
    "name",
    "type",
    "session_duration",
    "allowed_idps",
    "auto_redirect_to_identity",
    "app_launcher_visible",
    "http_only_cookie_attribute",
    "enable_binding_cookie",
    "policies",
)


def target_destinations(host: str, mode: str) -> list[dict]:
    """Destinations for ``mode`` ("narrow" -> host/login, "restore" -> host)."""
    if mode == "narrow":
        return [{"type": "public", "uri": f"{host}/{LOGIN_PATH}"}]
    if mode == "restore":
        return [{"type": "public", "uri": host}]
    raise ValueError(f"unknown mode {mode!r}")


def build_update(app: dict, host: str, mode: str) -> dict:
    """The PUT body for ``app``: carried settings + new destinations + cookie pins."""
    dests = target_destinations(host, mode)
    body = {k: app[k] for k in _CARRIED if app.get(k) is not None}
    if "policies" in body:
        body["policies"] = [
            {"id": p["id"], "precedence": p.get("precedence", i + 1)}
            for i, p in enumerate(body["policies"])
        ]
    body["domain"] = dests[0]["uri"]
    body["destinations"] = dests
    body["path_cookie_attribute"] = False
    body["same_site_cookie_attribute"] = "lax"
    return body


def find_app(apps: list[dict], host: str) -> dict:
    """The self-hosted app whose domain is ``host`` or ``host/<path>``."""
    hits = [
        a
        for a in apps
        if (a.get("domain") or "").split("/", 1)[0] == host
        and a.get("type") == "self_hosted"
    ]
    if len(hits) != 1:
        raise SystemExit(
            f"expected exactly one self_hosted Access app for {host}, found {len(hits)}"
        )
    return hits[0]


def _call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(  # nosec B310 - fixed Cloudflare API URL
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
        out = json.load(resp)
    if not out.get("success"):
        raise SystemExit(f"Cloudflare API error: {out.get('errors')}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True, help="e.g. chat.culture.dev")
    ap.add_argument(
        "--restore",
        action="store_true",
        help="restore the host-wide destination (rollback)",
    )
    ap.add_argument(
        "--apply", action="store_true", help="perform the change (default: dry run)"
    )
    args = ap.parse_args(argv)
    token, acc = os.environ.get("CF_TOK"), os.environ.get("CF_ACC")
    if not token or not acc:
        print("CF_TOK and CF_ACC must be set (inject via grant)", file=sys.stderr)
        return 2
    base = API.format(acc=acc)
    app = find_app(_call("GET", f"{base}?per_page=100", token)["result"], args.host)
    mode = "restore" if args.restore else "narrow"
    body = build_update(app, args.host, mode)
    before = [d.get("uri") for d in app.get("destinations") or []]
    after = [d["uri"] for d in body["destinations"]]
    print(
        f"app {app.get('name')!r}: destinations {before} -> {after}; path_cookie=False same_site=lax"
    )
    if not args.apply:
        print("dry run — pass --apply to change it")
        return 0
    _call("PUT", f"{base}/{app['id']}", token, body)
    print("applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
