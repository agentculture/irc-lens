"""Unit tests for `Session` state transitions and the event bus.

Phase 3 only covers behaviour that doesn't need a live AgentIRC server:
view-state mutators, no-op safety when the transport is offline, the
`LensConnectionLost` translation around the send paths, the IRC
dispatch handlers (driven by hand-built `Message` instances), and the
`SessionEventBus` overflow contract.

The live transport / connect / read-loop sweep lands in Phase 9b
against the real AgentIRC server fixture.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from irc_lens.irc import Message, MessageBuffer
from irc_lens.session import (
    EntityItem,
    LensConnectionLost,
    Session,
    SessionEvent,
    SessionEventBus,
    _OVERFLOW_DATA,
    _Subscriber,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_session() -> Session:
    """A Session whose transport is constructed but never connected.

    `IRCTransport.send_*` writes to `self._writer`, which is None until
    `connect()` runs — so any send is a no-op rather than a real write.
    Perfect for state-transition tests.
    """
    return Session(host="127.0.0.1", port=6667, nick="lens-test")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_session_initial_state(offline_session: Session) -> None:
    s = offline_session
    assert s.host == "127.0.0.1"
    assert s.port == 6667
    assert s.nick == "lens-test"
    assert s.icon is None
    assert s.current_channel == ""
    assert s.joined_channels == set()
    assert s.view == "chat"
    assert s.roster == []
    assert s.healthy is True
    assert s.connected is False
    assert isinstance(s.event_bus, SessionEventBus)
    assert isinstance(s.buffer, MessageBuffer)


def test_session_uses_supplied_event_bus() -> None:
    bus = SessionEventBus(queue_max=4)
    s = Session(host="x", port=0, nick="lens", event_bus=bus)
    assert s.event_bus is bus


def test_session_installs_query_handlers(offline_session: Session) -> None:
    """The transport's dispatch table must include our query numerics."""
    h = offline_session._transport._cmd_handlers
    for cmd in ("322", "323", "352", "315", "HISTORY", "HISTORYEND"):
        assert cmd in h, f"missing dispatcher for {cmd}"


# ---------------------------------------------------------------------------
# View-state mutators
# ---------------------------------------------------------------------------


def test_set_current_channel(offline_session: Session) -> None:
    offline_session.set_current_channel("#ops")
    assert offline_session.current_channel == "#ops"
    offline_session.set_current_channel("")
    assert offline_session.current_channel == ""


def test_set_view(offline_session: Session) -> None:
    for view in ("chat", "help", "overview", "status"):
        offline_session.set_view(view)  # type: ignore[arg-type]
        assert offline_session.view == view


def test_set_roster_copies_input(offline_session: Session) -> None:
    """Mutating the input list afterwards must not mutate session state."""
    src = [EntityItem(nick="alice", type="human")]
    offline_session.set_roster(src)
    src.append(EntityItem(nick="bob", type="agent"))
    assert [e.nick for e in offline_session.roster] == ["alice"]


# ---------------------------------------------------------------------------
# join / part — no-op safety when offline
# ---------------------------------------------------------------------------


def test_join_non_channel_is_noop(offline_session: Session) -> None:
    asyncio.run(offline_session.join("not-a-channel"))
    assert offline_session.joined_channels == set()


def test_join_tracks_channel(offline_session: Session) -> None:
    asyncio.run(offline_session.join("#ops"))
    asyncio.run(offline_session.join("#general"))
    assert offline_session.joined_channels == {"#ops", "#general"}


def test_part_clears_current_when_active(offline_session: Session) -> None:
    asyncio.run(offline_session.join("#ops"))
    offline_session.set_current_channel("#ops")
    asyncio.run(offline_session.part("#ops"))
    assert "#ops" not in offline_session.joined_channels
    assert offline_session.current_channel == ""


def test_part_keeps_current_when_inactive(offline_session: Session) -> None:
    asyncio.run(offline_session.join("#ops"))
    asyncio.run(offline_session.join("#general"))
    offline_session.set_current_channel("#general")
    asyncio.run(offline_session.part("#ops"))
    assert offline_session.current_channel == "#general"
    assert offline_session.joined_channels == {"#general"}


