"""Per-principal Session registry.

A registry hands out one :class:`Session` per authenticated principal,
opening it lazily on first request. Concurrent first-requests for the
same principal share one Session via a per-key lock + double-check.

Failed opens are *not* registered, so a transient AgentIRC outage
doesn't poison the cache for that principal.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from irc_lens.web.identity import Identity

if TYPE_CHECKING:
    from irc_lens.session import Session

SessionFactory = Callable[[str], "Session"]


class SessionRegistry:
    """Maps principal → Session, lazy-opening as needed."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory
        self._sessions: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def __contains__(self, principal: str) -> bool:
        return principal in self._sessions

    def values(self) -> list[Any]:
        return list(self._sessions.values())

    async def get_or_open(self, identity: Identity) -> Any:
        if identity.principal in self._sessions:
            return self._sessions[identity.principal]
        async with self._locks[identity.principal]:
            if identity.principal in self._sessions:           # double-check
                return self._sessions[identity.principal]
            session = self._factory(identity.nick)
            await session.connect()
            await session.wait_for_welcome()
            self._sessions[identity.principal] = session
            return session


async def disconnect_all(registry: SessionRegistry) -> None:
    """Disconnect every registered Session, swallowing individual failures."""
    sessions = registry.values()
    if not sessions:
        return
    await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
