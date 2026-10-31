"""t12: agent credentials and MCP endpoint authentication.

Acceptance criteria (plan t12):

* a signed-in human (any tier) can issue their agent credential from the
  app; it is shown once and stored only as a hash; issuing again replaces
  the old one and closes the old agent's connection (o1);
* the MCP endpoint rejects missing, expired or revoked credentials before
  any tool runs (o8);
* each request re-checks the human is still approved, not banned and not
  deleted (o16);
* the credential's bound human alone decides tier and grants; request
  parameters cannot change scope (o11).

Plus the integration facts: only the MCP mount is exempt from the browser
identity middleware (a bearer on any other route still gets 401), and the
CSRF middleware keeps refusing a cross-site Origin there.

Every address and host below is an example placeholder.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
import pytest_asyncio
from agentfront import current_caller
from agentfront.app import App
from aiohttp.test_utils import TestClient, TestServer
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens._errors import AfiError
from irc_lens.agent_nick import agent_nick_for
from irc_lens.config import (
    DEFAULT_AGENT_CREDENTIAL_TTL_S,
    load_config,
)
from irc_lens.session import Session
from irc_lens.web import agent_auth, app_session, csrf, make_app, routes
from irc_lens.web.identity import derive_nick
from irc_lens.web.sessions import BACKEND_MESH, BACKEND_SANDBOX
from test_session_routing import _config, consent  # noqa: F401 - fixture

ADMIN = "alice@example.com"
MEMBER = "bob@example.org"
GUEST = "gus@example.org"
GUEST_NICK = "sbx-gus"
SECRET = b"s" * 32
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
MCP_PATH = "/mcp"
_TOKEN_RE = re.compile(r'data-testid="agent-token"[^>]*>([A-Za-z0-9_-]+)<')


class FakeAgentConn:
    """Stands in for t13's lens-held agent IRC connection."""

    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class Env:
    def __init__(self, client, app, jwks, mesh, sandbox, calls):
        self.client, self.app, self.jwks = client, app, jwks
        self.mesh, self.sandbox, self.calls = mesh, sandbox, calls

    @property
    def access(self):
        return self.app[app_session.ACCESS_STORE]

    @property
    def guests(self):
        return self.app["guest_store"]

    def jwt(self, email: str) -> dict[str, str]:
        tok = self.jwks.mint(aud="aud-test", claims={"email": email, "sub": "s"})
        return {"Cf-Access-Jwt-Assertion": tok, **SAME_ORIGIN}

    def guest(self, email: str = GUEST) -> dict[str, str]:
        val = csrf.make_cookie_value(email, SECRET, 3600)
        return {"Cookie": f"{csrf.GUEST_COOKIE_NAME}={val}", **SAME_ORIGIN}

    def url(self, path: str = MCP_PATH) -> str:
        return str(self.client.make_url(path))

    async def issue(self, headers: dict[str, str]) -> str:
        r = await self.client.post(
            "/agent-credential", headers=headers, allow_redirects=False
        )
        assert r.status == 200, await r.text()
        found = _TOKEN_RE.search(await r.text())
        assert found, "issued token not shown"
        return found.group(1)

    async def rpc(self, headers: dict[str, str] | None = None, path: str = MCP_PATH):
        """One raw JSON-RPC tools/call; returns (status, parsed body)."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run", "arguments": {"command": ["whoami"], "args": {}}},
        }
        hdrs = {"Accept": "application/json, text/event-stream", **(headers or {})}
        r = await self.client.post(path, json=body, headers=hdrs)
        text = await r.text()
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, text

    async def whoami(
        self, token: str, args: dict | None = None, **extra_headers: str
    ) -> dict:
        """Stock MCP client: initialize + tools/call with a bearer token."""
        headers = {"Authorization": f"Bearer {token}", **extra_headers}
        async with streamablehttp_client(self.url(), headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(
                    "run", {"command": ["whoami"], "args": args or {}}
                )
        return json.loads(result.content[0].text)


def _counting_front(calls: list[str]):
    """The real agent front, with its tools wrapped to count executions."""
    real = agent_auth.build_agent_front

    def build(_app=None) -> App:
        front = App(name="agent-test", version="0.0.0", description="t12 test front")

        @front.tool
        def whoami() -> dict:
            """Report the authenticated agent principal."""
            calls.append("whoami")
            return agent_auth.describe_principal(current_caller())

        assert real  # the production builder exists
        return front

    return build


@pytest_asyncio.fixture
async def env(
    jwks: FakeJWKS, tmp_path, consent, monkeypatch  # noqa: F811
) -> AsyncIterator[Env]:
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", SECRET.decode())
    calls: list[str] = []
    monkeypatch.setattr(agent_auth, "build_agent_front", _counting_front(calls))
    mesh, sandbox = AgentIRCTestServer(), AgentIRCTestServer()
    await mesh.start()
    await sandbox.start()
    cfg = dataclasses.replace(
        _config(jwks, mesh, sandbox, tmp_path),
        access_enabled=True,
        allowed_emails=(ADMIN,),
        mcp_path=MCP_PATH,
    )
    app = make_app(
        cfg,
        lambda nick: Session(host=mesh.host, port=mesh.port, nick=nick),
        ban_sweep_interval_s=3600.0,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    e = Env(client, app, jwks, mesh, sandbox, calls)
    e.access.set_user(MEMBER, role="member", state="approved", actor=None)
    e.guests.record_guest(GUEST, GUEST_NICK, "127.0.0.1")
    try:
        yield e
    finally:
        await client.close()
        await mesh.stop()
        await sandbox.stop()


def _fake_agent(env: Env, key: str, backend: str = BACKEND_MESH) -> FakeAgentConn:
    conn = FakeAgentConn()
    principal = agent_auth.agent_registry_principal(key)
    env.app["registry"]._sessions[(principal, backend)] = conn  # noqa: SLF001
    return conn


# -- nick scheme (one function, t13 may refine) -----------------------------------


def test_agent_nick_keeps_server_prefix_and_ties_to_human() -> None:
    assert agent_nick_for("testsrv-bob") == "testsrv-pa_-bob"
    assert agent_nick_for(GUEST_NICK) == "sbx-pa_-gus"
    assert agent_auth.agent_nick_for is agent_nick_for  # one scheme, one place


@pytest.mark.parametrize(
    "email", ["pa_-bob@example.org", "pa-bob@example.org", "pa_bob@example.org"]
)
def test_agent_nick_never_equals_a_derived_human_nick(email: str) -> None:
    human = derive_nick("testsrv", email)
    assert agent_nick_for("testsrv-bob") != human
    # Derived mesh nicks never contain "_"; guest nicks never contain "-"
    # after the server prefix. An agent nick always has both.
    assert "_" not in human
    assert "_" in agent_nick_for("testsrv-bob")


def test_agent_nick_refuses_a_nick_without_server_prefix() -> None:
    for bad in ("nodash", "-x", "srv-"):
        with pytest.raises(ValueError):
            agent_nick_for(bad)


# -- TTL from config ------------------------------------------------------------------


def _cfg_file(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        "auth:\n  mode: dev\n  dev:\n    nick: tester\n    email: tester@example.com\n"
        + extra
        + "server:\n  name: spark\n  host: irc.example.org\n  port: 6667\n"
    )
    return p


def test_ttl_default_is_named_and_overridable(tmp_path: Path) -> None:
    assert DEFAULT_AGENT_CREDENTIAL_TTL_S > 0
    assert load_config(_cfg_file(tmp_path)).agent_credential_ttl_s == (
        DEFAULT_AGENT_CREDENTIAL_TTL_S
    )
    cfg = load_config(
        _cfg_file(
            tmp_path,
            "  email_permissions:\n    enabled: false\n"
            "    agent_credential_ttl_days: 7\n",
        )
    )
    assert cfg.agent_credential_ttl_s == 7 * 86400


@pytest.mark.parametrize("bad", ["0", "-3", "soon"])
def test_ttl_must_be_positive(tmp_path: Path, bad: str) -> None:
    with pytest.raises(AfiError):
        load_config(
            _cfg_file(
                tmp_path,
                f"  email_permissions:\n    agent_credential_ttl_days: {bad}\n",
            )
        )


# -- issue from the app (o1) ------------------------------------------------------------


@pytest.mark.parametrize("who", ["admin", "member", "guest"])
async def test_any_signed_in_tier_can_issue(env: Env, who: str) -> None:
    headers = {"admin": env.jwt(ADMIN), "member": env.jwt(MEMBER)}.get(who) or (
        env.guest()
    )
    page = await env.client.get("/agent-credential", headers=headers)
    assert page.status == 200
    assert "Issue" in await page.text()
    token = await env.issue(headers)
    assert env.access.verify_agent_credential(token) is not None


async def test_anonymous_cannot_issue(env: Env) -> None:
    r = await env.client.post("/agent-credential", headers=SAME_ORIGIN)
    assert r.status == 401
    r = await env.client.get("/agent-credential")
    assert r.status == 401


async def test_unapproved_email_cannot_issue(env: Env) -> None:
    r = await env.client.post(
        "/agent-credential", headers=env.jwt("mallory@example.net")
    )
    assert r.status == 401
    assert env.access.active_credential_count("mallory@example.net") == 0


async def test_cross_site_issue_refused(env: Env) -> None:
    headers = {**env.jwt(MEMBER), "Origin": "https://evil.example.net"}
    r = await env.client.post("/agent-credential", headers=headers)
    assert r.status == 403
    assert env.access.active_credential_count(MEMBER) == 0


async def test_token_shown_once_and_stored_only_as_hash(env: Env) -> None:
    token = await env.issue(env.jwt(MEMBER))
    raw = Path(env.access.path).read_bytes()
    assert token.encode() not in raw
    with closing(sqlite3.connect(env.access.path)) as con:
        rows = con.execute("SELECT token_hash, nick FROM agent_credentials").fetchall()
    assert len(rows) == 1 and rows[0][0] != token
    assert rows[0][1] == agent_auth.agent_nick_for(derive_nick("testsrv", MEMBER))
    # Not shown again: a later page view carries no token.
    page = await env.client.get("/agent-credential", headers=env.jwt(MEMBER))
    text = await page.text()
    assert token not in text and 'data-testid="agent-token"' not in text
    r = await env.client.post("/agent-credential", headers=env.jwt(MEMBER))
    assert r.headers.get("Cache-Control") == "no-store"


async def test_issue_again_replaces_and_closes_old_agent(env: Env) -> None:
    old = await env.issue(env.jwt(MEMBER))
    conn = _fake_agent(env, MEMBER)
    other = _fake_agent(env, ADMIN)
    new = await env.issue(env.jwt(MEMBER))
    assert new != old
    assert env.access.verify_agent_credential(old) is None
    assert env.access.verify_agent_credential(new) is not None
    assert env.access.active_credential_count(MEMBER) == 1
    assert conn.disconnected
    assert not env.app["registry"].has(
        agent_auth.agent_registry_principal(MEMBER), BACKEND_MESH
    )
    assert not other.disconnected  # someone else's agent is untouched


async def test_guest_reissue_closes_guest_agent(env: Env) -> None:
    await env.issue(env.guest())
    conn = _fake_agent(env, agent_auth.GUEST_KEY_PREFIX + GUEST, BACKEND_SANDBOX)
    await env.issue(env.guest())
    assert conn.disconnected


async def test_guest_agent_takes_no_guest_seat(env: Env) -> None:
    await env.issue(env.guest())
    _fake_agent(env, agent_auth.GUEST_KEY_PREFIX + GUEST, BACKEND_SANDBOX)
    assert env.app["registry"].active_guest_count() == 0


async def test_index_links_to_agent_page(env: Env) -> None:
    r = await env.client.get("/", headers=env.jwt(MEMBER))
    assert 'href="/agent-credential"' in await r.text()


# -- endpoint authentication (o8) --------------------------------------------------------


async def test_valid_credential_reaches_tool(env: Env) -> None:
    token = await env.issue(env.jwt(MEMBER))
    payload = await env.whoami(token)
    assert payload["result"] == {
        "nick": agent_auth.agent_nick_for(derive_nick("testsrv", MEMBER)),
        "tier": "approved",
    }
    assert env.calls == ["whoami"]


@pytest.mark.parametrize(
    "auth",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Bearer not-a-real-credential-but-long-enough",
        "Bearer has spaces in it",
    ],
)
async def test_missing_or_bad_credential_refused_before_tool(env: Env, auth) -> None:
    headers = {} if auth is None else {"Authorization": auth}
    status, body = await env.rpc(headers)
    assert status == 401
    assert body["error"]["code"] == 401
    assert env.calls == []


async def test_expired_credential_refused(env: Env) -> None:
    token = env.access.issue_agent_credential(
        MEMBER, "testsrv-pa_-bob", ttl=0, actor=None
    )
    status, _ = await env.rpc({"Authorization": f"Bearer {token}"})
    assert status == 401
    assert env.calls == []


async def test_revoked_credential_refused(env: Env) -> None:
    token = await env.issue(env.jwt(MEMBER))
    env.access.revoke_agent_credential(MEMBER, actor=None)
    status, _ = await env.rpc({"Authorization": f"Bearer {token}"})
    assert status == 401
    assert env.calls == []


async def test_replaced_credential_refused(env: Env) -> None:
    old = await env.issue(env.jwt(MEMBER))
    await env.issue(env.jwt(MEMBER))
    status, _ = await env.rpc({"Authorization": f"Bearer {old}"})
    assert status == 401
    assert env.calls == []


async def test_token_in_query_string_is_not_accepted(env: Env) -> None:
    token = await env.issue(env.jwt(MEMBER))
    status, _ = await env.rpc(path=f"{MCP_PATH}?access_token={token}")
    assert status == 401
    assert env.calls == []


async def test_browser_identity_does_not_authenticate_endpoint(env: Env) -> None:
    status, _ = await env.rpc(env.jwt(ADMIN))
    assert status == 401
    assert env.calls == []


# -- per-request re-check of the human (o16) ---------------------------------------------


async def test_removed_user_cut_off_and_agent_closed(env: Env) -> None:
    token = await env.issue(env.jwt(MEMBER))
    assert (await env.rpc({"Authorization": f"Bearer {token}"}))[0] == 200
    conn = _fake_agent(env, MEMBER)
    env.access.set_user(MEMBER, role="member", state="removed", actor=None)
    status, _ = await env.rpc({"Authorization": f"Bearer {token}"})
    assert status == 401
    assert conn.disconnected
    assert env.calls == ["whoami"]


async def test_reapproval_is_rechecked_not_cached(env: Env) -> None:
    token = await env.issue(env.jwt(MEMBER))
    env.access.set_user(MEMBER, role="member", state="removed", actor=None)
    assert (await env.rpc({"Authorization": f"Bearer {token}"}))[0] == 401
    env.access.set_user(MEMBER, role="member", state="approved", actor=None)
    assert (await env.rpc({"Authorization": f"Bearer {token}"}))[0] == 200


async def test_banned_guest_cut_off(env: Env) -> None:
    token = await env.issue(env.guest())
    assert (await env.rpc({"Authorization": f"Bearer {token}"}))[0] == 200
    conn = _fake_agent(env, agent_auth.GUEST_KEY_PREFIX + GUEST, BACKEND_SANDBOX)
    env.guests.ban(email=GUEST)
    assert (await env.rpc({"Authorization": f"Bearer {token}"}))[0] == 401
    assert conn.disconnected


async def test_guest_banned_by_caller_ip_cut_off(env: Env) -> None:
    token = await env.issue(env.guest())
    env.guests.ban(ip="203.0.113.9")
    status, _ = await env.rpc(
        {"Authorization": f"Bearer {token}", "CF-Connecting-IP": "203.0.113.9"}
    )
    assert status == 401


async def test_deleted_guest_cut_off(env: Env) -> None:
    token = await env.issue(env.guest())
    env.guests.delete_guest_inputs(GUEST)
    assert env.guests.get_guest(GUEST) == []
    status, _ = await env.rpc({"Authorization": f"Bearer {token}"})
    assert status == 401


async def test_sweep_closes_agent_after_out_of_band_revoke(env: Env) -> None:
    await env.issue(env.jwt(MEMBER))
    await env.issue(env.jwt(ADMIN))
    revoked = _fake_agent(env, MEMBER)
    live = _fake_agent(env, ADMIN)
    # e.g. `irc-lens access agents revoke` from another process (t8).
    env.access.revoke_agent_credential(MEMBER, actor=None)
    closed = await agent_auth.sweep_once(env.app)
    assert closed == [agent_auth.agent_registry_principal(MEMBER)]
    assert revoked.disconnected and not live.disconnected


async def test_sweep_closes_agent_of_banned_guest(env: Env) -> None:
    await env.issue(env.guest())
    conn = _fake_agent(env, agent_auth.GUEST_KEY_PREFIX + GUEST, BACKEND_SANDBOX)
    env.guests.ban(email=GUEST)
    await agent_auth.sweep_once(env.app)
    assert conn.disconnected


# -- scope comes only from the bound human (o11) -----------------------------------------


async def test_three_tiers_three_scopes_on_one_endpoint(env: Env) -> None:
    tokens = {
        "admin": await env.issue(env.jwt(ADMIN)),
        "member": await env.issue(env.jwt(MEMBER)),
        "guest": await env.issue(env.guest()),
    }
    got = {k: (await env.whoami(t))["result"] for k, t in tokens.items()}
    assert got["admin"]["tier"] == "approved"
    assert got["member"]["tier"] == "approved"
    assert got["guest"] == {"nick": "sbx-pa_-gus", "tier": "guest"}
    assert len({g["nick"] for g in got.values()}) == 3


async def test_request_parameters_cannot_change_scope(env: Env) -> None:
    token = await env.issue(env.guest())
    payload = await env.whoami(
        token,
        **{
            "X-Lens-Email": ADMIN,
            "X-Lens-Tier": "approved",
            "Cf-Access-Jwt-Assertion": env.jwt(ADMIN)["Cf-Access-Jwt-Assertion"],
        },
    )
    assert payload["result"] == {"nick": "sbx-pa_-gus", "tier": "guest"}
    # Tool arguments cannot name a scope either: whoami takes none.
    calls_before = list(env.calls)
    payload = await env.whoami(token, {"tier": "approved", "email": ADMIN})
    assert "result" not in payload
    assert env.calls == calls_before
    status, body = await env.rpc(
        {"Authorization": f"Bearer {token}"},
        path=f"{MCP_PATH}?email={ADMIN}&tier=approved",
    )
    assert status == 200
    assert json.loads(body["result"]["content"][0]["text"])["result"]["tier"] == (
        "guest"
    )


async def test_guest_key_never_resolves_to_approved_grants(env: Env) -> None:
    """A guest whose email equals an approved user's stays a guest agent."""
    env.guests.record_guest(MEMBER, "sbx-bobby", "127.0.0.1")
    token = await env.issue(env.guest(MEMBER))
    principal = await agent_auth.authenticate_token(env.app, token, ip="127.0.0.1")
    assert principal is not None and principal.tier == "guest"
    assert principal.credential_key == agent_auth.GUEST_KEY_PREFIX + MEMBER
    # The approved user's own (absent) credential is untouched by the guest's.
    assert env.access.active_credential_count(MEMBER) == 0


