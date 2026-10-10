"""Delivery alerts (task t7): tell the operators when mail stops flowing.

When a Resend send fails (HTTP error including quota exhaustion, or a
network error) the lens posts ``{"kind", "message"}`` as JSON to
``guest_mode.mail.alert_url`` -- a Cloudflare Worker that emails the
approved users -- with ``Authorization: Bearer <secret>``. At most one
alert per failure kind per hour. The payload never carries a recipient
address, a code, or a provider response body. Posting happens on a
daemon thread; every failure is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from collections.abc import Callable

from irc_lens import metrics
from irc_lens.config import LensConfig

logger = logging.getLogger(__name__)

#: One alert per kind per this many seconds.
ALERT_INTERVAL_S = 3600

KIND_QUOTA = "quota"
KIND_SEND_FAILED = "send_failed"

_TAIL = "sign-in and guest codes are not being delivered. Approved users can still sign in at /login."

Poster = Callable[[str, str, dict], None]


def message_for(kind: str, status: int | None) -> str:
    """The fixed alert text for *kind* (no address, no code, no body)."""
    if kind == KIND_QUOTA:
        return f"Resend quota used up: {_TAIL}"
    what = f"HTTP {status}" if status is not None else "network error"
    return f"Resend send failed ({what}): {_TAIL}"


def classify(status: int | None) -> str:
    return KIND_QUOTA if status == 429 else KIND_SEND_FAILED


def _http_post(url: str, secret: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
            "User-Agent": "irc-lens-alerts",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


class Alerter:
    """Rate-limited, never-raising alert client."""

    def __init__(
        self,
        url: str | None,
        secret: str | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        poster: Callable[[str, str, dict], None] = _http_post,
        interval_s: int = ALERT_INTERVAL_S,
    ) -> None:
        self._url = url or None
        self._secret = secret or None
        self._clock = clock
        self._poster = poster
        self._interval = interval_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._warned_disabled = False

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._secret)

    def notify(self, kind: str, message: str) -> bool:
        """Post an alert unless disabled or *kind* alerted within the hour.

        Returns True when a post was started. Never raises.
        """
        try:
            if not self.enabled:
                with self._lock:
                    warn, self._warned_disabled = not self._warned_disabled, True
                if warn:
                    logger.warning(
                        "delivery alert not sent: alert url/secret not configured"
                    )
                return False
            now = self._clock()
            with self._lock:
                last = self._last.get(kind)
                if last is not None and now - last < self._interval:
                    return False
                self._last[kind] = now
            metrics.get_metrics().delivery_alert()
            t = threading.Thread(
                target=self._post,
                args=(kind, message),
                daemon=True,
                name="delivery-alert",
            )
            self._threads.append(t)
            t.start()
            return True
        except Exception as exc:  # noqa: BLE001 -- alerting must never break a request
            logger.warning("delivery alert failed: %s", type(exc).__name__)
            return False

    def _post(self, kind: str, message: str) -> None:
        try:
            self._poster(self._url, self._secret, {"kind": kind, "message": message})
        except Exception as exc:  # noqa: BLE001
            logger.warning("delivery alert post failed: %s", type(exc).__name__)

    def wait(self, timeout: float = 5.0) -> None:
        """Join in-flight posts (tests / shutdown)."""
        for t in tuple(self._threads):  # snapshot: other threads may append
            t.join(timeout)
        self._threads = [t for t in self._threads if t.is_alive()]


def make_alerter(cfg: LensConfig) -> Alerter:
    """Build the app's alerter; the secret is read from the environment."""
    env = cfg.guest_mail_alert_secret_env
    secret = os.environ.get(env, "") if env else ""
    return Alerter(cfg.guest_mail_alert_url, secret)
