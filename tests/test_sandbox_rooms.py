"""Private guest rooms, live roster and per-session agent presence (d6).

Found in the 2026-10-09 browser pass: guests shared one #general with full
history, the sidebar member list was never filled from the server, and the
agent read 'offline' whenever it had been quiet for 60 s.
"""

from __future__ import annotations

import pytest

from _agentirc_server import AgentIRCTestServer
from irc_lens.session import EntityItem, Session
from irc_lens.web.render import render_chat_log
from irc_lens.web.sessions import guest_room, sandbox_presence


def test_guest_room_is_prefix_plus_nick_without_server_prefix() -> None:
    assert guest_room("sbx-vis1", "#g-") == "#g-vis1"
    assert guest_room("vis1", "#g-") == "#g-vis1"


def test_sandbox_presence_follows_room_membership() -> None:
    s = Session(host="127.0.0.1", port=1, nick="sbx-gus")
    s.set_roster([EntityItem("sbx-gus", "human")])
    assert sandbox_presence(s, "sbx-ask")["state"] == "offline"
    s.set_roster([EntityItem("sbx-gus", "human"), EntityItem("sbx-ask", "agent")])
    state = sandbox_presence(s, "sbx-ask")
    assert state["state"] == "online"
    assert state["online"] is True


def test_history_hides_system_lines_only_when_asked() -> None:
    entries = [
        {"nick": "system-sbx", "text": "sbx-x joined #g-x", "timestamp": "1"},
        {"nick": "system-sbx-welcome", "text": "Welcome sbx-x", "timestamp": "2"},
        {"nick": "sbx-ask", "text": "hello there", "timestamp": "3"},
    ]
    shown = render_chat_log(entries, hide_system=True)
    assert "hello there" in shown
    assert "joined" not in shown
    assert "Welcome" not in shown
    full = render_chat_log(entries)
    assert "joined" in full
    assert "Welcome" in full


@pytest.mark.asyncio
async def test_roster_filled_from_server_on_join() -> None:
    server = AgentIRCTestServer()
    await server.start()
    a = Session(host=server.host, port=server.port, nick="sbx-ask")
    b = Session(host=server.host, port=server.port, nick="sbx-gus")
    try:
        # The fake IRCd tracks one current nick, so connect+join in turn.
        for s in (a, b):
            await s.connect()
            await s.wait_for_welcome()
            await s.join("#g-gus")
        b.set_current_channel("#g-gus")
        await b.refresh_roster()
        assert {e.nick for e in b.roster} == {"sbx-ask", "sbx-gus"}
    finally:
        for s in (a, b):
            await s.disconnect()
        await server.stop()


@pytest.mark.asyncio
async def test_roster_refresh_requested_mid_flight_is_not_lost(monkeypatch) -> None:
    """The agent JOINs while the lens's first WHO is in flight: the refresh it
    triggers must run after, not race the same-key WHO (browser pass: guest
    B's room showed no agent although sbx-ask was in it)."""
    import asyncio

    s = Session(host="127.0.0.1", port=1, nick="sbx-gus")
    s.set_current_channel("#g-gus")
    monkeypatch.setattr(Session, "connected", property(lambda self: True))
    replies = [
        [{"nick": "sbx-gus", "flags": "H", "realname": "sbx-gus"}],
        [
            {"nick": "sbx-gus", "flags": "H", "realname": "sbx-gus"},
            {"nick": "sbx-ask", "flags": "H", "realname": "tool-less Q&A agent"},
        ],
    ]
    inflight = 0
    peak = 0

    async def who(_target):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return replies.pop(0) if replies else replies_last

    replies_last = replies[1]
    s.who = who  # type: ignore[method-assign]
    first = asyncio.create_task(s.refresh_roster())
    await asyncio.sleep(0.01)
    await s.refresh_roster()  # the agent's JOIN arrives mid-flight
    await first
    assert peak == 1
    assert {e.nick for e in s.roster} == {"sbx-gus", "sbx-ask"}
