"""Optional `culture:` section: residents_url + overview_name."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from irc_lens.cli._commands.config_cmd import _STARTER
from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_culture_section_absent_yields_none(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
""",
        )
    )
    assert cfg.culture_residents_url is None
    assert cfg.culture_overview_name is None


def test_culture_section_valid_values_land(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
culture:
  residents_url: "http://127.0.0.1:9000/residents.json"
  overview_name: spark
""",
        )
    )
    assert cfg.culture_residents_url == "http://127.0.0.1:9000/residents.json"
    assert cfg.culture_overview_name == "spark"


def test_culture_section_partial_yields_none_for_missing(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
culture:
  overview_name: spark
""",
        )
    )
    assert cfg.culture_residents_url is None
    assert cfg.culture_overview_name == "spark"


def test_culture_section_invalid_mapping_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(
                tmp_path,
                """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
culture: "invalid"
""",
            )
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "culture" in exc.value.message
    assert "mapping" in exc.value.message


def test_culture_residents_url_not_a_string_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(
                tmp_path,
                """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
culture:
  residents_url: 123
""",
            )
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "culture.residents_url" in exc.value.message


def test_culture_residents_url_bad_scheme_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(
                tmp_path,
                """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
culture:
  residents_url: not-a-url
""",
            )
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "culture.residents_url" in exc.value.message
    assert "http" in exc.value.remediation


def test_culture_residents_url_missing_host_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(
                tmp_path,
                """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
culture:
  residents_url: "https://"
""",
            )
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "culture.residents_url" in exc.value.message


def test_culture_overview_name_not_a_string_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(
                tmp_path,
                """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
culture:
  overview_name: 123
""",
            )
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "culture.overview_name" in exc.value.message


def test_culture_overview_name_empty_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(
            _write(
                tmp_path,
                """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
culture:
  overview_name: "   "
""",
            )
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "culture.overview_name" in exc.value.message
    assert "empty" in exc.value.message.lower()


def test_starter_template_contains_culture_section() -> None:
    assert "culture:" in _STARTER
    assert "residents_url" in _STARTER
    assert "overview_name" in _STARTER


def test_starter_template_parses_as_yaml() -> None:
    parsed = yaml.safe_load(_STARTER)
    assert isinstance(parsed, dict)
    assert "culture" not in parsed


def test_starter_template_with_culture_uncommented_loads(tmp_path: Path) -> None:
    uncommented = (
        _STARTER.replace("# culture:", "culture:")
        .replace("#   residents_url:", "  residents_url:")
        .replace("#   overview_name:", "  overview_name:")
    )
    p = tmp_path / "config.yaml"
    body = uncommented.replace(
        '  residents_url: ""', '  residents_url: "http://127.0.0.1:9000/residents.json"'
    ).replace('  overview_name: ""', '  overview_name: "spark"')
    p.write_text(body)
    cfg = load_config(p)
    assert cfg.culture_residents_url == "http://127.0.0.1:9000/residents.json"
    assert cfg.culture_overview_name == "spark"
