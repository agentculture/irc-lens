"""Per-principal Session registry.

A registry hands out one :class:`Session` per authenticated principal,
opening it lazily on first request. Concurrent first-requests for the
same principal share one Session via a per-key lock + double-check.

Failed opens are *not* registered, so a transient AgentIRC outage
doesn't poison the cache for that principal.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from irc_lens import metrics
from irc_lens.web.identity import Identity

if TYPE_CHECKING:
    from irc_lens.session import Session

SessionFactory = Callable[[str], "Session"]

#: Backends a Session can connect to. The backend is chosen server-side
#: from the verified tier (+ an approved user's sandbox toggle), never from
#: a client parameter.
BACKEND_MESH = "mesh"
BACKEND_SANDBOX = "sandbox"
BACKENDS = frozenset({BACKEND_MESH, BACKEND_SANDBOX})

#: Registry-key prefix for guest principals, so a guest whose email equals
#: an approved user's can never share that user's Session.
GUEST_PRINCIPAL_PREFIX = "guest:"


def _presence_listener(msg: Any) -> None:
    """Feed the sandbox-agent presence tracker from incoming traffic."""
    prefix = getattr(msg, "prefix", "") or ""
    nick = prefix.split("!", 1)[0]
    if not nick:
        return
    presence = metrics.get_presence()
    if msg.command in ("PRIVMSG", "JOIN"):
        presence.seen(nick)
    elif msg.command in ("PART", "QUIT"):
        presence.gone(nick)


def guest_room(nick: str, prefix: str) -> str:
    """The private room for a sandbox nick: ``sbx-vis1`` -> ``#g-vis1``."""
    return prefix + nick.split("-", 1)[-1]


def sandbox_presence(session: Any, agent_nick: str) -> dict[str, Any]:
    """Agent state for one sandbox session: is the agent in this room now?

    Room membership (from the live roster) rather than "spoke in the last
    60 s": an idle agent is online, and a stopped one drops out of the
    roster on PART/QUIT.
    """
    roster = getattr(session, "roster", None) or []
    online = any(e.nick.lower() == agent_nick.lower() for e in roster)
    return {
        "nick": agent_nick,
        "state": "online" if online else "offline",
        "online": online,
        "last_seen_age_s": None,
    }


def _has_open_tab(session: Any) -> bool:
    bus = getattr(session, "event_bus", None)
    return bus is not None and bus.subscriber_count > 0


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _join_room(session: Any, room: str) -> None:
    """Join *room* (tolerates test doubles without async join)."""
    await _maybe_await(session.join(room))


async def _open_sandbox_rooms(session: Any, own: str, others: list[str]) -> None:
    """Put a fresh sandbox session in its private room (d6/d7).

    A guest joins only its own room. The approved user's Guest view also
    joins every current guest's room (*others*) so the owner can watch them
    all; its own room stays current.
    """
    await _join_room(session, own)
    for room in others:
        if room != own:
            await _join_room(session, room)
    session.set_current_channel(own)
    refresh = getattr(session, "refresh_roster", None)
    if refresh is not None:
        await _maybe_await(refresh())


#: Provides the guest store lazily (it may be swapped after app build).
StoreGetter = Callable[[], Any]

#: How long a guest slot reserved at code verification is held for the
#: browser's first ``GET /`` (which opens the session and consumes it).
GUEST_RESERVATION_S = 120.0


