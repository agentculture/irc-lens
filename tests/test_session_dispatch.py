"""Unit tests for `Session.execute` and `Session.dispatch` (Phase 5).

`execute` is the entry point from `POST /input`; `dispatch` is the
listener wired into `IRCTransport.add_listener` for inbound IRC
messages. Both publish through `SessionEventBus`. The tests drive
the methods directly without touching a real socket — the offline
session's transport has `_writer is None`, so its `_send_raw` is a
silent no-op (see test_session_unit.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from irc_lens.commands import CommandType, ParsedCommand
from irc_lens.irc import Message
from irc_lens.session import LensConnectionLost, Session, SessionEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    return Session(host="127.0.0.1", port=6667, nick="lens-test")


def _drain(session: Session) -> list[SessionEvent]:
    """Subscribe + drain whatever's already queued. Skips the awaitable
    `events()` generator since the queue is filled synchronously by
    `publish` and we just want a snapshot."""
    sub = session.event_bus.subscribe()
    try:
        return sub.drain_nowait()
    finally:
        sub.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# execute — JOIN/PART/CHAT/SEND
# ---------------------------------------------------------------------------


def test_execute_join_publishes_roster_and_info(session: Session) -> None:
    """JOIN changes channel context — publishes `roster` (channel list
    changed) and `info` (channel context changed). Per spec line 162
    the `view` event is reserved for /help/overview/status switches,
    so it must NOT fire here."""
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.JOIN, args=["#ops"])))
    events = sub.drain_nowait()
    sub.close()

    assert "#ops" in session.joined_channels
    assert session.current_channel == "#ops"
    names = [e.name for e in events]
    assert "roster" in names
    assert "info" in names
    assert "view" not in names, (
        "JOIN must not publish a `view` event — that's reserved for "
        "/help/overview/status (spec line 162)."
    )
    # The roster fragment carries the channel testid + data attribute.
    roster = next(e for e in events if e.name == "roster")
    assert 'data-testid="sidebar-channel"' in roster.data
    assert 'data-channel="#ops"' in roster.data
    # The info fragment carries the view-indicator and the new channel.
    info = next(e for e in events if e.name == "info")
    assert 'data-testid="view-indicator"' in info.data
    assert "#ops" in info.data


def test_execute_join_without_args_publishes_error(session: Session) -> None:
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.JOIN, args=[])))
    events = sub.drain_nowait()
    sub.close()
    assert any(e.name == "error" for e in events)
    assert session.joined_channels == set()


def test_execute_join_non_hash_target_publishes_error_no_state_change(
    session: Session,
) -> None:
    """`/join ops` (no `#`) must not mutate view state.

    Session.join() no-ops on non-`#` targets; without an early
    validation in execute(), `set_current_channel` would still run and
    leave the UI pointing at a channel that's not in joined_channels.
    """
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.JOIN, args=["ops"])))
    events = sub.drain_nowait()
    sub.close()
    assert session.current_channel == ""
    assert session.joined_channels == set()
    names = [e.name for e in events]
    assert names == ["error"]
    assert "invalid channel" in events[0].data
    # No roster/view leakage either.
    assert "roster" not in names
    assert "view" not in names


def test_execute_part_publishes_roster(session: Session) -> None:
    session.joined_channels.add("#ops")
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.PART, args=["#ops"])))
    events = sub.drain_nowait()
    sub.close()
    assert "#ops" not in session.joined_channels
    # current_channel was the parted one — Session.part clears it.
    assert session.current_channel == ""
    assert any(e.name == "roster" for e in events)


def test_execute_chat_publishes_chat_fragment(session: Session) -> None:
    session.joined_channels.add("#ops")
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.CHAT, text="hi all")))
    events = sub.drain_nowait()
    sub.close()
    chat_events = [e for e in events if e.name == "chat"]
    assert len(chat_events) == 1
    body = chat_events[0].data
    assert "hi all" in body
    assert "lens-test" in body  # local nick echoed


def test_execute_chat_without_channel_publishes_error(session: Session) -> None:
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.CHAT, text="lonely")))
    events = sub.drain_nowait()
    sub.close()
    assert any(e.name == "error" for e in events)


def test_execute_send_to_active_channel_local_echoes(session: Session) -> None:
    session.joined_channels.add("#ops")
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(
        session.execute(
            ParsedCommand(type=CommandType.SEND, args=["#ops"], text="payload")
        )
    )
    events = sub.drain_nowait()
    sub.close()
    chat = [e for e in events if e.name == "chat"]
    assert len(chat) == 1
    assert "payload" in chat[0].data


def test_execute_unknown_publishes_error_does_not_raise(session: Session) -> None:
    sub = session.event_bus.subscribe()
    # parse_command produces UNKNOWN with `text=stripped`; mimic it.
    asyncio.run(
        session.execute(ParsedCommand(type=CommandType.UNKNOWN, text="/foo bar"))
    )
    events = sub.drain_nowait()
    sub.close()
    errors = [e for e in events if e.name == "error"]
    assert len(errors) == 1
    assert "/foo bar" in errors[0].data


def test_execute_propagates_lens_connection_lost(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send-path failures must reach the route layer so it can return 503."""

    async def broken(_target: str, _text: str) -> None:
        raise LensConnectionLost("pipe gone")

    session.set_current_channel("#ops")
    monkeypatch.setattr(session, "send_privmsg", broken)
    with pytest.raises(LensConnectionLost):
        asyncio.run(
            session.execute(ParsedCommand(type=CommandType.CHAT, text="boom"))
        )


