"""Spec contract tests for the ``/healthz`` endpoint.

Per the Phase 4 plan (T4.2):
1. ``/healthz`` returns HTTP 200 with body ``{"ok": true}`` and NOTHING ELSE.
2. No IRC state leak (opaque response).
3. Available unauthenticated in BOTH dev mode AND cloudflare-access mode.

The auth middleware's bypass for ``/healthz`` is a hard contract — this test
guards against future regression of that bypass.
"""
from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

import pytest_asyncio

from irc_lens.config import LensConfig
from irc_lens.web import make_app

from _jwks_server import FakeJWKS


async def test_healthz_returns_only_ok(lens_client: TestClient) -> None:
    """In dev mode, ``/healthz`` returns 200 with exact response ``{"ok": true}``.

    No IRC state, no extra fields, no allowlist leak — the response is opaque.
    Asserts exact JSON equality to catch accidental field additions in the future.
    """
    r = await lens_client.get("/healthz")
    assert r.status == 200
    body = await r.json()
    assert body == {"ok": True}


async def test_healthz_without_auth_in_cf_mode_works(jwks: FakeJWKS) -> None:
    """In cloudflare-access mode, ``/healthz`` bypasses auth and returns 200.

    No JWT header, no JWT cookie, no auth middleware validation — the
    response must still be 200 with ``{"ok": true}``. The session factory
    is never invoked, proving the middleware doesn't require auth to reach
    this endpoint.
    """
    # Build a CF-mode config pointed at the test JWKS server.
    config = LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=("alice@example.com",),
        allowed_service_tokens=(),
        server_name="spark",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
    )

    # Factory that raises if invoked — /healthz must not trigger session creation.
    def boom_factory(_nick: str):
        raise AssertionError("session factory must not run for /healthz")

    app = make_app(config, boom_factory)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # Hit /healthz with no JWT header and no JWT cookie.
        r = await client.get("/healthz")
        assert r.status == 200
        body = await r.json()
        assert body == {"ok": True}
    finally:
        await client.close()