def test_part_non_channel_is_noop(offline_session: Session) -> None:
    asyncio.run(offline_session.join("#ops"))
    asyncio.run(offline_session.part("not-a-channel"))
    assert offline_session.joined_channels == {"#ops"}


# ---------------------------------------------------------------------------
# LensConnectionLost translation
# ---------------------------------------------------------------------------


def _broken_pipe_transport(session: Session) -> None:
    """Replace transport send paths with broken-pipe stubs."""

    async def boom_raw(_line: str) -> None:
        raise BrokenPipeError("simulated")

    async def boom_priv(_target: str, _text: str) -> None:
        raise ConnectionResetError("simulated")

    async def boom_join(_ch: str) -> None:
        raise OSError("simulated")

    async def boom_part(_ch: str) -> None:
        raise ConnectionAbortedError("simulated")

    session._transport.send_raw = boom_raw  # type: ignore[assignment]
    session._transport.send_privmsg = boom_priv  # type: ignore[assignment]
    session._transport.join_channel = boom_join  # type: ignore[assignment]
    session._transport.part_channel = boom_part  # type: ignore[assignment]


def test_send_raw_translates_pipe_error(offline_session: Session) -> None:
    _broken_pipe_transport(offline_session)
    with pytest.raises(LensConnectionLost):
        asyncio.run(offline_session.send_raw("PING :x"))
    assert offline_session.healthy is False


def test_send_privmsg_translates_pipe_error(offline_session: Session) -> None:
    _broken_pipe_transport(offline_session)
    with pytest.raises(LensConnectionLost):
        asyncio.run(offline_session.send_privmsg("#ops", "hi"))
    assert offline_session.healthy is False


def test_join_translates_pipe_error(offline_session: Session) -> None:
    _broken_pipe_transport(offline_session)
    with pytest.raises(LensConnectionLost):
        asyncio.run(offline_session.join("#ops"))
    assert offline_session.healthy is False
    # Membership state must NOT advance on a failed JOIN.
    assert "#ops" not in offline_session.joined_channels


def test_part_translates_pipe_error(offline_session: Session) -> None:
    _broken_pipe_transport(offline_session)
    # Pre-seed the joined set so we can verify it survives a failed PART.
    offline_session.joined_channels.add("#ops")
    with pytest.raises(LensConnectionLost):
        asyncio.run(offline_session.part("#ops"))
    assert offline_session.healthy is False
    assert "#ops" in offline_session.joined_channels


def test_connect_translates_to_lens_connection_lost() -> None:
    s = Session(host="127.0.0.1", port=1, nick="lens-test")  # closed port

    async def boom_connect() -> None:
        raise ConnectionError("Cannot connect")

    s._transport.connect = boom_connect  # type: ignore[assignment]
    with pytest.raises(LensConnectionLost):
        asyncio.run(s.connect())
    assert s.healthy is False


def test_connect_disables_transport_auto_reconnect() -> None:
    """Spec lifecycle: 'no auto-reconnect in v1'.

    `IRCTransport.connect()` sets `_should_run = True`, which would let
    `_read_loop`'s finally block spawn `_reconnect()` on EOF. Session
    must flip the gate back to False so the read loop terminates
    cleanly when the socket closes.
    """
    s = Session(host="x", port=0, nick="lens")

    async def fake_connect() -> None:
        s._transport._should_run = True  # mimic IRCTransport.connect()

    s._transport.connect = fake_connect  # type: ignore[assignment]
    asyncio.run(s.connect())
    assert s._transport._should_run is False, (
        "Session.connect() must disable transport auto-reconnect (no "
        "auto-reconnect in v1 per the spec)"
    )


# ---------------------------------------------------------------------------
# Future-based query methods
# ---------------------------------------------------------------------------


def _make_msg(command: str, *params: str) -> Message:
    return Message(prefix=None, command=command, params=list(params), tags={})


