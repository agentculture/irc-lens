"""End-to-end tests for ``GET /residents`` (plan task t4).

Drives the real aiohttp app against the in-tree
:class:`~tests._culture_server.FakeCultureServer` — the spec's hard
requirement is that every upstream state renders HTTP 200 with a
kind-specific notice, never an error page
(docs/specs/2026-07-07-residents-presence-page.md, c8/c13/h1/h12).

The session factory used here raises on call: ``/residents`` reads
culture's HTTP surface only and must never open an AgentIRC session.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.web import make_app

from _culture_server import FakeCultureServer
from helpers import DEV_CONFIG

# The pinned notice texts are a rendering contract (spec h1) — asserted
# verbatim here, independently of the render module's own constants, so
# an accidental rewording fails loudly.
NOTICE_UNSUPPORTED = "Presence is pending the agentirc release (agentirc#53)."
NOTICE_UNREACHABLE = "IRCd down: the culture server is unreachable."
NOTICE_UNAVAILABLE = (
    "Resource view unavailable: the culture overview server is not "
    "reachable or not configured."
)


def _forbidden_factory(_nick: str):
    raise AssertionError("/residents must never open an AgentIRC session")


@pytest_asyncio.fixture
async def culture() -> AsyncIterator[FakeCultureServer]:
    server = FakeCultureServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _client_for(config: LensConfig) -> TestClient:
    client = TestClient(TestServer(make_app(config, _forbidden_factory)))
    await client.start_server()
    return client


async def _get_residents(config: LensConfig) -> tuple[int, str]:
    client = await _client_for(config)
    try:
        resp = await client.get("/residents")
        return resp.status, await resp.text()
    finally:
        await client.close()


def _config_for(culture: FakeCultureServer) -> LensConfig:
    return dataclasses.replace(DEV_CONFIG, culture_residents_url=culture.residents_url)


@pytest.mark.asyncio
async def test_supported_renders_table_sorted_by_nick(culture) -> None:
    culture.serve_supported(
        [
            {
                "nick": "zeta-codex",
                "server": "zeta",
                "state": "idle",
                "since": None,
                "task": None,
                "tokens_in": None,
                "tokens_out": None,
                "presumed_hung": False,
                "last_refresh": None,
                "token_budget": None,
                "budget_used_pct": None,
                "budget_warning": None,
            },
            {
                "nick": "alpha-claude",
                "server": "alpha",
                "state": "thinking",
                "since": None,
                "task": None,
                "tokens_in": None,
                "tokens_out": None,
                "presumed_hung": False,
                "last_refresh": None,
                "token_budget": None,
                "budget_used_pct": None,
                "budget_warning": None,
            },
        ]
    )
    status, body = await _get_residents(_config_for(culture))
    assert status == 200
    assert 'data-testid="residents-table"' in body
    assert body.index("alpha-claude") < body.index("zeta-codex")


@pytest.mark.asyncio
async def test_unsupported_renders_pending_notice(culture) -> None:
    culture.serve_unsupported()
    status, body = await _get_residents(_config_for(culture))
    assert status == 200
    assert 'data-testid="residents-notice"' in body
    assert NOTICE_UNSUPPORTED in body


@pytest.mark.asyncio
async def test_upstream_503_renders_ircd_down_notice(culture) -> None:
    culture.serve_unreachable()
    status, body = await _get_residents(_config_for(culture))
    assert status == 200
    assert 'data-testid="residents-notice"' in body
    assert NOTICE_UNREACHABLE in body
    assert "503" not in body


@pytest.mark.asyncio
async def test_upstream_503_garbled_body_still_ircd_down(culture) -> None:
    culture.serve_unreachable_garbled()
    status, body = await _get_residents(_config_for(culture))
    assert status == 200
    assert NOTICE_UNREACHABLE in body


@pytest.mark.asyncio
async def test_endpoint_down_renders_unavailable_notice(culture) -> None:
    config = _config_for(culture)
    await culture.stop()  # connection refused from here on
    status, body = await _get_residents(config)
    assert status == 200
    assert 'data-testid="residents-notice"' in body
    assert NOTICE_UNAVAILABLE in body


@pytest.mark.asyncio
async def test_unconfigured_discovery_renders_unavailable_notice() -> None:
    # No explicit URL, and an overview name whose port file cannot exist:
    # discovery resolves to None and the page degrades, no error.
    config = dataclasses.replace(
        DEV_CONFIG,
        culture_residents_url=None,
        culture_overview_name="no-such-overview-xyzzy",
    )
    status, body = await _get_residents(config)
    assert status == 200
    assert NOTICE_UNAVAILABLE in body


@pytest.mark.asyncio
async def test_upstream_500_renders_unavailable_notice(culture) -> None:
    culture.serve_internal_error()
    status, body = await _get_residents(_config_for(culture))
    assert status == 200
    assert NOTICE_UNAVAILABLE in body
    assert "500" not in body


@pytest.mark.asyncio
async def test_upstream_non_json_renders_unavailable_notice(culture) -> None:
    culture.serve_non_json()
    status, body = await _get_residents(_config_for(culture))
    assert status == 200
    assert NOTICE_UNAVAILABLE in body


@pytest.mark.asyncio
async def test_upstream_url_never_leaks_into_page(culture) -> None:
    culture.serve_supported()
    status, body = await _get_residents(_config_for(culture))
    assert status == 200
    assert "127.0.0.1" not in body
    assert "/residents.json" not in body
    assert str(culture.port) not in body


@pytest.mark.asyncio
async def test_cf_mode_rejects_unauthenticated_request(culture) -> None:
    # /residents is on NEITHER middleware exempt list: in
    # cloudflare-access mode a request without a JWT is turned away at
    # the door, exactly like the console root (spec c15/h5/h14). No
    # JWKS fetch happens on the missing-token path, so dummy CF values
    # suffice.
    config = dataclasses.replace(
        _config_for(culture),
        auth_mode="cloudflare-access",
        cf_aud="test-aud",
        cf_team_domain="testteam.cloudflareaccess.com",
        allowed_emails=("dev@local",),
    )
    client = await _client_for(config)
    try:
        resp = await client.get("/residents")
        assert resp.status == 401
        payload = await resp.json()
        assert set(payload) == {"error", "hint"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_only_get_is_registered(culture) -> None:
    # Read-only surface (spec c9/h9): no mutation path exists.
    client = await _client_for(_config_for(culture))
    try:
        resp = await client.post("/residents")
        assert resp.status == 405
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_malformed_supported_payload_still_renders_200_notice(culture) -> None:
    # supported:true with a garbage residents shape must degrade to the
    # unavailable notice, never a 500 (PR #54 review, comment
    # 3533556372 — the never-an-error-page contract).
    culture.serve_supported(residents="garbage")
    status, body = await _get_residents(_config_for(culture))
    assert status == 200
    assert NOTICE_UNAVAILABLE in body