# ---------------------------------------------------------------------------
# dispatch — inbound PRIVMSG / JOIN / PART
# ---------------------------------------------------------------------------


def _privmsg(prefix: str, target: str, text: str) -> Message:
    return Message(prefix=prefix, command="PRIVMSG", params=[target, text])


def test_dispatch_privmsg_in_active_channel_publishes_chat(session: Session) -> None:
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(session.dispatch(_privmsg("alice!~a@h", "#ops", "hello")))
    events = sub.drain_nowait()
    sub.close()
    assert len(events) == 1
    e = events[0]
    assert e.name == "chat"
    assert "alice" in e.data
    assert "hello" in e.data


def test_dispatch_skips_self_echo(session: Session) -> None:
    """Local echo of our own SEND already fired in `execute` — don't
    double-publish from the inbound path."""
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(session.dispatch(_privmsg("lens-test!~l@h", "#ops", "echo")))
    events = sub.drain_nowait()
    sub.close()
    assert events == []


def test_dispatch_skips_system_event_emitter(session: Session) -> None:
    """system-<server> PRIVMSGs are mesh events, not chat."""
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(session.dispatch(_privmsg("system-local!~s@h", "#ops", "joined")))
    events = sub.drain_nowait()
    sub.close()
    assert events == []


def test_dispatch_skips_inactive_channel(session: Session) -> None:
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(session.dispatch(_privmsg("alice!~a@h", "#elsewhere", "hi")))
    events = sub.drain_nowait()
    sub.close()
    assert events == []


def test_dispatch_join_publishes_roster(session: Session) -> None:
    session.joined_channels.add("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(
        session.dispatch(Message(prefix="alice!~a@h", command="JOIN", params=["#ops"]))
    )
    events = sub.drain_nowait()
    sub.close()
    assert any(e.name == "roster" for e in events)


# ---------------------------------------------------------------------------
# Phase 6: view-switch verbs (/help, /overview, /status)
# ---------------------------------------------------------------------------


def _events_after(session: Session, parsed: ParsedCommand) -> list[SessionEvent]:
    sub = session.event_bus.subscribe()
    try:
        asyncio.run(session.execute(parsed))
        return sub.drain_nowait()
    finally:
        sub.close()


def test_execute_help_switches_view_and_publishes_info(session: Session) -> None:
    events = _events_after(session, ParsedCommand(type=CommandType.HELP))
    assert session.view == "help"
    names = [e.name for e in events]
    assert names.count("view") == 1
    assert names.count("info") == 1
    view = next(e for e in events if e.name == "view")
    payload = json.loads(view.data)
    assert payload == {"view": "help"}, (
        "spec line 162 defines the view payload as `{view: <name>}` only"
    )
    info = next(e for e in events if e.name == "info")
    assert "Slash commands" in info.data
    assert 'data-testid="view-indicator"' in info.data
    assert 'data-view="help"' in info.data


