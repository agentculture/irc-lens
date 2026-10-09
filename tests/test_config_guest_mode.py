"""Optional `guest_mode:` section: switch, sandbox, store, legal URL, mail, rates."""

from __future__ import annotations

from pathlib import Path

import pytest

from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def _base(guest_block: str = "") -> str:
    return f"""
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
{guest_block}"""


def test_guest_mode_absent_uses_defaults(tmp_path: Path) -> None:
    """Absent guest_mode section yields all defaults (switch off)."""
    cfg = load_config(_write(tmp_path, _base()))
    assert cfg.guest_enabled is False
    assert cfg.guest_sandbox_name == "sbx"
    assert cfg.guest_sandbox_host == "127.0.0.1"
    assert cfg.guest_sandbox_port == 6668
    # store_path defaults to XDG_DATA_HOME/irc-lens/guests.db or ~/.local/share/irc-lens/guests.db
    assert cfg.guest_store_path.endswith("irc-lens/guests.db")
    assert cfg.guest_legal_version_url == "https://culture.dev/legal/version.json"
    assert cfg.guest_mail_provider == "none"
    assert cfg.guest_mail_from == ""
    assert cfg.guest_mail_api_key_env == "IRC_LENS_MAIL_API_KEY"
    assert cfg.guest_rate_entry_per_min == 10
    assert cfg.guest_rate_messages_per_min == 20
    assert cfg.guest_rate_password_attempts_per_15min == 5


def test_guest_mode_with_all_fields(tmp_path: Path) -> None:
    """Explicit guest_mode values are loaded."""
    cfg = load_config(
        _write(
            tmp_path,
            _base("""
guest_mode:
  enabled: true
  sandbox:
    name: sbx
    host: 127.0.0.1
    port: 6668
  store_path: /tmp/guests.db
  legal_version_url: https://culture.dev/legal/version.json
  mail:
    provider: none
    from: ""
    api_key_env: IRC_LENS_MAIL_API_KEY
  rate_limits:
    entry_per_min: 10
    messages_per_min: 20
    password_attempts_per_15min: 5
"""),
        )
    )
    assert cfg.guest_enabled is True
    assert cfg.guest_sandbox_name == "sbx"
    assert cfg.guest_sandbox_host == "127.0.0.1"
    assert cfg.guest_sandbox_port == 6668
    assert cfg.guest_store_path == "/tmp/guests.db"
    assert cfg.guest_legal_version_url == "https://culture.dev/legal/version.json"
    assert cfg.guest_mail_provider == "none"
    assert cfg.guest_mail_from == ""
    assert cfg.guest_mail_api_key_env == "IRC_LENS_MAIL_API_KEY"
    assert cfg.guest_rate_entry_per_min == 10
    assert cfg.guest_rate_messages_per_min == 20
    assert cfg.guest_rate_password_attempts_per_15min == 5


def test_guest_mode_partial_merges_defaults(tmp_path: Path) -> None:
    """Partial guest_mode section merges with defaults for missing keys."""
    cfg = load_config(
        _write(
            tmp_path,
            _base("""
guest_mode:
  enabled: true
  rate_limits:
    entry_per_min: 3
"""),
        )
    )
    assert cfg.guest_enabled is True
    assert cfg.guest_rate_entry_per_min == 3
    assert cfg.guest_rate_messages_per_min == 20  # default
    assert cfg.guest_rate_password_attempts_per_15min == 5  # default
    assert cfg.guest_sandbox_name == "sbx"  # default
    assert cfg.guest_sandbox_port == 6668  # default
    assert cfg.guest_store_path.endswith("irc-lens/guests.db")  # default
    assert cfg.guest_mail_provider == "none"  # default


def test_guest_mode_invalid_mapping_errors(tmp_path: Path) -> None:
    """guest_mode: with a non-mapping value raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, _base('guest_mode: "invalid"')))
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode" in exc.value.message
    assert "mapping" in exc.value.message


@pytest.mark.parametrize(
    "block,where",
    [
        ("guest_mode:\n  enabledd: true\n", "guest_mode"),
        (
            "guest_mode:\n  sandbox:\n    name: sbx\n    hostt: x\n",
            "guest_mode.sandbox",
        ),
        ("guest_mode:\n  mail:\n    provider: none\n    frm: x\n", "guest_mode.mail"),
        (
            "guest_mode:\n  rate_limits:\n    entry_per_minute: 1\n",
            "guest_mode.rate_limits",
        ),
    ],
)
def test_guest_mode_unknown_keys_rejected(
    tmp_path: Path, block: str, where: str
) -> None:
    """Unknown keys inside guest_mode (and its subsections) raise — typo guard."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, _base(block)))
    assert exc.value.code == EXIT_USER_ERROR
    assert "unknown" in exc.value.message.lower()
    assert where in exc.value.message


def test_guest_mode_enabled_bad_type_errors(tmp_path: Path) -> None:
    """enabled must be a boolean."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, _base('guest_mode:\n  enabled: "yes"\n')))
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.enabled" in exc.value.message


def test_guest_mode_sandbox_port_bad_value_errors(tmp_path: Path) -> None:
    """sandbox.port goes through the shared port coercion."""
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(tmp_path, _base("guest_mode:\n  sandbox:\n    port: nope\n"))
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.sandbox.port" in exc.value.message
    assert "between 1 and 65535" in exc.value.message


def test_guest_mode_sandbox_non_mapping_errors(tmp_path: Path) -> None:
    """sandbox: with a non-mapping value raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, _base("guest_mode:\n  sandbox: []\n")))
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.sandbox" in exc.value.message
    assert "mapping" in exc.value.message


def test_guest_mode_store_path_not_a_string_errors(tmp_path: Path) -> None:
    """store_path must be a string."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, _base("guest_mode:\n  store_path: 42\n")))
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.store_path" in exc.value.message


def test_guest_mode_legal_version_url_bad_scheme_errors(tmp_path: Path) -> None:
    """legal_version_url must be an http(s) URL when set."""
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(tmp_path, _base("guest_mode:\n  legal_version_url: not-a-url\n"))
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.legal_version_url" in exc.value.message
    assert "http" in exc.value.remediation


def test_guest_mode_legal_version_url_missing_host_errors(tmp_path: Path) -> None:
    """A bare scheme (no netloc) is rejected too."""
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(tmp_path, _base('guest_mode:\n  legal_version_url: "https://"\n'))
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.legal_version_url" in exc.value.message


def test_guest_mode_mail_non_mapping_errors(tmp_path: Path) -> None:
    """mail: with a non-mapping value raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, _base('guest_mode:\n  mail: "x"\n')))
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.mail" in exc.value.message
    assert "mapping" in exc.value.message


def test_guest_mode_mail_from_not_a_string_errors(tmp_path: Path) -> None:
    """mail.from must be a string."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, _base("guest_mode:\n  mail:\n    from: 42\n")))
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.mail.from" in exc.value.message


def test_guest_mode_rate_limits_bad_int_errors(tmp_path: Path) -> None:
    """rate_limits entries must be integers."""
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(
                tmp_path,
                _base(
                    "guest_mode:\n  rate_limits:\n    messages_per_min: not-a-number\n"
                ),
            )
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.rate_limits.messages_per_min" in exc.value.message


def test_guest_mode_rate_limits_non_mapping_errors(tmp_path: Path) -> None:
    """rate_limits: with a non-mapping value raises error."""
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, _base("guest_mode:\n  rate_limits: []\n")))
    assert exc.value.code == EXIT_USER_ERROR
    assert "guest_mode.rate_limits" in exc.value.message
    assert "mapping" in exc.value.message
