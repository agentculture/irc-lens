"""q14 (4, 5): admins rename approved users; a rename reconnects them.

``/admin`` has a nickname field on every user row (admin-only, CSRF-covered
like the rest) and the CLI has ``irc-lens users nick <email> <nickname>``
plus ``users add --nick``. A rename closes the user's open IRC sessions and
their agent's connection so both come back under the new nick; the agent's
credential, issued under the old nick, must be re-issued. Every set and
rename is audited with the nickname as target, never the email.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from irc_lens._errors import AfiError
from irc_lens.access_store import AccessStore
from irc_lens.agent_nick import agent_nick_for
from irc_lens.guest_store import email_hash
from irc_lens.web import agent_auth, agent_sessions
from irc_lens.web.sessions import BACKEND_MESH
from test_agent_sessions import ADMIN, MEMBER, MEMBER_AGENT, MEMBER_NICK, Env, settle
from test_agent_sessions import env as agent_env  # noqa: F401 - a fixture
from test_session_routing import consent  # noqa: F401 - fixture

pytestmark = pytest.mark.usefixtures("chosen_nicknames")

RENAME_PATH = "/admin/users/nickname"
SERVER = "testsrv"
NEW_NICK = f"{SERVER}-nova"
NEW_AGENT = agent_nick_for(NEW_NICK, SERVER)


@pytest.fixture
def env(agent_env: Env) -> Env:  # noqa: F811
    """The live lens of ``test_agent_sessions``: mesh, sandbox, MCP endpoint."""
    return agent_env


async def _rename(env: Env, email: str, raw: str, headers=None):
    return await env.client.post(
        RENAME_PATH,
        data={"email": email, "nickname": raw},
        headers=headers or env.jwt(ADMIN),
        allow_redirects=False,
    )


def _agent_key() -> tuple[str, str]:
    return agent_auth.agent_registry_principal(MEMBER), BACKEND_MESH


# -- /admin ----------------------------------------------------------------------


async def test_admin_page_has_a_nickname_field_per_user(env: Env) -> None:
    r = await env.client.get("/admin", headers=env.jwt(ADMIN))
    body = await r.text()
    assert f'action="{RENAME_PATH}"' in body
    assert 'value="basil"' in body


async def test_admin_renames_a_user(env: Env) -> None:
    r = await _rename(env, MEMBER, "Nova")
    assert r.status == 303
    assert r.headers["Location"] == "/admin?done=nickname"
    assert env.access.get_user(MEMBER)["nickname"] == "nova"
    row = [r for r in env.access.audit_rows() if r[2] == "nickname.rename"][-1]
    assert row[1] == email_hash(ADMIN)
    assert row[3] == "nova"


@pytest.mark.parametrize(
    ("email", "raw", "error"),
    [
        (MEMBER, "aurora", "Nickname taken"),
        (MEMBER, "x", "Invalid nickname"),
        (MEMBER, "pa_bot", "That nickname is reserved"),
        ("nobody@example.org", "nova", "No such user."),
    ],
)
async def test_rename_refusals(env: Env, email: str, raw: str, error: str) -> None:
    r = await _rename(env, email, raw)
    assert r.status == 400
    assert error in await r.text()
    assert env.access.get_user(MEMBER)["nickname"] == "basil"


async def test_rename_is_admin_only_and_csrf_checked(env: Env) -> None:
    r = await _rename(env, MEMBER, "nova", headers=env.jwt(MEMBER))
    assert r.status == 403
    cross = {**env.jwt(ADMIN), "Origin": "https://evil.example"}
    r = await _rename(env, MEMBER, "nova", headers=cross)
    assert r.status == 403
    assert env.access.get_user(MEMBER)["nickname"] == "basil"


async def test_rename_closes_sessions_and_agent_then_reconnects(env: Env) -> None:
    await env.human_online(env.jwt(MEMBER))
    token = await env.issue(env.jwt(MEMBER))
    assert (await env.ok(token, "whoami"))["nick"] == MEMBER_AGENT
    assert env.human_session(MEMBER).nick == MEMBER_NICK

    assert (await _rename(env, MEMBER, "nova")).status == 303
    assert not env.registry.has(MEMBER, BACKEND_MESH)
    assert not env.registry.has(*_agent_key())

    # The old credential carries the old agent nick: it must be re-issued.
    assert "re-issue" in await env.refused(token, "whoami")
    assert not env.registry.has(*_agent_key())
    await env.human_online(env.jwt(MEMBER))
    assert env.human_session(MEMBER).nick == NEW_NICK
    token = await env.issue(env.jwt(MEMBER))
    assert (await env.ok(token, "whoami"))["nick"] == NEW_AGENT


async def test_out_of_process_rename_is_swept(env: Env) -> None:
    """A CLI rename (another process) is picked up by the sweep."""
    await env.human_online(env.jwt(MEMBER))
    token = await env.issue(env.jwt(MEMBER))
    await env.ok(token, "whoami")
    AccessStore(env.access.path).set_nickname(MEMBER, "nova", actor=None)
    await agent_auth.sweep_once(env.app)
    assert not env.registry.has(MEMBER, BACKEND_MESH)
    assert not env.registry.has(*_agent_key())


async def test_sweep_leaves_current_nicks_alone(env: Env) -> None:
    await env.human_online(env.jwt(MEMBER))
    token = await env.issue(env.jwt(MEMBER))
    await env.ok(token, "whoami")
    await agent_auth.sweep_once(env.app)
    assert env.registry.has(MEMBER, BACKEND_MESH)
    assert env.registry.has(*_agent_key())


async def test_renamed_member_dms_their_agent_under_the_new_nick_only(
    env: Env,
) -> None:
    """The own-agent DM rule follows a rename: the reopened session's
    own_agent_nick is <server>-pa_-<new nickname>; the old one is refused."""
    await env.human_online(env.jwt(MEMBER))
    assert env.human_session(MEMBER).own_agent_nick == MEMBER_AGENT
    assert (await _rename(env, MEMBER, "nova")).status == 303

    await env.human_online(env.jwt(MEMBER))
    assert env.human_session(MEMBER).own_agent_nick == NEW_AGENT
    token = await env.issue(env.jwt(MEMBER))
    await env.ok(token, "whoami")
    for target in (MEMBER_AGENT, NEW_AGENT):
        r = await env.human_input(env.jwt(MEMBER), f"/send {target} hello {target}")
        assert r.status == 204, await r.text()
    await settle()

    got = (await env.ok(token, "read"))["lines"]
    assert [(ln["target"], ln["text"], ln["owner"]) for ln in got] == [
        ("DM", f"hello {NEW_AGENT}", True)
    ]
    # The old agent nick is refused before IRC: no connection sent it a line.
    to_old = [
        ln.raw
        for ln in env.mesh.received
        if ln.raw.startswith(f"PRIVMSG {MEMBER_AGENT} ")
    ]
    assert to_old == []


# -- the 0.17.1 hot-path read rules (busy store -> fail closed, close nothing) --


def _held_write_lock(path):
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("BEGIN EXCLUSIVE")
    return closing(con)


async def test_sweep_under_a_held_write_lock_closes_nothing_then_catches_up(
    env: Env, caplog
) -> None:
    await env.human_online(env.jwt(MEMBER))
    AccessStore(env.access.path).set_nickname(MEMBER, "nova", actor=None)
    caplog.set_level(logging.WARNING, logger="irc_lens.web.nickname")
    with _held_write_lock(env.access.path) as con:
        swept = await agent_auth.sweep_once(env.app)
        con.execute("ROLLBACK")
    assert swept == []
    assert env.registry.has(MEMBER, BACKEND_MESH)
    assert "nickname_sweep skipped" in caplog.text
    await agent_auth.sweep_once(env.app)
    assert not env.registry.has(MEMBER, BACKEND_MESH)


async def test_busy_store_refuses_the_agent_call_but_keeps_its_connection(
    monkeypatch,
) -> None:
    closed: list[str] = []

    def busy(_app, _principal):
        raise sqlite3.OperationalError("database is locked")

    async def spy(_app, key: str) -> None:
        closed.append(key)

    monkeypatch.setattr(agent_sessions, "human_state", busy)
    monkeypatch.setattr(agent_sessions, "_close_agent", spy)
    principal = SimpleNamespace(credential_key=MEMBER, nick=MEMBER_AGENT)
    with pytest.raises(AfiError, match="busy"):
        await agent_sessions.agent_session_for({}, principal)
    assert closed == []
