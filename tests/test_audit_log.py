"""Audit log: every authenticated request emits one structured line.

The format is `auth=ok principal=<…> nick=<…> method=<…> path=<…>`
on the `irc_lens.web.auth` logger. `/healthz` and `/static/*` are
bypassed by the middleware and produce no audit line."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.web import make_app

from _jwks_server import FakeJWKS


_AUTH_CANARY_PATH = "/test/auth-canary"


async def _auth_canary_handler(request: web.Request) -> web.Response:
    identity = request["identity"]
    return web.json_response({"ok": True, "principal": identity.principal})


def _cf_config_with_email(jwks: FakeJWKS, email: str) -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=(email,),
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
async def audit_client(jwks: FakeJWKS) -> AsyncIterator[TestClient]:
    config = _cf_config_with_email(jwks, "alice@example.com")

    def boom_factory(_n: str):
        raise AssertionError("session factory must not run during audit tests")

    app = make_app(config, boom_factory)
    app.router.add_get(_AUTH_CANARY_PATH, _auth_canary_handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_authenticated_request_logs_principal(
    audit_client: TestClient, jwks: FakeJWKS, caplog
) -> None:
    """One `auth=ok …` line per authenticated request, on the
    `irc_lens.web.auth` logger."""
    token = jwks.mint(
        aud="aud-test",
        claims={"email": "alice@example.com", "sub": "s-1"},
    )
    with caplog.at_level(logging.INFO, logger="irc_lens.web.auth"):
        r = await audit_client.get(
            _AUTH_CANARY_PATH,
            headers={"Cf-Access-Jwt-Assertion": token},
        )
        assert r.status == 200

    msgs = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "irc_lens.web.auth"
    ]
    assert any(
        "auth=ok" in m
        and "alice@example.com" in m
        and f"path={_AUTH_CANARY_PATH}" in m
        for m in msgs
    ), f"expected auth=ok line for alice; got: {msgs}"


async def test_healthz_does_not_emit_audit_line(
    audit_client: TestClient, caplog
) -> None:
    """`/healthz` bypasses the middleware (cloudflared probes hit it
    without a JWT), so no `auth=ok` line lands for it."""
    with caplog.at_level(logging.INFO, logger="irc_lens.web.auth"):
        r = await audit_client.get("/healthz")
        assert r.status == 200

    auth_ok_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "irc_lens.web.auth" and "auth=ok" in rec.getMessage()
    ]
    assert auth_ok_lines == [], f"expected no audit lines for /healthz; got: {auth_ok_lines}"


async def test_static_does_not_emit_audit_line(
    audit_client: TestClient, caplog
) -> None:
    """`/static/*` also bypasses the middleware → no audit line."""
    with caplog.at_level(logging.INFO, logger="irc_lens.web.auth"):
        # 404 from static handler is fine; we're checking the LOG, not status.
        await audit_client.get("/static/missing.js")

    auth_ok_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "irc_lens.web.auth" and "auth=ok" in rec.getMessage()
    ]
    assert auth_ok_lines == [], f"expected no audit lines for /static/*; got: {auth_ok_lines}"
