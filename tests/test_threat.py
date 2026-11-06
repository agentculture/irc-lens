"""Cheap, bounded request-path threat-signal accounting (task t6)."""

from __future__ import annotations

import socket
import sqlite3
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from irc_lens import threat


REAL_PATH = "/real"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _app(clock: FakeClock) -> web.Application:
    app = web.Application()
    app.router.add_get(REAL_PATH, _ok)
    threat.install(app, threat.ThreatCounters(clock=clock))
    return app


async def _ok(_request: web.Request) -> web.Response:
    return web.Response()


async def _denied_request(
    app: web.Application, path: str, source: str
) -> web.StreamResponse:
    request = make_mocked_request(
        "GET",
        path,
        app=app,
        headers={"CF-Connecting-IP": source},
    )
    request._match_info = await app.router.resolve(request)
    request._match_info.add_app(app)
    denied = AsyncMock(return_value=web.Response(status=401))
    response = await threat.threat_middleware(request, denied)
    denied.assert_awaited_once()
    return response


async def test_unrouted_401_is_a_probe_but_real_route_401_is_not() -> None:
    clock = FakeClock()
    app = _app(clock)

    missing = await _denied_request(app, "/made-up", "192.0.2.1")
    real = await _denied_request(app, REAL_PATH, "192.0.2.2")

    snapshot = app[threat.THREAT_COUNTERS].snapshot()
    assert missing.status == real.status == 401
    assert snapshot.counts[threat.Signal.REQUESTS] == 2
    assert snapshot.counts[threat.Signal.REFUSED_RESPONSES] == 2
    assert snapshot.counts[threat.Signal.PROBES] == 1


async def test_ten_thousand_requests_keep_fixed_bounded_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    app = _app(clock)
    counters = app[threat.THREAT_COUNTERS]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("request counting must not use a database or network")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)

    for number in range(10_000):
        await _denied_request(app, "/scan", f"198.51.{number // 256}.{number % 256}")

    snapshot = counters.snapshot()
    assert snapshot.counts[threat.Signal.REQUESTS] == 10_000
    assert snapshot.distinct_sources == threat.DISTINCT_SOURCE_CAP
    assert counters.state_slots == counters.max_state_slots
    assert snapshot.source_count_capped is True
    assert "198.51" not in repr(counters.__dict__)


async def test_window_reset_discards_counts_and_source_hashes() -> None:
    clock = FakeClock()
    app = _app(clock)
    counters = app[threat.THREAT_COUNTERS]
    await _denied_request(app, "/scan", "203.0.113.7")

    clock.now += threat.WINDOW_SECONDS
    snapshot = counters.snapshot()

    assert all(value == 0 for value in snapshot.counts.values())
    assert snapshot.distinct_sources == 0
    assert counters.state_slots == len(threat.Signal)


async def test_entry_posts_and_http_exceptions_are_counted() -> None:
    clock = FakeClock()
    app = web.Application()
    app.router.add_post("/entry/code", _ok)
    threat.install(app, threat.ThreatCounters(clock=clock))
    request = make_mocked_request("POST", "/entry/code", app=app)
    request._match_info = await app.router.resolve(request)
    request._match_info.add_app(app)

    async def refuse(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPForbidden()

    with pytest.raises(web.HTTPForbidden):
        await threat.threat_middleware(request, refuse)

    snapshot = app[threat.THREAT_COUNTERS].snapshot()
    assert snapshot.counts[threat.Signal.ENTRY_POSTS] == 1
    assert snapshot.counts[threat.Signal.REFUSED_RESPONSES] == 1
