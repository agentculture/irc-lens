"""Cloudflare Access JWT verification and middleware.

Validates the JWT against the Cloudflare-published JWKS, pinning
audience and issuer. Caches the JWK set in process; on a ``kid`` we
don't recognize, refresh once and retry — but never on every request
(anti-flood window). Identity (email or service-token common-name)
becomes ``request['identity']``; missing/invalid → 401, allowlist
deny → 403.

Tiers (guest mode): a verified, allowlisted JWT yields the ``approved``
tier. With ``guest_mode.enabled`` off that is the only way through —
behavior is exactly as above. With it on, every request that is not
approved (no JWT, unverifiable JWT, verified-but-not-allowlisted
principal) resolves to the ``anonymous`` tier instead of a 401/403, and
reaches its handler only when that route is explicitly marked with
:func:`allows_anonymous`; every other route (the real-mesh console,
stream, input, upload, residents, ``/agent``) still answers 401. Deny by
default: a route that forgets the marker stays approved-only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
import jwt
from aiohttp import web
from jwt.algorithms import RSAAlgorithm

from irc_lens._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import LensConfig
from irc_lens.web import app_session
from irc_lens.web.identity import (
    ANONYMOUS_IDENTITY,
    TIER_APPROVED,
    TIER_GUEST,
    Identity,
    derive_nick,
)

logger = logging.getLogger(__name__)

_JWKS_PATH = "/cdn-cgi/access/certs"
_KID_MISS_FLOOD_WINDOW_SECONDS = 5.0


def _http_error(status: int, error: str, hint: str) -> web.Response:
    return web.json_response({"error": error, "hint": hint}, status=status)


def _scheme_for(team_domain: str) -> str:
    """HTTP for tests (FakeJWKS uses host:port), HTTPS otherwise."""
    return "http" if ":" in team_domain else "https"


# Reused error message — extracted so SonarCloud S1192 (duplicate-literal,
# default threshold 3) stays quiet and so the wording can't drift across
# the three jwt-decode failure arms below.
_ERR_JWT_VERIFICATION = "Cloudflare Access JWT failed verification"


# Single response for an anonymous request on an approved-only route. The
# same body whether the JWT was missing, unverifiable, or for an email not
# on the allowlist, so the route leaks no membership signal (spec c45).
_ERR_APPROVED_REQUIRED = "approved sign-in required"
_HINT_APPROVED_REQUIRED = "sign in through /login to reach the real mesh"

# _AuthDenied statuses that mean "not approved" and therefore fall back to
# the anonymous tier in guest mode. 500 (nick derivation) and 502 (JWKS
# unreachable) are server-side faults and surface unchanged.
_ANONYMOUS_FALLBACK_STATUSES = frozenset({401, 403})

_ALLOWS_ANONYMOUS_ATTR = "_irc_lens_allows_anonymous"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def allows_anonymous(handler: Handler) -> Handler:
    """Mark a route handler as reachable by the ``anonymous`` tier.

    Only consulted when ``guest_mode.enabled``; the handler then sees
    ``request["identity"]`` with ``tier == "anonymous"`` (or ``approved``)
    and must branch on it. Unmarked handlers stay approved-only.
    """
    setattr(handler, _ALLOWS_ANONYMOUS_ATTR, True)
    return handler


def _route_allows_anonymous(request: web.Request) -> bool:
    route = getattr(request.match_info, "route", None)
    route_handler = getattr(route, "handler", None)
    return bool(getattr(route_handler, _ALLOWS_ANONYMOUS_ATTR, False))


def client_ip(request: web.Request) -> str:
    """Best-effort client address (Cloudflare header first, then the peer)."""
    return request.headers.get("CF-Connecting-IP") or request.remote or ""


def _guest_identity(request: web.Request) -> Identity | None:
    """Guest tier from a valid ``lens_guest`` cookie, or None.

    The cookie is HMAC-signed (``csrf.read_guest_cookie``); beyond that the
    guest must still exist in the store and not be banned (by email or IP).
    Nothing but that verified cookie + store row can produce this tier.
    """
    from irc_lens.web import csrf

    store = request.app.get("guest_store")
    if store is None:
        return None
    email = csrf.read_guest_cookie(request)
    if not email:
        return None
    rows = store.get_guest(email)
    if not rows or store.is_banned(email, client_ip(request)):
        return None
    return Identity(
        principal=email, nick=rows[-1][1], raw_jwt_subject="guest", tier=TIER_GUEST
    )


def _build_jwks_url(team_domain: str) -> str:
    return f"{_scheme_for(team_domain)}://{team_domain}{_JWKS_PATH}"


def _build_issuer(team_domain: str) -> str:
    return f"{_scheme_for(team_domain)}://{team_domain}"


class _JWKSCache:
    """In-process JWK set cache with kid-miss-then-refresh semantics."""

    def __init__(self, team_domain: str) -> None:
        self._url = _build_jwks_url(team_domain)
        self._keys: dict[str, Any] = {}
        # Use ``time.monotonic()`` rather than ``time.time()`` for the
        # flood-window arithmetic — wall-clock adjustments (NTP, leap
        # seconds, manual time changes) can otherwise produce negative
        # or huge deltas that collapse or extend the window unexpectedly.
        self._last_fetch: float = 0.0
        # Single-flight: a thundering herd of requests with an unknown
        # ``kid`` would otherwise each kick off their own JWKS refetch.
        # The lock serialises refreshes; the post-lock cache check
        # short-circuits everyone after the first.
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

    async def _refresh(self) -> None:
        async with aiohttp.ClientSession() as session, session.get(
            self._url, timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            r.raise_for_status()
            payload = await r.json()
        self._keys = {k["kid"]: k for k in payload.get("keys", [])}
        self._last_fetch = time.monotonic()

    async def get_key(self, kid: str) -> Any:
        if kid in self._keys:
            return self._keys[kid]
        # Anti-flood + single-flight: only one coroutine refreshes per
        # window. A malformed-kid storm would otherwise drive
        # request-time fetches every request.
        async with self._refresh_lock:
            # Double-check inside the lock: another coroutine may have
            # just refreshed and populated the kid we want.
            if kid in self._keys:
                return self._keys[kid]
            within_flood_window = (
                self._keys
                and (time.monotonic() - self._last_fetch)
                < _KID_MISS_FLOOD_WINDOW_SECONDS
            )
            if within_flood_window:
                raise KeyError(kid)
            await self._refresh()
            if kid not in self._keys:
                raise KeyError(kid)
            return self._keys[kid]

    async def warm(self) -> None:
        await self._refresh()


def _principal_from_claims(claims: dict[str, Any]) -> tuple[str | None, bool]:
    """Return (principal, is_email).

    Email under interactive SSO; common_name under service tokens.
    ``is_email`` lets the caller pick which allowlist to consult so the
    two principal types can't accidentally cross-authorize each other.
    """
    email = claims.get("email")
    if isinstance(email, str) and email:
        return email, True
    cn = claims.get("common_name")
    if isinstance(cn, str) and cn:
        return cn, False
    return None, False


class _AuthDenied(Exception):
    """Internal control-flow exception carrying the HTTP response.

    Lets the verification + authorization helpers surface rich
    ``web.Response`` objects upward without forcing the middleware to
    branch on union return types (which Sonar S3776 counts as
    cognitive complexity). Internal to this module — never propagates
    past ``build_cloudflare_middleware``'s closure.
    """

    __slots__ = ("response",)

    def __init__(self, response: web.Response) -> None:
        super().__init__()
        self.response = response


def _extract_token(request: web.Request) -> str | None:
    """Pull the JWT from the header (preferred) or the cookie.

    Returns ``None`` only when neither carrier holds a token.
    """
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if token:
        return token
    return request.cookies.get("CF_Authorization") or None


async def _decode_and_verify_jwt(
    cache: _JWKSCache,
    token: str,
    aud: str,
    issuer: str,
    team_domain: str,
) -> dict[str, Any]:
    """Verify signature + audience + issuer; return the claims dict.

    Pinned to ``RS256``. Raises :class:`_AuthDenied` carrying the
    appropriate HTTP response on every failure path so the middleware
    body stays linear.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            raise jwt.InvalidTokenError("missing kid in JWT header")
        jwk_data = await cache.get_key(kid)
        public_key = RSAAlgorithm.from_jwk(jwk_data)
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=aud,
            issuer=issuer,
        )
    except jwt.ExpiredSignatureError as exc:
        raise _AuthDenied(_http_error(
            401, "Cloudflare Access JWT expired", "sign in again"
        )) from exc
    except jwt.InvalidAudienceError as exc:
        raise _AuthDenied(_http_error(
            401,
            _ERR_JWT_VERIFICATION,
            "audience mismatch — verify auth.cloudflare.aud in the lens config",
        )) from exc
    except jwt.InvalidIssuerError as exc:
        raise _AuthDenied(_http_error(
            401,
            _ERR_JWT_VERIFICATION,
            "issuer mismatch — verify auth.cloudflare.team_domain in the lens config",
        )) from exc
    except (KeyError, jwt.InvalidTokenError) as exc:
        raise _AuthDenied(_http_error(
            401,
            _ERR_JWT_VERIFICATION,
            f"verify the request came through cloudflared ({type(exc).__name__})",
        )) from exc
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # ``aiohttp.ClientTimeout`` raises ``asyncio.TimeoutError``,
        # which is NOT a subclass of ``aiohttp.ClientError`` — without
        # the explicit catch it would bubble to a 500. Both surface
        # the same operator-facing 502.
        raise _AuthDenied(_http_error(
            502,
            "could not reach Cloudflare JWKS",
            f"check connectivity to {team_domain} ({type(exc).__name__}: {exc})",
        )) from exc


