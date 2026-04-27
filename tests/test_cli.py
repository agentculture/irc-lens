"""Smoke tests for irc-lens's CLI (shape-adapt from afi cli cite python-cli)."""

from __future__ import annotations

import json

import pytest

from irc_lens import __version__
from irc_lens.cli import main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_learn_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["learn"]) == 0
    out = capsys.readouterr().out
    assert len(out) >= 200
    for marker in ["purpose", "commands", "exit", "--json", "explain"]:
        assert marker.lower() in out.lower()


def test_learn_json_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["learn", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "irc-lens"


def test_explain_self(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "irc-lens"]) == 0
    assert capsys.readouterr().out.startswith("#")


def test_explain_unknown_path_fails_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["explain", "zzz-not-a-real-noun"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err


def test_unknown_verb_fails_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["nope-not-a-verb"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_overview_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["overview"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# overview:")


def test_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "all"
    assert isinstance(payload["sections"], list) and payload["sections"]


def test_overview_graceful_on_bad_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["overview", "definitely-not-a-real-subject"])
    out, err = capsys.readouterr()
    assert rc == 0, f"overview must exit 0 on unknown path; stderr={err!r}"
    assert "warning" in out.lower()


def test_cli_noun_overview(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cli", "overview"]) == 0
    out = capsys.readouterr().out
    assert "cli" in out.lower()


def test_cli_noun_overview_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cli", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "cli"
