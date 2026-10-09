"""Legal-version fetch + consent currency (guest mode, task t9 seam).

``irc_lens.legal.current_legal_versions(cfg)`` fetches
``cfg.guest_legal_version_url`` (JSON ``{"terms","privacy","effective"}``)
with a short timeout and an in-process cache; ``consent_is_current`` tells
the sandbox routing task whether a guest's latest consent matches.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from irc_lens import legal
from irc_lens.config import LensConfig
from irc_lens.guest_store import GuestStore

VERSIONS = {"terms": "2026-10-01", "privacy": "2026-09-15", "effective": "2026-10-01"}


def _cfg(url: str) -> LensConfig:
    return LensConfig(
        auth_mode="dev",
        dev_nick="d",
        dev_email="d@example.com",
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="t",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=False,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
        guest_enabled=True,
        guest_legal_version_url=url,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    legal.clear_cache()
    yield
    legal.clear_cache()


async def _serve(payload, hits: list[int], status: int = 200) -> TestServer:
    async def handler(_request):
        hits.append(1)
        if isinstance(payload, (dict, list)):
            return web.json_response(payload, status=status)
        return web.Response(text=payload, status=status)

    app = web.Application()
    app.router.add_get("/version.json", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_fetches_and_caches_versions():
    hits: list[int] = []
    server = await _serve(VERSIONS, hits)
    try:
        cfg = _cfg(str(server.make_url("/version.json")))
        assert await legal.current_legal_versions(cfg) == VERSIONS
        assert await legal.current_legal_versions(cfg) == VERSIONS
        assert len(hits) == 1  # in-process cache
    finally:
        await server.close()


async def test_cache_expires(monkeypatch):
    hits: list[int] = []
    server = await _serve(VERSIONS, hits)
    now = [1000.0]
    monkeypatch.setattr(legal, "_monotonic", lambda: now[0])
    try:
        cfg = _cfg(str(server.make_url("/version.json")))
        await legal.current_legal_versions(cfg)
        now[0] += legal.CACHE_TTL_S + 1
        await legal.current_legal_versions(cfg)
        assert len(hits) == 2
    finally:
        await server.close()


async def test_stale_cache_served_when_fetch_fails(monkeypatch):
    hits: list[int] = []
    server = await _serve(VERSIONS, hits)
    now = [1000.0]
    monkeypatch.setattr(legal, "_monotonic", lambda: now[0])
    cfg = _cfg(str(server.make_url("/version.json")))
    await legal.current_legal_versions(cfg)
    await server.close()
    now[0] += legal.CACHE_TTL_S + 1
    assert await legal.current_legal_versions(cfg) == VERSIONS


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"terms": "1"}, 200),  # privacy missing
        ({"terms": 1, "privacy": "2"}, 200),  # not a string
        ("not json", 200),
        (VERSIONS, 500),
    ],
)
async def test_bad_document_without_cache_raises(payload, status):
    server = await _serve(payload, [], status=status)
    try:
        cfg = _cfg(str(server.make_url("/version.json")))
        with pytest.raises(legal.LegalVersionsUnavailable):
            await legal.current_legal_versions(cfg)
    finally:
        await server.close()


async def test_unreachable_raises():
    cfg = _cfg("http://127.0.0.1:9/version.json")
    with pytest.raises(legal.LegalVersionsUnavailable):
        await legal.current_legal_versions(cfg)


def test_consent_is_current(tmp_path):
    store = GuestStore(tmp_path / "g.db")
    email = "g@example.com"
    assert not legal.consent_is_current(store, email, VERSIONS)
    store.record_consent(email, "1.2.3.4", tos_version="old", privacy_version="old")
    assert not legal.consent_is_current(store, email, VERSIONS)
    store.record_consent(
        email,
        "1.2.3.4",
        tos_version=VERSIONS["terms"],
        privacy_version=VERSIONS["privacy"],
    )
    assert legal.consent_is_current(store, email, VERSIONS)
    # A new terms version invalidates the old consent.
    assert not legal.consent_is_current(
        store, email, {**VERSIONS, "terms": "2027-01-01"}
    )
    assert not legal.consent_is_current(store, "other@example.com", VERSIONS)
