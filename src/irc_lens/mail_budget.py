"""Persistent global and per-source budgets for guest code mail."""

from __future__ import annotations

import asyncio
import threading
from typing import Protocol

HOUR_WINDOW_S = 60 * 60
DAILY_WINDOW_S = 24 * HOUR_WINDOW_S

_GLOBAL_KIND = "guest-mail"
_GLOBAL_KEY = "global"
_IP_KIND = "guest-mail-ip"
DEFAULT_IP_HOURLY_LIMIT = 5


class AttemptStore(Protocol):
    """The attempts-table operations used by :class:`MailBudget`."""

    def rate_limited(
        self, kind: str, key: str, *, limit: int, window: int
    ) -> bool: ...

    def record_attempt(self, kind: str, key: str) -> None: ...


class MailBudget:
    """Take persistent guest-mail slots without blocking the event loop."""

    def __init__(
        self,
        store: AttemptStore,
        *,
        daily_limit: int,
        hourly_limit: int,
        ip_hourly_limit: int = DEFAULT_IP_HOURLY_LIMIT,
    ) -> None:
        self._store = store
        self._daily_limit = daily_limit
        self._hourly_limit = hourly_limit
        # One source never gets the whole hourly budget: at most its own cap,
        # and always at least one global slot short, so another guest's code
        # still sends. An hourly budget of one permits no single source.
        self._ip_hourly_limit = min(ip_hourly_limit, max(hourly_limit - 1, 0))
        self._lock = threading.Lock()

    async def take(self, ip: str) -> bool:
        """Count one guest mail before scheduling it; false when exhausted."""
        return await asyncio.to_thread(self._take_sync, ip)

    def _take_sync(self, ip: str) -> bool:
        with self._lock:
            if self._global_limit_hit():
                return False
            ip_key = f"i:{ip}"
            if self._store.rate_limited(
                _IP_KIND,
                ip_key,
                limit=self._ip_hourly_limit,
                window=HOUR_WINDOW_S,
            ):
                return False
            self._store.record_attempt(_GLOBAL_KIND, _GLOBAL_KEY)
            self._store.record_attempt(_IP_KIND, ip_key)
            return True

    def _global_limit_hit(self) -> bool:
        for limit, window in (
            (self._daily_limit, DAILY_WINDOW_S),
            (self._hourly_limit, HOUR_WINDOW_S),
        ):
            if self._store.rate_limited(
                _GLOBAL_KIND, _GLOBAL_KEY, limit=limit, window=window
            ):
                return True
        return False