async def test_agent_principal_never_passes_admin_gate(env: Env) -> None:
    token = await env.issue(env.jwt(ADMIN))
    principal = await agent_auth.authenticate_token(env.app, token, ip="")
    assert principal is not None
    assert not principal.is_admin
    assert routes.admin_only_denial(principal, access_enabled=True) is not None


# -- only the MCP mount bypasses the browser identity middleware --------------------------


@pytest.mark.parametrize(
    "path", ["/", "/admin", "/owner/metrics", "/events", "/residents", "/mcp-other"]
)
async def test_bearer_alone_does_not_open_other_routes(env: Env, path: str) -> None:
    """A bearer is no browser identity: the route answers as for nobody."""
    token = await env.issue(env.jwt(ADMIN))
    bare = await env.client.get(path)
    r = await env.client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert r.status == bare.status
    text = await r.text()
    assert 'data-testid="tier-badge"' not in text  # never the console
    if path != "/":
        assert r.status in (401, 403)  # "/" shows the anonymous entry card


async def test_trailing_slash_mount_is_authenticated_too(env: Env) -> None:
    token = await env.issue(env.jwt(MEMBER))
    assert (await env.rpc(path=MCP_PATH + "/"))[0] == 401
    status, _ = await env.rpc({"Authorization": f"Bearer {token}"}, MCP_PATH + "/")
    assert status == 200


