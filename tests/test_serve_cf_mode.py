"""--nick is rejected in CF mode; --bind on a non-loopback is coerced."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from irc_lens.cli._commands.serve import _validate_cli_against_config
from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import LensConfig


def _cf_config() -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="a",
        cf_team_domain="t.cloudflareaccess.com",
        allowed_emails=("a@example.com",),
        allowed_service_tokens=(),
        server_name="spark",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=8765,
    )


def test_nick_rejected_in_cf_mode(tmp_path: Path) -> None:
    cfg = _cf_config()
    with pytest.raises(AfiError) as exc:
        _validate_cli_against_config(cfg, nick="something", bind="127.0.0.1")
    assert exc.value.code == EXIT_USER_ERROR
    assert "--nick" in exc.value.message


def test_bind_coerced_to_loopback_in_cf_mode(caplog) -> None:
    cfg = _cf_config()
    with caplog.at_level(logging.WARNING):
        coerced = _validate_cli_against_config(cfg, nick=None, bind="0.0.0.0")
    assert coerced.web_bind == "127.0.0.1"
    assert any("coerced" in r.getMessage().lower() for r in caplog.records)


def test_loopback_bind_unchanged_in_cf_mode() -> None:
    cfg = _cf_config()
    coerced = _validate_cli_against_config(cfg, nick=None, bind="127.0.0.1")
    assert coerced.web_bind == "127.0.0.1"


def test_dev_mode_passes_through_unchanged() -> None:
    """Dev mode is a no-op — no AfiError, no coercion."""
    cfg = LensConfig(
        auth_mode="dev",
        dev_nick="testuser",
        dev_email="testuser@local",
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="spark",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="0.0.0.0",
        web_port=8765,
    )
    result = _validate_cli_against_config(cfg, nick="anything", bind="0.0.0.0")
    assert result is cfg
    assert result.web_bind == "0.0.0.0"
