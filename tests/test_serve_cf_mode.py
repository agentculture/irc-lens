"""--nick is rejected in CF mode; --bind on a non-loopback is coerced."""
from __future__ import annotations

import logging
from dataclasses import replace

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
        media_enabled=True,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )


def test_nick_rejected_in_cf_mode() -> None:
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


def test_config_bind_coerced_when_no_cli_flag_in_cf_mode(caplog) -> None:
    """Config web_bind is non-loopback, no CLI --bind: still coerced.

    Mirrors the explicit-CLI-flag case but proves the coercion fires
    when the operator misconfigures `web.bind: 0.0.0.0` in YAML and
    omits the CLI flag entirely.
    """
    cfg = replace(_cf_config(), web_bind="0.0.0.0")
    with caplog.at_level(logging.WARNING):
        coerced = _validate_cli_against_config(cfg, nick=None, bind=None)
    assert coerced.web_bind == "127.0.0.1"
    assert any("coerced" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.parametrize("loopback_bind", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_unchanged_in_cf_mode(loopback_bind: str) -> None:
    cfg = _cf_config()
    coerced = _validate_cli_against_config(cfg, nick=None, bind=loopback_bind)
    assert coerced.web_bind == cfg.web_bind


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
        media_enabled=True,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )
    result = _validate_cli_against_config(cfg, nick="anything", bind="0.0.0.0")
    assert result is cfg
    assert result.web_bind == "0.0.0.0"
