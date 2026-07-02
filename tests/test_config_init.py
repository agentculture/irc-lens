"""`irc-lens config init` writes a starter dev-mode config."""
from __future__ import annotations

from pathlib import Path

from irc_lens.cli import main


def test_config_init_writes_default_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rc = main(["config", "init"])
    assert rc == 0
    target = tmp_path / "irc-lens" / "config.yaml"
    assert target.exists()
    body = target.read_text()
    assert "auth:" in body
    assert "mode: dev" in body
    assert "server:" in body


def test_config_init_with_explicit_path(tmp_path: Path) -> None:
    target = tmp_path / "custom.yaml"
    rc = main(["config", "init", "--path", str(target)])
    assert rc == 0
    assert target.exists()


def test_config_init_refuses_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "exists.yaml"
    target.write_text("# pre-existing\n")
    rc = main(["config", "init", "--path", str(target)])
    assert rc == 1
    assert target.read_text() == "# pre-existing\n"


def test_config_init_force_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "exists.yaml"
    target.write_text("# stale\n")
    rc = main(["config", "init", "--path", str(target), "--force"])
    assert rc == 0
    assert "auth:" in target.read_text()


def test_config_overview_works(tmp_path: Path) -> None:
    rc = main(["config", "overview"])
    assert rc == 0


def test_config_init_starter_includes_media_section(tmp_path: Path, monkeypatch) -> None:
    """The starter template includes the media section."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rc = main(["config", "init"])
    assert rc == 0
    target = tmp_path / "irc-lens" / "config.yaml"
    body = target.read_text()
    assert "media:" in body


def test_config_init_starter_round_trips(tmp_path: Path, monkeypatch) -> None:
    """The starter template can be loaded by load_config."""
    from irc_lens.config import load_config
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rc = main(["config", "init"])
    assert rc == 0
    target = tmp_path / "irc-lens" / "config.yaml"
    cfg = load_config(target)
    assert cfg.media_enabled is True
    assert cfg.media_remote_embeds == "click"
