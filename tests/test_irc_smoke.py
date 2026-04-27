"""Smoke tests for the cited IRC primitives (transport / buffer / message).

Phase 2 only proves the citations import cleanly and their pure-data
contracts work — the live ``IRCTransport`` connect/read-loop is exercised
via the AgentIRC server fixture in Phase 9b. A unit-level message
parser test guards the IRCv3 tag adaptation and the read-loop's reliance
on ``Message.parse``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from irc_lens.irc import BufferedMessage, IRCTransport, Message, MessageBuffer
from irc_lens.irc.transport import _maybe_await


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
    buf.add("#ops", "alice", "unthreaded line")
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


def test_read_loop_handles_split_utf8_multibyte() -> None:
    """Decoding per chunk would corrupt multibyte chars split across reads.

    Regression for the upstream bug: `data.decode("utf-8", errors="replace")`
    on each ``recv`` chunk replaces a half-arrived sequence with U+FFFD.
    The fix buffers as bytes and decodes per complete line.
    """

    class _SplitReader:
        """Yields a UTF-8 line in two reads that bisect a multibyte char."""

        def __init__(self) -> None:
            full = b":alice PRIVMSG #ops :hello \xe2\x9c\x85 done\n"
            cut = full.index(b"\xe2") + 1  # mid-sequence split
            self._chunks = [full[:cut], full[cut:], b""]

        async def read(self, _n: int) -> bytes:
            return self._chunks.pop(0)

    transport = IRCTransport(
        host="x", port=0, nick="lens", user="lens",
        channels=[], buffer=MessageBuffer(),
    )
    transport._reader = _SplitReader()  # type: ignore[assignment]
    transport._should_run = False  # don't spawn a reconnect task

    received: list[Message] = []

    async def capture(msg: Message) -> None:
        received.append(msg)

    transport._handle = capture  # type: ignore[assignment]
    asyncio.run(transport._read_loop())

    assert len(received) == 1
    text = received[0].params[-1]
    assert "✅" in text, f"check-mark corrupted: {text!r}"
    assert "�" not in text


def test_reconnect_keeps_retrying_after_connection_error(monkeypatch) -> None:
    """`_do_connect` wraps OSError as ConnectionError; reconnect must catch both."""

    transport = IRCTransport(
        host="x", port=0, nick="lens", user="lens",
        channels=[], buffer=MessageBuffer(),
    )
    transport._should_run = True

    attempts = {"n": 0}

    async def flaky_connect() -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            # Same shape as `_do_connect`'s real error path.
            raise ConnectionError("Cannot connect to IRC server")
        transport._should_run = False  # let the loop exit cleanly

    transport._do_connect = flaky_connect  # type: ignore[assignment]

    async def no_sleep(_d: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    asyncio.run(transport._reconnect())

    assert attempts["n"] == 3
    assert transport._reconnecting is False, "_reconnecting must be released"


def test_reconnect_releases_gate_on_unexpected_exception(monkeypatch) -> None:
    """If reconnect bails for any reason, `_reconnecting` must not stay True."""

    transport = IRCTransport(
        host="x", port=0, nick="lens", user="lens",
        channels=[], buffer=MessageBuffer(),
    )
    transport._should_run = True

    async def boom() -> None:
        raise RuntimeError("totally unexpected")

    transport._do_connect = boom  # type: ignore[assignment]

    async def no_sleep(_d: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with pytest.raises(RuntimeError):
        asyncio.run(transport._reconnect())

    assert transport._reconnecting is False


def test_maybe_await_passes_through_sync_value() -> None:
    """Inlined helper must handle both coroutines and plain values."""

    async def acoro() -> int:
        return 7

    assert asyncio.run(_maybe_await(acoro())) == 7
    assert asyncio.run(_maybe_await(42)) == 42