class SessionRegistry:
    """Maps principal → Session, lazy-opening as needed."""

    def __init__(
        self,
        factory: SessionFactory,
        sandbox_factory: SessionFactory | None = None,
        room_prefix: str = "#g-",
        guest_store: StoreGetter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factories: dict[str, SessionFactory] = {BACKEND_MESH: factory}
        # Prefix of the private room every sandbox session joins on open:
        # guests may not /join (command allowlist), so this is how they
        # reach the sandbox agent, which follows them in.
        self._room_prefix = room_prefix
        # Guest rooms are #<prefix><random id> from the guest store (d7);
        # without a store (unit tests) they fall back to the nickname.
        self._guest_store = guest_store or (lambda: None)
        self._idle_since: dict[tuple[str, str], float] = {}
        # Guest limit (c24) + action-based idle sign-off (c28). The clock
        # is the one the ban sweeper passes to reap_idle (monotonic).
        self._clock = clock
        self._last_action: dict[tuple[str, str], float] = {}
        # Guest principal -> expiry of a slot held for a browser that has
        # passed code verification (or a guest about to reopen) but has
        # not opened its session yet.
        self._reserved: dict[str, float] = {}
        #: Held across "count + reserve" so concurrent verifies/reopens can
        #: never admit more than max_guests guests.
        self.guest_lock = asyncio.Lock()
        if sandbox_factory is not None:
            self._factories[BACKEND_SANDBOX] = sandbox_factory
        self._sessions: dict[tuple[str, str], Any] = {}
        # Per-principal locks accumulate one entry per ever-seen principal
        # and are not pruned. Phase 2 has a single dev identity so this is
        # a non-issue; in CF mode an unbounded set of principals could
        # each leave a lock behind. Tracked in the Phase 2 PR description
        # as a Phase 3 cleanup item; not flagged inline because Sonar
        # S1135 treats every TODO comment as an unresolved task.
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    def __contains__(self, principal: str) -> bool:
        """Membership for the real-mesh backend (the pre-guest-mode meaning)."""
        return (principal, BACKEND_MESH) in self._sessions

    def has(self, principal: str, backend: str) -> bool:
        return (principal, backend) in self._sessions

    def keys(self) -> list[tuple[str, str]]:
        return list(self._sessions)

    def values(self) -> list[Any]:
        return list(self._sessions.values())

    def register(self, principal: str, session: Any) -> None:
        """Insert an already-connected real-mesh session under *principal*.

        Used by dev-mode startup paths (and tests) where the Session is
        opened ahead of time for fail-fast behaviour, then handed to the
        registry so subsequent ``get_or_open`` calls short-circuit
        without re-running ``connect()`` / ``wait_for_welcome()``. CF
        mode's lazy-open path does not call this — every principal goes
        through ``get_or_open`` on first request.
        """
        self._sessions[(principal, BACKEND_MESH)] = session

    @staticmethod
    def _counts_as_guest(principal: str, backend: str) -> bool:
        return backend == BACKEND_SANDBOX and principal.startswith(
            GUEST_PRINCIPAL_PREFIX
        )

    def note_all_closed(self) -> None:
        """Decrement the active-guest gauge for every live guest session."""
        for principal, backend in self._sessions:
            if self._counts_as_guest(principal, backend):
                metrics.get_metrics().session_closed()

    async def close(self, principal: str, backend: str) -> None:
        """Disconnect and forget one (principal, backend) Session."""
        session = self._sessions.pop((principal, backend), None)
        self._last_action.pop((principal, backend), None)
        self._idle_since.pop((principal, backend), None)
        if backend == BACKEND_SANDBOX:
            self._reserved.pop(principal, None)
        if session is None:
            return
        if self._counts_as_guest(principal, backend):
            metrics.get_metrics().session_closed()
        try:
            await session.disconnect()
        except Exception:  # noqa: BLE001 — closing must not raise
            pass

    # -- guest limit (c24/h16) ----------------------------------------------

    def _live_reservations(self, now: float) -> set[str]:
        for principal, expires in list(self._reserved.items()):
            if expires <= now:
                del self._reserved[principal]
        return set(self._reserved)

    def active_guests(self, now: float | None = None) -> set[str]:
        """Guest principals holding a slot: an open guest sandbox session or
        an unexpired reservation. An approved user's Guest view
        (``sandbox_preview``, no ``guest:`` prefix) never counts."""
        now = self._clock() if now is None else now
        open_ = {p for p, b in self._sessions if self._counts_as_guest(p, b)}
        return open_ | self._live_reservations(now)

    def active_guest_count(self, now: float | None = None) -> int:
        return len(self.active_guests(now))

    def is_active_guest(self, principal: str, now: float | None = None) -> bool:
        return principal in self.active_guests(now)

    def guest_slot_free(
        self, principal: str, max_guests: int, now: float | None = None
    ) -> bool:
        """True iff *principal* already holds a slot or one is free."""
        active = self.active_guests(now)
        return principal in active or len(active) < max_guests

    def try_reserve_guest(
        self,
        principal: str,
        max_guests: int,
        *,
        now: float | None = None,
        ttl: float = GUEST_RESERVATION_S,
    ) -> bool:
        """Count + reserve in one step; call with :attr:`guest_lock` held.

        A guest that already holds a slot keeps it (its reservation is only
        refreshed when it has no open session). Returns False when full.
        """
        now = self._clock() if now is None else now
        if not self.guest_slot_free(principal, max_guests, now):
            return False
        if not self.has(principal, BACKEND_SANDBOX):
            self._reserved[principal] = now + ttl
        return True

    def release_guest(self, principal: str) -> None:
        """Drop a reservation that will not be used (e.g. a failed open)."""
        self._reserved.pop(principal, None)

    def touch(self, principal: str, backend: str, now: float | None = None) -> None:
        """Record a user action (page load, message, command) on a session."""
        key = (principal, backend)
        if key in self._sessions:
            self._last_action[key] = self._clock() if now is None else now

    def _rooms_for(self, identity: Identity, is_guest: bool) -> tuple[str, list[str]]:
        """(own room, other rooms to join) for a new sandbox session."""
        store = self._guest_store()
        if is_guest:
            email = identity.principal[len(GUEST_PRINCIPAL_PREFIX) :]
            if store is None:
                return guest_room(identity.nick, self._room_prefix), []
            return self._room_prefix + store.room_id(email), []
        own = guest_room(identity.nick, self._room_prefix)
        if store is None:
            return own, []
        return own, [self._room_prefix + rid for _email, rid in store.list_rooms()]

    async def reap_idle(self, *, now: float, idle_s: float) -> list[tuple[str, str]]:
        """Close idle sandbox sessions; mesh sessions are never reaped.

        * Guests (``guest:`` principals) are signed off after *idle_s* with
          no action -- no message or command sent (c28) -- even with a tab
          open, so one forgotten tab cannot hold the only guest slot.
        * An approved user's Guest view keeps the tab rule: closed after
          *idle_s* with no open event stream (guests close the tab without
          telling us, d7).
        """
        closed = []
        sandbox = [(k, s) for k, s in self._sessions.items() if k[1] == BACKEND_SANDBOX]
        for key, session in sandbox:
            if self._counts_as_guest(*key):
                idle = now - self._last_action.setdefault(key, now) >= idle_s
            elif _has_open_tab(session):
                self._idle_since.pop(key, None)
                idle = False
            else:
                idle = now - self._idle_since.setdefault(key, now) >= idle_s
            if idle:
                await self.close(*key)
                closed.append(key)
        return closed

    async def _init_sandbox(self, session: Any, identity: Identity) -> None:
        """UI tier, presence + answer listeners, and the private room(s)."""
        # UI context only (badge / palette); enforcement lives in
        # the routes and never reads this attribute.
        is_guest = identity.principal.startswith(GUEST_PRINCIPAL_PREFIX)
        session.ui_tier = "guest" if is_guest else "sandbox_preview"
        for command in ("PRIVMSG", "JOIN", "PART", "QUIT"):
            session._transport.add_listener(command, _presence_listener)
        own, others = self._rooms_for(identity, is_guest)
        if is_guest:
            # d8: the guest store keeps the guest's Q&A (the sandbox
            # IRCd is memory-only), so record the agent's answers.
            from irc_lens.corpus import answer_recorder

            session._transport.add_listener(
                "PRIVMSG",
                answer_recorder(
                    self._guest_store,
                    identity.principal[len(GUEST_PRINCIPAL_PREFIX) :],
                    room=own,
                    own_nick=identity.nick,
                    agent_nick=metrics.get_presence().nick,
                ),
            )
        await _open_sandbox_rooms(session, own, others)

    async def get_or_open(self, identity: Identity, backend: str = BACKEND_MESH) -> Any:
        """Return the Session for (``identity.principal``, *backend*).

        *backend* is chosen by the caller from the verified tier only
        (``routes._resolve_session``); the sandbox backend is the only one
        a guest-tier principal may ever be given.
        """
        if backend not in self._factories:
            raise ValueError(f"no session factory for backend {backend!r}")
        key = (identity.principal, backend)
        existing = self._sessions.get(key)
        if existing is not None:
            if backend == BACKEND_MESH or getattr(existing, "healthy", True):
                return existing
            # A dead sandbox connection is evicted (and the active-guest
            # gauge decremented) so the next request reconnects.
            await self.close(*key)
        async with self._locks[key]:
            if key in self._sessions:  # double-check
                return self._sessions[key]
            session = self._factories[backend](identity.nick)
            # Two-step open: connect() opens the TCP socket and starts the
            # read task; wait_for_welcome() blocks for 001 RPL_WELCOME (or
            # raises on 432/433 nick rejection / timeout). If the second
            # step raises after the first succeeded, the socket and read
            # task are still alive — disconnect to avoid leaking transport
            # state on every failed handshake.
            try:
                await session.connect()
                await session.wait_for_welcome()
            except BaseException:
                try:
                    await session.disconnect()
                except Exception:  # noqa: BLE001 — never mask the original
                    pass
                raise
            if backend == BACKEND_SANDBOX:
                await self._init_sandbox(session, identity)
            self._sessions[key] = session
            if self._counts_as_guest(*key):
                # The open consumes the guest's reservation; it counts as
                # the first action for the idle sign-off.
                self._reserved.pop(identity.principal, None)
                self._last_action[key] = self._clock()
                metrics.get_metrics().session_opened()
            return session


async def disconnect_all(registry: SessionRegistry) -> None:
    """Disconnect every registered Session, swallowing individual failures."""
    sessions = registry.values()
    if not sessions:
        return
    registry.note_all_closed()
    await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
