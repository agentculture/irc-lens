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
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from irc_lens.commands import CommandType, ParsedCommand
from irc_lens.irc import IRCTransport, Message, MessageBuffer

logger = logging.getLogger(__name__)

# Mirrors ``culture/console/client.py``'s constants.
QUERY_TIMEOUT = 10.0
REGISTER_TIMEOUT = 15.0

#: The four named views the spec's ``info`` event can switch between.
ViewName = Literal["chat", "help", "overview", "status"]

#: SSE event names defined in the spec's "SSE event types" section.
#: ``log`` is a full chat-log replacement (innerHTML swap of ``#chat-log``)
#: emitted on /join and /switch so server-side history surfaces in the UI;
#: ``chat`` continues to append single live lines.
EventName = Literal["chat", "log", "roster", "info", "view", "error"]


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
        # Overflow path: the spec says "drop oldest on overflow" — the
        # newest event must always survive. To inject the one-shot
        # error notice without losing the new event, drop one extra
        # oldest entry so both the error and the new event fit.
        if not self._overflow_flagged:
            self._drop_one_oldest()  # make room for the error
            self._drop_one_oldest()  # make room for the new event
            err = SessionEvent(name="error", data=_OVERFLOW_DATA)
            try:
                self.queue.put_nowait(err)
                self._overflow_flagged = True
            except asyncio.QueueFull:
                # Bus completely jammed even after two drops (queue_max=1
                # edge case). Skip the error rather than loop forever.
                pass
        else:
            self._drop_one_oldest()  # just one drop on subsequent overflows
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def _drop_one_oldest(self) -> None:
        try:
            self.queue.get_nowait()
        except asyncio.QueueEmpty:
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

    def drain_nowait(self) -> list["SessionEvent"]:
        """Pop every queued event without awaiting. Test-only helper —
        production code uses ``events()`` instead. Exists so tests
        can snapshot publish output without reaching into the private
        ``_sub.queue``."""
        out: list[SessionEvent] = []
        while True:
            try:
                out.append(self._sub.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

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
        # Per-query-key lock: serialises concurrent same-key queries so
        # they can't clobber each other's collect-buffer + pending future.
        # IRC numerics like RPL_LISTEND (323) carry no query-id, so two
        # in-flight LIST calls would otherwise resolve each other's
        # futures with mixed results. Different keys (e.g. WHO #a vs
        # WHO #b) don't block each other.
        self._query_locks: dict[str, asyncio.Lock] = {}

        # Per-session dispatch lock. Serialises Session.execute() so two
        # POST /input handlers against the same lens cannot interleave
        # their verb logic. Without this, any verb that awaits I/O
        # (LIST, WHO, HISTORY, ...) can yield to a concurrent verb that
        # mutates view/current_channel/joined_channels/roster, producing
        # out-of-order observable side effects. Issue #22.
        # Distinct from `_query_locks` above, which de-conflicts
        # collect-buffers for IRC numerics carrying no query-id.
        self._exec_lock = asyncio.Lock()

        # Event bus: Phase 5 will wire publishes; Phase 3 just holds it.
        self.event_bus = event_bus if event_bus is not None else SessionEventBus()

        # Tracks transport health independent of `IRCTransport.connected`,
        # which only flips after the welcome (001) handshake. The lens
        # cares about "did we ever lose the pipe?" because Phase 5
        # marks the session unhealthy on first `LensConnectionLost`.
        self._healthy = True

        # Welcome / nick-rejection signalling for `wait_for_welcome`.
        # Initialised in __init__ so the listeners can be registered
        # *before* `transport.connect()` starts the read loop — without
        # that ordering, a fast 001 can land in the read loop before we
        # observe it and `wait_for_welcome` would time out spuriously.
        # asyncio.Event in 3.10+ is loop-agnostic until first awaited,
        # so constructing it pre-loop is safe.
        self._welcome_event = asyncio.Event()
        self._nick_rejection: str | None = None
        self._transport.add_listener("001", self._on_welcome_signal)
        self._transport._cmd_handlers.setdefault("432", self._on_nick_rejected)
        self._transport._cmd_handlers.setdefault("433", self._on_nick_rejected)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the TCP connection and register. Raises ``LensConnectionLost``
        on connect failure so callers can translate to a clean stderr
        message + non-zero exit (Phase 4 wires this into ``serve``)."""
        try:
            await self._transport.connect()
        except OSError as exc:
            # ConnectionError is an OSError subclass in Python 3.3+.
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc
        # Disable IRCTransport's auto-reconnect: the spec's lifecycle
        # contract is "no auto-reconnect in v1 — restart irc-lens to
        # reconnect". `_transport.connect()` set `_should_run = True`,
        # which would cause `_read_loop`'s finally to spawn `_reconnect`
        # on EOF. Flipping it back to False keeps the read loop alive
        # but lets it terminate cleanly when the socket closes.
        self._transport._should_run = False
        # Subscribe to inbound commands the SSE bus needs to surface.
        # The transport's own `_cmd_handlers` keep doing buffer-add;
        # `add_listener` runs us *after* them. See transport.py docstring.
        self._transport.add_listener("PRIVMSG", self.dispatch)
        self._transport.add_listener("JOIN", self.dispatch)
        self._transport.add_listener("PART", self.dispatch)

    def _on_welcome_signal(self, _msg: Message) -> None:
        # Runs after IRCTransport's own 001 handler (which set
        # `connected=True` and joined any boot channels); flipping the
        # Event releases `wait_for_welcome`.
        self._welcome_event.set()

    def _on_nick_rejected(self, msg: Message) -> None:
        # Stash the server's reason text and unblock `wait_for_welcome`
        # so it can translate to a clean error rather than time out.
        # Don't raise from a sync IRC handler — the read loop swallows
        # exceptions there.
        reason = msg.params[-1] if msg.params else "nickname rejected"
        self._nick_rejection = reason
        self._welcome_event.set()

    async def wait_for_welcome(self) -> None:
        """Block until the IRCd sends 001 RPL_WELCOME, or fail fast on
        a 432/433 rejection. Raises ``LensConnectionLost`` on failure
        so the serve command can translate to a user-readable error
        instead of running with a silently broken session.

        Wraps the welcome Event in `asyncio.timeout()` rather than
        taking a `timeout=` parameter (sonarcloud python:S7483: timeout
        belongs in a context manager, not a function arg). 5s is the
        established budget and isn't user-tunable.
        """
        try:
            async with asyncio.timeout(5.0):
                await self._welcome_event.wait()
        except asyncio.TimeoutError as exc:
            self._healthy = False
            raise LensConnectionLost(
                "no RPL_WELCOME within 5.0s — server may be unresponsive "
                "or quietly rejecting registration"
            ) from exc
        if self._nick_rejection is not None:
            self._healthy = False
            raise LensConnectionLost(f"nick rejected: {self._nick_rejection}")

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
        except OSError as exc:
            # Catches BrokenPipeError, ConnectionResetError,
            # ConnectionAbortedError, and ConnectionError — all OSError
            # subclasses in Python 3.3+.
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc

    async def send_privmsg(self, target: str, text: str) -> None:
        try:
            await self._transport.send_privmsg(target, text)
        except OSError as exc:
            # Catches BrokenPipeError, ConnectionResetError,
            # ConnectionAbortedError, and ConnectionError — all OSError
            # subclasses in Python 3.3+.
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc

    async def join(self, channel: str) -> None:
        if not channel.startswith("#"):
            return
        # Server-confirmed semantics: only mark the channel joined once
        # the JOIN write succeeds. A failed send raises
        # `LensConnectionLost` and `joined_channels` does NOT advance —
        # see `test_join_translates_pipe_error`.
        try:
            await self._transport.join_channel(channel)
        except OSError as exc:
            # Catches BrokenPipeError, ConnectionResetError,
            # ConnectionAbortedError, and ConnectionError — all OSError
            # subclasses in Python 3.3+.
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc
        self.joined_channels.add(channel)

    async def part(self, channel: str) -> None:
        if not channel.startswith("#"):
            return
        try:
            await self._transport.part_channel(channel)
        except OSError as exc:
            # Catches BrokenPipeError, ConnectionResetError,
            # ConnectionAbortedError, and ConnectionError — all OSError
            # subclasses in Python 3.3+.
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
    # Command execution + inbound dispatch (Phase 5 SSE wiring)
    # ------------------------------------------------------------------

    async def execute(self, parsed: ParsedCommand) -> None:
        """Dispatch a `ParsedCommand` from `POST /input`.

        Maps each command type to a small per-type helper that runs the
        matching send path and publishes the visible side-effect.
        ``LensConnectionLost`` is allowed to propagate so the route
        layer can translate it to HTTP 503; ``UNKNOWN`` and
        unsupported-yet types publish an ``error`` event and return
        normally — typing ``/foo`` should never crash a browser session.

        The body runs under ``self._exec_lock`` so two concurrent
        ``POST /input`` handlers against the same Session execute in
        submission order rather than interleaving (issue #22).
        """
        async with self._exec_lock:
            handler = self._exec_dispatch.get(parsed.type, self._exec_unsupported)
            await handler(parsed)

    @property
    def _exec_dispatch(self) -> dict[CommandType, Any]:
        # Built lazily so the bound-method references are stable per
        # instance; small dict, cheap to construct on demand.
        return {
            CommandType.CHAT: self._exec_chat,
            CommandType.SEND: self._exec_send,
            CommandType.JOIN: self._exec_join,
            CommandType.PART: self._exec_part,
            CommandType.HELP: self._exec_help,
            CommandType.OVERVIEW: self._exec_overview,
            CommandType.STATUS: self._exec_status,
            CommandType.SWITCH: self._exec_switch,
            CommandType.READ: self._exec_read,
            CommandType.CHANNELS: self._exec_channels,
            CommandType.WHO: self._exec_who,
            CommandType.AGENTS: self._exec_agents,
            CommandType.ME: self._exec_me,
            CommandType.TOPIC: self._exec_topic,
            CommandType.ICON: self._exec_icon,
            CommandType.UNKNOWN: self._exec_unknown,
        }

    async def _exec_chat(self, parsed: ParsedCommand) -> None:
        text = parsed.text
        if not text:
            return
        if not self.current_channel:
            self._publish_error("no active channel; /join #x first")
            return
        await self.send_privmsg(self.current_channel, text)
        self._publish_chat(self.nick, text)

    async def _exec_send(self, parsed: ParsedCommand) -> None:
        if not parsed.args:
            self._publish_error("/send needs a target")
            return
        target = parsed.args[0]
        text = parsed.text or ""
        if not text:
            self._publish_error("/send needs text")
            return
        await self.send_privmsg(target, text)
        # Echo only when the target is the active channel — that's the
        # one place the local echo will visually render today.
        if target == self.current_channel:
            self._publish_chat(self.nick, text)

    async def _exec_join(self, parsed: ParsedCommand) -> None:
        if not parsed.args:
            self._publish_error("/join needs a channel")
            return
        channel = parsed.args[0]
        # Validate before mutating view state. `Session.join` no-ops on
        # a non-`#` target, so a permissive `set_current_channel` here
        # would point the UI at a channel that's not in `joined_channels`.
        if not channel.startswith("#"):
            self._publish_error(f"invalid channel: {channel} (must start with #)")
            return
        await self.join(channel)
        self.set_current_channel(channel)
        self._publish_roster()
        # JOIN/PART change channel context, not the named view —
        # publish `info` (per spec line 161, "channel info refreshed"),
        # not `view` (which is reserved for /help/overview/status switches
        # per spec line 162).
        self._publish_info()
        # Server-persisted backlog: pull the last N messages so the chat
        # pane isn't blank on first join. Mirrors the culture console's
        # `_switch_to_channel` (../culture/culture/console/app.py:677).
        await self._fetch_and_publish_history(channel)

    async def _exec_switch(self, parsed: ParsedCommand) -> None:
        if not parsed.args:
            self._publish_error("/switch needs a channel")
            return
        channel = parsed.args[0]
        if not channel.startswith("#"):
            self._publish_error(f"invalid channel: {channel} (must start with #)")
            return
        if channel not in self.joined_channels:
            self._publish_error(f"not joined to {channel} — /join {channel} first")
            return
        # Switch is a pure view-state mutation — no IRC side-effect.
        self.set_current_channel(channel)
        self._publish_roster()
        self._publish_info()
        await self._fetch_and_publish_history(channel)

    async def _fetch_and_publish_history(
        self, channel: str, limit: int = 50, *, view_channel: str | None = None
    ) -> None:
        """Pull HISTORY RECENT and publish it as a `log` SSE event.

        ``channel`` is what we query the IRCd for; ``view_channel`` is
        the channel whose pane the result is meant to land in (defaults
        to ``channel`` for /switch and /join). Stale-guard compares
        ``self.current_channel`` against ``view_channel``: if the user
        moved away from the pane this fetch was for, drop. Mirrors
        ``culture/console/app.py:677-716``.

        For ``/read #other`` from ``#ops``: ``channel="#other"`` (we
        query that backlog) but ``view_channel="#ops"`` (we publish into
        the active pane the user invoked /read from).
        """
        from irc_lens.web.render import render_chat_log

        if view_channel is None:
            view_channel = channel
        if not self.connected:
            # Pre-welcome (or post-disconnect) — skip the IRCd round-trip
            # and publish an empty log so the chat pane still clears on
            # /switch. Without this, every offline unit test would hang
            # for QUERY_TIMEOUT seconds waiting for HISTORYEND that
            # never arrives.
            if self.current_channel == view_channel:
                self._publish_log(render_chat_log([]))
            return

        try:
            entries = await self.history(channel, limit=limit)
        except LensConnectionLost:
            # Surface but don't crash the dispatcher — the route layer
            # will translate to 503; we want the JOIN side-effect to
            # still publish via the earlier roster/info events.
            raise
        except Exception:
            # _query_locks / collect-buffer paths are defensive but may
            # raise on malformed numerics; degrade to empty log rather
            # than crashing the dispatcher.
            logger.exception("history fetch for %s failed", channel)
            entries = []
        if self.current_channel != view_channel:
            return  # user moved off the pane this fetch was for; skip swap
        self._publish_log(render_chat_log(entries))

    async def _exec_part(self, parsed: ParsedCommand) -> None:
        if not parsed.args:
            self._publish_error("/part needs a channel")
            return
        channel = parsed.args[0]
        if not channel.startswith("#"):
            self._publish_error(f"invalid channel: {channel} (must start with #)")
            return
        await self.part(channel)
        self._publish_roster()
        self._publish_info()

    def _require_connected(self, verb: str) -> bool:
        """Pre-welcome guard for query verbs.

        Without this gate, `/channels`/`/who`/`/agents`/`/read` would
        block for QUERY_TIMEOUT (10s) when AgentIRC hasn't sent 001 yet
        — e.g. registration is pending or the nick was rejected
        (`wait_for_welcome` already failed fast on startup, so reaching
        this state at runtime is unusual but possible after a future
        in-session reconnect feature lands)."""
        if not self.connected:
            self._publish_error(f"{verb}: not connected to AgentIRC yet")
            return False
        return True

    async def _exec_read(self, parsed: ParsedCommand) -> None:
        if not self._require_connected("/read"):
            return
        # Args: optional [#channel] [-n N]; default to current_channel and 50.
        channel = self.current_channel
        limit = 50
        # Lightweight arg scan — full argparse would be overkill for two
        # forms (`/read`, `/read #ch`, `/read -n 100`, `/read #ch -n 100`).
        i = 0
        args = parsed.args
        while i < len(args):
            tok = args[i]
            if tok == "-n" and i + 1 < len(args):
                try:
                    limit = max(1, min(500, int(args[i + 1])))
                except ValueError:
                    self._publish_error(f"/read: -n needs an integer, got {args[i + 1]!r}")
                    return
                i += 2
                continue
            if tok.startswith("#"):
                channel = tok
                i += 1
                continue
            i += 1
        if not channel:
            self._publish_error("/read: no channel — /join #x first or pass /read #x")
            return
        # /read peeks at history without switching panes — `view_channel`
        # is the *current* channel so the stale guard works correctly
        # when the user reads `#other` while sitting on `#ops`.
        await self._fetch_and_publish_history(
            channel, limit=limit, view_channel=self.current_channel or channel
        )

    async def _exec_channels(self, _parsed: ParsedCommand) -> None:
        if not self._require_connected("/channels"):
            return
        view_at_start = self.view
        try:
            channels = await self.list_channels()
        except Exception:
            logger.exception("LIST query failed")
            self._publish_error("/channels: query failed")
            return
        if self.view != view_at_start:
            return  # user moved off this view during the LIST; drop the publish
        self._publish_info_extra(channels=channels)

    async def _exec_who(self, parsed: ParsedCommand) -> None:
        if not self._require_connected("/who"):
            return
        target = parsed.args[0] if parsed.args else self.current_channel
        if not target:
            self._publish_error("/who: no target — /join a channel or pass /who #x")
            return
        view_at_start = self.view
        try:
            entries = await self.who(target)
        except Exception:
            logger.exception("WHO %s failed", target)
            self._publish_error(f"/who {target}: query failed")
            return
        if self.view != view_at_start:
            return  # stale: a later command switched the view; drop the publish
        self._publish_info_extra(who_target=target, who_entries=entries)

    async def _exec_agents(self, _parsed: ParsedCommand) -> None:
        if not self._require_connected("/agents"):
            return
        if not self.joined_channels:
            self._publish_error("/agents: no channels joined — /join #x first")
            return
        view_at_start = self.view
        nicks: dict[str, dict] = {}
        for ch in sorted(self.joined_channels):
            try:
                entries = await self.who(ch)
            except Exception:
                logger.exception("WHO %s failed during /agents", ch)
                continue
            for entry in entries:
                nick = entry.get("nick", "")
                if nick and nick not in nicks:
                    nicks[nick] = entry
        if self.view != view_at_start:
            return  # stale: a later command switched the view; drop the publish
        self._publish_info_extra(agents=sorted(nicks.values(), key=lambda e: e.get("nick", "")))

    async def _exec_me(self, parsed: ParsedCommand) -> None:
        text = parsed.text.strip() if parsed.text else " ".join(parsed.args)
        if not text:
            self._publish_error("/me needs an action")
            return
        if not self.current_channel:
            self._publish_error("no active channel; /join #x first")
            return
        # CTCP ACTION wire format: PRIVMSG #ch :\x01ACTION text\x01
        # Build via raw transport so we don't double-buffer through
        # `send_privmsg` (which would also rewrite buffer entries).
        ctcp = f"\x01ACTION {text}\x01"
        try:
            await self._transport.send_raw(f"PRIVMSG {self.current_channel} :{ctcp}")
        except OSError as exc:
            self._healthy = False
            raise LensConnectionLost(str(exc)) from exc
        # Local echo as an action chat-line.
        self._publish_chat(self.nick, text, kind="action")

    async def _exec_topic(self, parsed: ParsedCommand) -> None:
        if not parsed.args:
            self._publish_error("/topic needs a channel")
            return
        channel = parsed.args[0]
        if not channel.startswith("#"):
            self._publish_error(f"/topic: invalid channel {channel}")
            return
        # `/topic #ch` (no body) reads; `/topic #ch text…` writes.
        if parsed.text:
            await self.send_raw(f"TOPIC {channel} :{parsed.text}")
        else:
            await self.send_raw(f"TOPIC {channel}")

    async def _exec_icon(self, parsed: ParsedCommand) -> None:
        if not parsed.args:
            self._publish_error("/icon needs an emoji")
            return
        emoji = parsed.args[0]
        await self.send_raw(f"ICON {emoji}")
        self.icon = emoji

    async def _exec_help(self, _parsed: ParsedCommand) -> None:
        self._switch_view("help")

    async def _exec_overview(self, _parsed: ParsedCommand) -> None:
        self._switch_view("overview")

    async def _exec_status(self, _parsed: ParsedCommand) -> None:
        self._switch_view("status")

    def _switch_view(self, name: ViewName) -> None:
        """Common body for the three view-switch verbs.

        Sets `view`, then publishes the spec-strict `view` event
        (`{view: <name>}`) followed by an `info` event with the
        re-rendered pane for the new view. Two events because they
        target different DOM regions in Phase 7's lens.js: `view`
        toggles `<body data-view>` classes; `info` swaps `#info`.

        Sync because every body call here (set_view, _publish_view,
        _publish_info) is sync. The three `_exec_*` callers must
        stay `async def` (dispatch-table contract — `await
        handler(parsed)` in `Session.execute`) and call this
        without `await`.
        """
        self.set_view(name)
        self._publish_view()
        self._publish_info()

    async def _exec_unknown(self, parsed: ParsedCommand) -> None:
        self._publish_error(f"unknown command: {parsed.text}")

    async def _exec_unsupported(self, parsed: ParsedCommand) -> None:
        # Slash commands defined in commands.py but not yet wired
        # (CHANNELS/WHO/READ/AGENTS/START/STOP/RESTART/ICON/TOPIC/
        # KICK/INVITE/SERVER/QUIT). Surface a non-fatal error event
        # rather than 503ing the browser.
        self._publish_error(f"{parsed.type.name.lower()}: not yet supported")

    async def dispatch(self, msg: Message) -> None:
        """Listener for inbound IRC messages — publishes SSE events.

        Registered for PRIVMSG/JOIN/PART in `connect()`. The transport's
        own handlers still run first (buffer-add, etc.); this just emits
        the user-visible reactive update. Per-command branches live in
        helpers so this stays under sonarcloud's S3776 complexity cap.
        """
        if msg.command == "PRIVMSG":
            self._dispatch_privmsg(msg)
            return
        if msg.command in ("JOIN", "PART"):
            # Server-confirmed channel-membership change. The local
            # `joined_channels` set was already updated by our outbound
            # join/part call (or — for other users — needs no local
            # mutation in v1); re-render the sidebar regardless.
            self._publish_roster()

    def _dispatch_privmsg(self, msg: Message) -> None:
        """Handle inbound PRIVMSG: filter, decode CTCP ACTION, publish."""
        if len(msg.params) < 2:
            return
        target = msg.params[0]
        text = msg.params[1]
        sender = msg.prefix.split("!")[0] if msg.prefix else "unknown"
        # Local echo guard: SEND already published from `execute()`.
        # Most IRC daemons don't echo own PRIVMSGs, but defensive.
        if sender == self.nick:
            return
        # `system-<server>` PRIVMSGs announce mesh events, not chat —
        # the transport already drops them from the buffer; mirror.
        if sender.startswith("system-"):
            return
        # Only publish when the message belongs in the active pane.
        # DMs come addressed to our own nick.
        if target != self.current_channel and target != self.nick:
            return
        decoded = self._decode_ctcp(text)
        if decoded is None:
            return  # non-ACTION CTCP (VERSION, PING) — drop silently
        text, kind = decoded
        self._publish_chat(sender, text, kind=kind)

    @staticmethod
    def _decode_ctcp(text: str) -> tuple[str, str] | None:
        """Decode a PRIVMSG body. Returns (text, kind) for chat or
        ACTION, or None for other CTCP types we want to drop. Pulled
        out of `_dispatch_privmsg` to keep the cognitive complexity of
        the dispatcher under sonarcloud's S3776 cap."""
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            return text[len("\x01ACTION ") : -1], "action"
        if text.startswith("\x01") and text.endswith("\x01"):
            return None  # CTCP VERSION/PING/etc — protocol pings, not chat
        return text, "chat"

    # ------------------------------------------------------------------
    # Publish helpers (Phase 5)
    # ------------------------------------------------------------------
    # Templates are imported lazily because `web/render.py` imports
    # `Session` for type hints; a top-level import here would cycle.

    def _publish_chat(self, nick: str, text: str, *, kind: str = "chat") -> None:
        from irc_lens.web.render import render_fragment

        # Pre-format the timestamp so the SSE payload is byte-stable
        # (the initial-render path goes through a Jinja2 strftime
        # filter on `BufferedMessage.timestamp`; live publishes use
        # the wall clock here).
        ts_display = time.strftime("%H:%M:%S", time.localtime(time.time()))
        fragment = render_fragment(
            "_chat_line.html.j2",
            msg={"nick": nick, "text": text, "ts_display": ts_display, "kind": kind},
        )
        self.event_bus.publish(SessionEvent(name="chat", data=fragment))

    def _publish_log(self, html: str) -> None:
        """Publish a full chat-log replacement (innerHTML of #chat-log).

        The frontend's `log` listener swaps innerHTML so the user sees
        history-on-join and the channel switch wipes the previous
        channel's lines. Live `chat` events continue to append after.
        """
        self.event_bus.publish(SessionEvent(name="log", data=html))

    def _publish_roster(self) -> None:
        from irc_lens.web.render import render_fragment

        fragment = render_fragment("_sidebar.html.j2", session=self)
        self.event_bus.publish(SessionEvent(name="roster", data=fragment))

    def _publish_info(self) -> None:
        """Re-render and publish the info pane for the current view.

        Triggered by JOIN/PART (channel context changed) and by view
        switches (HELP/OVERVIEW/STATUS) — the template branches on
        ``session.view`` to pick the right per-view content.
        """
        from irc_lens.web.render import render_fragment

        fragment = render_fragment("_info.html.j2", session=self)
        self.event_bus.publish(SessionEvent(name="info", data=fragment))

    def _publish_info_extra(self, **extra: Any) -> None:
        """Re-render the info pane with extra context (channels list,
        who results, agents). Used by /channels, /who, /agents to surface
        query results without inventing a new SSE event type.

        The extras only render under the chat-view branch of
        _info.html.j2, so promote the view to chat first when the
        verb was invoked from status/help/overview — otherwise the
        result is silently dropped by the template (issue #20). The
        `view` event mirrors `_switch_view`'s contract so the client
        toggles `<body data-view>` to match before the info swap.

        Callers are expected to have stale-guarded against an
        intervening view switch (see _exec_channels/_exec_who/
        _exec_agents) so the promotion here can't overwrite a
        view the user explicitly moved to during a slow query.
        """
        from irc_lens.web.render import render_fragment

        if self.view != "chat":
            self.set_view("chat")
            self._publish_view()

        fragment = render_fragment("_info.html.j2", session=self, **extra)
        self.event_bus.publish(SessionEvent(name="info", data=fragment))

    def _publish_view(self) -> None:
        # Spec line 162 defines the payload as `{view: <name>}` only —
        # nothing else. Channel context belongs in the `info` event.
        payload = json.dumps({"view": self.view})
        self.event_bus.publish(SessionEvent(name="view", data=payload))

    def _publish_error(self, message: str) -> None:
        payload = json.dumps({"message": message})
        self.event_bus.publish(SessionEvent(name="error", data=payload))

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
        block three times. Wrapped in a per-key lock so concurrent
        same-key queries serialise instead of clobbering each other's
        future/buffer (upstream has the same defect — flag candidate to
        feed back to culture).
        """
        lock = self._query_locks.setdefault(key, asyncio.Lock())
        async with lock:
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