def _authorize_principal(
    claims: dict[str, Any],
    allowed_emails: set[str],
    allowed_tokens: set[str],
    server_name: str,
) -> Identity:
    """Extract the principal, enforce the allowlist, derive the nick.

    Emails and service tokens are sibling lists. The ``is_email`` flag
    prevents a service-token JWT from accidentally matching an entry
    in ``allowed_emails`` (or vice versa) just because the strings
    happen to match. Raises :class:`_AuthDenied` on every deny path.
    """
    principal, is_email = _principal_from_claims(claims)
    if not principal:
        raise _AuthDenied(_http_error(
            401,
            "JWT carried neither email nor common_name",
            "verify the Access policy issues an email or service-token JWT",
        ))
    if is_email and principal not in allowed_emails:
        raise _AuthDenied(_http_error(
            403,
            f"{principal} not on allowlist",
            "add to auth.allowed_emails in the lens config",
        ))
    if (not is_email) and principal not in allowed_tokens:
        raise _AuthDenied(_http_error(
            403,
            f"service token {principal} not on allowlist",
            "add to auth.allowed_service_tokens in the lens config",
        ))
    try:
        nick = derive_nick(server_name, principal)
    except ValueError as exc:
        logger.error("nick derivation failed: %s", exc)
        raise _AuthDenied(_http_error(
            500,
            "nick derivation failed",
            "principal sanitizes to empty; pick a different identity",
        )) from exc
    return Identity(
        principal=principal,
        nick=nick,
        raw_jwt_subject=str(claims.get("sub", "")),
        tier=TIER_APPROVED,
    )


