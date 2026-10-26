"""'<agent> is thinking' in sandbox rooms (SSE `typing`).

After a message is sent in a room the sandbox agent is in, the room shows
"<agent> is thinking" until the agent answers there, the timeout passes,
or the session disconnects. Lens-side for now: the agent itself sends no
typing signal yet (IRCv3 TAGMSG +typing is the follow-up).
"""

from __future__ import annotations

import asyncio

import pytest

from irc_lens import session as session_mod
from irc_lens.commands import CommandType, ParsedCommand
from irc_lens.irc import Message
from irc_lens.session import EntityItem, Session, SessionEvent
from irc_lens.web.render import render_index

AGENT = "sbx-ask"
ROOM = "#g-abc123"


def _sandbox_session(*, agent_in_room: bool = True) -> Session:
    s = Session(host="127.0.0.1", port=6668, nick="sbx-ann")
    s.ui_tier = "guest"
    s.thinking_agent = AGENT
    s.current_channel = ROOM
    s.joined_channels.add(ROOM)
    roster = [EntityItem(nick="sbx-ann", type="human")]
    if agent_in_room:
        roster.append(EntityItem(nick=AGENT, type="agent"))
    s.set_roster(roster)
    return s


def _typing(events: list[SessionEvent]) -> list[str]:
    return [e.data for e in events if e.name == "typing"]


async def _say(s: Session, text: str = "hello") -> None:
    await s.execute(ParsedCommand(type=CommandType.CHAT, text=text))


async def _agent_says(s: Session, target: str = ROOM) -> None:
    await s.dispatch(Message(prefix=f"{AGENT}!a@h", command="PRIVMSG", params=[target, "hi"]))


async def test_sending_shows_thinking_until_the_agent_answers() -> None:
    s = _sandbox_session()
    sub = s.event_bus.subscribe()
    await _say(s)
    shown = _typing(sub.drain_nowait())
    assert len(shown) == 1
    assert "sbx-ask</span> is thinking" in shown[0]
    assert 'data-testid="agent-typing"' in shown[0]
    assert s.thinking_in(ROOM) == AGENT
    await _agent_says(s)
    cleared = _typing(sub.drain_nowait())
    assert cleared == [""]
    assert s.thinking_in(ROOM) is None
    sub.close()


async def test_no_indicator_without_the_agent_or_on_the_real_mesh() -> None:
    away = _sandbox_session(agent_in_room=False)
    sub = away.event_bus.subscribe()
    await _say(away)
    assert _typing(sub.drain_nowait()) == []
    mesh = Session(host="127.0.0.1", port=6667, nick="spark-ori")
    mesh.current_channel = "#general"
    mesh.set_roster([EntityItem(nick=AGENT, type="agent")])
    sub2 = mesh.event_bus.subscribe()
    await _say(mesh)
    assert _typing(sub2.drain_nowait()) == []  # thinking_agent is None


async def test_other_speakers_do_not_clear_it() -> None:
    s = _sandbox_session()
    await _say(s)
    await s.dispatch(Message(prefix="sbx-bob!b@h", command="PRIVMSG", params=[ROOM, "x"]))
    assert s.thinking_in(ROOM) == AGENT


async def test_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "THINKING_TIMEOUT_S", 0.01)
    s = _sandbox_session()
    sub = s.event_bus.subscribe()
    await _say(s)
    await asyncio.sleep(0.05)
    assert s.thinking_in(ROOM) is None
    assert _typing(sub.drain_nowait())[-1] == ""


async def test_answer_in_another_room_clears_that_room_quietly() -> None:
    s = _sandbox_session()
    await _say(s)
    s.current_channel = "#g-other"
    sub = s.event_bus.subscribe()
    await _agent_says(s, ROOM)
    assert s.thinking_in(ROOM) is None
    assert _typing(sub.drain_nowait()) == []  # not the active pane


async def test_room_switch_publishes_that_rooms_state() -> None:
    s = _sandbox_session()
    await _say(s)
    s.current_channel = "#g-other"
    sub = s.event_bus.subscribe()
    s._publish_log("")
    assert _typing(sub.drain_nowait()) == [""]
    s.current_channel = ROOM
    s._publish_log("")
    assert "is thinking" in _typing(sub.drain_nowait())[0]


async def test_page_render_and_disconnect() -> None:
    s = _sandbox_session()
    await _say(s)
    html = render_index(s)
    assert 'id="typing"' in html
    assert 'data-testid="agent-typing"' in html
    await s.disconnect()
    assert s.thinking_in(ROOM) is None
    assert 'data-testid="agent-typing"' not in render_index(s)


# -- several agents: the named one thinks ---------------------------------

BOLT = "sbx-bolt"


def _two_agent_session() -> Session:
    s = _sandbox_session()
    s.set_roster([*s.roster, EntityItem(nick=BOLT, type="agent")])
    return s


async def test_tagging_an_agent_shows_that_agent() -> None:
    s = _two_agent_session()
    await _say(s, "@sbx-bolt what do you think?")
    assert s.thinking_in(ROOM) == BOLT
    await _agent_says(s)  # sbx-ask speaking doesn't clear bolt
    assert s.thinking_in(ROOM) == BOLT
    await s.dispatch(Message(prefix=f"{BOLT}!b@h", command="PRIVMSG", params=[ROOM, "x"]))
    assert s.thinking_in(ROOM) is None


async def test_untagged_human_message_shows_the_sandbox_agent() -> None:
    s = _two_agent_session()
    await _say(s, "hello there")
    assert s.thinking_in(ROOM) == AGENT


async def test_an_agent_mentioning_another_by_bare_nick_does_not() -> None:
    """Between agents only "@nick" hands over (a bare nick is a mention):
    the same rule the sandbox agents apply."""
    s = _two_agent_session()
    await _say(s, "@sbx-bolt hi")
    reply = "sbx-ann: for docs questions, sbx-ask is the one to ask."
    await s.dispatch(Message(prefix=f"{BOLT}!b@h", command="PRIVMSG", params=[ROOM, reply]))
    assert s.thinking_in(ROOM) is None


async def test_human_at_tag_beats_a_bare_mention() -> None:
    s = _two_agent_session()
    await _say(s, "@sbx-bolt please ask sbx-ask for me")
    assert s.thinking_in(ROOM) == BOLT


async def test_an_agent_naming_another_agent_hands_over() -> None:
    s = _two_agent_session()
    await _say(s, "@sbx-bolt ask sbx-ask something")
    reply = "sbx-ann: sure. @sbx-ask what is culture?"
    await s.dispatch(Message(prefix=f"{BOLT}!b@h", command="PRIVMSG", params=[ROOM, reply]))
    assert s.thinking_in(ROOM) == AGENT
