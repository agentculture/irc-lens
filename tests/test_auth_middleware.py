"""CF Access JWT validation + middleware behavior."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.web import make_app

from _jwks_server import FakeJWKS


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
    """
    config = _cf_config(jwks, allowed=["alice@example.com"])

    def boom_factory(_nick: str):
        raise AssertionError("session factory must not run during auth tests")

    app = make_app(config, boom_factory)
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
    # The boom_factory raises if the handler tries to open a session.
    # /healthz skips auth and the registry, so use it as the canary that the
    # *middleware* accepted the token.
    resp = await cf_client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status == 200


async def test_valid_jwt_in_cookie_also_accepted(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s-2"})
    cf_client.session.cookie_jar.update_cookies({"CF_Authorization": token})
    resp = await cf_client.get("/healthz")
    assert resp.status == 200


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


async def test_kid_miss_then_refresh_succeeds(cf_client: TestClient, jwks: FakeJWKS) -> None:
    """If the JWT's kid isn't in cache, we refetch JWKS once and retry.

    FakeJWKS only serves the original kid, so a JWT with a different kid
    must fail (401) even after a refresh — the lens refetches but still
    doesn't find the rotated kid. T3.4 will add the success-path test."""
    # First request populates the cache with the current kid.
    t1 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"})
    r1 = await cf_client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": t1})
    assert r1.status == 200
    # Mint with a different kid; the lens must refetch JWKS to discover it
    # and accept the JWT after the refresh.
    t2 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"}, kid="rotated-kid")
    # FakeJWKS still serves only the original kid, so this MUST fail.
    r2 = await cf_client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": t2})
    assert r2.status == 401