def _require_cf_config(config: LensConfig) -> None:
    """Refuse to build the CF middleware without a complete CF config."""
    if config.auth_mode != "cloudflare-access":
        # AfiError (not ValueError) so the dispatcher renders an
        # `error:`/`hint:` pair and exits with code 1 instead of
        # falling into the catch-all "file a bug" path. Reachable
        # only if a caller bypasses load_config's auth_mode check;
        # belt-and-suspenders for that case.
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"build_cloudflare_middleware called with auth_mode={config.auth_mode!r}",
            remediation="set `auth.mode: cloudflare-access` in the lens config",
        )
    if not config.cf_aud or not config.cf_team_domain:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=(
                "auth.mode='cloudflare-access' requires both "
                "auth.cloudflare.aud and auth.cloudflare.team_domain"
            ),
            remediation=(
                "fill in both `auth.cloudflare.aud` and "
                "`auth.cloudflare.team_domain` in the lens config"
            ),
        )


def _is_public_path(request: web.Request) -> bool:
    """Static assets, ``/healthz`` and capability ``/media/`` URLs need no identity."""
    return (
        request.path.startswith("/static/")
        or request.path == "/healthz"
        or request.path.startswith("/media/")
    )


def _adopt_app_session(request: web.Request) -> bool:
    """Stash the identity of a live app session on *request*; True if adopted."""
    app_identity = app_session.identity_for(request)
    if app_identity is None:
        return False
    request["identity"] = app_identity
    logger.info(
        "auth=ok via=app-session principal=%s nick=%s method=%s path=%s",
        app_identity.principal,
        app_identity.nick,
        request.method,
        request.path,
    )
    return True


@dataclass(frozen=True)
class _AccessContext:
    """What the cloudflare-access middleware checks a request against."""

    cache: _JWKSCache
    issuer: str
    aud: str
    team_domain: str
    allowed_emails: set[str]
    allowed_tokens: set[str]
    server_name: str
    guest_enabled: bool


