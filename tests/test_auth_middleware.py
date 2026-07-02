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
        media_enabled=True,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )


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


@pytest.mark.slow
async def test_rotated_kid_accepted_after_refresh(
    cf_client: TestClient, jwks: FakeJWKS
) -> None:
    """JWT signed with a NEW key after rotation must validate
    post-refresh.

    Without the wait, the lens's anti-flood window (5 s) would
    reject the second request without re-fetching JWKS. Sleeping
    past the window forces the lens to actually re-fetch and
    discover the rotated key.
    """
    import asyncio

    # Warm cache with the original kid.
    t1 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"})
    r1 = await cf_client.get(_AUTH_CANARY_PATH, headers={"Cf-Access-Jwt-Assertion": t1})
    assert r1.status == 200

    # Sleep past the 5 s anti-flood window so the next miss triggers
    # a real refetch instead of an immediate KeyError.
    await asyncio.sleep(5.1)

    # Rotate FakeJWKS, mint with the new key, expect success after
    # the lens refetches and discovers the new kid.
    jwks.rotate("kid-after-rotation")
    t2 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"})
    r2 = await cf_client.get(_AUTH_CANARY_PATH, headers={"Cf-Access-Jwt-Assertion": t2})
    assert r2.status == 200


async def test_warm_jwks_succeeds_against_live_server(jwks: FakeJWKS) -> None:
    """`warm_jwks(config)` is the startup fail-fast contract. With the
    FakeJWKS server live, it must complete without raising."""
    from irc_lens.web.auth import warm_jwks

    config = _cf_config(jwks, allowed=["alice@example.com"])
    await warm_jwks(config)  # no raise = success


async def test_warm_jwks_raises_when_server_unreachable(jwks: FakeJWKS) -> None:
    """If the JWKS endpoint can't be reached, `warm_jwks` raises a
    `ClientError` (which `serve.py` wraps as `AfiError(EXIT_ENV_ERROR)`)."""
    import aiohttp

    from irc_lens.web.auth import warm_jwks

    config = _cf_config(jwks, allowed=["alice@example.com"])
    await jwks.stop()  # tear down the JWKS server before warming
    with pytest.raises(aiohttp.ClientError):
        await warm_jwks(config)


