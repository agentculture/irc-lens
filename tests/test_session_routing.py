"""Guest-mode session routing (t10): tier -> backend, sandbox toggle, consent gate.

Two real fake-AgentIRC servers on random ports: "mesh" (the real mesh) and
"sandbox". Guest sessions must only ever touch the sandbox one.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import sys
import types
from collections.abc import AsyncIterator

import pytest

import irc_lens
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens import metrics
from irc_lens.config import LensConfig
from irc_lens.session import Session
from irc_lens.web import csrf, make_app
from irc_lens.web.identity import Identity
from irc_lens.web.sessions import SessionRegistry

_APPROVED = "alice@example.com"
_GUEST = "gus@example.org"
_SECRET = b"s" * 32
_SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _nicks(server: AgentIRCTestServer) -> list[str]:
    return [ln.params[0] for ln in server.received if ln.command == "NICK"]


def _config(jwks: FakeJWKS, mesh, sandbox, tmp_path, *, guest=True) -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=(_APPROVED,),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host=mesh.host,
        server_port=mesh.port,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=False,
        media_dir=str(tmp_path / "media"),
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
        guest_enabled=guest,
        guest_sandbox_host=sandbox.host,
        guest_sandbox_port=sandbox.port,
        guest_store_path=str(tmp_path / "guests.db"),
    )


class Env:
    def __init__(self, client, mesh, sandbox, app, jwks):
        self.client, self.mesh, self.sandbox, self.app = client, mesh, sandbox, app
        self.jwks = jwks

    @property
    def store(self):
        return self.app["guest_store"]

    def approved_headers(self) -> dict[str, str]:
        tok = self.jwks.mint(aud="aud-test", claims={"email": _APPROVED, "sub": "s"})
        return {"Cf-Access-Jwt-Assertion": tok, **_SAME_ORIGIN}

    def guest_headers(self, email: str = _GUEST) -> dict[str, str]:
        val = csrf.make_cookie_value(email, _SECRET, 3600)
        return {"Cookie": f"{csrf.GUEST_COOKIE_NAME}={val}", **_SAME_ORIGIN}

    def add_guest(self, email: str = _GUEST, nick: str = "sbx-gus") -> None:
        self.store.record_guest(email, nick, "127.0.0.1")


@pytest.fixture
def consent(monkeypatch):
    """Install a fake ``irc_lens.legal`` seam; flip ``state['ok']`` per test."""
    state = {"ok": True}
    mod = types.ModuleType("irc_lens.legal")

    async def current_legal_versions(cfg):
        return {"tos": "v1", "privacy": "v1"}

    def consent_is_current(store, email, versions):
        return state["ok"]

    mod.current_legal_versions = current_legal_versions
    mod.consent_is_current = consent_is_current
    monkeypatch.setitem(sys.modules, "irc_lens.legal", mod)
    # The real irc_lens.legal exists now (entry card task) and is bound as a
    # package attribute, which `from irc_lens import legal` prefers over
    # sys.modules — patch both so no test ever fetches the live URL.
    monkeypatch.setattr(irc_lens, "legal", mod)
    return state


@pytest.fixture
def entry_seam(monkeypatch):
    mod = types.ModuleType("irc_lens.web.entry")

    async def get_entry(request):
        return web.Response(text="ENTRY-CARD", content_type="text/html")

    mod.get_entry = get_entry
    monkeypatch.setitem(sys.modules, "irc_lens.web.entry", mod)
    import irc_lens.web as webpkg

    monkeypatch.setattr(webpkg, "entry", mod, raising=False)


@pytest_asyncio.fixture
async def env(jwks: FakeJWKS, tmp_path, consent, entry_seam, monkeypatch):
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", _SECRET.decode())
    mesh, sandbox = AgentIRCTestServer(), AgentIRCTestServer()
    await mesh.start()
    await sandbox.start()
    config = _config(jwks, mesh, sandbox, tmp_path)

    def mesh_factory(nick: str) -> Session:
        return Session(host=mesh.host, port=mesh.port, nick=nick)

    app = make_app(config, mesh_factory)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield Env(client, mesh, sandbox, app, jwks)
    finally:
        for s in app["registry"].values():
            await s.disconnect()
        await client.close()
        await mesh.stop()
        await sandbox.stop()


# -- criterion 1 / o2: tier-only routing, guests only reach the sandbox ------


async def test_guest_connects_only_to_sandbox(env: Env) -> None:
    env.add_guest()
    r = await env.client.get("/", headers=env.guest_headers())
    assert r.status == 200
    assert _nicks(env.sandbox) == ["sbx-gus"]
    assert _nicks(env.mesh) == []  # o2: the real mesh never sees a guest


async def test_guest_input_and_events_stay_in_sandbox(env: Env) -> None:
    env.add_guest()
    h = env.guest_headers()
    # (/join is refused for guests since t13's allowlist; plain chat still flows.)
    r = await env.client.post("/input", json={"text": "/me waves"}, headers=h)
    assert r.status == 204
    r = await env.client.post("/input", json={"text": "hello"}, headers=h)
    assert r.status == 204
    assert _nicks(env.sandbox) == ["sbx-gus"]
    assert env.mesh.received == []


async def test_client_params_cannot_pick_backend(env: Env) -> None:
    env.add_guest()
    h = env.guest_headers()
    params = {
        "backend": "mesh",
        "sandbox": "0",
        "host": env.mesh.host,
        "port": str(env.mesh.port),
        "tier": "approved",
    }
    r = await env.client.get("/", params=params, headers={**h, "X-Backend": "mesh"})
    assert r.status == 200
    r = await env.client.post(
        "/input",
        params=params,
        json={"text": "x", "backend": "mesh", "port": env.mesh.port},
        headers=h,
    )
    assert r.status == 204
    assert env.mesh.received == []
    assert _nicks(env.sandbox) == ["sbx-gus"]


async def test_approved_defaults_to_real_mesh(env: Env) -> None:
    r = await env.client.get("/", headers=env.approved_headers())
    assert r.status == 200
    assert _nicks(env.mesh) == ["testsrv-alice"]
    assert env.sandbox.received == []


async def test_client_param_cannot_push_approved_into_sandbox(env: Env) -> None:
    r = await env.client.get(
        "/",
        params={"sandbox": "1", "backend": "sandbox"},
        headers=env.approved_headers(),
    )
    assert r.status == 200
    assert _nicks(env.mesh) == ["testsrv-alice"]
    assert env.sandbox.received == []


async def test_anonymous_gets_entry_card_and_no_session(env: Env) -> None:
    r = await env.client.get("/")
    assert r.status == 200
    assert await r.text() == "ENTRY-CARD"
    assert env.mesh.received == [] and env.sandbox.received == []


async def test_anonymous_events_and_input_are_401(env: Env) -> None:
    assert (await env.client.get("/events")).status == 401
    assert (await env.client.post("/input", json={"text": "x"})).status == 401
    assert env.mesh.received == [] and env.sandbox.received == []


async def test_banned_or_unknown_guest_cookie_is_anonymous(env: Env) -> None:
    # unknown guest (cookie signed but no store row)
    r = await env.client.get("/", headers=env.guest_headers("nobody@x.org"))
    assert await r.text() == "ENTRY-CARD"
    env.add_guest()
    env.store.ban(email=_GUEST, reason="abuse")
    r = await env.client.get("/", headers=env.guest_headers())
    assert await r.text() == "ENTRY-CARD"
    assert env.sandbox.received == []


async def test_forged_guest_cookie_is_anonymous(env: Env) -> None:
    env.add_guest()
    val = csrf.make_cookie_value(_GUEST, b"w" * 32, 3600)
    r = await env.client.get(
        "/", headers={"Cookie": f"lens_guest={val}", **_SAME_ORIGIN}
    )
    assert await r.text() == "ENTRY-CARD"


async def test_guest_cannot_reach_real_mesh_routes(env: Env) -> None:
    env.add_guest()
    h = env.guest_headers()
    for path in ("/residents", "/owner/metrics"):
        assert (await env.client.get(path, headers=h)).status == 401
    for path in ("/sandbox/enter", "/sandbox/leave", "/upload"):
        assert (await env.client.post(path, headers=h)).status == 401
    assert env.mesh.received == []


# -- criterion 2: approved sandbox toggle round trip -------------------------


async def test_approved_toggle_round_trip(env: Env) -> None:
    h = env.approved_headers()
    assert (await env.client.get("/", headers=h)).status == 200
    assert _nicks(env.mesh) == ["testsrv-alice"]

    r = await env.client.post("/sandbox/enter", headers=h)
    assert r.status == 200 and (await r.json())["backend"] == "sandbox"
    assert (await env.client.get("/", headers=h)).status == 200
    assert _nicks(env.sandbox) == ["sbx-alice"]  # distinct sbx- nick
    assert _nicks(env.mesh) == ["testsrv-alice"]  # mesh session untouched

    r = await env.client.post("/sandbox/leave", headers=h)
    assert r.status == 200 and (await r.json())["backend"] == "mesh"
    assert (await env.client.get("/", headers=h)).status == 200
    # back on the original mesh session: no second mesh connect, no re-auth
    assert _nicks(env.mesh) == ["testsrv-alice"]
    reg = env.app["registry"]
    assert reg.has(_APPROVED, "mesh") and reg.has(_APPROVED, "sandbox")


async def test_toggle_is_per_principal_server_side(env: Env) -> None:
    await env.client.post("/sandbox/enter", headers=env.approved_headers())
    assert env.app["sandbox_toggle"] == {_APPROVED}
    await env.client.post("/sandbox/leave", headers=env.approved_headers())
    assert env.app["sandbox_toggle"] == set()


async def test_toggle_requires_csrf_proof_with_guest_cookie(env: Env) -> None:
    h = env.approved_headers()
    h.pop("Sec-Fetch-Site")
    h["Cookie"] = f"{csrf.GUEST_COOKIE_NAME}=whatever"
    r = await env.client.post("/sandbox/enter", headers=h)
    assert r.status == 403


# -- criterion 3 / o8: consent gate ------------------------------------------


async def test_guest_without_consent_redirected(env: Env, consent) -> None:
    consent["ok"] = False
    env.add_guest()
    r = await env.client.get("/", headers=env.guest_headers(), allow_redirects=False)
    assert r.status == 302
    assert r.headers["Location"] == "/consent"
    assert env.sandbox.received == []


async def test_guest_without_consent_refused_on_input_and_events(
    env: Env, consent
) -> None:
    consent["ok"] = False
    env.add_guest()
    h = env.guest_headers()
    r = await env.client.post("/input", json={"text": "hi"}, headers=h)
    assert r.status == 403 and (await r.json())["redirect"] == "/consent"
    assert (await env.client.get("/events", headers=h)).status == 403
    assert env.sandbox.received == []


async def test_consent_gate_fails_closed_when_legal_unavailable(
    env: Env, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "irc_lens.legal", None)  # import fails
    monkeypatch.delattr(irc_lens, "legal", raising=False)
    env.add_guest()
    r = await env.client.get("/", headers=env.guest_headers(), allow_redirects=False)
    assert r.status == 302
    assert env.sandbox.received == []


async def test_guest_with_consent_proceeds(env: Env) -> None:
    env.add_guest()
    r = await env.client.get("/", headers=env.guest_headers(), allow_redirects=False)
    assert r.status == 200


async def test_approved_not_subject_to_consent_gate(env: Env, consent) -> None:
    consent["ok"] = False
    r = await env.client.get("/", headers=env.approved_headers(), allow_redirects=False)
    assert r.status == 200


# -- guest bookkeeping: recorded input, metrics ------------------------------


async def test_guest_chat_recorded_and_slash_commands_not(env: Env) -> None:
    env.add_guest()
    h = env.guest_headers()
    await env.client.post("/input", json={"text": "/join #sbx"}, headers=h)
    await env.client.post("/input", json={"text": "hello sandbox"}, headers=h)
    con = sqlite3.connect(env.store.path)
    rows = con.execute("SELECT email, kind, payload FROM inputs").fetchall()
    con.close()
    assert rows == [(_GUEST, "message", "hello sandbox")]


async def test_guest_session_metrics_open_and_close(env: Env) -> None:
    before = metrics.get_metrics().snapshot()["active_guest_sessions"]
    env.add_guest()
    await env.client.get("/", headers=env.guest_headers())
    assert metrics.get_metrics().snapshot()["active_guest_sessions"] == before + 1
    await env.app["registry"].close(f"guest:{_GUEST}", "sandbox")
    assert metrics.get_metrics().snapshot()["active_guest_sessions"] == before


async def test_sandbox_agent_traffic_feeds_presence(env: Env) -> None:
    env.add_guest()
    await env.client.get("/", headers=env.guest_headers())
    session = env.app["registry"]._sessions[(f"guest:{_GUEST}", "sandbox")]
    from irc_lens.irc import Message

    msg = Message.parse(":sbx-ask!sbx-ask@h PRIVMSG #sbx :hi")
    presence = metrics.get_presence()
    presence._last_seen = None
    from irc_lens.web.sessions import _presence_listener

    assert _presence_listener in session._transport._listeners["PRIVMSG"]
    _presence_listener(msg)
    assert presence.state()["state"] == "online"


# -- o10: guest mode off ------------------------------------------------------


async def test_guest_mode_off_sandbox_routes_404_and_auth_unchanged(
    jwks: FakeJWKS, tmp_path
) -> None:
    mesh, sandbox = AgentIRCTestServer(), AgentIRCTestServer()
    await mesh.start()
    await sandbox.start()
    config = _config(jwks, mesh, sandbox, tmp_path, guest=False)
    app = make_app(
        config, lambda nick: Session(host=mesh.host, port=mesh.port, nick=nick)
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        tok = jwks.mint(aud="aud-test", claims={"email": _APPROVED, "sub": "s"})
        h = {"Cf-Access-Jwt-Assertion": tok}
        assert (await client.post("/sandbox/enter", headers=h)).status == 404
        assert (await client.post("/sandbox/leave", headers=h)).status == 404
        assert (await client.get("/")).status == 401  # unchanged
        assert "guest_store" not in app
        assert (await client.get("/", headers=h)).status == 200
        assert _nicks(mesh) == ["testsrv-alice"] and sandbox.received == []
    finally:
        for s in app["registry"].values():
            await s.disconnect()
        await client.close()
        await mesh.stop()
        await sandbox.stop()


# -- registry unit ------------------------------------------------------------


async def test_registry_keys_by_principal_and_backend() -> None:
    from unittest.mock import AsyncMock, MagicMock

    made: list[tuple[str, str]] = []

    def mk(label):
        def f(nick):
            s = MagicMock()
            s.connect = AsyncMock()
            s.wait_for_welcome = AsyncMock()
            s.disconnect = AsyncMock()
            made.append((label, nick))
            return s

        return f

    reg = SessionRegistry(mk("mesh"), sandbox_factory=mk("sandbox"))
    ident = Identity(principal="a@x", nick="srv-a", raw_jwt_subject="s")
    sbx = ident._replace(nick="sbx-a")
    m1 = await reg.get_or_open(ident)
    s1 = await reg.get_or_open(sbx, "sandbox")
    assert m1 is not s1
    assert await reg.get_or_open(ident) is m1
    assert await reg.get_or_open(sbx, "sandbox") is s1
    assert made == [("mesh", "srv-a"), ("sandbox", "sbx-a")]
    with pytest.raises(ValueError):
        await SessionRegistry(mk("mesh")).get_or_open(ident, "sandbox")


async def test_guest_session_joins_its_private_room(env: Env) -> None:
    """Each guest lands in a private #g-<nick> room the agent follows (d6).

    Guests may not /join (allowlist) and must never see each other, so the
    room is per guest and joined automatically.
    """
    env.add_guest()
    r = await env.client.get("/", headers=env.guest_headers())
    assert r.status == 200
    joins = [ln.params[0] for ln in env.sandbox.received if ln.command == "JOIN"]
    assert joins == ["#g-gus"]
    assert env.mesh.received == []


async def test_two_guests_never_share_a_room(env: Env) -> None:
    env.add_guest()
    env.add_guest(email="other@example.org", nick="sbx-ola")
    assert (await env.client.get("/", headers=env.guest_headers())).status == 200
    other = env.guest_headers(email="other@example.org")
    assert (await env.client.get("/", headers=other)).status == 200
    rooms = {
        s.nick: s.joined_channels
        for (p, b), s in zip(env.app["registry"].keys(), env.app["registry"].values())
        if b == "sandbox"
    }
    assert rooms == {"sbx-gus": {"#g-gus"}, "sbx-ola": {"#g-ola"}}


async def test_approved_guest_view_sees_every_guest_room(env: Env) -> None:
    """Guest view joins the owner's own room plus every existing guest room."""
    env.add_guest()
    assert (await env.client.get("/", headers=env.guest_headers())).status == 200
    r = await env.client.post("/sandbox/enter", headers=env.approved_headers())
    assert r.status in (200, 204)
    assert (await env.client.get("/", headers=env.approved_headers())).status == 200
    preview = [
        s for (p, b), s in zip(env.app["registry"].keys(), env.app["registry"].values())
        if b == "sandbox" and not p.startswith("guest:")
    ][0]
    assert preview.joined_channels >= {"#g-alice", "#g-gus"}
    assert preview.current_channel == "#g-alice"
