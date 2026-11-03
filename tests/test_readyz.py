"""Public readiness probes use real TCP listeners and never open IRC sessions."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from helpers import DEV_CONFIG
from irc_lens.web import make_app, routes


@asynccontextmanager
async def listener(port=0):
    connections = []
    closed = asyncio.Event()

    async def accept(reader, writer):
        connections.append(writer)
        try:
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(accept, "127.0.0.1", port)
    state = SimpleNamespace(
        server=server, port=server.sockets[0].getsockname()[1],
        connections=connections, closed=closed,
    )
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        for writer in connections:
            writer.close()
            await writer.wait_closed()


@asynccontextmanager
async def client_for(port, **kwargs):
    config = replace(DEV_CONFIG, server_host="127.0.0.1", server_port=port, **kwargs)

    def boom_factory(_nick):
        raise AssertionError("readiness must not create an IRC session")

    async with TestClient(TestServer(make_app(config, boom_factory))) as client:
        yield client


async def assert_result(client, ok):
    response = await client.get("/readyz")
    assert response.status == (200 if ok else 503)
    assert await response.text() == ('{"ok": true}' if ok else '{"ok": false}')
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" not in response.headers


async def test_readyz_open_closes_connection():
    async with listener() as tcp, client_for(tcp.port) as client:
        await assert_result(client, True)
        await asyncio.wait_for(tcp.closed.wait(), 1)
        assert len(tcp.connections) == 1


async def test_readyz_closed_port_is_opaque_and_logs(caplog):
    async with listener() as tcp:
        tcp.server.close()
        await tcp.server.wait_closed()
        async with client_for(tcp.port) as client:
            with caplog.at_level(logging.WARNING, logger=routes.__name__):
                await assert_result(client, False)
    assert any("IRC readiness probe failed:" in r.message and r.levelno == logging.WARNING
               for r in caplog.records)


async def test_readyz_cf_access_without_jwt(jwks):
    async with listener() as tcp, client_for(
        tcp.port, auth_mode="cloudflare-access", dev_nick=None, dev_email=None,
        cf_aud="aud-test", cf_team_domain=jwks.team_domain,
        allowed_emails=("alice@example.com",),
    ) as client:
        await assert_result(client, True)
        assert not client.session.cookie_jar


async def test_readyz_cache_success_and_failure(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: now[0]))
    async with listener() as tcp, client_for(tcp.port) as client:
        await assert_result(client, True)
        tcp.server.close()
        await tcp.server.wait_closed()
        now[0] += 4.999
        await assert_result(client, True)
        assert len(tcp.connections) == 1
        now[0] = 105.0
        await assert_result(client, False)
        async with listener(tcp.port) as reopened:
            now[0] = 109.999
            await assert_result(client, False)
            assert len(reopened.connections) == 0
            now[0] = 110.0
            await assert_result(client, True)
            assert len(reopened.connections) == 1


async def test_readyz_concurrent_requests_share_probe():
    async with listener() as tcp, client_for(tcp.port) as client:
        await asyncio.gather(*(assert_result(client, True) for _ in range(20)))
        assert len(tcp.connections) == 1


async def test_readyz_uses_main_server_not_sandbox():
    async with listener() as main, listener() as sandbox:
        main.server.close()
        await main.server.wait_closed()
        async with client_for(main.port, guest_sandbox_host="127.0.0.1",
                              guest_sandbox_port=sandbox.port) as client:
            await assert_result(client, False)
        assert len(sandbox.connections) == 0
