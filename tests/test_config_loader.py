"""Schema validation, defaults, and override semantics for LensConfig."""
from __future__ import annotations

from pathlib import Path

import pytest

from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError
from irc_lens.config import LensConfig, load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_dev_mode_minimal_loads(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
"""))
    assert cfg.auth_mode == "dev"
    assert cfg.dev_nick == "lens"
    assert cfg.dev_email == "dev@local"
    assert cfg.server_name == "spark"
    assert cfg.server_host == "127.0.0.1"
    assert cfg.server_port == 6667
    assert cfg.web_bind == "127.0.0.1"
    assert cfg.web_port == 8765


def test_cf_mode_minimal_loads(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, """
auth:
  mode: cloudflare-access
  cloudflare:
    aud: aud-tag
    team_domain: team.cloudflareaccess.com
  allowed_emails:
    - alice@example.com
server:
  name: spark
"""))
    assert cfg.auth_mode == "cloudflare-access"
    assert cfg.cf_aud == "aud-tag"
    assert cfg.cf_team_domain == "team.cloudflareaccess.com"
    assert cfg.allowed_emails == ("alice@example.com",)
    assert cfg.allowed_service_tokens == ()


def test_cf_mode_missing_aud_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: cloudflare-access
  cloudflare:
    team_domain: team.cloudflareaccess.com
  allowed_emails:
    - alice@example.com
server:
  name: spark
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "auth.cloudflare.aud" in exc.value.message


def test_dev_mode_missing_dev_section_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
server:
  name: spark
"""))
    assert "auth.dev.nick" in exc.value.message


def test_unknown_mode_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: oauth-passport
server:
  name: spark
"""))
    assert "auth.mode" in exc.value.message
    assert "dev" in exc.value.remediation
    assert "cloudflare-access" in exc.value.remediation


def test_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(tmp_path / "nope.yaml")
    assert exc.value.code == EXIT_USER_ERROR
    assert "config init" in exc.value.remediation


def test_malformed_yaml_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, "::: not: yaml :::"))
    assert "YAML" in exc.value.message or "parse" in exc.value.message


def test_invalid_port_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
  port: not-a-number
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "server.port" in exc.value.message
    assert "between 1 and 65535" in exc.value.message


def test_invalid_web_port_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
web:
  port: nope
"""))
    assert "web.port" in exc.value.message
    assert "between 1 and 65535" in exc.value.message


def test_empty_allowed_emails_errors_in_cf_mode(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: cloudflare-access
  cloudflare:
    aud: aud-tag
    team_domain: team.cloudflareaccess.com
  allowed_emails: []
server:
  name: spark
"""))
    assert "allowed_emails" in exc.value.message
    assert "empty" in exc.value.message.lower()


@pytest.mark.parametrize("port_value,expected_substring", [
    ("8765.9", "must be an integer between 1 and 65535"),
    ("true", "must be an integer between 1 and 65535"),
    ("0", "must be an integer between 1 and 65535"),
    ("70000", "must be an integer between 1 and 65535"),
    ("-1", "must be an integer between 1 and 65535"),
])
def test_port_validation_rejects_bad_values(
    tmp_path: Path, port_value: str, expected_substring: str
) -> None:
    yaml_body = f"""
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
  port: {port_value}
"""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, yaml_body))
    assert expected_substring in exc.value.message


def test_falsy_web_section_rejected(tmp_path: Path) -> None:
    """web: [] (or 0, or false) must NOT silently become {}."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
web: []
"""))
    assert "web" in exc.value.message
    assert "mapping" in exc.value.message


def test_unreadable_config_raises_env_error(tmp_path: Path) -> None:
    """A path that exists but can't be read fails with EXIT_ENV_ERROR."""
    p = tmp_path / "config.yaml"
    p.write_text("auth: {}")
    p.chmod(0o000)
    try:
        with pytest.raises(AfiError) as exc:
            load_config(p)
        assert exc.value.code == EXIT_ENV_ERROR
        assert "could not read" in exc.value.message
    finally:
        p.chmod(0o600)  # so cleanup works


def test_media_section_absent_uses_defaults(tmp_path: Path) -> None:
    """Absent media section yields all defaults."""
    cfg = load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
"""))
    assert cfg.media_enabled is True
    assert cfg.media_max_file_bytes == 10485760  # 10 MiB
    assert cfg.media_max_store_bytes == 268435456  # 256 MiB
    assert cfg.media_public_base_url == ""
    assert cfg.media_remote_embeds == "click"
    assert cfg.media_trusted_hosts == ()
    # media_dir defaults to XDG_DATA_HOME/irc-lens/media or ~/.local/share/irc-lens/media
    assert "irc-lens/media" in cfg.media_dir


def test_media_section_with_all_fields(tmp_path: Path) -> None:
    """Explicit media section values are loaded."""
    custom_dir = str(tmp_path / "custom-media")
    cfg = load_config(_write(tmp_path, f"""
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
media:
  enabled: false
  dir: {custom_dir}
  max_file_bytes: 5242880
  max_store_bytes: 104857600
  public_base_url: "http://192.168.1.1:8765"
  remote_embeds: auto
  trusted_hosts:
    - example.com
    - trusted.local
"""))
    assert cfg.media_enabled is False
    assert cfg.media_dir == custom_dir
    assert cfg.media_max_file_bytes == 5242880
    assert cfg.media_max_store_bytes == 104857600
    assert cfg.media_public_base_url == "http://192.168.1.1:8765"
    assert cfg.media_remote_embeds == "auto"
    assert cfg.media_trusted_hosts == ("example.com", "trusted.local")


def test_media_section_partial_uses_defaults_for_missing(tmp_path: Path) -> None:
    """Partial media section merges with defaults."""
    cfg = load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
media:
  enabled: false
  remote_embeds: "off"
"""))
    assert cfg.media_enabled is False
    assert cfg.media_remote_embeds == "off"
    assert cfg.media_max_file_bytes == 10485760  # default
    assert cfg.media_max_store_bytes == 268435456  # default
    assert cfg.media_public_base_url == ""  # default
    assert cfg.media_trusted_hosts == ()  # default


def test_media_invalid_remote_embeds_value_errors(tmp_path: Path) -> None:
    """Invalid remote_embeds value raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
media:
  remote_embeds: invalid
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "remote_embeds" in exc.value.message
    assert "click" in exc.value.remediation
    assert "auto" in exc.value.remediation
    assert "off" in exc.value.remediation


def test_media_invalid_type_for_enabled_errors(tmp_path: Path) -> None:
    """Invalid type for enabled field raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
media:
  enabled: "yes"
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "media.enabled" in exc.value.message


def test_media_invalid_type_for_max_file_bytes_errors(tmp_path: Path) -> None:
    """Invalid type for max_file_bytes raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
media:
  max_file_bytes: not-a-number
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "media.max_file_bytes" in exc.value.message


def test_media_invalid_type_for_trusted_hosts_errors(tmp_path: Path) -> None:
    """Invalid type for trusted_hosts (not a list) raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
media:
  trusted_hosts: example.com
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "media.trusted_hosts" in exc.value.message


def test_media_trusted_hosts_must_be_strings(tmp_path: Path) -> None:
    """All entries in trusted_hosts must be strings."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
media:
  trusted_hosts:
    - example.com
    - 123
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "media.trusted_hosts" in exc.value.message


def test_media_section_invalid_mapping_errors(tmp_path: Path) -> None:
    """media: with a non-mapping value raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
media: "invalid"
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "media" in exc.value.message
    assert "mapping" in exc.value.message
