"""App-native approved sessions: cookies, identity, logout and revocation.

An approved user who signs in inside the app (password + emailed code, see
``entry.py``) gets a server-side session: the guest store's ``sessions``
table holds only ``sha256(session id)``, the email and created/last-seen
times; the raw id lives only in the ``lens_session`` cookie
(``HttpOnly; Secure; SameSite=Lax; Path=/``, 30 days). A session idle for
7 days or older than 30 days is refused by the store.

The auth middleware (``auth.build_cloudflare_middleware``) consults
:func:`identity_for` before the Cloudflare Access JWT path. The identity it
produces is exactly the one an Access JWT yields for the same email
(principal = email, nick via ``derive_nick``, tier ``approved``), so the
session registry shares one IRC connection per user however they signed
in. The email is re-checked against ``auth.allowed_emails`` on every
request. A missing, unknown, expired or no-longer-allowed session simply
yields ``None`` and the middleware falls through to the JWT path, so
``/login`` stays usable as break-glass.

Ending a session (logout, password change, expiry, allowlist removal) also
closes the user's live IRC session(s) opened through it: every registry
entry opened by a request authenticated with an app session is remembered
against that session's id hash (:data:`LINKS`), and :func:`sweep_once`
(run on the ban sweeper's interval) closes those whose app session is gone.
Registry entries opened through an Access JWT are never closed here.

The pending sign-in cookie ``lens_signin`` (``SameSite=Strict``,
``Path=/entry``, 10 minutes) ties the emailed code to the browser that
entered the password; its value is opaque to this module.

A browser that completes sign-in with "Trust this browser" ticked gets the
trusted-browser cookie ``lens_device`` (``HttpOnly; Secure; SameSite=Lax;
Path=/``, one year): a random id whose sha256 the store keeps per email. It
exempts that browser from the sign-in attempt limits for that email only.
Logout keeps it; setting or resetting the password revokes it.

Raw session ids, device ids and cookie values are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiohttp import web

from irc_lens import metrics
from irc_lens.guest_store import session_id_hash
from irc_lens.web.identity import TIER_APPROVED, Identity, derive_nick

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "lens_session"
SIGNIN_COOKIE_NAME = "lens_signin"
SESSION_COOKIE_MAX_AGE = 30 * 86400
SIGNIN_COOKIE_MAX_AGE = 600
SIGNIN_COOKIE_PATH = "/entry"
DEVICE_COOKIE_NAME = "lens_device"
DEVICE_COOKIE_MAX_AGE = 365 * 86400

#: Write ``last_seen`` at most this often per session (no DB write per request).
TOUCH_INTERVAL_S = 60

#: ``request[REQUEST_SESSION_HASH]``: the id hash of the app session that
#: authenticated this request (absent for Access-JWT / guest / anonymous).
REQUEST_SESSION_HASH = "app_session_hash"

#: ``{(principal, backend): id_hash}`` for registry sessions opened via an
#: app session.
LINKS = web.AppKey("app_session_links", dict)

# secrets.token_urlsafe output; anything else is not one of our ids.
_RAW_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")


# -- cookies -------------------------------------------------------------------


def issue_session_cookie(response: web.StreamResponse, raw_id: str) -> None:
    """Set ``lens_session`` (HttpOnly, Secure, SameSite=Lax, Path=/, 30 days)."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_id,
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        secure=True,
        samesite="Lax",
    )


def read_session_cookie(request: web.Request) -> str | None:
    """The raw session id from ``lens_session``, or None if absent/malformed."""
    value = request.cookies.get(SESSION_COOKIE_NAME)
    if not value or not _RAW_ID_RE.fullmatch(value):
        return None
    return value


def clear_session_cookie(response: web.StreamResponse) -> None:
    response.del_cookie(
        SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="Lax"
    )


def set_signin_cookie(
    response: web.StreamResponse, value: str, max_age: int = SIGNIN_COOKIE_MAX_AGE
) -> None:
    """Set the pending sign-in cookie (HttpOnly, Secure, Strict, Path=/entry)."""
    response.set_cookie(
        SIGNIN_COOKIE_NAME,
        value,
        max_age=max_age,
        path=SIGNIN_COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="Strict",
    )


def read_signin_cookie(request: web.Request) -> str | None:
    return request.cookies.get(SIGNIN_COOKIE_NAME) or None


def clear_signin_cookie(response: web.StreamResponse) -> None:
    response.del_cookie(
        SIGNIN_COOKIE_NAME,
        path=SIGNIN_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="Strict",
    )


def issue_device_cookie(response: web.StreamResponse, raw_id: str) -> None:
    """Set ``lens_device`` (HttpOnly, Secure, SameSite=Lax, Path=/, one year)."""
    response.set_cookie(
        DEVICE_COOKIE_NAME,
        raw_id,
        max_age=DEVICE_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        secure=True,
        samesite="Lax",
    )


def read_device_cookie(request: web.Request) -> str | None:
    """The raw trusted-browser id from ``lens_device``, or None if absent/malformed."""
    value = request.cookies.get(DEVICE_COOKIE_NAME)
    if not value or not _RAW_ID_RE.fullmatch(value):
        return None
    return value


def clear_device_cookie(response: web.StreamResponse) -> None:
    response.del_cookie(
        DEVICE_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="Lax"
    )


# -- identity (called by the auth middleware) ----------------------------------


