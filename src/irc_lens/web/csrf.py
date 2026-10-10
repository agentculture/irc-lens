"""Signed guest session cookie + CSRF guard for state-changing requests.

Guest mode makes the console public; guests are identified by a lens-issued
cookie. This module owns two things:

* the cookie primitive -- :func:`issue_guest_cookie` /
  :func:`read_guest_cookie` -- an HMAC-SHA256 signed, short-lived,
  ``HttpOnly; Secure; SameSite=Strict`` cookie carrying a guest id;
* :func:`csrf_middleware` -- rejects cross-site state-changing requests
  (every method except GET/HEAD/OPTIONS, so consent / deletion / upload
  routes are covered the moment they are added) with 403 before any handler
  runs, so nothing reaches IRC.

Cookie value: ``<b64url(json {"g": guest_id, "exp": unix_ts})>.<b64url(hmac)>``.

The HMAC secret is never hard-coded: it comes from the
``IRC_LENS_GUEST_COOKIE_SECRET`` environment variable (see
``docs/guest-mode-config.md``). If unset, a random per-process secret is
generated (cookies then die on restart -- safe, just inconvenient).

Composition with the pre-existing Origin floor (``routes._origin_ok``): the
middleware applies that same check first (a present, mismatching ``Origin``
is always 403). It adds a stricter rule for requests that carry a guest
cookie: ``Origin`` must be present and match, or -- when absent -- the
browser's ``Sec-Fetch-Site`` must say ``same-origin``/``none``. A
``cross-site``/``same-site`` fetch with a cookie is refused. The same rule
covers the approved app-session cookie ``lens_session`` and the pending
sign-in cookie ``lens_signin`` (:data:`PROOF_COOKIE_NAMES`). Requests without
any of them keep the original lenient behaviour (Origin-absent allowed).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from aiohttp import web

from irc_lens.web.app_session import SESSION_COOKIE_NAME, SIGNIN_COOKIE_NAME

logger = logging.getLogger("irc_lens.web.csrf")

GUEST_COOKIE_NAME = "lens_guest"
#: Cookies whose presence demands same-origin proof on an Origin-less
#: state-changing request: the guest cookie, the approved app session and
#: the pending sign-in cookie (``web/app_session.py``).
PROOF_COOKIE_NAMES = (GUEST_COOKIE_NAME, SESSION_COOKIE_NAME, SIGNIN_COOKIE_NAME)
SECRET_ENV = "IRC_LENS_GUEST_COOKIE_SECRET"
DEFAULT_TTL_SECONDS = 3600
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SECRET_KEY = web.AppKey("guest_cookie_secret", bytes)

_global_secret: bytes | None = None


def load_secret(env: dict[str, str] | None = None) -> bytes:
    """Secret from the environment, else a random per-process one."""
    raw = (env if env is not None else os.environ).get(SECRET_ENV, "")
    if raw:
        return raw.encode("utf-8")
    logger.warning(
        "%s unset; using a random per-process guest cookie secret "
        "(guest cookies will not survive a restart)",
        SECRET_ENV,
    )
    return secrets.token_bytes(32)


def install(app: web.Application, secret: bytes | None = None) -> None:
    """Register the secret on *app* (and as module default).

    The middleware itself is listed in ``make_app``'s ``middlewares=`` so its
    position (after security headers, before identity) is explicit.
    """
    global _global_secret
    secret = secret if secret is not None else load_secret()
    app[SECRET_KEY] = secret
    _global_secret = secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: bytes) -> str:
    return _b64(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())


def make_cookie_value(
    guest_id: str,
    secret: bytes,
    ttl: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    exp = int((time.time() if now is None else now) + ttl)
    payload = _b64(
        json.dumps({"g": guest_id, "exp": exp}, separators=(",", ":")).encode()
    )
    return f"{payload}.{_sign(payload, secret)}"


def verify_cookie_value(
    value: str, secret: bytes, now: float | None = None
) -> str | None:
    """Return the guest id if *value* is authentic and unexpired, else None."""
    try:
        payload, sig = value.split(".", 1)
        if not hmac.compare_digest(sig, _sign(payload, secret)):
            return None
        data = json.loads(_unb64(payload))
        guest_id = data["g"]
        exp = data["exp"]
    except (ValueError, KeyError, TypeError):  # incl. binascii.Error / UnicodeError
        return None
    if not isinstance(guest_id, str) or not guest_id or not isinstance(exp, int):
        return None
    if (time.time() if now is None else now) >= exp:
        return None
    return guest_id


def issue_guest_cookie(
    response: web.StreamResponse,
    guest_id: str,
    *,
    secret: bytes | None = None,
    ttl: int = DEFAULT_TTL_SECONDS,
) -> None:
    """Set the signed guest cookie on *response*."""
    secret = secret if secret is not None else _global_secret
    if secret is None:
        raise RuntimeError(
            "guest cookie secret not configured (csrf.install not called)"
        )
    response.set_cookie(
        GUEST_COOKIE_NAME,
        make_cookie_value(guest_id, secret, ttl),
        max_age=ttl,
        path="/",
        httponly=True,
        secure=True,
        samesite="Strict",
    )


def read_guest_cookie(request: web.Request) -> str | None:
    """Return the verified guest id from *request*'s cookie, or None."""
    value = request.cookies.get(GUEST_COOKIE_NAME)
    if not value:
        return None
    secret = request.app.get(SECRET_KEY) or _global_secret
    if secret is None:
        return None
    return verify_cookie_value(value, secret)


def _denied(request: web.Request, reason: str) -> web.Response:
    logger.warning(
        "csrf_denied reason=%s method=%s path=%s origin=%s sec_fetch_site=%s",
        reason,
        request.method,
        request.path,
        request.headers.get("Origin"),
        request.headers.get("Sec-Fetch-Site"),
    )
    return web.json_response(
        {
            "error": "cross-site request refused",
            "hint": "this is a CSRF defense; submit from the lens UI itself",
        },
        status=403,
    )


@web.middleware
async def csrf_middleware(request: web.Request, handler):
    """403 cross-site state-changing requests before any handler runs."""
    if request.method in SAFE_METHODS:
        return await handler(request)
    # Imported lazily: routes owns the shared Origin comparison.
    from irc_lens.web import routes

    if not routes._origin_ok(request):
        return routes._origin_denied_response(request)  # same body + log as /input
    has_cookie = any(request.cookies.get(name) for name in PROOF_COOKIE_NAMES)
    if has_cookie and request.headers.get("Origin") is None:
        if request.headers.get("Sec-Fetch-Site", "").lower() not in (
            "same-origin",
            "none",
        ):
            return _denied(request, "cookie_without_same_origin_proof")
    return await handler(request)
