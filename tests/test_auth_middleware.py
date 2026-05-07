"""CF Access JWT validation + middleware behavior."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.web import make_app

from _jwks_server import FakeJWKS

# Test-only path that triggers the auth middleware but bypasses
# `_resolve_session` so we don't need a real (or mocked) Session.
# `/healthz` and `/static/*` are both bypassed by the middleware
# per spec, so neither can serve as the "did the middleware run?"
# canary — hence the dedicated route mounted by `cf_client`.
_AUTH_CANARY_PATH = "/test/auth-canary"


async def _auth_canary_handler(request: web.Request) -> web.Response:
    identity = request["identity"]
    return web.json_response({"ok": True, "principal": identity.principal})


def _cf_config(jwks: FakeJWKS, allowed: list[str]) -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=tuple(allowed),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
    )


@pytest_asyncio.fixture
async def jwks() -> AsyncIterator[FakeJWKS]:
    j = FakeJWKS()
    await j.start()
    try:
        yield j
    finally:
        await j.stop()


@pytest_asyncio.fixture
async def cf_client(jwks: FakeJWKS) -> AsyncIterator[TestClient]:
    """A lens TestClient in cloudflare-access mode wired to FakeJWKS.

    Sessions never actually open in these tests — the middleware
    intercepts before any handler that would call get_or_open.
    The factory raises if invoked, to catch accidental routing.
    A test-only `/test/auth-canary` route is mounted to give us a
    path that triggers the middleware without reaching the
    SessionRegistry (so the boom_factory contract holds for both
    accept and reject cases).
    """
    config = _cf_config(jwks, allowed=["alice@example.com"])

    def boom_factory(_nick: str):
        raise AssertionError("session factory must not run during auth tests")

    app = make_app(config, boom_factory)
    app.router.add_get(_AUTH_CANARY_PATH, _auth_canary_handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_missing_jwt_returns_401(cf_client: TestClient) -> None:
    resp = await cf_client.get("/")
    assert resp.status == 401
    body = await resp.json()
    assert "error" in body and "hint" in body


async def test_valid_jwt_in_header_passes_to_handler(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s-1"})
    # `/test/auth-canary` triggers the middleware but doesn't open a
    # Session, so the boom_factory contract holds. The handler echoes
    # the validated principal back so we can confirm identity stashing.
    resp = await cf_client.get(_AUTH_CANARY_PATH, headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "principal": "alice@example.com"}


async def test_valid_jwt_in_cookie_also_accepted(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s-2"})
    cf_client.session.cookie_jar.update_cookies({"CF_Authorization": token})
    resp = await cf_client.get(_AUTH_CANARY_PATH)
    assert resp.status == 200


async def test_healthz_bypasses_auth_per_spec(cf_client: TestClient) -> None:
    """`/healthz` must NOT require a JWT — cloudflared and external
    uptime probes hit it without auth. The handler returns the opaque
    `{"ok": true}` so it can't leak allowlist state either way."""
    resp = await cf_client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True}


async def test_wrong_audience_returns_401(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-other", claims={"email": "alice@example.com", "sub": "s"})
    resp = await cf_client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status == 401


async def test_email_not_on_allowlist_returns_403(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": "mallory@example.com", "sub": "s"})
    resp = await cf_client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status == 403
    body = await resp.json()
    assert "allowlist" in body["error"].lower()


async def test_static_path_skips_auth(cf_client: TestClient) -> None:
    # Static asset path must not require a JWT (browser fetches assets first
    # before the SSO redirect lands on every request).
    resp = await cf_client.get("/static/missing.js")
    # 404 from the static handler is fine; 401 would mean middleware ran.
    assert resp.status in (200, 404)


async def test_kid_miss_then_refresh_fails_when_kid_truly_unknown(
    cf_client: TestClient, jwks: FakeJWKS
) -> None:
    """If the JWT's kid isn't in cache, we refetch JWKS once and retry.

    FakeJWKS only serves the original kid, so a JWT with a different
    kid must fail (401) even after a refresh — the lens refetches
    JWKS but still doesn't find the rotated kid. T3.4 covers the
    success path where the rotated kid IS present in the refreshed
    JWKS."""
    # First request populates the cache with the current kid.
    t1 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"})
    r1 = await cf_client.get(_AUTH_CANARY_PATH, headers={"Cf-Access-Jwt-Assertion": t1})
    assert r1.status == 200
    # Mint with a kid the FakeJWKS doesn't actually serve. The lens
    # refetches JWKS once after the cache miss and still won't find
    # this kid — must respond 401.
    t2 = jwks.mint(
        aud="aud-test",
        claims={"email": "alice@example.com", "sub": "s"},
        kid="rotated-kid",
    )
    r2 = await cf_client.get(_AUTH_CANARY_PATH, headers={"Cf-Access-Jwt-Assertion": t2})
    assert r2.status == 401
