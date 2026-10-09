"""Guest-mode observability (t8): counters, agent presence, owner route."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _jwks_server import FakeJWKS
from irc_lens import metrics as metrics_mod
from irc_lens.metrics import AGENT_OFFLINE_AFTER_S, AgentPresence, Metrics
from irc_lens.web import make_app
from irc_lens.web.identity import TIER_GUEST, Identity
from test_auth_tiers import _APPROVED, _boom_factory, _config


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# --- criterion 1: counters -------------------------------------------------


def test_counters_and_snapshot() -> None:
    m = Metrics()
    assert m.snapshot() == {
        "entries": 0,
        "active_guest_sessions": 0,
        "rate_limited_429": 0,
        "failed_sign_ins": 0,
        "agent_errors": 0,
    }
    m.entry()
    m.entry()
    m.rate_limited()
    m.failed_sign_in()
    m.agent_error()
    m.session_opened()
    m.session_opened()
    m.session_closed()
    snap = m.snapshot()
    assert snap["entries"] == 2
    assert snap["rate_limited_429"] == 1
    assert snap["failed_sign_ins"] == 1
    assert snap["agent_errors"] == 1
    assert snap["active_guest_sessions"] == 1


def test_active_sessions_gauge_never_negative() -> None:
    m = Metrics()
    m.session_closed()
    assert m.snapshot()["active_guest_sessions"] == 0


def test_counters_thread_safe() -> None:
    m = Metrics()

    def work() -> None:
        for _ in range(1000):
            m.entry()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m.snapshot()["entries"] == 8000


def test_process_wide_singleton() -> None:
    assert metrics_mod.get_metrics() is metrics_mod.get_metrics()
    assert metrics_mod.get_presence() is metrics_mod.get_presence()


# --- criterion 2: agent absence within 60 s --------------------------------


def test_presence_unknown_before_any_sighting() -> None:
    p = AgentPresence(clock=FakeClock())
    state = p.state()
    assert state["state"] == "unknown"
    assert state["online"] is False


def test_presence_online_then_offline_within_60s() -> None:
    clock = FakeClock()
    p = AgentPresence(clock=clock)
    assert p.nick == "sbx-ask"
    p.seen("sbx-ask")
    assert p.state()["state"] == "online"
    clock.t += AGENT_OFFLINE_AFTER_S - 1
    assert p.state()["state"] == "online"
    clock.t += 1
    state = p.state()
    assert AGENT_OFFLINE_AFTER_S <= 60
    assert state["state"] == "offline"
    assert state["online"] is False
    assert state["last_seen_age_s"] == AGENT_OFFLINE_AFTER_S
    p.seen("sbx-ask")
    assert p.state()["state"] == "online"


def test_presence_ignores_other_nicks_and_case() -> None:
    p = AgentPresence(clock=FakeClock())
    p.seen("somebody-else")
    assert p.state()["state"] == "unknown"
    p.seen("SBX-Ask")
    assert p.state()["state"] == "online"


def test_presence_gone_marks_offline_immediately() -> None:
    p = AgentPresence(clock=FakeClock())
    p.seen("sbx-ask")
    p.gone("sbx-ask")
    assert p.state()["state"] == "offline"


# --- owner-only route ------------------------------------------------------


@pytest_asyncio.fixture
async def guest_app(jwks: FakeJWKS) -> AsyncIterator[TestClient]:
    app = make_app(_config(jwks, guest=True), _boom_factory)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_route_approved_gets_snapshot_and_agent(
    guest_app: TestClient, jwks: FakeJWKS
) -> None:
    metrics_mod.get_metrics().entry()
    metrics_mod.get_presence().seen("sbx-ask")
    token = jwks.mint(aud="aud-test", claims={"email": _APPROVED, "sub": "s"})
    resp = await guest_app.get(
        "/owner/metrics", headers={"Cf-Access-Jwt-Assertion": token}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["counters"]["entries"] >= 1
    assert set(body["counters"]) == {
        "entries",
        "active_guest_sessions",
        "rate_limited_429",
        "failed_sign_ins",
        "agent_errors",
    }
    assert body["agent"]["state"] == "online"
    assert body["agent"]["nick"] == "sbx-ask"


async def test_route_anonymous_denied(guest_app: TestClient) -> None:
    resp = await guest_app.get("/owner/metrics")
    assert resp.status == 401
    assert "counters" not in await resp.text()


async def test_route_guest_tier_denied() -> None:
    from irc_lens.web.routes import get_owner_metrics

    class Req(dict):
        pass

    req = Req(identity=Identity("g", "sbx-g", "", tier=TIER_GUEST))
    resp = await get_owner_metrics(req)  # type: ignore[arg-type]
    assert isinstance(resp, web.Response)
    assert resp.status == 401


def test_route_not_marked_allows_anonymous() -> None:
    from irc_lens.web.routes import get_owner_metrics

    assert not getattr(get_owner_metrics, "_irc_lens_allows_anonymous", False)
