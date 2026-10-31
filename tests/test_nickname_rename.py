"""q14 (4, 5): admins rename approved users; a rename reconnects them.

``/admin`` has a nickname field on every user row (admin-only, CSRF-covered
like the rest) and the CLI has ``irc-lens users nick <email> <nickname>``
plus ``users add --nick``. A rename closes the user's open IRC sessions and
their agent's connection so both come back under the new nick; the agent's
credential, issued under the old nick, must be re-issued. Every set and
rename is audited with the nickname as target, never the email.
"""

from __future__ import annotations

import pytest

from irc_lens.access_store import AccessStore
from irc_lens.agent_nick import agent_nick_for
from irc_lens.guest_store import email_hash
from irc_lens.web import agent_auth
from irc_lens.web.sessions import BACKEND_MESH
from test_agent_sessions import ADMIN, MEMBER, MEMBER_AGENT, MEMBER_NICK, Env
from test_agent_sessions import env as agent_env  # noqa: F401 - a fixture
from test_session_routing import consent  # noqa: F401 - fixture

pytestmark = pytest.mark.usefixtures("chosen_nicknames")

RENAME_PATH = "/admin/users/nickname"
NEW_NICK = "testsrv-nova"


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
    assert (await env.ok(token, "whoami"))["nick"] == agent_nick_for(NEW_NICK)


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