def test_list_channels_collects_and_sorts(offline_session: Session) -> None:
    """Drive _on_rpl_list/_on_rpl_listend by hand and assert sorting."""

    async def run() -> list[str]:
        sent: list[str] = []

        async def fake_send(line: str) -> None:
            sent.append(line)

            async def fire() -> None:
                offline_session._on_rpl_list(_make_msg("322", "lens-test", "#zeta"))
                offline_session._on_rpl_list(_make_msg("322", "lens-test", "#alpha"))
                offline_session._on_rpl_list(_make_msg("322", "lens-test", "#mid"))
                offline_session._on_rpl_listend(_make_msg("323", "lens-test", "End"))

            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(fire()))

        offline_session._transport.send_raw = fake_send  # type: ignore[assignment]
        result = await offline_session.list_channels()
        assert sent == ["LIST"]
        return result

    assert asyncio.run(run()) == ["#alpha", "#mid", "#zeta"]


def test_who_collects_entries(offline_session: Session) -> None:
    async def run() -> list[dict]:
        async def fake_send(line: str) -> None:
            async def fire() -> None:
                offline_session._on_rpl_whoreply(
                    _make_msg("352", "lens-test", "#ops", "alice", "host1", "srv", "a", "H", "alice real")
                )
                offline_session._on_rpl_endofwho(_make_msg("315", "lens-test", "#ops", "End"))

            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(fire()))

        offline_session._transport.send_raw = fake_send  # type: ignore[assignment]
        return await offline_session.who("#ops")

    entries = asyncio.run(run())
    assert len(entries) == 1
    assert entries[0]["nick"] == "a"  # WHO param[5] per upstream shape


def test_history_collects_entries(offline_session: Session) -> None:
    async def run() -> list[dict]:
        async def fake_send(line: str) -> None:
            async def fire() -> None:
                offline_session._on_history(
                    _make_msg("HISTORY", "#ops", "alice", "1234", "hello")
                )
                offline_session._on_historyend(_make_msg("HISTORYEND", "#ops"))

            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(fire()))

        offline_session._transport.send_raw = fake_send  # type: ignore[assignment]
        return await offline_session.history("#ops", limit=10)

    entries = asyncio.run(run())
    assert entries == [{"channel": "#ops", "nick": "alice", "timestamp": "1234", "text": "hello"}]


def test_query_timeout_returns_partial(offline_session: Session, monkeypatch) -> None:
    """When the END numeric never arrives, the query times out and returns
    whatever was collected so far rather than raising."""

    monkeypatch.setattr("irc_lens.session.QUERY_TIMEOUT", 0.05)

    async def run() -> list[str]:
        async def fake_send(line: str) -> None:
            offline_session._on_rpl_list(_make_msg("322", "lens-test", "#solo"))
            # No 323 — wait_for hits the timeout.

        offline_session._transport.send_raw = fake_send  # type: ignore[assignment]
        return await offline_session.list_channels()

    assert asyncio.run(run()) == ["#solo"]


def test_query_clears_state_on_send_failure(offline_session: Session) -> None:
    """If the underlying send raises LensConnectionLost, we must drop the
    pending future + collect-buffer; otherwise a retry would deadlock or
    stack stale state."""
    _broken_pipe_transport(offline_session)
    with pytest.raises(LensConnectionLost):
        asyncio.run(offline_session.list_channels())
    assert "LIST" not in offline_session._collect_buffers
    assert "323" not in offline_session._pending


