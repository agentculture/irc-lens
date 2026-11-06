"""Fixed, counts-only threat rules evaluated off the request path."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Protocol

from aiohttp import web

from irc_lens import alerts, decider, threat
from irc_lens.config import LensConfig

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 5.0
_BUDGET_WARNING = "real guests may be getting no mail"


class AlertSink(Protocol):
    def notify(self, kind: str, message: str) -> bool: ...


class ThreatDecider(Protocol):
    def assess(
        self,
        snapshot: threat.ThreatSnapshot,
        callback: decider.VerdictCallback,
    ) -> bool: ...


@dataclass
class RuleState:
    task: asyncio.Task | None = None


RULE_STATE = web.AppKey("fixed_rule_state", RuleState)


class RuleMonitor:
    """Re-check configured thresholds and alert once for each window."""

    def __init__(
        self,
        counters: threat.ThreatCounters,
        alerter: AlertSink,
        config: LensConfig,
        *,
        threat_decider: ThreatDecider | None = None,
    ) -> None:
        self._counters = counters
        self._alerter = alerter
        self._thresholds = {
            threat.Signal.REFUSED_RESPONSES: config.threat_refused_responses_threshold,
            threat.Signal.ENTRY_POSTS: config.threat_entry_posts_threshold,
            threat.Signal.RESERVED_ADDRESSES: (
                config.threat_reserved_addresses_threshold
            ),
            threat.Signal.FAILED_SENDS: config.threat_failed_sends_threshold,
            threat.Signal.BUDGET_HITS: config.threat_budget_hits_threshold,
        }
        self._alerted_window: float | None = None
        self._checked_completed_window: float | None = None
        self._threat_decider = threat_decider

    def check(self) -> bool:
        """Alert if the current, freshly-read counters cross a fixed rule."""
        completed, current = self._counters.snapshots()
        notified = self._check_completed(completed)
        return self._check_snapshot(current) or notified

    def _check_completed(self, snapshot: threat.ThreatSnapshot | None) -> bool:
        if snapshot is None:
            return False
        if snapshot.window_started_at == self._checked_completed_window:
            return False
        self._checked_completed_window = snapshot.window_started_at
        return self._check_snapshot(snapshot)

    def _check_snapshot(self, snapshot: threat.ThreatSnapshot) -> bool:
        if snapshot.window_started_at == self._alerted_window:
            return False
        if not self._triggered(snapshot):
            return False
        self._alerted_window = snapshot.window_started_at
        return self._assess_and_alert(snapshot)

    def _assess_and_alert(self, snapshot: threat.ThreatSnapshot) -> bool:
        kind = self._kind(snapshot)
        message = self._message(snapshot)
        if self._threat_decider is None:
            return self._notify(kind, message, None)
        try:
            started = self._threat_decider.assess(
                snapshot,
                lambda verdict: self._notify(kind, message, verdict),
            )
        except Exception:  # noqa: BLE001 -- an optional verdict cannot hide an alert
            logger.exception("threat decider failed before starting")
            return self._notify(kind, message, None)
        if not started:
            return self._notify(kind, message, None)
        return True

    def _notify(
        self,
        kind: str,
        message: str,
        verdict: decider.Verdict | None,
    ) -> bool:
        if verdict is None:
            assessment = "Clef advisory verdict: not assessed."
        else:
            assessment = (
                f"Clef advisory verdict: {verdict.choice} "
                f"(confidence={verdict.confidence:.3f})."
            )
        return self._alerter.notify(kind, f"{message} {assessment}")

    def _triggered(self, snapshot: threat.ThreatSnapshot) -> bool:
        return any(
            snapshot.counts[signal] >= threshold
            for signal, threshold in self._thresholds.items()
        )

    def _kind(self, snapshot: threat.ThreatSnapshot) -> str:
        counts = snapshot.counts
        if counts[threat.Signal.BUDGET_HITS] >= self._thresholds[
            threat.Signal.BUDGET_HITS
        ]:
            return alerts.KIND_RULE_BUDGET_HIT
        if counts[threat.Signal.FAILED_SENDS] >= self._thresholds[
            threat.Signal.FAILED_SENDS
        ]:
            return alerts.KIND_RULE_FAILED_SEND
        if counts[threat.Signal.RESERVED_ADDRESSES] >= self._thresholds[
            threat.Signal.RESERVED_ADDRESSES
        ]:
            return alerts.KIND_RULE_RESERVED_ADDRESS
        return alerts.KIND_RULE_BURST

    @staticmethod
    def _message(snapshot: threat.ThreatSnapshot) -> str:
        values = [
            *(f"{signal.value}={snapshot.counts[signal]}" for signal in threat.Signal),
            f"distinct_sources={snapshot.distinct_sources}",
        ]
        message = f"Fixed threat rule triggered; counts: {', '.join(values)}."
        if snapshot.counts[threat.Signal.BUDGET_HITS]:
            message = f"{message} {_BUDGET_WARNING}."
        return message


async def _loop(monitor: RuleMonitor, interval: float) -> None:
    while True:
        try:
            monitor.check()
        except Exception:  # noqa: BLE001 -- monitoring must not stop the app
            logger.exception("fixed threat rule check failed")
        await asyncio.sleep(interval)


def install(
    app: web.Application,
    alerter: AlertSink,
    config: LensConfig,
    *,
    interval: float = CHECK_INTERVAL_S,
    threat_decider: ThreatDecider | None = None,
) -> RuleMonitor:
    """Start and stop one periodic fixed-rule monitor with the application."""
    assessor = (
        threat_decider
        if threat_decider is not None
        else decider.make_threat_verdict_caller(config)
    )
    monitor = RuleMonitor(
        app[threat.THREAT_COUNTERS],
        alerter,
        config,
        threat_decider=assessor,
    )
    state = RuleState()
    app[RULE_STATE] = state

    async def start(_app: web.Application) -> None:  # NOSONAR S7503 - on_startup
        state.task = asyncio.create_task(_loop(monitor, interval))

    async def stop(_app: web.Application) -> None:
        task = state.task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return monitor
