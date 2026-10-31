"""t7: approval + roles come from the access store (email-keyed permissions).

Covers: identity_for and the Cloudflare-JWT path decide approved/admin from
the store on every request; the one-time allowed_emails admin import; the
zero-admin enable refusal; sessions that predate the migration still work.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens._errors import EXIT_USER_ERROR, AfiError
from irc_lens.access_store import AccessStore
from irc_lens.guest_store import GuestStore
from irc_lens.session import Session
from irc_lens.web import app_session, make_app
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.identity import ROLE_ADMIN, TIER_APPROVED, Identity
from test_session_routing import _config

ALICE = "alice@example.com"
BOB = "bob@example.org"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


@allows_anonymous
async def _whoami(request: web.Request) -> web.Response:
    i = request["identity"]
    return web.json_response({"tier": i.tier, "principal": i.principal, "role": i.role})


class Env:
    def __init__(self, client, app, guests, access, jwks, mesh):
        self.client, self.app, self.guests = client, app, guests
        self.access, self.jwks, self.mesh = access, jwks, mesh

    def cookie(self, raw):
        return {"Cookie": f"lens_session={raw}", **SAME_ORIGIN}

    def jwt(self, email):
        tok = self.jwks.mint(aud="aud-test", claims={"email": email, "sub": "s"})
        return {"Cf-Access-Jwt-Assertion": tok, **SAME_ORIGIN}

    async def approved(self, headers) -> bool:
        # Guest mode is on in _config, so a denied principal degrades to the
        # anonymous tier (200) rather than 403; approval is the tier.
        status, body = await self.get(headers)
        return status == 200 and body["tier"] == "approved"

    async def get(self, headers):
        r = await self.client.get("/_whoami", headers=headers)
        return r.status, (await r.json() if r.status == 200 else None)


async def _build(jwks, tmp_path, monkeypatch, *, install=True, **cfg_kw):
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", "s" * 32)
    mesh = AgentIRCTestServer()
    await mesh.start()
    cfg = dataclasses.replace(
        _config(jwks, mesh, mesh, tmp_path), **{"access_enabled": True, **cfg_kw}
    )
    app = make_app(
        cfg, lambda nick: Session(host=mesh.host, port=mesh.port, nick=nick),
        ban_sweep_interval_s=3600.0,
    )
    guests = GuestStore(cfg.guest_store_path)
    app["guest_store"] = guests
    access = app_session.install_access(app) if install else None
    app.router.add_get("/_whoami", _whoami)
    client = TestClient(TestServer(app))
    await client.start_server()
    return Env(client, app, guests, access, jwks, mesh)


@pytest_asyncio.fixture
async def env(jwks: FakeJWKS, tmp_path, monkeypatch) -> AsyncIterator[Env]:
    e = await _build(jwks, tmp_path, monkeypatch)
    try:
        yield e
    finally:
        await e.client.close()
        await e.mesh.stop()


def test_identity_carries_role() -> None:
    i = Identity("a@example.com", "n", "s", tier=TIER_APPROVED, role=ROLE_ADMIN)
    assert i.role == "admin"
    assert i.is_admin
    assert not Identity("a@example.com", "n", "s", tier=TIER_APPROVED).is_admin
    assert Identity("a", "n", "s").role == ""


# -- one-time import (o21) ----------------------------------------------------


async def test_first_start_imports_allowed_emails_as_admins(env: Env) -> None:
    user = env.access.get_user("ALICE@example.com")
    assert user
    assert (user["role"], user["state"]) == ("admin", "approved")


async def test_allowed_emails_never_read_again(jwks, tmp_path, monkeypatch) -> None:
    e = await _build(jwks, tmp_path, monkeypatch)
    await e.client.close()
    await e.mesh.stop()
    # Second start with a different allowed_emails: nothing new is imported.
    e2 = await _build(jwks, tmp_path, monkeypatch, allowed_emails=(ALICE, BOB))
    try:
        assert e2.access.get_user(BOB) is None
        assert not await e2.approved(e2.jwt(BOB))
    finally:
        await e2.client.close()
        await e2.mesh.stop()


async def test_demotion_survives_restart(jwks, tmp_path, monkeypatch) -> None:
    e = await _build(jwks, tmp_path, monkeypatch)
    e.access.set_user(BOB, role="admin", state="approved", actor=None)
    e.access.set_user(ALICE, role="member", state="approved", actor=None)
    await e.client.close()
    await e.mesh.stop()
    e2 = await _build(jwks, tmp_path, monkeypatch)
    try:
        assert e2.access.get_user(ALICE)["role"] == "member"
    finally:
        await e2.client.close()
        await e2.mesh.stop()


# -- enable gate (o20) ----------------------------------------------------------


async def test_enable_with_zero_admins_refuses_with_hint(
    jwks, tmp_path, monkeypatch
) -> None:
    with pytest.raises(AfiError) as exc:
        await _build(jwks, tmp_path, monkeypatch, allowed_emails=())
    assert exc.value.code == EXIT_USER_ERROR
    assert "admin" in str(exc.value)
    assert exc.value.remediation


async def test_disabled_does_not_install_or_refuse(jwks, tmp_path, monkeypatch) -> None:
    e = await _build(
        jwks, tmp_path, monkeypatch, access_enabled=False, allowed_emails=()
    )
    try:
        assert e.access is None
        assert "access_store" not in e.app
    finally:
        await e.client.close()
        await e.mesh.stop()


# -- per-request decisions from the store ----------------------------------------


async def test_jwt_approval_and_role_from_store(env: Env) -> None:
    status, body = await env.get(env.jwt(ALICE))
    assert status == 200
    assert body["tier"] == "approved"
    assert body["role"] == "admin"
    env.access.set_user(BOB, role="member", state="approved", actor=None)
    status, body = await env.get(env.jwt(BOB))
    assert status == 200
    assert body["role"] == "member"


async def test_jwt_unknown_pending_and_allowlist_not_consulted(env: Env) -> None:
    assert not await env.approved(env.jwt(BOB))
    env.access.set_user(BOB, role="member", state="pending", actor=None)
    assert not await env.approved(env.jwt(BOB))


async def test_changes_apply_on_next_request(env: Env) -> None:
    env.access.set_user(BOB, role="member", state="approved", actor=None)
    assert await env.approved(env.jwt(BOB))
    env.access.set_user(BOB, role="member", state="suspended", actor=None)
    assert not await env.approved(env.jwt(BOB))
    env.access.set_user(ALICE, role="member", state="approved", actor=None)
    assert (await env.get(env.jwt(ALICE)))[1]["role"] == "member"


async def test_app_session_follows_store(env: Env) -> None:
    env.access.set_user(BOB, role="member", state="approved", actor=None)
    raw = env.guests.create_session(BOB)
    status, body = await env.get(env.cookie(raw))
    assert status == 200
    assert body["principal"] == BOB
    assert body["role"] == "member"
    env.access.set_user(BOB, role="member", state="suspended", actor=None)
    assert not await env.approved(env.cookie(raw))


async def test_preexisting_session_survives_migration(
    jwks, tmp_path, monkeypatch
) -> None:
    # A session minted before the feature existed (plain GuestStore row).
    pre = GuestStore(tmp_path / "guests.db")
    raw = pre.create_session(ALICE)
    e = await _build(jwks, tmp_path, monkeypatch)
    try:
        status, body = await e.get(e.cookie(raw))
        assert status == 200
        assert body["tier"] == "approved"
        assert body["role"] == "admin"
    finally:
        await e.client.close()
        await e.mesh.stop()


async def test_sweep_closes_sessions_of_unapproved_users(env: Env) -> None:
    env.access.set_user(BOB, role="member", state="approved", actor=None)
    raw = env.guests.create_session(BOB)
    id_hash = app_session.session_id_hash(raw)
    env.app[app_session.LINKS] = {(BOB, "mesh"): id_hash}
    env.app["registry"].has = lambda *k: True  # type: ignore[method-assign]
    closed: list = []

    async def _close(*k):
        closed.append(k)

    env.app["registry"].close = _close  # type: ignore[method-assign]
    assert await app_session.sweep_once(env.app) == []
    env.access.set_user(BOB, role="member", state="suspended", actor=None)
    assert await app_session.sweep_once(env.app) == [(BOB, "mesh")]


async def test_store_missing_while_enabled_fails_closed(
    jwks, tmp_path, monkeypatch
) -> None:
    e = await _build(jwks, tmp_path, monkeypatch, install=False)
    # make_app installs the store itself (t11); drop it to simulate it missing.
    e.app._state.pop(app_session.ACCESS_STORE, None)
    try:
        assert not await e.approved(e.jwt(ALICE))
    finally:
        await e.client.close()
        await e.mesh.stop()


# -- config key ------------------------------------------------------------------


def test_config_email_permissions_key(tmp_path) -> None:
    from irc_lens.config import load_config

    def load(extra: str):
        p = tmp_path / "c.yaml"
        p.write_text(
            "auth:\n  mode: dev\n  dev:\n    nick: lens\n    email: dev@local\n"
            f"{extra}server:\n  name: spark\n"
        )
        return load_config(p)

    assert load("").access_enabled is False
    on = "  email_permissions:\n    enabled: true\n"
    assert load(on).access_enabled is True
    with pytest.raises(AfiError):
        load("  email_permissions:\n    enable: true\n")
