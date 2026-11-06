"""Bounded, in-memory threat-signal counters.

The request middleware does no I/O.  It retains only fixed counter names and
keyed hashes of source addresses, with the latter capped for every 15-minute
window.  Paths, addresses, user agents, and other visitor strings are never
stored.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from aiohttp import web

from irc_lens.web.auth import client_ip

WINDOW_SECONDS = 15 * 60
DISTINCT_SOURCE_CAP = 256
_ENTRY_PREFIX = "/entry/"
_REFUSED_STATUSES = frozenset({401, 403})
_HASH_BYTES = 16


class Signal(StrEnum):
    """The complete, fixed key space used by threat rules."""

    REQUESTS = "requests"
    PROBES = "probes"
    REFUSED_RESPONSES = "refused_responses"
    ENTRY_POSTS = "entry_posts"
    MAIL_SENDS = "mail_sends"
    RESERVED_ADDRESSES = "reserved_addresses"
    FAILED_SENDS = "failed_sends"
    BUDGET_HITS = "budget_hits"
    MESSAGES_BLOCKED = "messages_blocked"
    MESSAGES_FLAGGED = "messages_flagged"
    MESSAGES_UNCHECKED = "messages_unchecked"


@dataclass(frozen=True)
class ThreatSnapshot:
    """A counts-only copy safe to pass to rule and verdict code."""

    window_started_at: float
    counts: Mapping[Signal, int]
    distinct_sources: int
    source_count_capped: bool


class ThreatCounters:
    """One fixed-size set of counters and capped source hashes."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        source_cap: int = DISTINCT_SOURCE_CAP,
        hash_key: bytes | None = None,
    ) -> None:
        if source_cap < 1:
            raise ValueError("source_cap must be positive")
        self._clock = clock
        self._source_cap = source_cap
        self._hash_key = hash_key or secrets.token_bytes(32)
        self._window_started_at = clock()
        self._counts = dict.fromkeys(Signal, 0)
        self._source_hashes: set[bytes] = set()
        self._source_count_capped = False
        self._completed_snapshot: ThreatSnapshot | None = None
        self._lock = threading.Lock()

    @property
    def state_slots(self) -> int:
        """Number of variable collection entries currently retained."""
        with self._lock:
            return len(self._counts) + len(self._source_hashes)

    @property
    def max_state_slots(self) -> int:
        """Hard upper bound for collection entries retained in a window."""
        return len(Signal) + self._source_cap

    def increment(self, signal: Signal, amount: int = 1) -> None:
        """Increment one known signal for non-middleware producers."""
        if amount < 0:
            raise ValueError("amount must not be negative")
        now = self._clock()
        with self._lock:
            self._roll_window(now)
            self._counts[signal] += amount

    def observe_request(
        self,
        *,
        is_probe: bool,
        status: int | None,
        is_entry_post: bool,
        source: str,
    ) -> None:
        """Record derived request facts without retaining their raw source."""
        now = self._clock()
        source_hash = self._hash_source(source) if source else None
        with self._lock:
            self._roll_window(now)
            self._counts[Signal.REQUESTS] += 1
            self._counts[Signal.PROBES] += int(is_probe)
            self._counts[Signal.REFUSED_RESPONSES] += int(status in _REFUSED_STATUSES)
            self._counts[Signal.ENTRY_POSTS] += int(is_entry_post)
            if source_hash is not None:
                self._retain_source(source_hash)

    def snapshot(self) -> ThreatSnapshot:
        """Copy the current window as derived numbers only."""
        now = self._clock()
        with self._lock:
            self._roll_window(now)
            return self._snapshot_locked()

    def snapshots(self) -> tuple[ThreatSnapshot | None, ThreatSnapshot]:
        """Return the last completed window and the current window atomically."""
        now = self._clock()
        with self._lock:
            self._roll_window(now)
            return self._completed_snapshot, self._snapshot_locked()

    def _snapshot_locked(self) -> ThreatSnapshot:
        counts = MappingProxyType(dict(self._counts))
        return ThreatSnapshot(
            window_started_at=self._window_started_at,
            counts=counts,
            distinct_sources=len(self._source_hashes),
            source_count_capped=self._source_count_capped,
        )

    def _hash_source(self, source: str) -> bytes:
        return hashlib.blake2s(
            source.encode("utf-8", "surrogateescape"),
            key=self._hash_key,
            digest_size=_HASH_BYTES,
        ).digest()

    def _retain_source(self, source_hash: bytes) -> None:
        if source_hash in self._source_hashes:
            return
        if len(self._source_hashes) >= self._source_cap:
            self._source_count_capped = True
            return
        self._source_hashes.add(source_hash)

    def _roll_window(self, now: float) -> None:
        if now - self._window_started_at < WINDOW_SECONDS:
            return
        self._completed_snapshot = self._snapshot_locked()
        self._window_started_at = now
        self._counts = dict.fromkeys(Signal, 0)
        self._source_hashes.clear()
        self._source_count_capped = False


THREAT_COUNTERS = web.AppKey("threat_counters", ThreatCounters)


def install(
    app: web.Application, counters: ThreatCounters | None = None
) -> ThreatCounters:
    """Attach one counter window to an application."""
    installed = counters or ThreatCounters()
    app[THREAT_COUNTERS] = installed
    return installed


def _is_unrouted(request: web.Request) -> bool:
    """Use router resolution, not the eventual response status."""
    exception = getattr(request.match_info, "http_exception", None)
    return isinstance(exception, web.HTTPNotFound)


@web.middleware
async def threat_middleware(request: web.Request, handler):
    """Count request signals after downstream auth chooses its response."""
    status = None
    try:
        response = await handler(request)
        status = response.status
        return response
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        request.app[THREAT_COUNTERS].observe_request(
            is_probe=_is_unrouted(request),
            status=status,
            is_entry_post=(
                request.method == "POST" and request.path.startswith(_ENTRY_PREFIX)
            ),
            source=client_ip(request),
        )
