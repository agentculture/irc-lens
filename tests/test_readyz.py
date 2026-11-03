"""Public readiness probes use real TCP listeners and never open IRC sessions."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, Mock

from _readyz_server import listener
from aiohttp.test_utils import TestClient, TestServer

from helpers import DEV_CONFIG
from irc_lens.web import make_app, routes


@asynccontextmanager
async def client_for(port, host="127.0.0.1", **kwargs):
    config = replace(DEV_CONFIG, server_host=host, server_port=port, **kwargs)

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


async def test_readyz_silent_server_times_out(monkeypatch):
    monkeypatch.setattr(routes.ReadinessProbe, "TIMEOUT", 0.05)
    async with listener(reply=None) as tcp, client_for(tcp.port) as client:
        await asyncio.wait_for(assert_result(client, False), 1)
        await asyncio.wait_for(tcp.closed.wait(), 1)


async def test_readyz_eof_without_pong():
    async with listener(reply=b"") as tcp, client_for(tcp.port) as client:
        await assert_result(client, False)


async def test_readyz_junk_then_pong():
    async with listener(reply=b"NOTICE junk\r\n:spark PONG spark readyz\r\n") as tcp:
        async with client_for(tcp.port) as client:
            await assert_result(client, True)


async def test_readyz_invalid_host_is_opaque():
    async with client_for(6667, host="foo..bar") as client:
        await assert_result(client, False)


@pytest.mark.parametrize("failure", [OSError("refused"), UnicodeError("bad host")])
async def test_probe_failed_results_cached(monkeypatch, failure):
    now = [100.0]
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: now[0]))
    connect = AsyncMock(side_effect=failure)
    monkeypatch.setattr(routes.asyncio, "open_connection", connect)
    probe = routes.ReadinessProbe("foo..bar", 6667)
    assert await probe.check() is False
    now[0] = 104.999
    assert await probe.check() is False
    assert connect.await_count == 1
    now[0] = 105.0
    assert await probe.check() is False
    assert connect.await_count == 2


async def test_probe_logs_only_state_changes(monkeypatch, caplog):
    reader = asyncio.StreamReader()
    reader.feed_data(b"PONG\r\n" * 3)
    writer = Mock(drain=AsyncMock())
    connect = AsyncMock(side_effect=[
        (reader, writer), OSError("refused"), OSError("still refused"),
        (reader, writer), (reader, writer), OSError("down again"),
    ])
    monkeypatch.setattr(routes.asyncio, "open_connection", connect)
    probe = routes.ReadinessProbe("localhost", 6667)
    with caplog.at_level(logging.INFO, logger=routes.__name__):
        for expected, count in [(True, 0), (False, 1), (False, 1),
                                (True, 2), (True, 2), (False, 3)]:
            probe._expires_at = 0
            assert await probe.check() is expected
            assert len(caplog.records) == count
    assert [r.levelno for r in caplog.records] == [
        logging.WARNING, logging.INFO, logging.WARNING,
    ]
    assert "refused" in caplog.records[0].message
    assert "recovered" in caplog.records[1].message


@pytest.mark.parametrize("reply", [b"PONG\r\n", b"", None, b"junk\r\n", b"x" * 70000])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_probe_closes_and_caches_every_outcome(monkeypatch, reply, close_fails):
    reader = asyncio.StreamReader()
    if reply is not None:
        reader.feed_data(reply)
        if not reply:
            reader.feed_eof()
    writer = Mock(drain=AsyncMock())
    if close_fails:
        writer.close.side_effect = RuntimeError("close failed")
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(routes.asyncio, "open_connection", connect)
    monkeypatch.setattr(routes.ReadinessProbe, "TIMEOUT", 0.01)
    probe = routes.ReadinessProbe("localhost", 6667)
    expected = reply == b"PONG\r\n"
    assert await probe.check() is expected
    writer.write.assert_called_once_with(b"PING :readyz\r\n")
    writer.close.assert_called_once()
    assert await probe.check() is expected
    assert connect.await_count == 1