def test_execute_overview_switches_view(session: Session) -> None:
    session.joined_channels.add("#ops")
    session.joined_channels.add("#dev")
    session.set_current_channel("#ops")
    events = _events_after(session, ParsedCommand(type=CommandType.OVERVIEW))
    assert session.view == "overview"
    info = next(e for e in events if e.name == "info")
    assert "Joined channels" in info.data
    assert "#dev" in info.data
    assert "#ops" in info.data


def test_execute_status_switches_view(session: Session) -> None:
    events = _events_after(session, ParsedCommand(type=CommandType.STATUS))
    assert session.view == "status"
    info = next(e for e in events if e.name == "info")
    assert "Session status" in info.data
    # Status pane shows nick/server.
    assert "lens-test" in info.data


def test_view_event_payload_is_spec_strict(session: Session) -> None:
    """`view` payload must be exactly {view: <name>} — nothing else."""
    events = _events_after(session, ParsedCommand(type=CommandType.HELP))
    view = next(e for e in events if e.name == "view")
    payload = json.loads(view.data)
    assert set(payload.keys()) == {"view"}
    assert payload["view"] in ("chat", "help", "overview", "status")


# ---------------------------------------------------------------------------
# History on JOIN, /switch, CTCP ACTION (console-parity round)
# ---------------------------------------------------------------------------


def test_execute_join_publishes_log_event(session: Session) -> None:
    """History-on-join: JOIN also publishes a `log` event so the chat
    pane gets the server-side backlog. Offline session → empty log
    payload (no IRCd to query), but the event must still fire so the
    frontend swaps innerHTML and clears whatever the previous channel
    rendered. Regression guard for the literal user complaint that
    triggered this work."""
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.JOIN, args=["#ops"])))
    events = sub.drain_nowait()
    sub.close()
    log_events = [e for e in events if e.name == "log"]
    assert len(log_events) == 1, (
        "JOIN must publish exactly one `log` event (history replacement)"
    )


def test_execute_switch_to_joined_channel(session: Session) -> None:
    """/switch flips current_channel without re-joining and publishes
    log/roster/info. Mirrors clickable-sidebar UX."""
    session.joined_channels.update({"#ops", "#dev"})
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.SWITCH, args=["#dev"])))
    events = sub.drain_nowait()
    sub.close()
    assert session.current_channel == "#dev"
    names = [e.name for e in events]
    assert "roster" in names
    assert "info" in names
    assert "log" in names
    assert "error" not in names


def test_execute_read_other_channel_publishes_log(session: Session) -> None:
    """Regression for the /read #other stale-guard bug: with the user
    sitting on #ops, `/read #dev` must produce one `log` event for the
    pane the user is looking at — even though the queried channel isn't
    `current_channel`. Before the view_channel fix, the stale guard
    inside `_fetch_and_publish_history` matched against the queried
    channel and silently dropped the result."""
    session.joined_channels.update({"#ops", "#dev"})
    session.set_current_channel("#ops")
    # Force `connected=True` on the offline transport so the
    # query-not-connected guard doesn't short-circuit; the real history
    # call no-ops on the None writer and returns [] after timeout. We
    # short the timeout via the connected guard upstream — but here we
    # want to specifically prove the publish path runs.
    session._transport.connected = True
    sub = session.event_bus.subscribe()

    async def fire_history_end() -> list:
        # Drive the IRC dispatch handler manually so the history Future
        # resolves immediately rather than waiting QUERY_TIMEOUT.
        async def fake_send(_line: str) -> None:
            # `send_raw` is awaited in production, so this stand-in must
            # be `async` too — even though the body is synchronous.
            # `asyncio.sleep(0)` yields once so sonarcloud's S7503
            # ("async without await") sees a real await.
            await asyncio.sleep(0)
            session._on_historyend(
                Message(prefix=None, command="HISTORYEND", params=["#dev", "End"])
            )

        session._transport.send_raw = fake_send  # type: ignore[assignment]
        await session.execute(
            ParsedCommand(type=CommandType.READ, args=["#dev"])
        )
        return sub.drain_nowait()

    events = asyncio.run(fire_history_end())
    sub.close()
    log_events = [e for e in events if e.name == "log"]
    assert len(log_events) == 1, (
        "/read #dev from #ops must still publish one `log` event into "
        "the active pane"
    )


