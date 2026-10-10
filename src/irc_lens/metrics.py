"""Guest-mode observability: process-wide counters and agent presence.

Counters (all monotonic except the ``active_guest_sessions`` gauge):
``entries``, ``active_guest_sessions``, ``rate_limited_429``,
``failed_sign_ins``, ``agent_errors``. Exposed by the owner-only
``GET /owner/metrics`` route.

:class:`AgentPresence` tracks when the sandbox agent nick (default
``sbx-ask``) was last seen on the sandbox IRCd. The live feed belongs to
the sandbox routing code: it calls ``presence.seen(nick)`` on any sign of
life (JOIN, PRIVMSG, NAMES/WHO entry, PONG) and ``presence.gone(nick)`` on
QUIT/PART. A nick not seen for :data:`AGENT_OFFLINE_AFTER_S` seconds reads
as ``offline`` (never seen at all reads ``unknown``); the UI renders
``state`` from the route. The clock is injectable so tests never sleep.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

AGENT_OFFLINE_AFTER_S = 60.0
DEFAULT_AGENT_NICK = "sbx-ask"

_COUNTERS = (
    "entries",
    "rate_limited_429",
    "failed_sign_ins",
    "agent_errors",
    "signin_codes_sent",
    "sessions_started",
    "sessions_ended",
    "guest_busy",
    "delivery_alerts",
)


class Metrics:
    """Thread-safe counters plus the active-guest-sessions gauge."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(_COUNTERS, 0)
        self._active = 0

    def _bump(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def entry(self) -> None:
        self._bump("entries")

    def rate_limited(self) -> None:
        self._bump("rate_limited_429")

    def failed_sign_in(self) -> None:
        self._bump("failed_sign_ins")

    def agent_error(self) -> None:
        self._bump("agent_errors")

    def signin_code_sent(self) -> None:
        self._bump("signin_codes_sent")

    def session_started(self) -> None:
        self._bump("sessions_started")

    def session_ended(self) -> None:
        self._bump("sessions_ended")

    def guest_busy(self) -> None:
        self._bump("guest_busy")

    def delivery_alert(self) -> None:
        self._bump("delivery_alerts")

    def session_opened(self) -> None:
        with self._lock:
            self._active += 1

    def session_closed(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            c = self._counts
            return {
                "entries": c["entries"],
                "active_guest_sessions": self._active,
                "rate_limited_429": c["rate_limited_429"],
                "failed_sign_ins": c["failed_sign_ins"],
                "agent_errors": c["agent_errors"],
                "signin_codes_sent": c["signin_codes_sent"],
                "sessions_started": c["sessions_started"],
                "sessions_ended": c["sessions_ended"],
                "guest_busy": c["guest_busy"],
                "delivery_alerts": c["delivery_alerts"],
            }


class AgentPresence:
    """Last-seen tracker for the sandbox agent nick."""

    def __init__(
        self,
        nick: str = DEFAULT_AGENT_NICK,
        *,
        clock: Callable[[], float] = time.monotonic,
        offline_after_s: float = AGENT_OFFLINE_AFTER_S,
    ) -> None:
        self.nick = nick
        self._clock = clock
        self._offline_after = offline_after_s
        self._lock = threading.Lock()
        self._last_seen: float | None = None
        self._gone = False

    def seen(self, nick: str) -> None:
        if nick.lower() != self.nick.lower():
            return
        with self._lock:
            self._last_seen = self._clock()
            self._gone = False

    def gone(self, nick: str) -> None:
        if nick.lower() != self.nick.lower():
            return
        with self._lock:
            self._gone = True

    def state(self) -> dict[str, Any]:
        """``state`` is ``online`` | ``offline`` | ``unknown`` (never seen)."""
        with self._lock:
            last, gone = self._last_seen, self._gone
        if last is None:
            return {
                "nick": self.nick,
                "state": "unknown",
                "online": False,
                "last_seen_age_s": None,
            }
        age = self._clock() - last
        online = not gone and age < self._offline_after
        return {
            "nick": self.nick,
            "state": "online" if online else "offline",
            "online": online,
            "last_seen_age_s": age,
        }


_METRICS = Metrics()
_PRESENCE = AgentPresence()


def get_metrics() -> Metrics:
    return _METRICS


def get_presence() -> AgentPresence:
    return _PRESENCE
