"""App sign-in config keys, guest limits, alert settings, and new metrics."""

from __future__ import annotations

from pathlib import Path

import pytest

from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import load_config
from irc_lens.metrics import Metrics


def _load(tmp_path: Path, auth_extra: str = "", guest_block: str = ""):
    p = tmp_path / "config.yaml"
    p.write_text(
        f"""
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
{auth_extra}
server:
  name: spark
{guest_block}"""
    )
    return load_config(p)


def _guest(body: str) -> str:
    return "guest_mode:\n" + "".join(f"  {ln}\n" for ln in body.strip().splitlines())


def test_defaults(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    # No mail provider configured, so app sign-in stays off (review fix).
    assert cfg.app_signin_enabled is False
    assert cfg.guest_max_guests == 1
    assert cfg.guest_idle_close_s == 900
    assert cfg.guest_mail_alert_url is None
    assert cfg.guest_mail_alert_secret_env is None


def test_app_signin_can_be_disabled(tmp_path: Path) -> None:
    cfg = _load(tmp_path, auth_extra="  app_signin:\n    enabled: false")
    assert cfg.app_signin_enabled is False


def test_app_signin_invalid(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        _load(tmp_path, auth_extra="  app_signin:\n    enabled: maybe")
    assert exc.value.code == EXIT_USER_ERROR
    assert "auth.app_signin.enabled" in str(exc.value)


def test_app_signin_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(AfiError, match="auth.app_signin"):
        _load(tmp_path, auth_extra="  app_signin:\n    enable: true")


def test_guest_limits_and_alerts_parse(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        guest_block=_guest(
            """
max_guests: 3
idle_close_s: 120
mail:
  alert_url: https://alerts.example.com/hook
  alert_secret_env: MY_ALERT_SECRET
"""
        ),
    )
    assert cfg.guest_max_guests == 3
    assert cfg.guest_idle_close_s == 120
    assert cfg.guest_mail_alert_url == "https://alerts.example.com/hook"
    assert cfg.guest_mail_alert_secret_env == "MY_ALERT_SECRET"


@pytest.mark.parametrize("url", ["http://127.0.0.1:9000/x", "http://localhost/x"])
def test_alert_url_http_allowed_for_loopback(tmp_path: Path, url: str) -> None:
    cfg = _load(tmp_path, guest_block=_guest(f"mail:\n  alert_url: {url}"))
    assert cfg.guest_mail_alert_url == url


@pytest.mark.parametrize(
    "url", ["http://example.com/x", "ftp://example.com/x", "https://", "nonsense"]
)
def test_alert_url_invalid(tmp_path: Path, url: str) -> None:
    block = _guest(f"mail:\n  alert_url: {url}")
    with pytest.raises(AfiError, match="alert_url"):
        _load(tmp_path, guest_block=block)


@pytest.mark.parametrize("val", ["0", "-1", "'x'", "1.5"])
def test_max_guests_invalid(tmp_path: Path, val: str) -> None:
    block = _guest(f"max_guests: {val}")
    with pytest.raises(AfiError, match="max_guests"):
        _load(tmp_path, guest_block=block)


def test_alert_secret_env_must_be_string(tmp_path: Path) -> None:
    block = _guest("mail:\n  alert_secret_env: 5")
    with pytest.raises(AfiError, match="alert_secret_env"):
        _load(tmp_path, guest_block=block)


def test_new_counters() -> None:
    m = Metrics()
    snap = m.snapshot()
    for key in (
        "signin_codes_sent",
        "sessions_started",
        "sessions_ended",
        "guest_busy",
        "delivery_alerts",
    ):
        assert snap[key] == 0
    m.signin_code_sent()
    m.session_started()
    m.session_started()
    m.session_ended()
    m.guest_busy()
    m.delivery_alert()
    snap = m.snapshot()
    assert snap["signin_codes_sent"] == 1
    assert snap["sessions_started"] == 2
    assert snap["sessions_ended"] == 1
    assert snap["guest_busy"] == 1
    assert snap["delivery_alerts"] == 1