def test_execute_switch_to_unjoined_channel_errors(session: Session) -> None:
    """/switch must refuse channels the lens hasn't joined — preserves
    the invariant that current_channel is always in joined_channels."""
    session.joined_channels.add("#ops")
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.SWITCH, args=["#dev"])))
    events = sub.drain_nowait()
    sub.close()
    assert session.current_channel == "#ops"  # unchanged
    assert any(e.name == "error" for e in events)


def test_execute_me_publishes_action_chat(session: Session) -> None:
    """/me waves → publishes chat event with action styling so the
    template renders `* nick waves` instead of the standard nick:text."""
    session.joined_channels.add("#ops")
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(
        session.execute(ParsedCommand(type=CommandType.ME, text="waves"))
    )
    events = sub.drain_nowait()
    sub.close()
    chat = [e for e in events if e.name == "chat"]
    assert len(chat) == 1
    body = chat[0].data
    # Action lines render as `* nick text` in the template.
    assert "* lens-test waves" in body
    assert "lens-chat-line--action" in body


def test_execute_me_without_channel_errors(session: Session) -> None:
    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.ME, text="waves")))
    events = sub.drain_nowait()
    sub.close()
    assert any(e.name == "error" for e in events)


def test_dispatch_ctcp_action_renders_as_action(session: Session) -> None:
    """Inbound `\\x01ACTION text\\x01` PRIVMSG must surface as an action
    line, not a literal control-char chat line."""
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(
        session.dispatch(_privmsg("alice!~a@h", "#ops", "\x01ACTION waves\x01"))
    )
    events = sub.drain_nowait()
    sub.close()
    assert len(events) == 1
    body = events[0].data
    assert "* alice waves" in body
    assert "\x01" not in body  # CTCP wrapper stripped
    assert "lens-chat-line--action" in body


def test_dispatch_ctcp_non_action_dropped(session: Session) -> None:
    """Other CTCP types (VERSION, PING) are protocol pings — not chat."""
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    asyncio.run(
        session.dispatch(_privmsg("alice!~a@h", "#ops", "\x01VERSION\x01"))
    )
    events = sub.drain_nowait()
    sub.close()
    assert events == []


def test_back_to_back_view_switches_publish_one_event_each(session: Session) -> None:
    """Sequence /help → /overview → /status: each switch must emit
    exactly one `view` event + one `info` event. Regression guard
    against `_publish_view` ever drifting to multi-publish behaviour
    (or a stale spec line 162 fix being undone)."""
    events: list[SessionEvent] = []

    async def collect() -> None:
        sub = session.event_bus.subscribe()
        try:
            for cmd_type in (CommandType.HELP, CommandType.OVERVIEW, CommandType.STATUS):
                await session.execute(ParsedCommand(type=cmd_type))
            # SessionEventBus.publish is synchronous (no await needed) —
            # drain_nowait sees every event the loop produced.
            events.extend(sub.drain_nowait())
        finally:
            sub.close()

    asyncio.run(collect())
    names = [e.name for e in events]
    assert names.count("view") == 3, (
        f"expected 3 view events (one per switch), got {names.count('view')}: {names}"
    )
    assert names.count("info") == 3, (
        f"expected 3 info events (one per switch), got {names.count('info')}: {names}"
    )
    # Each view event payload reflects the active view at publish time.
    view_payloads = [json.loads(e.data) for e in events if e.name == "view"]
    assert [p["view"] for p in view_payloads] == ["help", "overview", "status"]
    assert session.view == "status"


# ---------------------------------------------------------------------------
# Issue #20 regression: /channels, /who, /agents from a non-chat view must
# promote the view back to chat so the info-extra template branch renders.
# ---------------------------------------------------------------------------


def _drive_to_status(session: Session) -> None:
    """Switch the session to `view = "status"` and discard the
    resulting events. Subsequent tests then start from a fresh
    subscriber and only see the events under test."""
    asyncio.run(session.execute(ParsedCommand(type=CommandType.STATUS)))
    assert session.view == "status"


def _stub_async_return(value):
    async def _stub(*_args, **_kwargs):
        return value

    return _stub


