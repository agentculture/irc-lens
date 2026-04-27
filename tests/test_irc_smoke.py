"""Smoke tests for the cited IRC primitives (transport / buffer / message).

Phase 2 only proves the citations import cleanly and their pure-data
contracts work — the live ``IRCTransport`` connect/read-loop is exercised
via the AgentIRC server fixture in Phase 9b. A unit-level message
parser test guards the IRCv3 tag adaptation and the read-loop's reliance
on ``Message.parse``.
"""

from __future__ import annotations

import time

from irc_lens.irc import BufferedMessage, IRCTransport, Message, MessageBuffer


def test_imports_resolve() -> None:
    """Phase 2 verification block: every cited symbol importable."""
    assert IRCTransport is not None
    assert MessageBuffer is not None
    assert Message is not None
    assert BufferedMessage is not None


def test_message_parse_simple_privmsg() -> None:
    msg = Message.parse(":alice!a@host PRIVMSG #ops :hello there")
    assert msg.prefix == "alice!a@host"
    assert msg.command == "PRIVMSG"
    assert msg.params == ["#ops", "hello there"]
    assert msg.tags == {}


def test_message_parse_with_ircv3_tags() -> None:
    """Lens strips CAP REQ but the parser still tolerates inbound tags."""
    msg = Message.parse("@key=val;flag :bob PRIVMSG #ops :hi")
    assert msg.tags == {"key": "val", "flag": ""}
    assert msg.command == "PRIVMSG"
    assert msg.prefix == "bob"


def test_message_parse_numeric() -> None:
    msg = Message.parse(":server 332 lens-nick #ops :the topic")
    assert msg.command == "332"
    assert msg.params == ["lens-nick", "#ops", "the topic"]


def test_message_buffer_add_and_read() -> None:
    buf = MessageBuffer(max_per_channel=10)
    buf.add("#ops", "alice", "hi")
    buf.add("#ops", "bob", "hello")
    msgs = buf.read("#ops", limit=10)
    assert [m.nick for m in msgs] == ["alice", "bob"]
    # Cursor is advanced — second read drains nothing new.
    assert buf.read("#ops", limit=10) == []


def test_message_buffer_thread_extraction() -> None:
    buf = MessageBuffer()
    buf.add("#ops", "alice", "[thread:foo] threaded line")
    buf.add("#ops", "alice", "untreaded line")
    threaded = buf.read_thread("#ops", "foo", limit=10)
    assert len(threaded) == 1
    assert threaded[0].thread == "foo"


def test_buffered_message_dataclass() -> None:
    bm = BufferedMessage(nick="alice", text="hi", timestamp=time.time())
    assert bm.thread is None


def test_irc_transport_constructs_without_telemetry() -> None:
    """Constructor signature dropped tracer/metrics/backend kwargs."""
    transport = IRCTransport(
        host="127.0.0.1",
        port=6667,
        nick="lens",
        user="lens",
        channels=["#ops"],
        buffer=MessageBuffer(),
    )
    assert transport.host == "127.0.0.1"
    assert transport.connected is False
    assert "PRIVMSG" in transport._cmd_handlers
