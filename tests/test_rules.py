"""Fixed, counts-only alert rules (task t7)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from aiohttp import web

from helpers import DEV_CONFIG
from irc_lens import alerts, rules, threat


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RecordingAlerter:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []

    def notify(self, kind: str, message: str) -> bool:
        self.notifications.append((kind, message))
        return True


def _monitor(**config_changes):
    clock = Clock()
    counters = threat.ThreatCounters(clock=clock)
    alerter = RecordingAlerter()
    config = replace(DEV_CONFIG, **config_changes)
    return rules.RuleMonitor(counters, alerter, config), counters, alerter, clock


@pytest.mark.parametrize(
    ("signal", "amount", "kind"),
    [
        (threat.Signal.REFUSED_RESPONSES, 50, alerts.KIND_RULE_BURST),
        (threat.Signal.ENTRY_POSTS, 10, alerts.KIND_RULE_BURST),
        (threat.Signal.RESERVED_ADDRESSES, 1, alerts.KIND_RULE_RESERVED_ADDRESS),
        (threat.Signal.FAILED_SENDS, 1, alerts.KIND_RULE_FAILED_SEND),
        (threat.Signal.BUDGET_HITS, 1, alerts.KIND_RULE_BUDGET_HIT),
    ],
)
def test_each_trigger_alerts_once_with_counts(
    signal: threat.Signal, amount: int, kind: str
) -> None:
    monitor, counters, alerter, _ = _monitor()
    counters.increment(signal, amount)

    assert monitor.check() is True
    assert monitor.check() is False

    assert len(alerter.notifications) == 1
    alert_kind, message = alerter.notifications[0]
    assert alert_kind == kind
    assert f"{signal.value}={amount}" in message


def test_budget_hit_warns_that_real_guests_may_get_no_mail() -> None:
    monitor, counters, alerter, _ = _monitor()
    counters.increment(threat.Signal.BUDGET_HITS)

    monitor.check()

    assert "real guests may be getting no mail" in alerter.notifications[0][1]


def test_multiple_triggers_in_one_window_raise_one_rule_owned_kind() -> None:
    monitor, counters, alerter, _ = _monitor()
    counters.increment(threat.Signal.ENTRY_POSTS, 10)
    counters.increment(threat.Signal.BUDGET_HITS)

    assert monitor.check() is True
    assert monitor.check() is False
    assert [kind for kind, _message in alerter.notifications] == [
        alerts.KIND_RULE_BUDGET_HIT
    ]


def test_lone_guests_normal_window_does_not_alert() -> None:
    monitor, counters, alerter, _ = _monitor()
    counters.increment(threat.Signal.REQUESTS, 3)
    counters.increment(threat.Signal.ENTRY_POSTS)
    counters.increment(threat.Signal.MAIL_SENDS)

    assert monitor.check() is False
    assert alerter.notifications == []


def test_thresholds_are_read_from_config_and_rechecked() -> None:
    monitor, counters, alerter, _ = _monitor(
        threat_entry_posts_threshold=3,
        threat_refused_responses_threshold=4,
        threat_reserved_addresses_threshold=2,
        threat_failed_sends_threshold=2,
        threat_budget_hits_threshold=2,
    )
    counters.increment(threat.Signal.ENTRY_POSTS, 2)

    assert monitor.check() is False

    counters.increment(threat.Signal.ENTRY_POSTS)
    assert monitor.check() is True
    assert len(alerter.notifications) == 1


def test_new_window_can_alert_after_previous_window() -> None:
    monitor, counters, alerter, clock = _monitor()
    counters.increment(threat.Signal.ENTRY_POSTS, 10)
    assert monitor.check() is True

    clock.now += threat.WINDOW_SECONDS
    counters.increment(threat.Signal.RESERVED_ADDRESSES)

    assert monitor.check() is True
    assert len(alerter.notifications) == 2


def test_trigger_at_end_of_window_is_not_lost_when_counters_roll() -> None:
    monitor, counters, alerter, clock = _monitor()
    clock.now += threat.WINDOW_SECONDS - 1
    counters.increment(threat.Signal.ENTRY_POSTS, 10)
    clock.now += 1

    assert monitor.check() is True
    assert "entry_posts=10" in alerter.notifications[0][1]


def test_rule_alerts_use_the_existing_hourly_limit_per_kind() -> None:
    clock = Clock()
    posts = []
    alerter = alerts.Alerter(
        "https://worker.example/alert",
        "secret",
        clock=clock,
        poster=lambda _url, _secret, payload: posts.append(payload),
    )
    counters = threat.ThreatCounters(clock=clock)
    monitor = rules.RuleMonitor(counters, alerter, DEV_CONFIG)

    results = []
    for _ in range(2):
        counters.increment(threat.Signal.ENTRY_POSTS, 10)
        results.append(monitor.check())
        alerter.wait()
        clock.now += threat.WINDOW_SECONDS

    assert results == [True, False]
    assert len(posts) == 1

    clock.now += alerts.ALERT_INTERVAL_S - (2 * threat.WINDOW_SECONDS)
    counters.increment(threat.Signal.ENTRY_POSTS, 10)
    assert monitor.check() is True
    alerter.wait()
    assert len(posts) == 2


async def test_periodic_rule_check_starts_and_stops_with_the_app() -> None:
    app = web.Application()
    counters = threat.install(app)
    alerter = RecordingAlerter()
    counters.increment(threat.Signal.ENTRY_POSTS, 10)
    rules.install(app, alerter, DEV_CONFIG, interval=60)
    app.freeze()

    await app.startup()
    await asyncio.sleep(0)

    task = app[rules.RULE_STATE].task
    assert task is not None
    assert alerter.notifications
    assert task.done() is False

    await app.cleanup()
    assert task.cancelled()