def test_channels_from_status_view_promotes_to_chat(session: Session) -> None:
    """Issue #20: `/channels` after `/status` was silently swallowed —
    the channels block in `_info.html.j2` only renders under the chat
    branch. The fix forces a view-switch back to chat before publishing
    the info-extra fragment."""
    session._transport.connected = True  # type: ignore[attr-defined]
    session.list_channels = _stub_async_return(["#general", "#ops"])  # type: ignore[assignment]
    _drive_to_status(session)

    sub = session.event_bus.subscribe()
    try:
        asyncio.run(session.execute(ParsedCommand(type=CommandType.CHANNELS)))
        events = sub.drain_nowait()
    finally:
        sub.close()

    assert session.view == "chat"
    names = [e.name for e in events]
    assert names.count("view") == 1, (
        f"expected exactly one `view` event (chat-promotion), got {names}"
    )
    assert names.count("info") == 1, (
        f"expected exactly one `info` event (rendered fragment), got {names}"
    )
    view = next(e for e in events if e.name == "view")
    assert json.loads(view.data) == {"view": "chat"}
    info = next(e for e in events if e.name == "info")
    assert 'data-testid="info-channels-heading"' in info.data, (
        "info fragment must carry the channels heading once the view is "
        "promoted back to chat"
    )
    assert "#general" in info.data and "#ops" in info.data


def test_who_from_help_view_promotes_to_chat(session: Session) -> None:
    """`/who #ops` from the help view: same swallowing trap as /channels."""
    session._transport.connected = True  # type: ignore[attr-defined]
    session.who = _stub_async_return(  # type: ignore[assignment]
        [{"nick": "alice", "flags": "H", "realname": "Alice"}]
    )
    asyncio.run(session.execute(ParsedCommand(type=CommandType.HELP)))
    assert session.view == "help"

    sub = session.event_bus.subscribe()
    try:
        asyncio.run(
            session.execute(ParsedCommand(type=CommandType.WHO, args=["#ops"]))
        )
        events = sub.drain_nowait()
    finally:
        sub.close()

    assert session.view == "chat"
    names = [e.name for e in events]
    assert names.count("view") == 1
    assert names.count("info") == 1
    view = next(e for e in events if e.name == "view")
    assert json.loads(view.data) == {"view": "chat"}
    info = next(e for e in events if e.name == "info")
    assert 'data-testid="info-who-heading"' in info.data
    assert "alice" in info.data


def test_agents_from_overview_view_promotes_to_chat(session: Session) -> None:
    """`/agents` from the overview view: union of WHO results across
    joined channels — same swallowing trap when the active view isn't
    chat."""
    session._transport.connected = True  # type: ignore[attr-defined]
    session.joined_channels.add("#ops")
    session.who = _stub_async_return(  # type: ignore[assignment]
        [{"nick": "alice", "flags": "H"}, {"nick": "bob", "flags": ""}]
    )
    asyncio.run(session.execute(ParsedCommand(type=CommandType.OVERVIEW)))
    assert session.view == "overview"

    sub = session.event_bus.subscribe()
    try:
        asyncio.run(session.execute(ParsedCommand(type=CommandType.AGENTS)))
        events = sub.drain_nowait()
    finally:
        sub.close()

    assert session.view == "chat"
    names = [e.name for e in events]
    assert names.count("view") == 1
    assert names.count("info") == 1
    view = next(e for e in events if e.name == "view")
    assert json.loads(view.data) == {"view": "chat"}
    info = next(e for e in events if e.name == "info")
    assert 'data-testid="info-agents-heading"' in info.data
    assert "alice" in info.data and "bob" in info.data


def test_channels_from_chat_view_does_not_emit_view_event(session: Session) -> None:
    """Control case: when the user is already on the chat view, the
    promotion is a no-op — no spurious `view` event, just the info
    fragment with the channels block. Guards against accidental flicker
    from an unconditional view-publish."""
    session._transport.connected = True  # type: ignore[attr-defined]
    session.list_channels = _stub_async_return(["#general"])  # type: ignore[assignment]
    assert session.view == "chat"  # default

    sub = session.event_bus.subscribe()
    try:
        asyncio.run(session.execute(ParsedCommand(type=CommandType.CHANNELS)))
        events = sub.drain_nowait()
    finally:
        sub.close()

    names = [e.name for e in events]
    assert "view" not in names, (
        f"chat→chat must not emit a view event, got {names}"
    )
    assert names.count("info") == 1
    info = next(e for e in events if e.name == "info")
    assert 'data-testid="info-channels-heading"' in info.data
