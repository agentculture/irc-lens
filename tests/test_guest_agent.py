"""t14: a guest's sponsored agent -- seat, bans, deletion, retention, idle,
extension and consent.

Acceptance criteria (plan t14):

* a guest's agent never adds to ``active_guests``; banning or deleting the
  guest refuses and disconnects the agent and removes its credential rows (o9);
* the idle reaper closes the agent with its human unless a live admin
  extension exists; it then stays until the extension's end time (o19);
* the consent record names the guest's agent; a guest whose consent predates
  the current legal version is re-prompted before connecting one;
* a guest and their agent can exchange lines in the guest's own sandbox room
  through the MCP endpoint.

Fixtures come from ``test_agent_sessions`` (real relay IRCd on both
backends). Every address is an example placeholder.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing


from irc_lens.guest_store import GuestStore
from irc_lens.web import agent_auth, bans
from irc_lens.web.sessions import BACKEND_SANDBOX
from test_agent_sessions import (  # noqa: F401 - env is a fixture
    GUEST,
    GUEST_AGENT,
    OTHER_GUEST,
    SAME_ORIGIN,
    Env,
    consent,
    env,
    settle,
)

GUEST_KEY = "guest:" + GUEST
GUEST_PRINCIPAL = "guest:" + GUEST
AGENT_PRINCIPAL = agent_auth.agent_registry_principal(GUEST_KEY)
IDLE_S = 600.0


def _credential_rows(env: Env, key: str = GUEST_KEY) -> int:
    with closing(sqlite3.connect(env.access.path)) as con:
        return con.execute(
            "SELECT COUNT(*) FROM agent_credentials WHERE email=?", (key,)
        ).fetchone()[0]


def _agent_open(env: Env) -> bool:
    return env.registry.has(AGENT_PRINCIPAL, BACKEND_SANDBOX)


def _human_open(env: Env) -> bool:
    return env.registry.has(GUEST_PRINCIPAL, BACKEND_SANDBOX)


async def _guest_with_agent(env: Env) -> str:
    """Human online, credential issued, agent connected; the token."""
    await env.human_online(env.guest())
    token = await env.issue(env.guest())
    await env.ok(token, "whoami")
    assert _human_open(env)
    assert _agent_open(env)
    return token


def _record_consent(env: Env, email: str = GUEST) -> None:
    env.guests.record_consent(
        email, "127.0.0.1", tos_version="v1", privacy_version="v1"
    )


async def _reap(env: Env, *, ahead: float = IDLE_S + 1) -> list:
    return await env.registry.reap_idle(now=time.monotonic() + ahead, idle_s=IDLE_S)


# -- seat (o9) -----------------------------------------------------------------------


async def test_agent_never_adds_to_active_guests(env: Env) -> None:
    await _guest_with_agent(env)
    assert env.registry.active_guests() == {GUEST_PRINCIPAL}
    assert env.registry.active_guest_count() == 1
    # A second agent from another guest still takes no seat.
    env.guests.record_consent(
        OTHER_GUEST, "127.0.0.2", tos_version="v1", privacy_version="v1"
    )
    # Its human is signed off, so only a live extension lets it run.
    env.access.set_extension(
        OTHER_GUEST, until=int(time.time()) + 3600, set_by="system"
    )
    other = await env.issue(env.guest(OTHER_GUEST))
    await env.ok(other, "whoami")
    assert env.registry.active_guests() == {GUEST_PRINCIPAL}


async def test_agent_alone_holds_no_seat(env: Env) -> None:
    """Finding 5: a guest's agent is present only where its human is. With
    the human signed off and no extension the agent is not opened at all;
    under a live admin extension it opens -- and still takes no seat."""
    token = await env.issue(env.guest())
    assert "signed in" in await env.refused(token, "whoami")
    assert not _agent_open(env)
    assert env.registry.active_guests() == set()
    env.access.set_extension(GUEST, until=int(time.time()) + 3600, set_by="system")
    await env.ok(token, "whoami")
    assert _agent_open(env)
    assert env.registry.active_guests() == set()


async def test_agent_is_not_reopened_after_its_human_signs_off(env: Env) -> None:
    """Finding 5: once the reaper closes the agent with its human, a tool
    call must not lazily reopen it (and chat on until the next reap)."""
    token = await _guest_with_agent(env)
    await _reap(env)
    assert not _human_open(env)
    assert not _agent_open(env)
    for verb in ("whoami", "read", "channels"):
        assert "signed in" in await env.refused(token, verb)
    assert "signed in" in await env.refused(token, "send", text="still here?")
    assert not _agent_open(env)
    # The human coming back lets the agent in again.
    await env.human_online(env.guest())
    await env.ok(token, "whoami")
    assert _agent_open(env)


async def test_open_agent_is_stopped_once_its_human_is_gone(env: Env) -> None:
    """Finding 5: between reaps, an already open agent whose human left is
    closed on its next call rather than kept chatting."""
    token = await _guest_with_agent(env)
    await env.registry.close(GUEST_PRINCIPAL, BACKEND_SANDBOX)
    assert _agent_open(env)
    assert "signed in" in await env.refused(token, "send", text="hello?")
    assert not _agent_open(env)


# -- ban (o9) ------------------------------------------------------------------------


async def test_ban_refuses_and_disconnects_agent_and_removes_rows(env: Env) -> None:
    token = await _guest_with_agent(env)
    assert _credential_rows(env) == 1
    env.guests.ban(email=GUEST, reason="test")
    dropped = await bans.sweep_once(env.registry, env.guests, access=env.access)
    assert GUEST in dropped
    assert not _agent_open(env)
    assert not _human_open(env)
    assert _credential_rows(env) == 0
    assert env.access.verify_agent_credential(token) is None
    r = await env.client.post(
        "/mcp", headers={"Authorization": f"Bearer {token}"}, json={}
    )
    assert r.status == 401


async def test_ban_removes_credential_of_a_guest_with_no_open_session(
    env: Env,
) -> None:
    await env.issue(env.guest())  # never connected, human not online
    env.guests.ban(email=GUEST, reason="test")
    await bans.sweep_once(env.registry, env.guests, access=env.access)
    assert _credential_rows(env) == 0


async def test_ban_by_recorded_ip_reaches_the_agent(env: Env) -> None:
    await _guest_with_agent(env)
    env.guests.ban(ip="127.0.0.1", reason="test")
    await bans.sweep_once(env.registry, env.guests, access=env.access)
    assert not _agent_open(env)
    assert _credential_rows(env) == 0


async def test_ban_leaves_other_guests_agents_alone(env: Env) -> None:
    await _guest_with_agent(env)
    env.guests.record_consent(
        OTHER_GUEST, "127.0.0.2", tos_version="v1", privacy_version="v1"
    )
    env.access.set_extension(
        OTHER_GUEST, until=int(time.time()) + 3600, set_by="system"
    )
    other = await env.issue(env.guest(OTHER_GUEST))
    await env.ok(other, "whoami")
    env.guests.ban(email=GUEST, reason="test")
    await bans.sweep_once(env.registry, env.guests, access=env.access)
    assert _credential_rows(env, "guest:" + OTHER_GUEST) == 1
    assert env.registry.has(
        agent_auth.agent_registry_principal("guest:" + OTHER_GUEST), BACKEND_SANDBOX
    )


# -- deletion (o9) -------------------------------------------------------------------


async def test_self_service_deletion_closes_agent_and_removes_rows(env: Env) -> None:
    token = await _guest_with_agent(env)
    code = env.guests.issue_token(GUEST, purpose="delete")
    r = await env.client.post(
        "/delete/confirm",
        data={"email": GUEST, "code": code},
        headers=SAME_ORIGIN,
        allow_redirects=False,
    )
    assert r.status == 200, await r.text()
    assert not _agent_open(env)
    assert not _human_open(env)
    assert _credential_rows(env) == 0
    assert env.access.verify_agent_credential(token) is None


# -- retention -----------------------------------------------------------------------


def test_retention_sweep_clears_agent_rows(tmp_path) -> None:
    from irc_lens.access_store import AccessStore

    path = tmp_path / "g.db"
    now = [1_000_000.0]
    guests = GuestStore(path, clock=lambda: now[0])
    access = AccessStore(path, clock=lambda: now[0])
    guests.record_guest(GUEST, "sbx-gus", "203.0.113.9")
    guests.record_consent(GUEST, "203.0.113.9", tos_version="v1", privacy_version="v1")
    access.issue_agent_credential(GUEST_KEY, GUEST_AGENT, ttl=3600, actor=None)
    access.set_extension(GUEST, until=int(now[0]) + 10, set_by="system")
    guests.set_consent_agent(GUEST, GUEST_AGENT)
    now[0] += 200 * 86400
    counts = guests.sweep(inactive_days=90)
    assert counts["guests"] == 1
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("SELECT COUNT(*) FROM agent_credentials").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM agent_extensions").fetchone()[0] == 0
    assert guests.consent_agent(GUEST) is None


def test_retention_sweeps_an_orphan_agent_credential(tmp_path) -> None:
    """A credential row whose guest rows are all gone is inactive too."""
    from irc_lens.access_store import AccessStore

    path = tmp_path / "g.db"
    now = [1_000_000.0]
    guests = GuestStore(path, clock=lambda: now[0])
    access = AccessStore(path, clock=lambda: now[0])
    access.issue_agent_credential(GUEST_KEY, GUEST_AGENT, ttl=3600, actor=None)
    now[0] += 200 * 86400
    guests.sweep(inactive_days=90)
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("SELECT COUNT(*) FROM agent_credentials").fetchone()[0] == 0


def test_retention_keeps_an_approved_users_credential(tmp_path) -> None:
    from irc_lens.access_store import AccessStore

    path = tmp_path / "g.db"
    now = [1_000_000.0]
    guests = GuestStore(path, clock=lambda: now[0])
    access = AccessStore(path, clock=lambda: now[0])
    access.issue_agent_credential(
        GUEST, "x-pa_-y", ttl=10**9, actor=None
    )  # approved key
    now[0] += 200 * 86400
    guests.sweep(inactive_days=90)
    assert access.active_credential_count(GUEST) == 1


def test_guest_store_works_without_access_tables(tmp_path) -> None:
    guests = GuestStore(tmp_path / "plain.db")
    guests.record_guest(GUEST, "sbx-gus", "203.0.113.9")
    assert guests.delete_guest_inputs(GUEST) >= 1


# -- idle reaper + extension (o19) -----------------------------------------------------


async def test_idle_reap_closes_agent_with_its_human(env: Env) -> None:
    await _guest_with_agent(env)
    closed = await _reap(env)
    assert (GUEST_PRINCIPAL, BACKEND_SANDBOX) in closed
    assert not _human_open(env)
    assert not _agent_open(env)
    # The credential itself survives an idle close: only the connection goes.
    assert _credential_rows(env) == 1


async def test_live_extension_keeps_the_agent_after_its_human_signs_off(
    env: Env,
) -> None:
    await _guest_with_agent(env)
    env.access.set_extension(GUEST, until=int(time.time()) + 3600, set_by="system")
    await _reap(env)
    assert not _human_open(env)
    assert _agent_open(env)
    await _reap(env)  # still extended on the next sweep
    assert _agent_open(env)


async def test_agent_closes_once_the_extension_ends(env: Env) -> None:
    await _guest_with_agent(env)
    env.access.set_extension(GUEST, until=int(time.time()) + 3600, set_by="system")
    await _reap(env)
    assert _agent_open(env)
    env.access.set_extension(GUEST, until=int(time.time()) - 1, set_by="system")
    await _reap(env)
    assert not _agent_open(env)


async def test_cleared_extension_ends_the_exemption(env: Env) -> None:
    await _guest_with_agent(env)
    env.access.set_extension(GUEST, until=int(time.time()) + 3600, set_by="system")
    await _reap(env)
    env.access.clear_extension(GUEST, actor=None)
    await _reap(env)
    assert not _agent_open(env)


async def test_extension_of_another_email_does_not_keep_the_agent(env: Env) -> None:
    await _guest_with_agent(env)
    env.access.set_extension(
        OTHER_GUEST, until=int(time.time()) + 3600, set_by="system"
    )
    await _reap(env)
    assert not _agent_open(env)


async def test_an_active_human_keeps_the_agent(env: Env) -> None:
    await _guest_with_agent(env)
    closed = await _reap(env, ahead=1.0)  # not idle yet
    assert closed == []
    assert _human_open(env)
    assert _agent_open(env)


async def test_agent_sends_do_not_touch_the_humans_idle_clock(env: Env) -> None:
    token = await _guest_with_agent(env)
    key = (GUEST_PRINCIPAL, BACKEND_SANDBOX)
    before = env.registry._last_action[key]  # pylint: disable=protected-access
    await settle(0.05)
    await env.ok(token, "send", text="agent chatter")
    await env.ok(token, "read")
    assert env.registry._last_action[key] == before  # pylint: disable=protected-access
    closed = await env.registry.reap_idle(now=before + IDLE_S + 1, idle_s=IDLE_S)
    assert key in closed


async def test_the_humans_own_action_does_count(env: Env) -> None:
    await _guest_with_agent(env)
    key = (GUEST_PRINCIPAL, BACKEND_SANDBOX)
    before = env.registry._last_action[key]  # pylint: disable=protected-access
    await settle(0.05)
    r = await env.human_input(env.guest(), "hello")
    assert r.status == 204
    assert env.registry._last_action[key] > before  # pylint: disable=protected-access


# -- consent names the agent ---------------------------------------------------------------


async def test_issuing_records_the_agent_in_the_consent_record(env: Env) -> None:
    _record_consent(env)
    assert env.guests.consent_agent(GUEST) is None
    await env.issue(env.guest())
    assert env.guests.consent_agent(GUEST) == GUEST_AGENT


async def test_consent_record_keeps_the_original_columns(env: Env) -> None:
    _record_consent(env)
    await env.issue(env.guest())
    (row,) = env.guests.get_consents(GUEST)
    assert row[2:4] == ("v1", "v1")  # tos, privacy versions unchanged


async def test_a_later_consent_row_still_names_the_agent(env: Env) -> None:
    _record_consent(env)
    await env.issue(env.guest())
    env.guests.record_consent(
        GUEST, "127.0.0.1", tos_version="v2", privacy_version="v2"
    )
    assert env.guests.consent_agent(GUEST) == GUEST_AGENT


async def test_stale_consent_is_reprompted_before_an_agent_is_issued(
    env: Env, consent  # noqa: F811
) -> None:
    _record_consent(env)
    consent["ok"] = False  # the legal version moved past the guest's consent
    r = await env.client.post(
        "/agent-credential", headers=env.guest(), allow_redirects=False
    )
    assert r.status == 403
    body = await r.json()
    assert body["redirect"] == "/consent"
    assert _credential_rows(env) == 0
    assert env.guests.consent_agent(GUEST) is None
    r = await env.client.get(
        "/agent-credential", headers=env.guest(), allow_redirects=False
    )
    assert r.status == 403
    consent["ok"] = True  # re-consented through the entry flow
    await env.issue(env.guest())
    assert _credential_rows(env) == 1


async def test_a_legal_bump_stops_an_existing_agent_until_reconsent(
    env: Env, consent  # noqa: F811
) -> None:
    """Finding 5: consent is re-checked on every agent request, so a legal
    version bump stops an agent that was connected under the old version."""
    token = await _guest_with_agent(env)
    consent["ok"] = False
    assert "consent" in await env.refused(token, "whoami")
    assert "consent" in await env.refused(token, "send", text="hi")
    assert not _agent_open(env)
    consent["ok"] = True
    await env.ok(token, "whoami")
    assert _agent_open(env)


def test_consent_agent_migrates_an_existing_store(tmp_path) -> None:
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as con, con:
        con.execute(
            "CREATE TABLE consents (email TEXT NOT NULL, ip TEXT NOT NULL, "
            "tos_version TEXT NOT NULL, privacy_version TEXT NOT NULL, "
            "ts INTEGER NOT NULL, train INTEGER NOT NULL DEFAULT 0)"
        )
        con.execute(
            "INSERT INTO consents VALUES (?,?,?,?,?,0)",
            (GUEST, "203.0.113.9", "v1", "v1", 5),
        )
    guests = GuestStore(path)
    assert guests.consent_agent(GUEST) is None
    guests.set_consent_agent(GUEST, GUEST_AGENT)
    assert guests.consent_agent(GUEST) == GUEST_AGENT
    assert guests.get_consents(GUEST)[0][:4] == (GUEST, "203.0.113.9", "v1", "v1")


# -- the guest + agent loop through the MCP endpoint ---------------------------------------


async def test_guest_and_agent_exchange_lines_in_the_guests_room(env: Env) -> None:
    token = await _guest_with_agent(env)
    room = env.app["config"].guest_room_prefix + env.guests.room_id(GUEST)
    from test_agent_sessions import GUEST_NICK

    human = env.human_session(GUEST_PRINCIPAL, BACKEND_SANDBOX)
    await env.ok(token, "read")  # drain joins
    # human -> agent
    r = await env.human_input(env.guest(), f"@{GUEST_AGENT} what is a sandbox?")
    assert r.status == 204, await r.text()
    await settle()
    got = (await env.ok(token, "read"))["lines"]
    assert [(ln["target"], ln["nick"], ln["owner"]) for ln in got] == [
        (room, GUEST_NICK, True)
    ]
    assert "what is a sandbox?" in got[0]["text"]
    # agent -> human, in the same room
    sent = await env.ok(token, "send", text="a sandbox is a safe room")
    assert sent
    await settle()
    lines = [m for m in human.buffer.read(room) if m.nick == GUEST_AGENT]
    assert any("a safe room" in m.text for m in lines)
    # Nothing leaked outside the guest's own room.
    other = env.app["config"].guest_room_prefix + env.guests.room_id(OTHER_GUEST)
    assert other not in " ".join(env.sent_by(env.sandbox, GUEST_AGENT))
    assert env.sent_by(env.mesh, GUEST_AGENT) == []


async def test_reap_closes_every_signed_off_guest_agent_in_one_sweep() -> None:
    """``close`` mutates the registry mid-sweep, so the reaper must walk a
    snapshot: two signed-off guests' agents both close, nothing raises."""
    from irc_lens.web.sessions import (
        AGENT_PRINCIPAL_PREFIX,
        GUEST_PRINCIPAL_PREFIX,
        SessionRegistry,
    )

    class _Closable:
        disconnected = False

        async def disconnect(self) -> None:
            self.disconnected = True

    async def _never(*_args, **_kwargs):  # pragma: no cover - never opened
        raise AssertionError("the reaper must not open sessions")

    registry = SessionRegistry(_never, sandbox_factory=_never)
    agents = {}
    for email in ("one@example.com", "two@example.com"):
        principal = AGENT_PRINCIPAL_PREFIX + GUEST_PRINCIPAL_PREFIX + email
        agents[principal] = _Closable()
        registry._sessions[(principal, BACKEND_SANDBOX)] = agents[principal]

    await registry._reap_guest_agents()

    for principal, session in agents.items():
        assert not registry.has(principal, BACKEND_SANDBOX)
        assert session.disconnected
