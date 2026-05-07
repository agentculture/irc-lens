"""Cloudflare Access JWT verification and middleware.

Validates the JWT against the Cloudflare-published JWKS, pinning
audience and issuer. Caches the JWK set in process; on a ``kid`` we
don't recognize, refresh once and retry — but never on every request
(anti-flood window). Identity (email or service-token common-name)
becomes ``request['identity']``; missing/invalid → 401, allowlist
deny → 403.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
import jwt
from aiohttp import web

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


def _build_jwks_url(team_domain: str) -> str:
    return f"{_scheme_for(team_domain)}://{team_domain}{_JWKS_PATH}"


def _build_issuer(team_domain: str) -> str:
    return f"{_scheme_for(team_domain)}://{team_domain}"


class _JWKSCache:
    """In-process JWK set cache with kid-miss-then-refresh semantics."""

    def __init__(self, team_domain: str) -> None:
        self._url = _build_jwks_url(team_domain)
        self._keys: dict[str, Any] = {}
        self._last_fetch: float = 0.0

    async def _refresh(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self._url, timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                r.raise_for_status()
                payload = await r.json()
        self._keys = {k["kid"]: k for k in payload.get("keys", [])}
        self._last_fetch = time.time()

    async def get_key(self, kid: str) -> Any:
        if kid in self._keys:
            return self._keys[kid]
        # Anti-flood: only refresh if cache is empty OR the last fetch
        # was longer ago than the flood window.  A malformed-kid storm
        # would otherwise drive request-time fetches every request.
        within_flood_window = (
            self._keys
            and (time.time() - self._last_fetch) < _KID_MISS_FLOOD_WINDOW_SECONDS
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


def build_cloudflare_middleware(config: LensConfig):
    """Build the @web.middleware coroutine for cloudflare-access mode.

    Pins audience to ``config.cf_aud`` and issuer to
    ``<scheme>://<config.cf_team_domain>``.  Identity is stashed on
    ``request['identity']`` so downstream handlers stay mode-agnostic.
    """
    if config.auth_mode != "cloudflare-access":
        raise ValueError(
            f"build_cloudflare_middleware called with auth_mode={config.auth_mode!r}"
        )
    if not config.cf_aud or not config.cf_team_domain:
        raise ValueError(
            "auth.mode='cloudflare-access' requires both "
            "auth.cloudflare.aud and auth.cloudflare.team_domain"
        )
    cache = _JWKSCache(config.cf_team_domain)
    issuer = _build_issuer(config.cf_team_domain)
    aud = config.cf_aud
    allowed_emails = set(config.allowed_emails)
    allowed_tokens = set(config.allowed_service_tokens)
    server_name = config.server_name

    @web.middleware
    async def middleware(request: web.Request, handler):
        # Static assets never require identity (browser fetches them before
        # the SSO redirect lands on every page load).
        if request.path.startswith("/static/"):
            return await handler(request)

        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            token = request.cookies.get("CF_Authorization")
        if not token:
            return _http_error(
                401,
                "missing Cloudflare Access identity",
                "ensure this request is reaching the lens through cloudflared",
            )

        try:
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")
            if not kid:
                raise jwt.InvalidTokenError("missing kid in JWT header")
            jwk_data = await cache.get_key(kid)
            from jwt.algorithms import RSAAlgorithm
            public_key = RSAAlgorithm.from_jwk(jwk_data)
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=aud,
                issuer=issuer,
            )
        except jwt.ExpiredSignatureError:
            return _http_error(401, "Cloudflare Access JWT expired", "sign in again")
        except jwt.InvalidAudienceError:
            return _http_error(
                401,
                "Cloudflare Access JWT failed verification",
                "audience mismatch — verify auth.cloudflare.aud in the lens config",
            )
        except jwt.InvalidIssuerError:
            return _http_error(
                401,
                "Cloudflare Access JWT failed verification",
                "issuer mismatch — verify auth.cloudflare.team_domain in the lens config",
            )
        except (KeyError, jwt.InvalidTokenError) as exc:
            return _http_error(
                401,
                "Cloudflare Access JWT failed verification",
                f"verify the request came through cloudflared ({type(exc).__name__})",
            )
        except aiohttp.ClientError as exc:
            return _http_error(
                502,
                "could not reach Cloudflare JWKS",
                f"check connectivity to {config.cf_team_domain} ({exc})",
            )

        principal, is_email = _principal_from_claims(claims)
        if not principal:
            return _http_error(
                401,
                "JWT carried neither email nor common_name",
                "verify the Access policy issues an email or service-token JWT",
            )

        # Allowlist enforcement — emails and service tokens are sibling
        # lists.  The ``is_email`` flag prevents a service-token JWT from
        # accidentally matching an entry in ``allowed_emails`` (or vice
        # versa) just because both happen to be string-identical.
        if is_email and principal not in allowed_emails:
            return _http_error(
                403,
                f"{principal} not on allowlist",
                "add to auth.allowed_emails in the lens config",
            )
        if (not is_email) and principal not in allowed_tokens:
            return _http_error(
                403,
                f"service token {principal} not on allowlist",
                "add to auth.allowed_service_tokens in the lens config",
            )

        try:
            nick = derive_nick(server_name, principal)
        except ValueError as exc:
            logger.error("nick derivation failed: %s", exc)
            return _http_error(
                500,
                "nick derivation failed",
                "principal sanitizes to empty; pick a different identity",
            )

        request["identity"] = Identity(
            principal=principal,
            nick=nick,
            raw_jwt_subject=str(claims.get("sub", "")),
        )
        logger.info(
            "auth=ok principal=%s nick=%s method=%s path=%s",
            principal,
            nick,
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
