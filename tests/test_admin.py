"""t11: in-app admin (users, grants, agent extensions) + store-decided sign-in.

Covers: admin-only /admin routes (403 for members, guests, anonymous and
agent bearers), invite = mailed set-password link with approval only after
sign-in completes, live grant/role changes without restart, a CSP-clean
page, and (deviation d1) sign-in / set-password / guests CLI deciding
'approved' from the access store.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import secrets
import types
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens import access
from irc_lens.access_store import AccessStore
from irc_lens.guest_store import GuestStore
from irc_lens.mail import RecordingAdapter
from irc_lens.session import Session
from irc_lens.web import app_session, csrf, entry, make_app, setpw
from test_session_routing import _config

ADMIN = "alice@example.com"
MEMBER = "bob@example.org"
NEWBIE = "carol@example.net"
GUEST = "guest@example.org"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
SECRET = b"s" * 32
BASE = "https://lens.example.com"
SIGNIN_SUBJECT = "Your chat.culture.dev sign-in code"
PW = secrets.token_urlsafe(16)


class Env:
    def __init__(self, client, app, guests, access_store, jwks, mesh, mail):
        self.client, self.app, self.guests = client, app, guests
        self.access, self.jwks, self.mesh, self.mail = access_store, jwks, mesh, mail

    def jwt(self, email):
        tok = self.jwks.mint(aud="aud-test", claims={"email": email, "sub": "s"})
        return {"Cf-Access-Jwt-Assertion": tok, **SAME_ORIGIN}

    def guest(self, email=GUEST):
        val = csrf.make_cookie_value(email, SECRET, 3600)
        return {"Cookie": f"{csrf.GUEST_COOKIE_NAME}={val}", **SAME_ORIGIN}

    async def post(self, path, headers, **data):
        r = await self.client.post(
            path, data=data, headers=headers, allow_redirects=False
        )
        await self.drain()
        return r

    async def drain(self):
        tasks = list(self.app.get(setpw.MAIL_TASKS, ()))
        tasks += list(self.app[entry.ENTRY_STATE].mail_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _build(jwks, tmp_path, monkeypatch, **cfg_kw) -> Env:
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", SECRET.decode())
    mesh = AgentIRCTestServer()
    await mesh.start()
    db = str(tmp_path / "guests.db")
    cfg = dataclasses.replace(
        _config(jwks, mesh, mesh, tmp_path),
        **{
            "access_enabled": True,
            "app_signin_base_url": BASE,
            "guest_store_path": db,
            "allowed_emails": (ADMIN,),
            **cfg_kw,
        },
    )
    app = make_app(
        cfg,
        lambda nick: Session(host=mesh.host, port=mesh.port, nick=nick),
        ban_sweep_interval_s=3600.0,
    )
    guests = app["guest_store"]
    mail = RecordingAdapter()
    state = app[entry.ENTRY_STATE]
    state.mailer = mail
    state.signin_floor_s = 0.0
    client = TestClient(TestServer(app))
    await client.start_server()
    return Env(client, app, guests, app.get(app_session.ACCESS_STORE), jwks, mesh, mail)


@pytest_asyncio.fixture
async def env(jwks: FakeJWKS, tmp_path, monkeypatch) -> AsyncIterator[Env]:
    e = await _build(jwks, tmp_path, monkeypatch)
    e.access.set_user(MEMBER, role="member", state="approved", actor=None)
    try:
        yield e
    finally:
        await e.client.close()
        await e.mesh.stop()


# -- make_app installs the access store (deviation d1) ---------------------------


async def test_make_app_installs_access_store(env: Env) -> None:
    assert isinstance(env.access, AccessStore)
    assert env.access.get_user(ADMIN)["role"] == "admin"


async def test_zero_admin_store_refuses_startup(jwks, tmp_path, monkeypatch) -> None:
    from irc_lens._errors import AfiError

    with pytest.raises(AfiError):
        await _build(jwks, tmp_path, monkeypatch, allowed_emails=())


async def test_admin_routes_absent_when_feature_off(
    jwks, tmp_path, monkeypatch
) -> None:
    e = await _build(jwks, tmp_path, monkeypatch, access_enabled=False)
    try:
        r = await e.client.get("/admin", headers=e.jwt(ADMIN))
        assert r.status == 404
    finally:
        await e.client.close()
        await e.mesh.stop()


# -- 403 for everyone but admins (o18) --------------------------------------------


async def test_admin_gets_page(env: Env) -> None:
    r = await env.client.get("/admin", headers=env.jwt(ADMIN))
    assert r.status == 200
    body = await r.text()
    assert ADMIN in body and MEMBER in body


async def test_non_admins_get_403(env: Env) -> None:
    for headers in (
        env.jwt(MEMBER),
        env.guest(),
        {**SAME_ORIGIN},
        {"Authorization": "Bearer not-a-real-agent-token", **SAME_ORIGIN},
    ):
        r = await env.client.get("/admin", headers=headers)
        assert r.status == 403, headers
        for path, data in (
            ("/admin/users", {"email": NEWBIE, "role": "member"}),
            ("/admin/grants", {"email": MEMBER, "channel": "#x", "level": "read"}),
            ("/admin/extension", {"email": MEMBER, "hours": "5"}),
        ):
            r = await env.client.post(path, data=data, headers=headers)
            assert r.status == 403, (path, headers)
    assert env.access.get_user(NEWBIE) is None
    assert env.access.grants_for(MEMBER) == []
    assert env.access.extension_until(MEMBER) is None


async def test_real_agent_token_gets_403(env: Env) -> None:
    token = env.access.issue_agent_credential(
        MEMBER, "testsrv-bob-agent", ttl=600, actor=None
    )
    r = await env.client.get(
        "/admin", headers={"Authorization": f"Bearer {token}", **SAME_ORIGIN}
    )
    assert r.status == 403


# -- CSP-clean --------------------------------------------------------------------


async def test_admin_page_is_csp_clean(env: Env) -> None:
    r = await env.client.get("/admin", headers=env.jwt(ADMIN))
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    body = (await r.text()).lower()
    assert "<script" not in body
    assert "<style" not in body
    assert not re.search(r"\sstyle\s*=", body)
    assert not re.search(r"\son[a-z]+\s*=", body)
    assert "javascript:" not in body


# -- add / invite -----------------------------------------------------------------


async def test_add_user_with_role_is_approved(env: Env) -> None:
    r = await env.post(
        "/admin/users", env.jwt(ADMIN), email=NEWBIE.upper(), role="admin", mode="add"
    )
    assert r.status == 303
    user = env.access.get_user(NEWBIE)
    assert (user["role"], user["state"]) == ("admin", "approved")
    assert env.mail.sent == []


async def test_invite_mails_setpw_link_and_is_not_yet_approved(env: Env) -> None:
    r = await env.post(
        "/admin/users", env.jwt(ADMIN), email=NEWBIE, role="member", mode="invite"
    )
    assert r.status == 303
    user = env.access.get_user(NEWBIE)
    assert (user["role"], user["state"]) == ("member", "invited")
    assert app_session.approved_user(env.app, NEWBIE) is None
    assert len(env.mail.sent) == 1
    to, _subject, body = env.mail.sent[0]
    assert to == NEWBIE
    assert re.search(re.escape(BASE) + r"/password/[A-Za-z0-9_-]+", body)


async def test_invited_user_approved_only_after_signin_completes(env: Env) -> None:
    await env.post(
        "/admin/users", env.jwt(ADMIN), email=NEWBIE, role="member", mode="invite"
    )
    token = re.search(r"/password/([A-Za-z0-9_-]+)", env.mail.sent[-1][2]).group(1)
    # Setting the password (via the mailed link) does not approve.
    r = await env.client.post(
        f"/password/{token}",
        data={"password": PW, "confirm": PW},
        headers=SAME_ORIGIN,
    )
    assert r.status == 200
    assert env.access.get_user(NEWBIE)["state"] == "invited"
    # Password step -> code mailed.
    r = await env.client.post(
        "/entry/signin", data={"email": NEWBIE, "password": PW}, headers=SAME_ORIGIN
    )
    await env.drain()
    pending = r.cookies[app_session.SIGNIN_COOKIE_NAME].value
    assert env.access.get_user(NEWBIE)["state"] == "invited"
    code = re.search(r"^ {4}(\S+)$", env.mail.sent[-1][2], flags=re.M).group(1)
    # Wrong code: still invited.
    bad = await env.client.post(
        "/entry/code",
        data={"email": NEWBIE, "code": "wrong"},
        headers={
            "Cookie": f"{app_session.SIGNIN_COOKIE_NAME}={pending}",
            **SAME_ORIGIN,
        },
        allow_redirects=False,
    )
    assert bad.status == 401
    assert env.access.get_user(NEWBIE)["state"] == "invited"
    ok = await env.client.post(
        "/entry/code",
        data={"email": NEWBIE, "code": code},
        headers={
            "Cookie": f"{app_session.SIGNIN_COOKIE_NAME}={pending}",
            **SAME_ORIGIN,
        },
        allow_redirects=False,
    )
    assert ok.status == 303
    assert env.access.get_user(NEWBIE)["state"] == "approved"
    assert app_session.approved_user(env.app, NEWBIE) is not None


async def test_bad_input_rejected(env: Env) -> None:
    for data in (
        {"email": "not-an-email", "role": "member", "mode": "add"},
        {"email": NEWBIE, "role": "root", "mode": "add"},
        {"email": NEWBIE, "role": "member", "mode": "weird"},
    ):
        r = await env.post("/admin/users", env.jwt(ADMIN), **data)
        assert r.status == 400, data
    assert env.access.get_user(NEWBIE) is None


# -- roles / grants: live, no restart -------------------------------------------


async def test_grant_visible_on_next_action_without_restart(env: Env) -> None:
    assert not access.can_read(MEMBER, "#lab", store=env.access)
    r = await env.post(
        "/admin/grants", env.jwt(ADMIN), email=MEMBER, channel="#lab", level="write"
    )
    assert r.status == 303
    assert access.can_write(MEMBER, "#lab", store=env.access)
    r = await env.post(
        "/admin/grants", env.jwt(ADMIN), email=MEMBER, channel="#lab", level="revoke"
    )
    assert r.status == 303
    assert not access.can_read(MEMBER, "#lab", store=env.access)


async def test_revoke_of_a_missing_grant_says_so(env: Env) -> None:
    """Finding 2: revoking a channel the user was never granted (a case
    variant here) is refused, not reported as 'Grant revoked.'."""
    env.access.set_grant(MEMBER, "channel", "#Ops", "write", actor=None)
    r = await env.post(
        "/admin/grants", env.jwt(ADMIN), email=MEMBER, channel="#ops", level="revoke"
    )
    assert r.status == 400
    assert "No such grant" in await r.text()
    assert env.access.grants_for(MEMBER) == [("channel", "#Ops", "write")]


async def test_bad_grant_rejected(env: Env) -> None:
    for data in (
        {"email": MEMBER, "channel": "lab", "level": "read"},
        {"email": MEMBER, "channel": "#lab", "level": "root"},
        {"email": "nobody@example.com", "channel": "#lab", "level": "read"},
    ):
        r = await env.post("/admin/grants", env.jwt(ADMIN), **data)
        assert r.status == 400, data
    assert env.access.grants_for(MEMBER) == []


async def test_role_change_applies_to_open_session(env: Env) -> None:
    h = env.jwt(MEMBER)
    assert (await env.client.get("/admin", headers=h)).status == 403
    await env.post(
        "/admin/users", env.jwt(ADMIN), email=MEMBER, role="admin", mode="add"
    )
    assert (await env.client.get("/admin", headers=h)).status == 200
    await env.post(
        "/admin/users", env.jwt(ADMIN), email=MEMBER, role="member", mode="add"
    )
    assert (await env.client.get("/admin", headers=h)).status == 403


async def test_remove_user_and_last_admin_guard(env: Env) -> None:
    r = await env.post("/admin/users/remove", env.jwt(ADMIN), email=MEMBER)
    assert r.status == 303
    assert env.access.get_user(MEMBER)["state"] == "removed"
    # The only admin cannot be removed or demoted.
    r = await env.post("/admin/users/remove", env.jwt(ADMIN), email=ADMIN)
    assert r.status == 400
    r = await env.post(
        "/admin/users", env.jwt(ADMIN), email=ADMIN, role="member", mode="add"
    )
    assert r.status == 400
    assert env.access.admin_count() == 1


# -- agent extension -----------------------------------------------------------------


async def test_extension_n_hours_no_fixed_max(env: Env) -> None:
    now = env.access._now()
    r = await env.post("/admin/extension", env.jwt(ADMIN), email=MEMBER, hours="5")
    assert r.status == 303
    until = env.access.extension_until(MEMBER)
    assert abs(until - (now + 5 * 3600)) < 5
    r = await env.post("/admin/extension", env.jwt(ADMIN), email=MEMBER, hours="100000")
    assert r.status == 303
    assert env.access.extension_until(MEMBER) - now >= 100000 * 3600 - 5


async def test_extension_rejects_bad_hours_and_unknown_user(env: Env) -> None:
    for hours in ("0", "-3", "abc", "", "nan", "1e999"):
        r = await env.post(
            "/admin/extension", env.jwt(ADMIN), email=MEMBER, hours=hours
        )
        assert r.status == 400, hours
    r = await env.post(
        "/admin/extension", env.jwt(ADMIN), email="no@example.com", hours="2"
    )
    assert r.status == 400
    assert env.access.extension_until(MEMBER) is None


async def test_extension_clear(env: Env) -> None:
    await env.post("/admin/extension", env.jwt(ADMIN), email=MEMBER, hours="2")
    r = await env.post("/admin/extension", env.jwt(ADMIN), email=MEMBER, hours="clear")
    assert r.status == 303
    assert env.access.extension_until(MEMBER) is None


# -- d1: sign-in / set-password decide from the store ------------------------------


async def test_setpw_link_for_store_user_not_in_allowlist(env: Env) -> None:
    assert MEMBER not in env.app["config"].allowed_emails
    await env.client.post("/password", data={"email": MEMBER}, headers=SAME_ORIGIN)
    await env.drain()
    assert [m[0] for m in env.mail.sent] == [MEMBER]


async def test_setpw_no_link_for_removed_or_unknown(env: Env) -> None:
    env.access.set_user(MEMBER, role="member", state="removed", actor=None)
    for email in (MEMBER, "nobody@example.com"):
        await env.client.post("/password", data={"email": email}, headers=SAME_ORIGIN)
        await env.drain()
    assert env.mail.sent == []


async def test_signin_password_step_uses_store(env: Env) -> None:
    env.guests.set_password(MEMBER, PW)
    await env.client.post(
        "/entry/signin", data={"email": MEMBER, "password": PW}, headers=SAME_ORIGIN
    )
    await env.drain()
    assert [(m[0], m[1]) for m in env.mail.sent] == [(MEMBER, SIGNIN_SUBJECT)]
    # Removing them in the store stops the sign-in code even with the right password.
    env.access.set_user(MEMBER, role="member", state="removed", actor=None)
    env.mail.sent.clear()
    await env.client.post(
        "/entry/signin", data={"email": MEMBER, "password": PW}, headers=SAME_ORIGIN
    )
    await env.drain()
    # Only a guest-style code now, never the approved sign-in code.
    assert SIGNIN_SUBJECT not in [m[1] for m in env.mail.sent]


async def test_allowlist_entry_not_in_store_cannot_sign_in(env: Env) -> None:
    env.access.set_user(ADMIN, role="admin", state="removed", actor=None)
    env.access.set_user(MEMBER, role="admin", state="approved", actor=None)
    env.guests.set_password(ADMIN, PW)
    await env.client.post(
        "/entry/signin", data={"email": ADMIN, "password": PW}, headers=SAME_ORIGIN
    )
    await env.drain()
    # ADMIN is still in config.allowed_emails but the store says removed.
    assert SIGNIN_SUBJECT not in [m[1] for m in env.mail.sent]


# -- guests CLI --------------------------------------------------------------------


def test_guests_cli_warning_uses_store(tmp_path, capsys, monkeypatch) -> None:
    import argparse

    from irc_lens.cli._commands import guests

    db = tmp_path / "g.db"
    store = AccessStore(db)
    store.set_user(MEMBER, role="member", state="approved", actor=None)
    cfg = types.SimpleNamespace(
        access_enabled=True, guest_store_path=str(db), allowed_emails=(NEWBIE,)
    )
    monkeypatch.setattr("irc_lens.config.resolve_config", lambda _p=None: cfg)
    args = argparse.Namespace(config=None, store=str(db))
    guests._warn_if_unapproved(args, MEMBER)
    assert capsys.readouterr().err == ""
    guests._warn_if_unapproved(args, NEWBIE)
    assert "not an approved user" in capsys.readouterr().err
