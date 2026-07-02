"""Cloudflare Access JWT verification and middleware.

Validates the JWT against the Cloudflare-published JWKS, pinning
audience and issuer. Caches the JWK set in process; on a ``kid`` we
don't recognize, refresh once and retry — but never on every request
(anti-flood window). Identity (email or service-token common-name)
becomes ``request['identity']``; missing/invalid → 401, allowlist
deny → 403.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import jwt
from aiohttp import web
from jwt.algorithms import RSAAlgorithm

from irc_lens._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import LensConfig
from irc_lens.web.identity import Identity, derive_nick

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
        async with aiohttp.ClientSession() as session:
            async with session.get(
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
    )


def build_cloudflare_middleware(config: LensConfig):
    """Build the @web.middleware coroutine for cloudflare-access mode.

    Pins audience to ``config.cf_aud`` and issuer to
    ``<scheme>://<config.cf_team_domain>``.  Identity is stashed on
    ``request['identity']`` so downstream handlers stay mode-agnostic.
    """
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
    cache = _JWKSCache(config.cf_team_domain)
    issuer = _build_issuer(config.cf_team_domain)
    aud = config.cf_aud
    team_domain = config.cf_team_domain
    allowed_emails = set(config.allowed_emails)
    allowed_tokens = set(config.allowed_service_tokens)
    server_name = config.server_name

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
        if (
            request.path.startswith("/static/")
            or request.path == "/healthz"
            or request.path.startswith("/media/")
        ):
            return await handler(request)
        token = _extract_token(request)
        if not token:
            return _http_error(
                401,
                "missing Cloudflare Access identity",
                "ensure this request is reaching the lens through cloudflared",
            )
        try:
            claims = await _decode_and_verify_jwt(
                cache, token, aud, issuer, team_domain
            )
            identity = _authorize_principal(
                claims, allowed_emails, allowed_tokens, server_name
            )
        except _AuthDenied as denied:
            return denied.response
        request["identity"] = identity
        logger.info(
            "auth=ok principal=%s nick=%s method=%s path=%s",
            identity.principal,
            identity.nick,
            request.method,
            request.path,
        )
        return await handler(request)

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