async def test_csrf_still_refuses_cross_site_origin_on_mcp(env: Env) -> None:
    token = await env.issue(env.jwt(MEMBER))
    status, _ = await env.rpc(
        {"Authorization": f"Bearer {token}", "Origin": "https://evil.example.net"}
    )
    assert status == 403
    assert env.calls == []


async def test_endpoint_absent_when_feature_off(
    jwks: FakeJWKS, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", SECRET.decode())
    mesh = AgentIRCTestServer()
    await mesh.start()
    cfg = _config(jwks, mesh, mesh, tmp_path)
    app = make_app(
        cfg,
        lambda nick: Session(host=mesh.host, port=mesh.port, nick=nick),
        ban_sweep_interval_s=3600.0,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.post(MCP_PATH, json={})
        assert r.status in (401, 404)
        r = await client.get("/agent-credential")
        assert r.status in (401, 404)
        assert agent_auth.MCP_ROUTES not in app
    finally:
        await client.close()
        await mesh.stop()


async def test_dev_mode_mcp_path_carries_no_browser_identity(tmp_path: Path) -> None:
    """The dev middleware also leaves the MCP mount alone (no identity)."""
    cfg = load_config(_cfg_file(tmp_path))
    seen = {}
    app = make_app(cfg, lambda nick: Session(host="127.0.0.1", port=1, nick=nick))

    async def probe(request):
        seen["identity"] = request.get("identity")
        from aiohttp import web

        return web.Response(text="ok")

    # Pretend the probe is the MCP mount: exemption keys on the mount's routes.
    resource = app.router.add_resource("/probe-mcp")
    route = resource.add_route("GET", probe)
    app[agent_auth.MCP_ROUTES] = frozenset({route})
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.get("/probe-mcp")
        assert r.status == 200
        assert seen["identity"] is None
    finally:
        await client.close()


def test_production_front_exposes_no_identityless_tools() -> None:
    """The endpoint serves the agent front, never cli.build_app()'s live tools."""
    module = agent_auth  # no env fixture here, so nothing is monkeypatched
    tools = module.build_agent_front().list_tools()
    names = sorted(getattr(t, "name", t) for t in tools)
    # t13's real agent tools; none of them is a tools.py (dev path) function.
    assert names == ["channels", "join", "part", "read", "send", "whoami"]
    from irc_lens import tools as dev_tools

    assert not any(t.func.__module__ == dev_tools.__name__ for t in tools)
    principal = module.AgentPrincipal(
        credential_key=MEMBER, email=MEMBER, tier="approved", nick="n", token="t"
    )
    assert module.describe_principal(principal) == {"nick": "n", "tier": "approved"}
    assert module.describe_principal({"email": ADMIN}) == {}
    assert "token" not in repr(principal)
