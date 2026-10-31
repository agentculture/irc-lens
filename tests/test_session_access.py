"""Session enforcement of the email-keyed channel grants (task t9).

A fake IRC server is bolted onto ``Session._transport.send_raw`` so every
line the session would emit is recorded (a refused request must emit none)
and LIST / WHO / HISTORY are answered from a small in-memory mesh.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from irc_lens import access
from irc_lens.access_store import AccessStore
from irc_lens.commands import CommandType, ParsedCommand
from irc_lens.irc import Message
from irc_lens.session import Session

ADMIN = "root@example.com"
USER = "alice@example.com"
ROOMS = {
    "#x": ["lens-test", "xnick"],
    "#y": ["lens-test", "ynick-only"],
}


class FakeServer:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.lines: list[str] = []
        session._transport.connected = True
        session._transport.send_raw = self.send_raw  # type: ignore[assignment]

    async def send_raw(self, line: str) -> None:
        self.lines.append(line)
        h = self.session._transport._cmd_handlers
        parts = line.split()
        if parts[0] == "LIST":
            for ch in ROOMS:
                h["322"](Message(command="322", params=["me", ch, "2"]))
            h["323"](Message(command="323", params=["me", "end"]))
        elif parts[0] == "WHO":
            for nick in ROOMS.get(parts[1], []):
                h["352"](
                    Message(
                        command="352",
                        params=["me", parts[1], "u", "h", "srv", nick, "H", "0 real"],
                    )
                )
            h["315"](Message(command="315", params=["me", parts[1], "end"]))
        elif parts[0] == "HISTORY":
            ch = parts[2]
            h["HISTORY"](
                Message(command="HISTORY", params=[ch, "xnick", "1", "old line"])
            )
            h["HISTORYEND"](Message(command="HISTORYEND", params=[ch, "end"]))


@pytest.fixture
def store(tmp_path: Path) -> AccessStore:
    s = AccessStore(tmp_path / "lens.db", clock=lambda: 1000.0)
    s.set_user(ADMIN, role="admin", state="approved", actor=None)
    s.set_user(USER, role="member", state="approved", actor=None)
    s.set_grant(USER, "channel", "#x", "write", actor=None)
    return s


def _make(store: AccessStore, human: str | None) -> tuple[Session, FakeServer]:
    session = Session(host="127.0.0.1", port=6667, nick="lens-test")
    session.set_access(store, human)
    return session, FakeServer(session)


def _run(session: Session, cmd_type: CommandType, *args: str, text: str = "") -> list:
    sub = session.event_bus.subscribe()
    asyncio.run(
        session.execute(ParsedCommand(type=cmd_type, args=list(args), text=text))
    )
    events = sub.drain_nowait()
    sub.close()
    return events


def _blob(events) -> str:
    return "\n".join(e.data for e in events)


def _denials(store: AccessStore) -> int:
    return sum(1 for r in store.audit_rows() if r[2] == access.DENY_ACTION)


# --- join / send / read -------------------------------------------------


def test_join_ungranted_refused_no_line(store):
    session, srv = _make(store, USER)
    events = _run(session, CommandType.JOIN, "#y")
    assert srv.lines == []
    assert "#y" not in session.joined_channels
    assert session.current_channel == ""
    assert [e.name for e in events] == ["error"]
    assert _denials(store) == 1


def test_join_granted_allowed(store):
    session, srv = _make(store, USER)
    _run(session, CommandType.JOIN, "#x")
    assert "JOIN #x" in srv.lines
    assert "#x" in session.joined_channels


def test_join_is_case_sensitive_on_grant(store):
    """A grant on ``#x`` must not open ``#X``: AgentIRC treats them as two
    different channels, so a case variant is an ungranted room."""
    session, srv = _make(store, USER)
    events = _run(session, CommandType.JOIN, "#X")
    assert srv.lines == []
    assert "#X" not in session.joined_channels
    assert [e.name for e in events] == ["error"]
    _run(session, CommandType.READ, "#X")
    _run(session, CommandType.SEND, "#X", text="hi")
    assert srv.lines == []


def test_send_and_chat_and_me_ungranted_emit_nothing(store):
    session, srv = _make(store, USER)
    _run(session, CommandType.SEND, "#y", text="hi")
    _run(session, CommandType.SEND, "someone", text="dm")
    session.current_channel = "#y"
    session.joined_channels.add("#y")
    _run(session, CommandType.CHAT, text="hello")
    _run(session, CommandType.ME, text="waves")
    assert [ln for ln in srv.lines if ln.startswith("PRIVMSG")] == []


def test_send_granted_allowed(store):
    session, srv = _make(store, USER)
    _run(session, CommandType.SEND, "#x", text="hi")
    assert "PRIVMSG #x :hi" in srv.lines


def test_read_ungranted_refused_no_history_line(store):
    session, srv = _make(store, USER)
    events = _run(session, CommandType.READ, "#y")
    assert srv.lines == []
    assert "error" in [e.name for e in events]
    assert "old line" not in _blob(events)


def test_read_granted_queries_history(store):
    session, srv = _make(store, USER)
    session.current_channel = "#x"
    _run(session, CommandType.READ, "#x")
    assert any(ln.startswith("HISTORY RECENT #x") for ln in srv.lines)


def test_switch_to_revoked_channel_refused(store):
    session, srv = _make(store, USER)
    session.joined_channels.update({"#x", "#y"})
    _run(session, CommandType.SWITCH, "#y")
    assert session.current_channel != "#y"
    assert not any(ln.startswith("HISTORY") for ln in srv.lines)


def test_session_public_query_methods_guarded(store):
    session, srv = _make(store, USER)

    async def go():
        for coro in (session.who("#y"), session.history("#y")):
            with pytest.raises(PermissionError):
                await coro

    asyncio.run(go())
    assert srv.lines == []


# --- listings -----------------------------------------------------------


def test_channels_lists_only_granted_and_does_not_audit_each(store):
    session, srv = _make(store, USER)
    before = _denials(store)
    events = _run(session, CommandType.CHANNELS)
    blob = _blob(events)
    assert "#x" in blob
    assert "#y" not in blob
    assert _denials(store) == before


def test_who_ungranted_refused_no_line(store):
    session, srv = _make(store, USER)
    events = _run(session, CommandType.WHO, "#y")
    assert srv.lines == []
    assert "ynick-only" not in _blob(events)


def test_who_granted_shows_members(store):
    session, srv = _make(store, USER)
    events = _run(session, CommandType.WHO, "#x")
    assert "xnick" in _blob(events)


def test_agents_only_queries_granted_joined(store):
    session, srv = _make(store, USER)
    session.joined_channels.update({"#x", "#y"})
    before = _denials(store)
    events = _run(session, CommandType.AGENTS)
    assert "WHO #y" not in srv.lines
    assert "WHO #x" in srv.lines
    blob = _blob(events)
    assert "xnick" in blob
    assert "ynick-only" not in blob
    assert _denials(store) == before


def test_mesh_names_only_granted_channels_and_their_nicks(store):
    session, srv = _make(store, USER)
    session.joined_channels.update({"#x", "#y"})
    before = _denials(store)
    events = _run(session, CommandType.MESH)
    assert "WHO #y" not in srv.lines
    mesh = [e for e in events if e.name == "mesh"]
    graph = json.loads(mesh[-1].data)
    ids = {n["id"] for n in graph["nodes"]}
    assert "#x" in ids
    assert "xnick" in ids
    assert "#y" not in ids
    assert "ynick-only" not in ids
    assert "#y" not in _blob(events)
    assert "ynick-only" not in _blob(events)
    assert _denials(store) == before


def test_overview_and_status_do_not_name_ungranted_channels(store):
    session, srv = _make(store, USER)
    session.joined_channels.update({"#x", "#y"})
    for ct in (CommandType.OVERVIEW, CommandType.STATUS):
        events = _run(session, ct)
        assert "#y" not in _blob(events)
    assert "#y" not in session.joined_channels
    assert "#x" in session.joined_channels


def test_revocation_mid_session_drops_current_channel(store):
    session, srv = _make(store, USER)
    _run(session, CommandType.JOIN, "#x")
    store.remove_grant(USER, "channel", "#x", actor=None)
    events = _run(session, CommandType.OVERVIEW)
    assert session.current_channel == ""
    assert "#x" not in _blob(events)


# --- live dispatch --------------------------------------------------------


def _privmsg(target: str) -> Message:
    return Message(prefix="xnick!u@h", command="PRIVMSG", params=[target, "secret"])


def test_dispatch_drops_lines_from_revoked_channel(store):
    session, srv = _make(store, USER)
    session.current_channel = "#x"
    sub = session.event_bus.subscribe()
    asyncio.run(session.dispatch(_privmsg("#x")))
    assert any(e.name == "chat" for e in sub.drain_nowait())
    store.remove_grant(USER, "channel", "#x", actor=None)
    asyncio.run(session.dispatch(_privmsg("#x")))
    assert not any(e.name == "chat" for e in sub.drain_nowait())
    sub.close()


def test_dispatch_drop_does_not_audit_each_line(store):
    session, srv = _make(store, USER)
    session.current_channel = "#y"
    before = _denials(store)
    for _ in range(3):
        asyncio.run(session.dispatch(_privmsg("#y")))
    assert _denials(store) == before


# --- admin-only commands ---------------------------------------------------


@pytest.mark.parametrize(
    "ct,args,text",
    [
        (CommandType.TOPIC, ("#x",), "new"),
        (CommandType.TOPIC, ("#x",), ""),
        (CommandType.KICK, ("#x", "bob"), ""),
        (CommandType.INVITE, ("bob", "#x"), ""),
        (CommandType.ICON, ("🔥",), ""),
    ],
)
def test_channel_changing_commands_admin_only(store, ct, args, text):
    session, srv = _make(store, USER)
    events = _run(session, ct, *args, text=text)
    assert srv.lines == []
    assert [e.name for e in events] == ["error"]
    assert session.icon != "🔥"


def test_admin_may_topic_and_icon(store):
    session, srv = _make(store, ADMIN)
    _run(session, CommandType.TOPIC, "#x", text="new")
    _run(session, CommandType.ICON, "🔥")
    assert "TOPIC #x :new" in srv.lines
    assert "ICON 🔥" in srv.lines


def test_admin_passes_every_channel_check(store):
    session, srv = _make(store, ADMIN)
    _run(session, CommandType.JOIN, "#y")
    assert "JOIN #y" in srv.lines
    events = _run(session, CommandType.CHANNELS)
    assert "#x" in _blob(events)
    assert "#y" in _blob(events)


def test_unapproved_or_missing_human_fails_closed(store):
    for human in ("nobody@example.org", None, ""):
        session, srv = _make(store, human)
        _run(session, CommandType.JOIN, "#x")
        assert srv.lines == []


def test_access_off_is_unrestricted():
    session = Session(host="127.0.0.1", port=6667, nick="lens-test")
    srv = FakeServer(session)
    _run(session, CommandType.JOIN, "#y")
    _run(session, CommandType.TOPIC, "#y", text="t")
    assert "JOIN #y" in srv.lines
    assert "TOPIC #y :t" in srv.lines
