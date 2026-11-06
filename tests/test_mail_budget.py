"""Persistent guest-mail budget (c2/h7, c3/h8, c21/h21, c12/h5)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from irc_lens._errors import AfiError
from irc_lens.config import load_config
from irc_lens.guest_store import ATTEMPT_RETENTION_S, GuestStore
from irc_lens.mail_budget import DAILY_WINDOW_S, HOUR_WINDOW_S, MailBudget

T0 = 1_800_000_000


@pytest.fixture(autouse=True)
def _joined_to_thread(monkeypatch):
    """Exercise worker-thread calls without this sandbox's stuck executor."""

    async def run(func, /, *args, **kwargs):
        result = []
        error = []

        def invoke() -> None:
            try:
                result.append(func(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 - re-raised in caller
                error.append(exc)

        worker = threading.Thread(target=invoke)
        worker.start()
        worker.join()
        if error:
            raise error[0]
        return result[0]

    monkeypatch.setattr(asyncio, "to_thread", run)


def _config_text(mail: str = "") -> str:
    return f"""
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
guest_mode:
  mail:
{mail or '    provider: none'}
"""


def test_mail_budget_config_defaults_and_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_config_text())
    defaults = load_config(path)
    assert defaults.guest_mail_daily_budget == 50
    assert defaults.guest_mail_hourly_budget == 20

    path.write_text(
        _config_text("    daily_budget: 75\n    hourly_budget: 0")
    )
    configured = load_config(path)
    assert configured.guest_mail_daily_budget == 75
    assert configured.guest_mail_hourly_budget == 0


@pytest.mark.parametrize("key", ["daily_budget", "hourly_budget"])
def test_mail_budget_config_rejects_negative_values(tmp_path: Path, key: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_config_text(f"    {key}: -1"))
    with pytest.raises(AfiError) as exc:
        load_config(path)
    assert f"guest_mode.mail.{key}" in str(exc.value)


@pytest.mark.asyncio
async def test_budget_persists_and_lift_after_restart_restores_mail(
    tmp_path: Path,
) -> None:
    now = {"value": T0}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        _config_text("    daily_budget: 1\n    hourly_budget: 2")
    )
    low = load_config(config_path)
    database = tmp_path / "guests.db"
    store = GuestStore(database, clock=lambda: now["value"])
    budget = MailBudget(
        store,
        daily_limit=low.guest_mail_daily_budget,
        hourly_limit=low.guest_mail_hourly_budget,
    )
    assert await budget.take("192.0.2.1")
    assert not await budget.take("192.0.2.2")

    restarted_store = GuestStore(database, clock=lambda: now["value"])
    restarted = MailBudget(restarted_store, daily_limit=1, hourly_limit=2)
    assert not await restarted.take("192.0.2.2")

    now["value"] += HOUR_WINDOW_S + 1
    after_hour = MailBudget(restarted_store, daily_limit=1, hourly_limit=2)
    assert not await after_hour.take("192.0.2.2")

    config_path.write_text(
        _config_text("    daily_budget: 2\n    hourly_budget: 2")
    )
    high = load_config(config_path)
    lifted = MailBudget(
        restarted_store,
        daily_limit=high.guest_mail_daily_budget,
        hourly_limit=high.guest_mail_hourly_budget,
    )
    assert await lifted.take("192.0.2.2")


@pytest.mark.asyncio
async def test_one_ip_cannot_spend_the_whole_hourly_budget(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db", clock=lambda: T0)
    budget = MailBudget(store, daily_limit=10, hourly_limit=3)

    assert await budget.take("192.0.2.1")
    assert await budget.take("192.0.2.1")
    assert not await budget.take("192.0.2.1")
    assert await budget.take("192.0.2.2")
    assert not await budget.take("192.0.2.3")


@pytest.mark.asyncio
async def test_ip_hourly_cap_limits_one_source(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db", clock=lambda: T0)
    budget = MailBudget(store, daily_limit=50, hourly_limit=20, ip_hourly_limit=2)

    assert await budget.take("192.0.2.1")
    assert await budget.take("192.0.2.1")
    assert not await budget.take("192.0.2.1")
    assert await budget.take("192.0.2.2")


def test_ip_hourly_cap_defaults_to_five_and_is_configurable(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_config_text())
    assert load_config(path).guest_mail_ip_hourly_budget == 5
    path.write_text(_config_text("    ip_hourly_budget: 1"))
    assert load_config(path).guest_mail_ip_hourly_budget == 1


@pytest.mark.asyncio
async def test_daily_window_has_retention_margin(tmp_path: Path) -> None:
    assert ATTEMPT_RETENTION_S > DAILY_WINDOW_S
    now = {"value": T0}
    store = GuestStore(tmp_path / "guests.db", clock=lambda: now["value"])
    budget = MailBudget(store, daily_limit=1, hourly_limit=10)
    assert await budget.take("192.0.2.1")

    now["value"] += DAILY_WINDOW_S - 1
    store.sweep()
    restarted = MailBudget(store, daily_limit=1, hourly_limit=10)
    assert not await restarted.take("192.0.2.2")

    now["value"] += 2
    assert await restarted.take("192.0.2.2")


class _ThreadCheckingStore:
    def __init__(self) -> None:
        self.caller = threading.get_ident()

    def rate_limited(self, *_args, **_kwargs) -> bool:
        assert threading.get_ident() != self.caller
        return False

    def record_attempt(self, *_args) -> None:
        assert threading.get_ident() != self.caller


@pytest.mark.asyncio
async def test_sqlite_helpers_stay_off_the_event_loop() -> None:
    store = _ThreadCheckingStore()
    budget = MailBudget(store, daily_limit=50, hourly_limit=20)
    assert await budget.take("192.0.2.1")
    await asyncio.sleep(0)
