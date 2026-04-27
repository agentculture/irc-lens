"""Session — the lens's owner of one IRC connection.

A `Session` wires together the cited transport / buffer / commands and
adds the bits the spec asks for that don't belong in a re-cited file:

* ``LensConnectionLost`` — raised by send paths when the underlying
  socket is broken; ``POST /input`` will translate this to ``503``.
* View state — ``current_channel``, ``joined_channels``, ``view``,
  ``roster``.
* Future-based query methods (``list_channels``, ``who``, ``history``)
  using the *collect-buffer + future* pattern from
  ``../culture/culture/console/client.py:206-288``. The shape is
  reused; that file is **not** imported.
* ``SessionEvent`` + ``SessionEventBus`` — a bounded, drop-oldest pub/sub
  bus that Phase 5 will wire into the SSE response stream. For Phase 3
  the bus is interface-only: Session can hold one and publish events
  without crashing, so that future-phase wiring is plug-and-play.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from irc_lens.irc import IRCTransport, Message, MessageBuffer

logger = logging.getLogger(__name__)

# Mirrors ``culture/console/client.py``'s constants.
QUERY_TIMEOUT = 10.0
REGISTER_TIMEOUT = 15.0

#: The four named views the spec's ``info`` event can switch between.
ViewName = Literal["chat", "help", "overview", "status"]

#: SSE event names defined in the spec's "SSE event types" section.
EventName = Literal["chat", "roster", "info", "view", "error"]


class LensConnectionLost(ConnectionError):
    """Raised when the underlying IRC socket is broken mid-send.

    ``POST /input`` translates this to HTTP 503 (see Phase 5). The SSE
    stream is left open so the user sees a system chat line surfaced by
    the dispatcher.
    """


@dataclass
class EntityItem:
    """A roster entry for the sidebar (`_sidebar.j2`)."""

    nick: str
    type: str  # "human" | "agent" | "server" | …
    online: bool = True


@dataclass
class SessionEvent:
    """One reactive update destined for the browser.

    For ``chat`` / ``roster`` / ``info`` the payload is a pre-rendered
    HTML fragment (Phase 6 finalises the templates). For ``view`` /
    ``error`` it is a JSON-serialised dict per the spec's table. The
    bus is payload-agnostic — Session is responsible for emitting the
    right shape for each name.
    """

    name: EventName
    data: str


_OVERFLOW_DATA = '{"message":"events dropped"}'


class _Subscriber:
    """Per-subscriber bounded queue with single-shot overflow signalling.

    Drop-oldest semantics mean a slow consumer falls behind in real
    time but never blocks publishers. A "burst" is a continuous run of
    overflows; the first overflow in a burst injects one ``error``
    event so the browser can toast it. The flag is cleared by the next
    *non-overflow* publish (the queue caught up), which means a later
    burst gets its own error notice.
    """

    def __init__(self, queue_max: int) -> None:
        self.queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=queue_max)
        self._overflow_flagged = False

    def publish(self, event: SessionEvent) -> None:
        if not self.queue.full():
            self.queue.put_nowait(event)
            # Queue had room; we're out of any prior overflow burst, so
            # the next burst is allowed to issue a fresh error event.
            self._overflow_flagged = False
            return
        # Overflow path: drop oldest, signal once, then try the new one.
        try:
            self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        if not self._overflow_flagged:
            err = SessionEvent(name="error", data=_OVERFLOW_DATA)
            try:
                self.queue.put_nowait(err)
                self._overflow_flagged = True
            except asyncio.QueueFull:
                # Bus completely jammed; the subscriber will see the
                # next event whenever it drains. Don't loop forever.
                pass
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def iter(self) -> AsyncIterator[SessionEvent]:
        while True:
            event = await self.queue.get()
            yield event


class Subscription:
    """A registered subscriber with an explicit ``close()``.

    Returned by ``SessionEventBus.subscribe()``. The handle is registered
    immediately so events ``publish``-ed between subscribe and the first
    ``events()`` await are queued, not lost. Iterating ``events()`` to
    completion calls ``close()`` automatically; the SSE handler in
    Phase 5 will also call ``close()`` from a ``finally`` block to
    cover the client-disconnect case where iteration is interrupted
    before the generator's own finally fires.
    """

    def __init__(self, bus: "SessionEventBus", queue_max: int) -> None:
        self._bus = bus
        self._sub = _Subscriber(queue_max)
        bus._subscribers.append(self._sub)
        self._closed = False

    def publish(self, event: SessionEvent) -> None:
        """Direct publish — used by the bus and exposed for tests."""
        self._sub.publish(event)

    async def events(self) -> AsyncIterator[SessionEvent]:
        try:
            async for event in self._sub.iter():
                yield event
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._bus._subscribers.remove(self._sub)
        except ValueError:
            pass
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class SessionEventBus:
    """Bounded, drop-oldest pub/sub for ``SessionEvent`` instances.

    Every ``subscribe()`` returns a ``Subscription`` whose own queue
    defaults to 256. ``publish()`` is fire-and-forget and never awaits;
    that's what makes it safe to call from synchronous IRC handlers in
    ``IRCTransport``'s dispatch table.
    """

    def __init__(self, *, queue_max: int = 256) -> None:
        self._queue_max = queue_max
        self._subscribers: list[_Subscriber] = []

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> Subscription:
        return Subscription(self, self._queue_max)

    def publish(self, event: SessionEvent) -> None:
        for sub in self._subscribers:
            sub.publish(event)


# IRC numerics used by the future-based query methods.
_RPL_LIST = "322"
_RPL_LISTEND = "323"
_RPL_WHOREPLY = "352"
_RPL_ENDOFWHO = "315"
_HISTORY = "HISTORY"
_HISTORYEND = "HISTORYEND"


class Session:
    """The lens's view of one AgentIRC connection.

    Owns one ``IRCTransport`` and one ``MessageBuffer``, layers query
    methods (LIST/WHO/HISTORY) on top via the future-based collect-buffer
    pattern, holds the view state the SSE renderer needs, and exposes a
    ``SessionEventBus`` that Phase 5 will wire into ``GET /events``.

    The session does **not** spin up its own IRC read loop — it
    registers extra handlers in ``self._transport._cmd_handlers`` so the
    transport's existing read loop dispatches query responses to us.
    """

    def __init__(
        self,
        host: str,
        port: int,
        nick: str,
        *,
        icon: str | None = None,
        event_bus: SessionEventBus | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.nick = nick
        self.icon = icon

        # View state (mutated by command dispatch in Phase 5+).
        self.current_channel: str = ""
        self.joined_channels: set[str] = set()
        self.view: ViewName = "chat"
        self.roster: list[EntityItem] = []

        # Cited primitives.
        self.buffer = MessageBuffer()
        self._transport = IRCTransport(
            host=host,
            port=port,
            nick=nick,
            user=nick,
            channels=[],
            buffer=self.buffer,
            icon=icon,
        )
        self._install_query_handlers()

        # Future + collect-buffer state for LIST / WHO / HISTORY.
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._collect_buffers: dict[str, list[Any]] = {}

        # Event bus: Phase 5 will wire publishes; Phase 3 just holds it.
        self.event_bus = event_bus if event_bus is not None else SessionEventBus()

        # Tracks transport health independent of `IRCTransport.connected`,
        # which only flips after the welcome (001) handshake. The lens
        # cares about "did we ever lose the pipe?" because Phase 5
        # marks the session unhealthy on first `LensConnectionLost`.
        self._healthy = True

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the TCP connection and register. Raises ``LensConnectionLost``
        on connect failure so callers can translate to a clean stderr
        message + non-zero exit (Phase 4 wires this into ``serve``)."""
        try:
            await self._transport.connect()
        except (OSError, ConnectionError) as exc:
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc

    async def disconnect(self) -> None:
        await self._transport.disconnect()

    @property
    def healthy(self) -> bool:
        """False once any send path has hit a broken pipe.

        Stays False until the user restarts irc-lens (no auto-reconnect
        in v1, per the spec). The Phase 5 ``POST /input`` handler reads
        this to decide between dispatching a command and returning 503.
        """
        return self._healthy

    @property
    def connected(self) -> bool:
        """Mirrors ``IRCTransport.connected`` (set by the 001 handler)."""
        return self._transport.connected

    # ------------------------------------------------------------------
    # Send paths — every path that touches the socket translates broken
    # pipe errors into ``LensConnectionLost`` per the spec's lifecycle
    # contract.
    # ------------------------------------------------------------------

    async def send_raw(self, line: str) -> None:
        try:
            await self._transport.send_raw(line)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc

    async def send_privmsg(self, target: str, text: str) -> None:
        try:
            await self._transport.send_privmsg(target, text)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc

    async def join(self, channel: str) -> None:
        if not channel.startswith("#"):
            return
        # Track locally first so the optimistic UI matches the user's
        # intent even if the JOIN ack hasn't landed yet. The server's
        # ack will be a no-op via this set (set semantics).
        try:
            await self._transport.join_channel(channel)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc
        self.joined_channels.add(channel)

    async def part(self, channel: str) -> None:
        if not channel.startswith("#"):
            return
        try:
            await self._transport.part_channel(channel)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc
        self.joined_channels.discard(channel)
        if self.current_channel == channel:
            self.current_channel = ""

    # ------------------------------------------------------------------
    # View-state mutators — Phase 5 will publish events from these. For
    # Phase 3 they're plain state setters so unit tests can drive them.
    # ------------------------------------------------------------------

    def set_current_channel(self, channel: str) -> None:
        """Switch the active channel. ``""`` clears it."""
        self.current_channel = channel

    def set_view(self, view: ViewName) -> None:
        self.view = view

    def set_roster(self, entries: list[EntityItem]) -> None:
        self.roster = list(entries)

    # ------------------------------------------------------------------
    # Future-based query methods
    # ------------------------------------------------------------------
    # The shape mirrors ``culture/console/client.py:206-288`` (LIST/WHO/
    # HISTORY): per-call collect-buffer + asyncio.Future, resolved by the
    # IRC dispatch handlers below. We register the handlers in
    # ``self._transport._cmd_handlers`` so the transport's existing read
    # loop drives our resolution.

    async def list_channels(self) -> list[str]:
        key = "LIST"
        pending_key = _RPL_LISTEND
        return await self._collect_until(
            key=key,
            pending_key=pending_key,
            send=lambda: self.send_raw("LIST"),
            sort=True,
        )

    async def who(self, target: str) -> list[dict]:
        key = f"WHO {target}"
        pending_key = f"{_RPL_ENDOFWHO}:{target}"
        return await self._collect_until(
            key=key,
            pending_key=pending_key,
            send=lambda: self.send_raw(f"WHO {target}"),
        )

    async def history(self, channel: str, limit: int = 50) -> list[dict]:
        key = f"HISTORY {channel}"
        pending_key = f"{_HISTORYEND}:{channel}"
        return await self._collect_until(
            key=key,
            pending_key=pending_key,
            send=lambda: self.send_raw(f"HISTORY RECENT {channel} {limit}"),
        )

    async def _collect_until(
        self,
        *,
        key: str,
        pending_key: str,
        send: Callable[[], Any],
        sort: bool = False,
    ) -> list:
        """Common shape: stage collect-buffer + future, send, await end, drain.

        Mirrors the body of LIST/WHO/HISTORY in
        ``culture/console/client.py``. Extracted so each query verb is a
        five-line wrapper; the upstream files duplicate this 25-line
        block three times.
        """
        self._collect_buffers[key] = []
        end_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending[pending_key] = end_future
        try:
            await send()
        except LensConnectionLost:
            self._pending.pop(pending_key, None)
            self._collect_buffers.pop(key, None)
            raise

        try:
            await asyncio.wait_for(end_future, timeout=QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            pass
        finally:
            self._pending.pop(pending_key, None)

        items = self._collect_buffers.pop(key, [])
        return sorted(items) if sort else items

    # ------------------------------------------------------------------
    # IRC dispatch handlers (registered into IRCTransport._cmd_handlers)
    # ------------------------------------------------------------------

    def _install_query_handlers(self) -> None:
        h = self._transport._cmd_handlers
        h[_RPL_LIST] = self._on_rpl_list
        h[_RPL_LISTEND] = self._on_rpl_listend
        h[_RPL_WHOREPLY] = self._on_rpl_whoreply
        h[_RPL_ENDOFWHO] = self._on_rpl_endofwho
        h[_HISTORY] = self._on_history
        h[_HISTORYEND] = self._on_historyend

    def _on_rpl_list(self, msg: Message) -> None:
        if len(msg.params) >= 2:
            buf = self._collect_buffers.get("LIST")
            if buf is not None:
                buf.append(msg.params[1])

    def _on_rpl_listend(self, _msg: Message) -> None:
        fut = self._pending.pop(_RPL_LISTEND, None)
        if fut and not fut.done():
            fut.set_result(None)

    def _on_rpl_whoreply(self, msg: Message) -> None:
        if len(msg.params) >= 6:
            entry = {
                "nick": msg.params[5],
                "user": msg.params[2],
                "host": msg.params[3],
                "server": msg.params[4],
                "flags": msg.params[6] if len(msg.params) > 6 else "",
                "realname": msg.params[7] if len(msg.params) > 7 else "",
            }
            target = msg.params[1]
            buf = self._collect_buffers.get(f"WHO {target}")
            if buf is not None:
                buf.append(entry)

    def _on_rpl_endofwho(self, msg: Message) -> None:
        target = msg.params[1] if len(msg.params) >= 2 else ""
        fut = self._pending.pop(f"{_RPL_ENDOFWHO}:{target}", None)
        if fut and not fut.done():
            fut.set_result(None)

    def _on_history(self, msg: Message) -> None:
        if len(msg.params) >= 4:
            channel = msg.params[0]
            entry = {
                "channel": channel,
                "nick": msg.params[1],
                "timestamp": msg.params[2],
                "text": msg.params[3],
            }
            buf = self._collect_buffers.get(f"HISTORY {channel}")
            if buf is not None:
                buf.append(entry)

    def _on_historyend(self, msg: Message) -> None:
        channel = msg.params[0] if msg.params else ""
        fut = self._pending.pop(f"{_HISTORYEND}:{channel}", None)
        if fut and not fut.done():
            fut.set_result(None)
