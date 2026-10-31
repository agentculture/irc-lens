"""q14 (2, 3): approved users choose a nickname; their IRC nick is built from it.

With ``auth.email_permissions`` on, an approved user (admin or member) with
no nickname is sent to ``/nickname`` after sign-in and gets no IRC session
until they choose one. Their nick is then ``<server>-<nickname>`` on every
path (Access JWT, app session, the agent's human), never the email's local
part. With the feature off nothing changes: ``derive_nick`` still applies.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import types
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from _jwks_server import FakeJWKS
from irc_lens.web import app_session
from irc_lens.web import nickname as nickname_web
from irc_lens.web.agent_sessions import human_state
from irc_lens.web.identity import TIER_APPROVED, Identity, derive_nick
from irc_lens.web.sessions import BACKEND_MESH
from test_admin import ADMIN, MEMBER, SAME_ORIGIN, Env, _build

NEW_PATH = "/nickname"


@pytest_asyncio.fixture
async def env(jwks: FakeJWKS, tmp_path, monkeypatch) -> AsyncIterator[Env]:
    e = await _build(jwks, tmp_path, monkeypatch)
    e.access.set_user(MEMBER, role="member", state="approved", actor=None)
    try:
        yield e
    finally:
        await e.client.close()
        await e.mesh.stop()


def _mesh_nicks(env: Env) -> dict[str, str]:
    registry = env.app["registry"]
    return {
        principal: session.nick
        for (principal, backend), session in zip(registry.keys(), registry.values())
        if backend == BACKEND_MESH
    }


async def _choose(env: Env, email: str, raw: str):
    return await env.client.post(
        NEW_PATH,
        data={"nickname": raw},
        headers=env.jwt(email),
        allow_redirects=False,
    )


# -- first sign-in: choose before the console (2) ---------------------------------


async def test_console_redirects_to_the_nickname_form(env: Env) -> None:
    for who in (ADMIN, MEMBER):
        r = await env.client.get("/", headers=env.jwt(who), allow_redirects=False)
        assert r.status == 303, who
        assert r.headers["Location"] == NEW_PATH
    assert _mesh_nicks(env) == {}


async def test_no_irc_session_until_a_nickname_is_chosen(env: Env) -> None:
    r = await env.client.post("/input", json={"text": "hello"}, headers=env.jwt(MEMBER))
    assert r.status == 409
    assert (await r.json())["error"] == nickname_web.ERR_CHOOSE_FIRST
    hx = await env.client.post(
        "/input", json={"text": "hi"}, headers={**env.jwt(MEMBER), "HX-Request": "1"}
    )
    assert hx.headers["HX-Redirect"] == NEW_PATH
    for path in ("/events", "/agent-credential", "/admin"):
        r = await env.client.get(path, headers=env.jwt(ADMIN), allow_redirects=False)
        assert r.status == 303, path
    assert _mesh_nicks(env) == {}


async def test_form_is_csp_clean_and_never_shows_the_email_as_a_nick(
    env: Env,
) -> None:
    r = await env.client.get(NEW_PATH, headers=env.jwt(MEMBER))
    assert r.status == 200
    assert "unsafe-inline" not in r.headers["Content-Security-Policy"]
    body = (await r.text()).lower()
    assert 'action="/nickname"' in body
    assert "testsrv-" in body  # the prefix the nick will carry
    assert "<script" not in body
    assert not re.search(r"\sstyle\s*=", body)
    assert not re.search(r"\son[a-z]+\s*=", body)
    assert 'value="bob"' not in body  # nothing prefilled from the address


async def test_choosing_a_nickname_opens_the_console_under_it(env: Env) -> None:
    r = await _choose(env, MEMBER, "Comet")
    assert r.status == 303
    assert r.headers["Location"] == "/"
    assert env.access.get_user(MEMBER)["nickname"] == "comet"
    page = await env.client.get("/", headers=env.jwt(MEMBER))
    assert page.status == 200
    assert _mesh_nicks(env) == {MEMBER: "testsrv-comet"}


async def test_form_errors(env: Env) -> None:
    await _choose(env, ADMIN, "comet")
    cases = (
        ("COMET", nickname_web.ERR_TAKEN),
        ("x", nickname_web.ERR_INVALID),
        ("bob@example.org", nickname_web.ERR_INVALID),
        ("ask", nickname_web.ERR_RESERVED),
        ("pa_bot", nickname_web.ERR_RESERVED),
    )
    for raw, error in cases:
        r = await _choose(env, MEMBER, raw)
        assert r.status == 400, raw
        assert error in await r.text(), raw
    assert env.access.get_user(MEMBER)["nickname"] is None


async def test_chosen_nickname_cannot_be_changed_by_its_user(env: Env) -> None:
    await _choose(env, MEMBER, "comet")
    r = await _choose(env, MEMBER, "nova")
    assert r.status == 303
    assert env.access.get_user(MEMBER)["nickname"] == "comet"
    page = await env.client.get(
        NEW_PATH, headers=env.jwt(MEMBER), allow_redirects=False
    )
    assert page.status == 303


async def test_resident_agent_nick_is_refused(jwks, tmp_path, monkeypatch) -> None:
    async def residents(_request: web.Request) -> web.Response:
        return web.json_response(
            {"supported": True, "residents": [{"nick": "testsrv-culture"}]}
        )

    upstream = web.Application()
    upstream.router.add_get("/residents.json", residents)
    server = TestServer(upstream)
    await server.start_server()
    e = await _build(
        jwks,
        tmp_path,
        monkeypatch,
        culture_residents_url=str(server.make_url("/residents.json")),
    )
    try:
        r = await _choose(e, ADMIN, "Culture")
        assert r.status == 400
        assert nickname_web.ERR_TAKEN in await r.text()
        assert (await _choose(e, ADMIN, "comet")).status == 303
    finally:
        await e.client.close()
        await e.mesh.stop()
        await server.close()


async def test_nick_online_on_the_mesh_is_refused(env: Env) -> None:
    online = types.SimpleNamespace(
        nick="testsrv-other",
        roster=[types.SimpleNamespace(nick="testsrv-claude")],
    )
    env.app["registry"].register("someone@example.com", online)
    r = await _choose(env, MEMBER, "claude")
    assert r.status == 400
    assert nickname_web.ERR_TAKEN in await r.text()


async def test_anonymous_cannot_reach_the_form(env: Env) -> None:
    r = await env.client.get(NEW_PATH, headers=SAME_ORIGIN, allow_redirects=False)
    assert r.status in (401, 303)
    r = await env.client.post(NEW_PATH, data={"nickname": "x1"}, headers=SAME_ORIGIN)
    assert r.status in (401, 403)


async def test_choice_is_audited_without_the_email(env: Env) -> None:
    await _choose(env, MEMBER, "comet")
    rows = [r for r in env.access.audit_rows() if r[2] == "nickname.set"]
    assert [r[3] for r in rows] == ["comet"]
    assert all(MEMBER not in str(c) for r in env.access.audit_rows() for c in r)


# -- the nick on every path (3) ------------------------------------------------------


def _cookie(raw: str) -> dict[str, str]:
    return {"Cookie": f"{app_session.SESSION_COOKIE_NAME}={raw}", **SAME_ORIGIN}


async def test_app_session_identity_uses_the_nickname(env: Env) -> None:
    raw = env.guests.create_session(MEMBER)
    r = await env.client.get("/", headers=_cookie(raw), allow_redirects=False)
    assert r.status == 303  # signed in, but no nickname yet
    assert r.headers["Location"] == NEW_PATH
    r = await env.client.post(
        NEW_PATH,
        data={"nickname": "comet"},
        headers=_cookie(raw),
        allow_redirects=False,
    )
    assert r.status == 303
    r = await env.client.get("/", headers=_cookie(raw))
    assert r.status == 200
    assert _mesh_nicks(env) == {MEMBER: "testsrv-comet"}


async def test_agent_human_nick_is_the_nickname(env: Env) -> None:
    principal = types.SimpleNamespace(tier=TIER_APPROVED, email=MEMBER)
    assert human_state(env.app, principal) is None  # no nickname: no human nick
    env.access.set_nickname(MEMBER, "comet", actor=None)
    assert human_state(env.app, principal).nick == "testsrv-comet"


async def test_registry_refuses_an_identity_without_a_nick(env: Env) -> None:
    identity = Identity(MEMBER, "", "s", tier=TIER_APPROVED)
    with pytest.raises(ValueError):
        await env.app["registry"].get_or_open(identity)


async def test_registry_reopens_under_a_renamed_nick(env: Env) -> None:
    env.access.set_nickname(MEMBER, "comet", actor=None)
    await env.client.get("/", headers=env.jwt(MEMBER))
    env.access.set_nickname(MEMBER, "nova", actor=None)
    await env.client.get("/", headers=env.jwt(MEMBER))
    assert _mesh_nicks(env) == {MEMBER: "testsrv-nova"}


async def test_legacy_mode_keeps_the_derived_nick(jwks, tmp_path, monkeypatch) -> None:
    e = await _build(jwks, tmp_path, monkeypatch, access_enabled=False)
    try:
        r = await e.client.get("/", headers=e.jwt(ADMIN))
        assert r.status == 200
        assert _mesh_nicks(e) == {ADMIN: derive_nick("testsrv", ADMIN)}
        r = await e.client.get(NEW_PATH, headers=e.jwt(ADMIN))
        assert r.status == 404
    finally:
        await e.client.close()
        await e.mesh.stop()


# -- review of PR #86 ------------------------------------------------------------


async def test_finding2_a_second_in_flight_choice_never_renames(
    env: Env, monkeypatch
) -> None:
    store = env.access
    real_claim = store.claim_nickname

    def raced(email: str, raw: str, *, actor):
        # The other tab's POST passed the gate too and stored first.
        real_claim(email, "first", actor=actor)
        return real_claim(email, raw, actor=actor)

    monkeypatch.setattr(store, "claim_nickname", raced)
    r = await _choose(env, MEMBER, "second")
    assert r.status == 303
    assert r.headers["Location"] == "/"
    assert store.get_user(MEMBER)["nickname"] == "first"
    actions = [a[2] for a in store.audit_rows() if a[2].startswith("nickname.")]
    assert actions == ["nickname.set"]


_DATA_RIGHTS = (
    ("GET", "/delete"),
    ("POST", "/delete/request"),
    ("POST", "/delete/confirm"),
    ("GET", "/consent"),
)


@pytest.mark.parametrize(("method", "path"), _DATA_RIGHTS)
async def test_finding4_data_rights_routes_skip_the_nickname_gate(
    env: Env, method: str, path: str
) -> None:
    r = await env.client.request(
        method, path, headers=env.jwt(MEMBER), allow_redirects=False
    )
    assert r.headers.get("Location") != NEW_PATH
    assert r.headers.get("HX-Redirect") != NEW_PATH
    assert r.status != 409


async def test_finding8_store_error_is_a_form_error_not_a_500(
    env: Env, monkeypatch
) -> None:
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(env.access, "claim_nickname", locked)
    r = await _choose(env, MEMBER, "comet")
    assert r.status == 503
    assert nickname_web.ERR_STORE in await r.text()


async def test_finding8_choice_is_stored_off_the_event_loop(
    env: Env, monkeypatch
) -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real_claim = env.access.claim_nickname

    def spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(env.access, "claim_nickname", spy)
    assert (await _choose(env, MEMBER, "comet")).status == 303
    assert seen
    assert loop_thread not in seen


async def _admin_rename(env: Env, raw: str):
    return await env.client.post(
        "/admin/users/nickname",
        data={"email": MEMBER, "nickname": raw},
        headers=env.jwt(ADMIN),
        allow_redirects=False,
    )


async def test_finding8_admin_rename_store_error_is_a_form_error(
    env: Env, monkeypatch
) -> None:
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    env.access.set_nickname(ADMIN, "root", actor=None)
    monkeypatch.setattr(env.access, "set_nickname", locked)
    r = await _admin_rename(env, "comet")
    assert r.status == 400
    assert nickname_web.ERR_STORE in await r.text()


async def test_finding8_admin_rename_runs_off_the_event_loop(
    env: Env, monkeypatch
) -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []
    store = env.access

    def spy(real):
        def call(*args, **kwargs):
            seen.append(threading.get_ident())
            return real(*args, **kwargs)

        return call

    store.set_nickname(ADMIN, "root", actor=None)
    monkeypatch.setattr(store, "set_nickname", spy(store.set_nickname))
    r = await _admin_rename(env, "comet")
    assert r.status == 303
    assert env.access.get_user(MEMBER)["nickname"] == "comet"
    assert seen
    assert loop_thread not in seen
