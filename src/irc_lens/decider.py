"""Small, optional client for bounded Clef choice verdicts.

The HTTP client permits one call in flight and never queues or retries.  The
threat-specific caller separately owns its per-window and rolling-day caps.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from irc_lens import threat
from irc_lens.config import LensConfig

logger = logging.getLogger(__name__)

TIMEOUT_S = 5.0
DAILY_WINDOW_S = 24 * 60 * 60
DAILY_CALL_CAP = 24
THREAT_QUESTION_KEY = "threat_verdict"
THREAT_CHOICES = frozenset({"genuine", "abusive", "unclear"})


@dataclass(frozen=True)
class Verdict:
    """One validated choice answer from the decider."""

    choice: str
    confidence: float
    probabilities: Mapping[str, float]


Poster = Callable[[str, str, dict, float], object]
VerdictCallback = Callable[[Verdict | None], None]


class ChoiceClient(Protocol):
    def submit(
        self,
        request: dict,
        question_key: str,
        choices: frozenset[str],
        callback: VerdictCallback,
    ) -> bool: ...


def _http_post(url: str, credential: str, payload: dict, timeout: float) -> object:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "User-Agent": "irc-lens-decider",
            "X-Lobes-Model": "decider:general",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("decider returned a non-200 status")
        return json.load(response)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0.0 <= number <= 1.0 else None


def _validated_verdict(
    payload: object, question_key: str, choices: frozenset[str]
) -> Verdict | None:
    if not isinstance(payload, dict):
        return None
    answers = payload.get("answers")
    answer = answers.get(question_key) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type", "choice") != "choice":
        return None
    choice = answer.get("choice")
    confidence = _number(answer.get("confidence"))
    raw_probabilities = answer.get("probabilities")
    if (
        choice not in choices
        or confidence is None
        or not isinstance(raw_probabilities, dict)
    ):
        return None
    if set(raw_probabilities) != choices:
        return None
    probabilities: dict[str, float] = {}
    for key, value in raw_probabilities.items():
        probability = _number(value)
        if probability is None:
            return None
        probabilities[key] = probability
    return Verdict(choice, confidence, probabilities)


class DeciderClient:
    """Run at most one five-second HTTP choice assessment in a daemon thread."""

    def __init__(
        self,
        url: str,
        credential: str,
        *,
        poster: Poster = _http_post,
    ) -> None:
        self._url = url
        self._credential = credential
        self._poster = poster
        self._lock = threading.Lock()
        self._in_flight = False
        self._callback_done = False
        self._generation = 0
        self._thread: threading.Thread | None = None

    def submit(
        self,
        request: dict,
        question_key: str,
        choices: frozenset[str],
        callback: VerdictCallback,
    ) -> bool:
        """Start one call, or return ``False`` without queueing it."""
        with self._lock:
            if self._in_flight:
                return False
            self._in_flight = True
            self._callback_done = False
            self._generation += 1
            generation = self._generation
            thread = threading.Thread(
                target=self._run,
                args=(generation, request, question_key, choices, callback),
                daemon=True,
                name="clef-decider",
            )
            self._thread = thread
            thread.start()
        return True

    def _run(
        self,
        generation: int,
        request: dict,
        question_key: str,
        choices: frozenset[str],
        callback: VerdictCallback,
    ) -> None:
        timer = threading.Timer(
            TIMEOUT_S,
            self._time_out,
            args=(generation, callback),
        )
        timer.daemon = True
        timer.start()
        verdict = None
        try:
            payload = self._poster(self._url, self._credential, request, TIMEOUT_S)
            verdict = _validated_verdict(payload, question_key, choices)
        except Exception as exc:  # noqa: BLE001 -- optional assessment fails open
            logger.warning("decider assessment failed: %s", type(exc).__name__)
        finally:
            timer.cancel()
            deliver = False
            with self._lock:
                self._in_flight = False
                if generation == self._generation and not self._callback_done:
                    self._callback_done = True
                    deliver = True
            if deliver:
                self._deliver(callback, verdict)

    def _time_out(self, generation: int, callback: VerdictCallback) -> None:
        with self._lock:
            if generation != self._generation or self._callback_done:
                return
            self._callback_done = True
        self._deliver(callback, None)

    @staticmethod
    def _deliver(callback: VerdictCallback, verdict: Verdict | None) -> None:
        try:
            callback(verdict)
        except Exception:  # noqa: BLE001 -- a callback must not kill future calls
            logger.exception("decider callback failed")

    def wait(self, timeout: float = 6.0) -> None:
        """Wait for the current call during tests or orderly shutdown."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)


def build_threat_request(snapshot: threat.ThreatSnapshot) -> dict:
    """Build a fixed-schema Clef request exclusively from derived counters."""
    facts = [
        "15-minute public traffic counts:",
        *(f"{signal.value}={int(snapshot.counts[signal])}" for signal in threat.Signal),
        f"distinct_sources={int(snapshot.distinct_sources)}",
        f"source_count_capped={str(bool(snapshot.source_count_capped)).lower()}",
    ]
    return {
        "model": "clef",
        "state": "\n".join(facts),
        "questions": {
            THREAT_QUESTION_KEY: {
                "type": "choice",
                "instructions": (
                    "Do these aggregate counts most likely represent genuine traffic, "
                    "abusive traffic, or remain unclear?"
                ),
                "criteria": {
                    "genuine": "Likely ordinary public use.",
                    "abusive": "Likely automated probing or sign-in or mail abuse.",
                    "unclear": (
                        "The aggregate counts do not support either conclusion."
                    ),
                },
            }
        },
    }


class ThreatVerdictCaller:
    """Apply threat-only per-window and rolling-day caps to a choice client."""

    def __init__(
        self,
        client: ChoiceClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._clock = clock
        self._calls: deque[float] = deque()
        self._last_window: float | None = None
        self._lock = threading.Lock()

    def assess(
        self,
        snapshot: threat.ThreatSnapshot,
        callback: VerdictCallback,
    ) -> bool:
        """Start an allowed assessment, returning false on any cap or contention."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            window = snapshot.window_started_at
            if self._last_window is not None and window <= self._last_window:
                return False
            self._last_window = window
            if len(self._calls) >= DAILY_CALL_CAP:
                return False
            started = self._client.submit(
                build_threat_request(snapshot),
                THREAT_QUESTION_KEY,
                THREAT_CHOICES,
                callback,
            )
            if started:
                self._calls.append(now)
            return started

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= DAILY_WINDOW_S:
            self._calls.popleft()


def make_threat_verdict_caller(config: LensConfig) -> ThreatVerdictCaller | None:
    """Build the optional caller, reading only the named credential variable."""
    url = config.threat_decider_url
    env_name = config.threat_decider_credential_env
    credential = os.environ.get(env_name, "") if env_name else ""
    if not url or not credential:
        return None
    return ThreatVerdictCaller(DeciderClient(url, credential))