def test_concurrent_list_calls_serialize(offline_session: Session) -> None:
    """Two concurrent list_channels() calls on the same Session must not
    clobber each other's collect-buffer + pending future. The per-key
    lock serialises them; each call sees its own canned response."""

    async def run() -> tuple[list[str], list[str]]:
        first_call_active = asyncio.Event()
        first_call_complete = asyncio.Event()

        async def fake_send(line: str) -> None:
            # Tag which call we're inside via what the buffer already
            # holds — first call sets `first_call_active`, holds, then
            # fires its own END; second call follows.
            if not first_call_active.is_set():
                first_call_active.set()

                async def fire_first() -> None:
                    offline_session._on_rpl_list(_make_msg("322", "lens", "#a1"))
                    offline_session._on_rpl_list(_make_msg("322", "lens", "#a2"))
                    # Wait until the second call is also started so we
                    # can prove they don't interleave; release ours.
                    await asyncio.sleep(0.02)
                    offline_session._on_rpl_listend(_make_msg("323", "lens", "End"))
                    first_call_complete.set()

                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.ensure_future(fire_first())
                )
            else:

                async def fire_second() -> None:
                    offline_session._on_rpl_list(_make_msg("322", "lens", "#b1"))
                    offline_session._on_rpl_listend(_make_msg("323", "lens", "End"))

                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.ensure_future(fire_second())
                )

        offline_session._transport.send_raw = fake_send  # type: ignore[assignment]
        a, b = await asyncio.gather(
            offline_session.list_channels(),
            offline_session.list_channels(),
        )
        return a, b

    a, b = asyncio.run(run())
    # Each call sees its own response (no cross-talk).
    assert a == ["#a1", "#a2"]
    assert b == ["#b1"]


# ---------------------------------------------------------------------------
# SessionEventBus
# ---------------------------------------------------------------------------


def test_event_bus_publish_to_one_subscriber() -> None:
    bus = SessionEventBus(queue_max=8)

    async def run() -> list[SessionEvent]:
        received: list[SessionEvent] = []
        sub = bus.subscribe()
        # Subscription is registered immediately, so publish-before-iterate
        # still queues into our subscriber.
        bus.publish(SessionEvent(name="chat", data="<li>hi</li>"))
        bus.publish(SessionEvent(name="roster", data="<aside/>"))
        async for event in sub.events():
            received.append(event)
            if len(received) == 2:
                break
        return received

    received = asyncio.run(run())
    assert [(e.name, e.data) for e in received] == [
        ("chat", "<li>hi</li>"),
        ("roster", "<aside/>"),
    ]


def test_event_bus_publish_to_multiple_subscribers() -> None:
    bus = SessionEventBus(queue_max=8)

    async def run() -> tuple[list[str], list[str]]:
        a = bus.subscribe()
        b = bus.subscribe()
        bus.publish(SessionEvent(name="chat", data="<x/>"))

        async def first(sub) -> str:
            async for event in sub.events():
                return event.name
            return ""

        return await asyncio.gather(first(a), first(b))  # type: ignore[return-value]

    a, b = asyncio.run(run())
    assert a == "chat"
    assert b == "chat"


def test_event_bus_publish_no_subscribers_is_noop() -> None:
    bus = SessionEventBus()
    bus.publish(SessionEvent(name="chat", data="ignored"))  # must not raise
    assert bus.subscriber_count == 0


def test_event_bus_overflow_emits_single_error_then_drops() -> None:
    """Spec: bounded per-subscriber queue, drop-oldest, single overflow error.

    Test directly against `_Subscriber` so we can inspect the queue
    contents without driving the iterator. A small burst (queue_max=4,
    push 6) exercises overflow without itself evicting the error event
    from the queue. Sync — `_Subscriber.publish` is a plain method.
    """
    sub = _Subscriber(queue_max=4)
    for i in range(4):
        sub.publish(SessionEvent(name="chat", data=f"a{i}"))
    # Two extra publishes push us into the overflow path twice; the
    # first one injects exactly one error event, the second does NOT
    # (single-shot per burst).
    sub.publish(SessionEvent(name="chat", data="b0"))
    sub.publish(SessionEvent(name="chat", data="b1"))
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait())
    names = [e.name for e in drained]
    assert names.count("error") == 1, f"expected exactly one error event; got {names}"