def allowed_lower(config) -> frozenset[str]:
    """``auth.allowed_emails`` lowercased: app sign-in compares emails without case."""
    return frozenset(e.lower() for e in getattr(config, "allowed_emails", ()))


def _store(app: web.Application):
    return app.get("guest_store")


def identity_for(request: web.Request) -> Identity | None:
    """Approved identity from a valid ``lens_session`` cookie, else None.

    None (never an error) for: sign-in disabled, no store, no/malformed
    cookie, unknown/expired/deleted session, or an email no longer in
    ``auth.allowed_emails`` (read from the app's config on every request).
    On success stamps ``request[REQUEST_SESSION_HASH]``.
    """
    config = request.app["config"]
    store = _store(request.app)
    if not getattr(config, "app_signin_enabled", False) or store is None:
        return None
    raw = read_session_cookie(request)
    if raw is None:
        return None
    row = store.get_session(raw)
    if row is None:
        return None
    email, _created, last_seen = row
    if email not in allowed_lower(config):
        return None
    try:
        nick = derive_nick(config.server_name, email)
    except ValueError:
        return None
    if store.now() - last_seen >= TOUCH_INTERVAL_S:
        store.touch_session(raw)
    request[REQUEST_SESSION_HASH] = session_id_hash(raw)
    return Identity(
        principal=email, nick=nick, raw_jwt_subject="app-session", tier=TIER_APPROVED
    )


def via_app_session(request: web.Request) -> bool:
    """True iff this request was authenticated by an app session."""
    return request.get(REQUEST_SESSION_HASH) is not None


# -- registry links + revocation -----------------------------------------------


def note_registry_open(
    request: web.Request, principal: str, backend: str, existed: bool
) -> None:
    """Record which app session (if any) opened registry entry (principal, backend).

    Called by ``routes._resolve_session`` around ``get_or_open``. Only a
    fresh open is attributed; a fresh open through anything else (an Access
    JWT) drops any stale link so the sweep never closes it.
    """
    links = request.app.get(LINKS)
    if links is None or existed:
        return
    id_hash = request.get(REQUEST_SESSION_HASH)
    if id_hash is None:
        links.pop((principal, backend), None)
    else:
        links[(principal, backend)] = id_hash


async def _close_links(app: web.Application, keys: list[tuple[str, str]]) -> None:
    links = app.get(LINKS)
    registry = app["registry"]
    for key in keys:
        if links is not None:
            links.pop(key, None)
        await registry.close(*key)


async def sweep_once(app: web.Application) -> list[tuple[str, str]]:
    """Close registry sessions whose app session ended; return their keys.

    Ended = deleted (logout, password change, revoke), expired (7 days idle
    / 30 days old), or its email no longer in ``auth.allowed_emails``.
    """
    links = app.get(LINKS)
    store = _store(app)
    if not links or store is None:
        return []
    registry = app["registry"]
    allowed = allowed_lower(app["config"])
    ended: list[tuple[str, str]] = []
    for key, id_hash in tuple(links.items()):  # snapshot: links.pop below
        if not registry.has(*key):
            links.pop(key, None)  # closed elsewhere (idle reap, shutdown)
            continue
        email = await asyncio.to_thread(store.session_email_by_hash, id_hash)
        if email is None or email not in allowed:
            ended.append(key)
    await _close_links(app, ended)
    if ended:
        logger.info("app_session_sweep closed=%d", len(ended))
    return ended


async def end_session(app: web.Application, raw_id: str) -> bool:
    """Delete one app session and close the registry sessions it opened."""
    store = _store(app)
    if store is None:
        return False
    id_hash = session_id_hash(raw_id)
    removed = await asyncio.to_thread(store.delete_session, raw_id)
    links = app.get(LINKS) or {}
    await _close_links(app, [k for k, h in links.items() if h == id_hash])
    if removed:
        metrics.get_metrics().session_ended()
    return bool(removed)


async def end_sessions_for_email(app: web.Application, email: str) -> int:
    """Revoke every app session of *email* and close its live IRC sessions now.

    Used when a password is set or reset. Returns how many sessions ended.
    """
    store = _store(app)
    if store is None:
        return 0
    removed = await asyncio.to_thread(store.delete_sessions_for_email, email)
    links = app.get(LINKS) or {}
    await _close_links(app, [k for k in links if k[0] == email])
    for _ in range(removed):
        metrics.get_metrics().session_ended()
    return removed


# -- POST /logout ----------------------------------------------------------------


async def post_logout(request: web.Request) -> web.Response:
    """End this browser's app session, clear the cookie, go home.

    The trusted-browser cookie ``lens_device`` is deliberately kept (c37).

    Approved-only (not ``allows_anonymous``) and CSRF-checked like every
    POST. htmx requests get ``HX-Redirect`` (an XHR would follow a 303
    silently); plain form posts get ``303 /``.
    """
    identity: Identity = request["identity"]
    raw = read_session_cookie(request)
    if raw is not None:
        await end_session(request.app, raw)
    logger.info("logout principal=%s", identity.principal)
    if request.headers.get("HX-Request"):
        response = web.Response(status=204, headers={"HX-Redirect": "/"})
    else:
        response = web.Response(status=303, headers={"Location": "/"})
    clear_session_cookie(response)
    return response


def install(app: web.Application) -> None:
    """Enable app sessions on *app*: the link table and ``POST /logout``."""
    app[LINKS] = {}
    app.router.add_post("/logout", post_logout)
