"""t15: fail-closed switches, refusal audit wiring, metrics, log hygiene.

Switch states of ``auth.email_permissions`` (o17):

* ``enabled: true``                          -> enforced
* ``enabled: false``, access store never initialised -> legacy allowlist
* ``enabled: false``, access store initialised       -> admins-only (fail closed)
* ``agents_enabled: false``                  -> kill switch: no agent call or
  credential gets through, in any state

Every address and host below is an example placeholder.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens import config as cfgmod
from irc_lens import metrics as lens_metrics
from irc_lens.access_store import AccessStore
from irc_lens.config import load_config
from irc_lens.session import Session
from irc_lens.web import agent_auth, app_session, make_app
from test_agent_auth import (  # noqa: F401 - fixtures + helpers
    ADMIN,
    GUEST,
    MCP_PATH,
    MEMBER,
    SAME_ORIGIN,
    SECRET,
    FakeAgentConn,
    consent,
)
from test_session_routing import _config

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


# -- config: switch names and states ------------------------------------------------


def _cfg_file(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        "auth:\n  mode: dev\n  dev:\n    nick: tester\n    email: tester@example.com\n"
        + extra
        + "server:\n  name: spark\n  host: irc.example.org\n  port: 6667\n"
    )
    return p


def test_agents_switch_defaults_on_and_parses(tmp_path: Path) -> None:
    assert load_config(_cfg_file(tmp_path)).agents_enabled is True
    off = load_config(
        _cfg_file(
            tmp_path,
            "  email_permissions:\n    enabled: true\n    agents_enabled: false\n",
        )
    )
    assert off.agents_enabled is False


def test_agents_switch_must_be_a_bool(tmp_path: Path) -> None:
    from irc_lens._errors import AfiError

    with pytest.raises(AfiError):
        load_config(
            _cfg_file(tmp_path, "  email_permissions:\n    agents_enabled: maybe\n")
        )


def _base(tmp_path: Path, **kw) -> cfgmod.LensConfig:
    class _J:  # minimal stand-ins for the shared _config helper
        team_domain = "team.example.com"

    class _S:
        host, port = "127.0.0.1", 6667

    cfg = _config(_J(), _S(), _S(), tmp_path)
    return dataclasses.replace(cfg, **kw)


def _init_store(path: Path) -> AccessStore:
    store = AccessStore(path)
    store.import_admins([ADMIN])
    store.set_user(MEMBER, role="member", state="approved", actor=None)
    return store


def test_state_legacy_when_store_never_initialised(tmp_path: Path) -> None:
    cfg = cfgmod.resolve_access(_base(tmp_path, access_enabled=False))
    assert cfg.access_mode == cfgmod.ACCESS_LEGACY
    assert cfg.access_enabled is False


def test_state_legacy_for_a_plain_guest_database(tmp_path: Path) -> None:
    # An existing guest database (no access tables, or an empty import
    # ledger) is not "migrated".
    AccessStore(tmp_path / "guests.db")  # schema only, ledger empty
    cfg = cfgmod.resolve_access(_base(tmp_path, access_enabled=False))
    assert cfg.access_mode == cfgmod.ACCESS_LEGACY
    plain = tmp_path / "plain.db"
    sqlite3.connect(plain).execute("create table t (x)").connection.commit()
    cfg = cfgmod.resolve_access(
        _base(tmp_path, access_enabled=False, guest_store_path=str(plain))
    )
    assert cfg.access_mode == cfgmod.ACCESS_LEGACY


def test_state_admins_only_after_migration(tmp_path: Path) -> None:
    _init_store(tmp_path / "guests.db")
    cfg = cfgmod.resolve_access(_base(tmp_path, access_enabled=False))
    assert cfg.access_mode == cfgmod.ACCESS_ADMINS_ONLY
    assert cfg.access_enabled is True  # the store keeps deciding
    assert cfg.agents_enabled is False  # and no agent runs


def _install(cfg: cfgmod.LensConfig) -> AccessStore:
    from aiohttp import web

    app = web.Application()
    app["config"] = cfg
    store = app_session.install_access(app)
    assert store is not None
    return store


def test_first_enforced_start_with_no_imports_still_initialises(
    tmp_path: Path,
) -> None:
    """Finding 9: the admin was created by the CLI and allowed_emails was
    empty on the first enforced start. The store must still count as
    initialised: a later start never imports allowed_emails as admins, and
    switching the feature off is admins-only, not the legacy allowlist."""
    db = tmp_path / "guests.db"
    AccessStore(db).set_user(ADMIN, role="admin", state="approved", actor=None)
    _install(_base(tmp_path, access_enabled=True, allowed_emails=()))
    # Restart with an address in the (retired) list: it is not imported.
    store = _install(_base(tmp_path, access_enabled=True, allowed_emails=(MEMBER,)))
    assert store.get_user(MEMBER) is None
    assert store.admin_count() == 1
    # Feature off afterwards: fail closed to admins-only.
    cfg = cfgmod.resolve_access(
        _base(tmp_path, access_enabled=False, allowed_emails=(MEMBER,))
    )
    assert cfg.access_mode == cfgmod.ACCESS_ADMINS_ONLY


def test_a_refused_first_start_does_not_initialise(tmp_path: Path) -> None:
    """With no admin at all the first start is refused and the store stays
    uninitialised, so fixing allowed_emails and restarting still imports."""
    from irc_lens._errors import AfiError

    with pytest.raises(AfiError):
        _install(_base(tmp_path, access_enabled=True, allowed_emails=()))
    store = _install(_base(tmp_path, access_enabled=True, allowed_emails=(ADMIN,)))
    assert store.get_user(ADMIN)["role"] == "admin"


def test_state_admins_only_when_the_store_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / "guests.db").write_bytes(b"this is not a sqlite database" * 20)
    cfg = cfgmod.resolve_access(_base(tmp_path, access_enabled=False))
    assert cfg.access_mode == cfgmod.ACCESS_ADMINS_ONLY


def test_state_enforced_when_on(tmp_path: Path) -> None:
    _init_store(tmp_path / "guests.db")
    cfg = cfgmod.resolve_access(_base(tmp_path, access_enabled=True))
    assert cfg.access_mode == cfgmod.ACCESS_ENFORCED
    assert cfg.agents_enabled is True


def test_kill_switch_survives_resolution(tmp_path: Path) -> None:
    cfg = cfgmod.resolve_access(
        _base(tmp_path, access_enabled=True, agents_enabled=False)
    )
    assert cfg.agents_enabled is False


# -- app harness ----------------------------------------------------------------------


class Env:
    def __init__(self, client, app, jwks, mesh):
        self.client, self.app, self.jwks, self.mesh = client, app, jwks, mesh

    @property
    def access(self) -> AccessStore:
        return self.app[app_session.ACCESS_STORE]

    def jwt(self, email: str) -> dict[str, str]:
        tok = self.jwks.mint(aud="aud-test", claims={"email": email, "sub": "s"})
        return {"Cf-Access-Jwt-Assertion": tok, **SAME_ORIGIN}

    async def rpc(self, headers: dict[str, str]):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run", "arguments": {"command": ["whoami"], "args": {}}},
        }
        hdrs = {"Accept": "application/json, text/event-stream", **headers}
        r = await self.client.post(MCP_PATH, json=body, headers=hdrs)
        return r.status

    def audit_actions(self) -> list[str]:
        return [row[2] for row in self.access.audit_rows()]


async def _start(jwks, tmp_path, monkeypatch, *, preinit: bool = False, **over):
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", SECRET.decode())
    mesh = AgentIRCTestServer()
    await mesh.start()
    if preinit:
        _init_store(tmp_path / "guests.db")
    cfg = dataclasses.replace(
        _config(jwks, mesh, mesh, tmp_path),
        mcp_path=MCP_PATH,
        **{"allowed_emails": (ADMIN,), **over},
    )
    app = make_app(
        cfg,
        lambda nick: Session(host=mesh.host, port=mesh.port, nick=nick),
        ban_sweep_interval_s=3600.0,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return Env(client, app, jwks, mesh)


@pytest_asyncio.fixture
async def killed(
    jwks: FakeJWKS, tmp_path, consent, monkeypatch
) -> AsyncIterator[Env]:  # noqa: F811
    """Permissions on, agent kill switch thrown, a credential already issued."""
    e = await _start(
        jwks, tmp_path, monkeypatch, access_enabled=True, agents_enabled=False
    )
    e.access.set_user(MEMBER, role="member", state="approved", actor=None)
    try:
        yield e
    finally:
        await e.client.close()
        await e.mesh.stop()


@pytest_asyncio.fixture
async def live(
    jwks: FakeJWKS, tmp_path, consent, monkeypatch
) -> AsyncIterator[Env]:  # noqa: F811
    e = await _start(jwks, tmp_path, monkeypatch, access_enabled=True)
    e.access.set_user(MEMBER, role="member", state="approved", actor=None)
    try:
        yield e
    finally:
        await e.client.close()
        await e.mesh.stop()


@pytest_asyncio.fixture
async def after_migration(
    jwks: FakeJWKS, tmp_path, consent, monkeypatch  # noqa: F811
) -> AsyncIterator[Env]:
    """Switch off, but the access store was initialised earlier."""
    e = await _start(
        jwks,
        tmp_path,
        monkeypatch,
        preinit=True,
        access_enabled=False,
        allowed_emails=(ADMIN, MEMBER),  # the retired allowlist must not matter
    )
    try:
        yield e
    finally:
        await e.client.close()
        await e.mesh.stop()


# -- kill switch (o17) --------------------------------------------------------------


async def test_kill_switch_refuses_a_live_credential(killed: Env) -> None:
    token = killed.access.issue_agent_credential(
        MEMBER, "testsrv-pa_-bob", ttl=3600, actor=None
    )
    assert await killed.rpc({"Authorization": f"Bearer {token}"}) == 401


async def test_kill_switch_refuses_every_credential_tier(killed: Env) -> None:
    tokens = [
        killed.access.issue_agent_credential(k, "n", ttl=3600, actor=None)
        for k in (ADMIN, MEMBER, f"guest:{GUEST}")
    ]
    for tok in tokens:
        assert await killed.rpc({"Authorization": f"Bearer {tok}"}) == 401
    assert await killed.rpc({}) == 401


async def test_kill_switch_blocks_issuing(killed: Env) -> None:
    for method in (killed.client.get, killed.client.post):
        r = await method(
            "/agent-credential", headers=killed.jwt(MEMBER), allow_redirects=False
        )
        assert r.status == 403
    assert killed.access.active_credential_count(MEMBER) == 0


async def test_kill_switch_sweep_closes_every_agent(killed: Env) -> None:
    key = agent_auth.agent_registry_principal(MEMBER)
    conn = FakeAgentConn()
    killed.app["registry"]._sessions[(key, "mesh")] = conn  # noqa: SLF001
    killed.access.issue_agent_credential(MEMBER, "n", ttl=3600, actor=None)
    closed = await agent_auth.sweep_once(killed.app)
    assert closed == [key]
    assert conn.disconnected


async def test_without_the_kill_switch_a_credential_works(live: Env) -> None:
    token = live.access.issue_agent_credential(MEMBER, "n", ttl=3600, actor=None)
    assert await live.rpc({"Authorization": f"Bearer {token}"}) == 200


# -- permissions disabled after migration: admins only (o17) --------------------------


async def test_disabled_after_migration_refuses_non_admin_the_mesh(
    after_migration: Env,
) -> None:
    r = await after_migration.client.get(
        "/residents", headers=after_migration.jwt(MEMBER)
    )
    assert r.status in (401, 403)


async def test_disabled_after_migration_admin_still_reaches_the_console(
    after_migration: Env,
) -> None:
    r = await after_migration.client.get(
        "/residents", headers=after_migration.jwt(ADMIN)
    )
    assert r.status == 200


async def test_disabled_after_migration_no_fallback_to_config_allowlist(
    after_migration: Env,
) -> None:
    # The retired config allowlist must not re-admit anyone: a member who is
    # also listed there is still refused.
    assert MEMBER in after_migration.app["config"].allowed_emails
    assert app_session.approved_user(after_migration.app, MEMBER) is None
    assert app_session.approved_user(after_migration.app, ADMIN) is not None
    assert app_session.can_sign_in(after_migration.app, MEMBER) is False


async def test_disabled_after_migration_agents_are_refused(
    after_migration: Env,
) -> None:
    token = after_migration.access.issue_agent_credential(
        ADMIN, "n", ttl=3600, actor=None
    )
    assert await after_migration.rpc({"Authorization": f"Bearer {token}"}) == 401


async def test_disabled_before_migration_keeps_the_legacy_allowlist(
    jwks: FakeJWKS, tmp_path, consent, monkeypatch  # noqa: F811
) -> None:
    e = await _start(
        jwks,
        tmp_path,
        monkeypatch,
        access_enabled=False,
        allowed_emails=(ADMIN, MEMBER),
    )
    try:
        assert app_session.ACCESS_STORE not in e.app
        for who in (ADMIN, MEMBER):
            r = await e.client.get("/residents", headers=e.jwt(who))
            assert r.status == 200
        r = await e.client.get("/residents", headers=e.jwt("eve@example.net"))
        assert r.status == 401
    finally:
        await e.client.close()
        await e.mesh.stop()


# -- audit ------------------------------------------------------------------------------


async def test_refused_agent_calls_are_audited_without_the_email(killed: Env) -> None:
    token = killed.access.issue_agent_credential(MEMBER, "n", ttl=3600, actor=None)
    await killed.rpc({"Authorization": f"Bearer {token}"})
    await killed.rpc({})
    rows = [r for r in killed.access.audit_rows() if r[2] == "agent.refuse"]
    # Both are "agents-off" from one client within a minute: one audit row
    # (finding 6 throttle), both still counted in the metrics.
    assert len(rows) == 1
    assert all(MEMBER not in "".join(map(str, r)) for r in rows)
    r = await killed.client.get("/owner/metrics", headers=killed.jwt(ADMIN))
    assert (await r.json())["access"]["agent_refusals"] == 2


async def test_unauthenticated_refusals_cannot_flood_the_audit(live: Env) -> None:
    """Finding 6: every refused MCP request used to write an audit row
    (BEGIN IMMEDIATE + INSERT on the shared sqlite file). Rows are now
    throttled to one per reason per client per minute; the metric still
    counts every refusal."""
    for _ in range(25):
        await live.rpc({})  # no bearer
        await live.rpc({"Authorization": "Bearer " + "x" * 40})  # unknown
    rows = [r for r in live.access.audit_rows() if r[2] == "agent.refuse"]
    assert sorted(r[3] for r in rows) == ["no-bearer", "unknown"]
    r = await live.client.get("/owner/metrics", headers=live.jwt(ADMIN))
    assert (await r.json())["access"]["agent_refusals"] == 50


def test_refusal_throttle_is_per_reason_per_client_and_bounded() -> None:
    from irc_lens.web import agent_auth

    t = [1000.0]
    gate = agent_auth.RefusalThrottle(clock=lambda: t[0], max_keys=4)
    assert gate.should_audit("no-bearer", "203.0.113.1") is True
    assert gate.should_audit("no-bearer", "203.0.113.1") is False
    assert gate.should_audit("unknown", "203.0.113.1") is True
    assert gate.should_audit("no-bearer", "203.0.113.2") is True
    t[0] += 61
    assert gate.should_audit("no-bearer", "203.0.113.1") is True
    # Many distinct clients: the table never grows past its bound, and a
    # spoofed-address flood falls back to the per-reason ceiling.
    audited = sum(gate.should_audit("malformed", f"198.51.100.{i}") for i in range(200))
    assert audited <= agent_auth.RefusalThrottle.PER_REASON_PER_WINDOW
    assert len(gate) <= 4
    assert gate.suppressed > 0


async def test_agent_refusals_by_reason_are_audited(live: Env) -> None:
    await live.rpc({})  # no bearer
    await live.rpc({"Authorization": "Bearer " + "x" * 40})  # unknown
    token = live.access.issue_agent_credential(MEMBER, "n", ttl=3600, actor=None)
    live.access.set_user(MEMBER, role="member", state="removed", actor=None)
    await live.rpc({"Authorization": f"Bearer {token}"})  # human no longer ok
    targets = {r[3] for r in live.access.audit_rows() if r[2] == "agent.refuse"}
    assert {"no-bearer", "unknown", "human"} <= targets


async def test_grant_credential_and_refusal_events_all_land_in_audit(
    live: Env,
) -> None:
    r = await live.client.post(
        "/admin/grants",
        data={"email": MEMBER, "channel": "#lab", "level": "write"},
        headers=live.jwt(ADMIN),
        allow_redirects=False,
    )
    assert r.status == 303
    r = await live.client.post(
        "/agent-credential", headers=live.jwt(MEMBER), allow_redirects=False
    )
    assert r.status == 200
    await live.rpc({})
    from irc_lens import access

    assert access.can_write(MEMBER, "#nope", store=live.access) is False
    actions = set(live.audit_actions())
    assert {"grant.set", "credential.issue", "agent.refuse", "access.deny"} <= actions


# -- metrics: counts only -----------------------------------------------------------------


def test_access_counts_are_plain_integers(tmp_path: Path) -> None:
    store = _init_store(tmp_path / "a.db")
    store.set_grant(MEMBER, "channel", "#x", "read", actor=ADMIN)
    store.remove_grant(MEMBER, "channel", "#x", actor=ADMIN)
    store.issue_agent_credential(MEMBER, "n", ttl=60, actor=MEMBER)
    store.revoke_agent_credential(MEMBER, actor=ADMIN)
    counts = lens_metrics.access_counts(store)
    assert counts["grant_changes"] == 2
    assert counts["credential_events"] == 2
    assert counts["access_denials"] == 0
    assert counts["agent_refusals"] == 0
    assert all(type(v) is int for v in counts.values())


async def test_owner_metrics_expose_access_counts_only(live: Env) -> None:
    await live.rpc({})
    live.access.set_grant(MEMBER, "channel", "#lab", "read", actor=ADMIN)
    r = await live.client.get("/owner/metrics", headers=live.jwt(ADMIN))
    assert r.status == 200
    body = await r.json()
    counts = body["access"]
    assert counts["agent_refusals"] >= 1
    assert counts["grant_changes"] >= 1
    assert all(type(v) is int for v in counts.values())
    assert not _EMAIL.search(await r.text())
    assert MEMBER not in str(body) and ADMIN not in str(body)


# -- logs never carry an email address ---------------------------------------------------------


async def test_service_logs_carry_no_email(live: Env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    await live.rpc({})  # refused agent call
    await live.rpc({"Authorization": "Bearer " + "y" * 40})
    token = live.access.issue_agent_credential(MEMBER, "n", ttl=3600, actor=None)
    live.access.set_user(MEMBER, role="member", state="removed", actor=None)
    await live.rpc({"Authorization": f"Bearer {token}"})
    r = await live.client.post(  # grant change through the admin route
        "/admin/grants",
        data={"email": ADMIN, "channel": "#lab", "level": "read"},
        headers=live.jwt(ADMIN),
        allow_redirects=False,
    )
    assert r.status == 303
    r = await live.client.post(  # credential issue
        "/agent-credential", headers=live.jwt(ADMIN), allow_redirects=False
    )
    assert r.status == 200
    lines = [rec.getMessage() for rec in caplog.records]
    assert lines, "expected some service log lines"
    for line in lines:
        assert "example." not in line, line
        assert not _EMAIL.search(line), line


def test_no_log_call_passes_an_email_or_principal() -> None:
    """Static guard: no logger call formats an email/principal value."""
    import ast

    import irc_lens

    root = Path(irc_lens.__file__).parent
    bad: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"debug", "info", "warning", "error", "exception", "critical"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"logger", "log", "_log", "LOG"}
            ):
                continue
            hashed = {
                id(n)
                for c in ast.walk(node)
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "_who"  # the hash-prefix helper in web/auth.py
                for n in ast.walk(c)
            }
            for arg in node.args[1:]:
                for sub in ast.walk(arg):
                    if id(sub) in hashed:
                        continue
                    name = (
                        sub.id
                        if isinstance(sub, ast.Name)
                        else sub.attr if isinstance(sub, ast.Attribute) else ""
                    )
                    if name in {"email", "principal", "dev_email", "human"}:
                        bad.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not bad, bad