def test_overflow_flag_clears_after_queue_drains() -> None:
    """A later overflow burst (after the queue catches up) issues its own
    error notice, rather than being silently coalesced forever. Sync —
    `_Subscriber.publish` is a plain method."""
    sub = _Subscriber(queue_max=2)
    sub.publish(SessionEvent(name="chat", data="a0"))
    sub.publish(SessionEvent(name="chat", data="a1"))
    sub.publish(SessionEvent(name="chat", data="b0"))  # first overflow
    first_names: list[str] = []
    while not sub.queue.empty():
        first_names.append(sub.queue.get_nowait().name)
    # Second burst: first the queue gets a normal publish (clears the
    # flag because the queue had room), then refills + overflows.
    sub.publish(SessionEvent(name="chat", data="c0"))
    sub.publish(SessionEvent(name="chat", data="c1"))
    sub.publish(SessionEvent(name="chat", data="d0"))  # second overflow
    second_names: list[str] = []
    while not sub.queue.empty():
        second_names.append(sub.queue.get_nowait().name)
    assert first_names.count("error") == 1
    assert second_names.count("error") == 1


def test_overflow_error_payload_matches_spec() -> None:
    """Overflow error event uses the documented payload shape."""
    sub = _Subscriber(queue_max=1)
    sub.publish(SessionEvent(name="chat", data="first"))
    sub.publish(SessionEvent(name="chat", data="second"))  # triggers overflow

    seen = []
    while not sub.queue.empty():
        seen.append(sub.queue.get_nowait())
    payloads = [e.data for e in seen if e.name == "error"]
    assert _OVERFLOW_DATA in payloads


def test_overflow_keeps_newest_event() -> None:
    """Spec: 'drop oldest on overflow' — the newest event must always
    survive, even on the burst's first overflow that injects the error."""
    sub = _Subscriber(queue_max=4)
    for i in range(4):
        sub.publish(SessionEvent(name="chat", data=f"old{i}"))
    # First overflow: must drop oldest, inject error, AND retain `latest`.
    sub.publish(SessionEvent(name="chat", data="latest"))
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait())
    datas = [e.data for e in drained]
    assert "latest" in datas, f"newest event must survive overflow; got {datas}"
    # The error event is still present.
    assert any(e.name == "error" for e in drained)


def test_overflow_subsequent_within_burst_keeps_newest() -> None:
    """Subsequent overflows within the same burst (flag already set)
    must still retain the newest event."""
    sub = _Subscriber(queue_max=2)
    sub.publish(SessionEvent(name="chat", data="a0"))
    sub.publish(SessionEvent(name="chat", data="a1"))
    sub.publish(SessionEvent(name="chat", data="b0"))  # first overflow
    sub.publish(SessionEvent(name="chat", data="b1"))  # second overflow (flag set)
    sub.publish(SessionEvent(name="chat", data="b2"))  # third overflow
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait().data)
    assert "b2" in drained, f"newest of subsequent overflows must survive; got {drained}"


def test_subscription_close_unregisters() -> None:
    """Phase 5's SSE handler calls ``Subscription.close()`` from a
    ``finally`` to handle client-disconnect mid-stream."""
    bus = SessionEventBus()
    sub = bus.subscribe()
    assert bus.subscriber_count == 1
    sub.close()
    assert bus.subscriber_count == 0
    assert sub.closed is True


def test_subscription_close_is_idempotent() -> None:
    bus = SessionEventBus()
    sub = bus.subscribe()
    sub.close()
    sub.close()  # must not raise or double-remove
    assert bus.subscriber_count == 0


def test_subscription_iterator_closes_on_explicit_aclose() -> None:
    """The events() generator's finally fires on aclose(), unregistering
    the subscription. The Phase 5 SSE handler will also call
    Subscription.close() in a `finally` to handle the client-disconnect
    path where the generator is suspended waiting on `queue.get()` and
    the consuming task is cancelled before the generator can run its
    own cleanup."""
    bus = SessionEventBus(queue_max=4)

    async def run() -> int:
        sub = bus.subscribe()
        bus.publish(SessionEvent(name="chat", data="x"))
        gen = sub.events()
        async for _ev in gen:
            break
        await gen.aclose()
        return bus.subscriber_count

    assert asyncio.run(run()) == 0