async def _as_anonymous(request: web.Request, handler, reason: str):
    """Guest mode: resolve to the anonymous tier, deny-by-default routes."""
    logger.info(
        "auth=anonymous reason=%s method=%s path=%s",
        reason,
        request.method,
        request.path,
    )
    if not _route_allows_anonymous(request):
        return _http_error(401, _ERR_APPROVED_REQUIRED, _HINT_APPROVED_REQUIRED)
    request["identity"] = _guest_identity(request) or ANONYMOUS_IDENTITY
    return await handler(request)


async def _fallback(
    ctx: _AccessContext, request: web.Request, handler, reason: str, deny
):
    """Guest mode: continue as anonymous; otherwise answer with ``deny()``."""
    if ctx.guest_enabled:
        return await _as_anonymous(request, handler, reason)
    return deny()


async def _via_access_jwt(
    ctx: _AccessContext, request: web.Request, handler, token: str
):
    """Verify the Access JWT, authorize its principal, run the handler.

    A denial whose status allows it falls back to the anonymous tier in
    guest mode; any other denial is answered as-is.
    """
    try:
        claims = await _decode_and_verify_jwt(
            ctx.cache, token, ctx.aud, ctx.issuer, ctx.team_domain
        )
        identity = _authorize_principal(
            claims, ctx.allowed_emails, ctx.allowed_tokens, ctx.server_name
        )
    except _AuthDenied as denied:
        refusal = denied.response
        if refusal.status not in _ANONYMOUS_FALLBACK_STATUSES:
            return refusal
        return await _fallback(
            ctx, request, handler, f"not-approved-{refusal.status}", lambda: refusal
        )
    request["identity"] = identity
    logger.info(
        "auth=ok principal=%s nick=%s method=%s path=%s",
        identity.principal,
        identity.nick,
        request.method,
        request.path,
    )
    return await handler(request)


def _missing_identity() -> web.Response:
    return _http_error(
        401,
        "missing Cloudflare Access identity",
        "ensure this request is reaching the lens through cloudflared",
    )


def build_cloudflare_middleware(config: LensConfig):
    """Build the @web.middleware coroutine for cloudflare-access mode.

    Pins audience to ``config.cf_aud`` and issuer to
    ``<scheme>://<config.cf_team_domain>``.  Identity is stashed on
    ``request['identity']`` so downstream handlers stay mode-agnostic.
    """
    _require_cf_config(config)
    ctx = _AccessContext(
        cache=_JWKSCache(config.cf_team_domain),
        issuer=_build_issuer(config.cf_team_domain),
        aud=config.cf_aud,
        team_domain=config.cf_team_domain,
        allowed_emails=set(config.allowed_emails),
        allowed_tokens=set(config.allowed_service_tokens),
        server_name=config.server_name,
        guest_enabled=config.guest_enabled,
    )

    @web.middleware
    async def middleware(request: web.Request, handler):
        # Static assets never require identity (browser fetches them
        # before the SSO redirect lands on every page load). `/healthz`
        # is also unauthenticated by spec — cloudflared and external
        # uptime probes hit it without a JWT, and the response is opaque
        # (`{"ok": true}`) so it doesn't leak any allowlist state.
        # `/media/{token}.{ext}` (task t6) is a capability URL — the
        # unguessable token in the path *is* the credential, so a JWT
        # would be redundant and would break the agent-fetch path
        # (other agents on the mesh have no Cloudflare identity to
        # present). See docs/superpowers/specs/
        # 2026-07-02-media-support-design.md ("Upload path").
        if _is_public_path(request):
            return await handler(request)
        # App-native sign-in: a valid lens_session cookie is checked before
        # the Access JWT. Anything short of a live, still-allowlisted
        # session yields None and falls through unchanged (break-glass).
        if _adopt_app_session(request):
            return await handler(request)
        token = _extract_token(request)
        if not token:
            return await _fallback(
                ctx, request, handler, "no-jwt", _missing_identity
            )
        return await _via_access_jwt(ctx, request, handler, token)

    return middleware


async def warm_jwks(config: LensConfig) -> None:
    """Fail fast at startup if Cloudflare's JWKS is unreachable.

    No-op for non-CF auth modes.  Raises whatever ``aiohttp.ClientError``
    or ``KeyError`` the cache surfaces; the caller (``serve.py``)
    wraps that as ``AfiError(EXIT_ENV_ERROR, ...)``.
    """
    if config.auth_mode != "cloudflare-access" or not config.cf_team_domain:
        return
    cache = _JWKSCache(config.cf_team_domain)
    await cache.warm()