async def test_jwks_unreachable_mid_request_returns_502(jwks: FakeJWKS) -> None:
    """If the JWKS server is reachable at startup but goes away before
    the first request triggers a refresh, the middleware must surface
    a 502, not crash with an unhandled exception."""
    config = _cf_config(jwks, allowed=["alice@example.com"])

    def boom_factory(_n: str):
        raise AssertionError("session factory must not run during auth tests")

    app = make_app(config, boom_factory)
    app.router.add_get(_AUTH_CANARY_PATH, _auth_canary_handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # Tear down the JWKS server BEFORE the first request, so the
        # cache is empty and the refresh attempt fails with ClientError.
        await jwks.stop()
        token = jwks.mint(
            aud="aud-test",
            claims={"email": "alice@example.com", "sub": "s"},
        )
        r = await client.get(
            _AUTH_CANARY_PATH,
            headers={"Cf-Access-Jwt-Assertion": token},
        )
        assert r.status == 502
        body = await r.json()
        assert "could not reach Cloudflare JWKS" in body["error"]
    finally:
        await client.close()


async def test_jwks_timeout_returns_502(jwks: FakeJWKS, monkeypatch) -> None:
    """`aiohttp.ClientTimeout` raises `asyncio.TimeoutError`, which is
    NOT a subclass of `aiohttp.ClientError`. The middleware must catch
    both and surface a 502 — without it, a slow JWKS endpoint would
    return an unhandled 500 (Qodo PR #34 review)."""
    import asyncio
    from irc_lens.web import auth as auth_module

    config = _cf_config(jwks, allowed=["alice@example.com"])

    def boom_factory(_n: str):
        raise AssertionError("session factory must not run during auth tests")

    app = make_app(config, boom_factory)
    app.router.add_get(_AUTH_CANARY_PATH, _auth_canary_handler)

    # Patch the JWKSCache._refresh to raise asyncio.TimeoutError.
    async def fake_refresh(self):
        raise asyncio.TimeoutError("simulated JWKS slow response")

    monkeypatch.setattr(auth_module._JWKSCache, "_refresh", fake_refresh)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        token = jwks.mint(
            aud="aud-test",
            claims={"email": "alice@example.com", "sub": "s"},
        )
        r = await client.get(
            _AUTH_CANARY_PATH,
            headers={"Cf-Access-Jwt-Assertion": token},
        )
        assert r.status == 502
        body = await r.json()
        assert "could not reach Cloudflare JWKS" in body["error"]
        assert "TimeoutError" in body["hint"]
    finally:
        await client.close()


async def test_jwks_concurrent_misses_share_one_refresh(jwks: FakeJWKS) -> None:
    """Concurrent `get_key` calls with the same unknown kid must NOT
    each fire a separate JWKS fetch — single-flight via the lock means
    one refresh, then the others see the populated cache (Copilot
    PR #34 thundering-herd review)."""
    import asyncio
    from irc_lens.web.auth import _JWKSCache

    cache = _JWKSCache(jwks.team_domain)
    fetch_count = 0
    original_refresh = cache._refresh

    async def counting_refresh():
        nonlocal fetch_count
        fetch_count += 1
        await original_refresh()

    cache._refresh = counting_refresh
    # Fire 5 concurrent get_key calls for the kid the FakeJWKS is
    # serving. Without the lock they would each call _refresh; with
    # it, exactly one call fetches.
    results = await asyncio.gather(
        *(cache.get_key("test-kid-1") for _ in range(5))
    )
    assert all(r is not None for r in results)
    assert fetch_count == 1, (
        f"expected exactly 1 JWKS refresh under contention, got {fetch_count}"
    )


async def test_build_cloudflare_middleware_invalid_mode_raises_afierror(
    jwks: FakeJWKS,
) -> None:
    """If a caller bypasses load_config and hands in a non-CF mode,
    the build-time guard must raise AfiError (so the dispatcher
    renders error/hint), not a bare ValueError that would land in the
    catch-all `file a bug` arm (Qodo PR #34 review)."""
    from irc_lens._errors import EXIT_USER_ERROR, AfiError
    from irc_lens.web.auth import build_cloudflare_middleware

    bad_config = LensConfig(
        auth_mode="dev",  # wrong for build_cloudflare_middleware
        dev_nick="x",
        dev_email="x@x",
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=True,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )
    with pytest.raises(AfiError) as exc:
        build_cloudflare_middleware(bad_config)
    assert exc.value.code == EXIT_USER_ERROR

    # Also: missing aud / team_domain in CF mode → AfiError, not ValueError.
    incomplete = LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud=None,         # missing
        cf_team_domain=None, # missing
        allowed_emails=("a@example.com",),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=True,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )
    with pytest.raises(AfiError) as exc2:
        build_cloudflare_middleware(incomplete)
    assert exc2.value.code == EXIT_USER_ERROR
    assert "auth.cloudflare.aud" in exc2.value.message


async def test_service_token_common_name_accepted(jwks: FakeJWKS) -> None:
    """A JWT with `common_name` (no `email`) is accepted iff CN is in
    auth.allowed_service_tokens. A rogue CN gets 403 from the
    service-token allowlist branch."""
    config = LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=(),
        allowed_service_tokens=("ci-bot",),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=True,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )

    def boom_factory(_n: str):
        raise AssertionError("session factory must not run during auth tests")

    app = make_app(config, boom_factory)
    app.router.add_get(_AUTH_CANARY_PATH, _auth_canary_handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # Allowed common_name → middleware accepts, principal echoes back.
        token = jwks.mint(
            aud="aud-test",
            claims={"common_name": "ci-bot", "sub": "svc"},
        )
        r = await client.get(
            _AUTH_CANARY_PATH,
            headers={"Cf-Access-Jwt-Assertion": token},
        )
        assert r.status == 200
        body = await r.json()
        assert body == {"ok": True, "principal": "ci-bot"}

        # Rogue common_name (NOT in allowed_service_tokens) → 403.
        bad = jwks.mint(
            aud="aud-test",
            claims={"common_name": "rogue", "sub": "svc"},
        )
        r2 = await client.get(
            _AUTH_CANARY_PATH,
            headers={"Cf-Access-Jwt-Assertion": bad},
        )
        assert r2.status == 403
        body2 = await r2.json()
        assert "service token" in body2["error"]
        assert "rogue" in body2["error"]
    finally:
        await client.close()
