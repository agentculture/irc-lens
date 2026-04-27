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


def test_learn_text_lists_overview(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: learn TEXT must mention every registered global verb."""
    assert main(["learn"]) == 0
    out = capsys.readouterr().out
    assert "irc-lens overview" in out
    assert "irc-lens cli overview" in out


def test_learn_json_lists_overview(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: learn JSON commands must include overview + cli overview."""
    assert main(["learn", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    paths = [tuple(c["path"]) for c in payload["commands"]]
    assert ("overview",) in paths
    assert ("cli", "overview") in paths


def test_cli_noun_no_subcommand_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`irc-lens cli` (no verb) must print help and return 0, not AttributeError."""
    rc = main(["cli"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "overview" in out  # cli noun's help mentions its only verb
    assert "Traceback" not in err
    assert "unexpected" not in err.lower()


def test_argparse_error_in_json_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parse-time errors must respect --json so machine consumers can parse."""
    with pytest.raises(SystemExit):
        main(["--json", "nope-not-a-verb"])
    err = capsys.readouterr().err
    payload = json.loads(err)  # JSON-mode errors emit to stderr per the rubric
    assert payload["code"] != 0
    assert "remediation" in payload


def test_cli_overview_extra_path_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`cli overview <bogus>` has no sub-subjects, so it must warn (still exit 0)."""
    rc = main(["cli", "overview", "definitely-not-a-real-subpath"])
    out, err = capsys.readouterr()
    assert rc == 0, f"cli overview must exit 0 on unknown sub-path; stderr={err!r}"
    assert "warning" in out.lower()


def test_explain_cli_overview_resolves(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `cli overview` noun-verb is registered, so it must be explainable."""
    assert main(["explain", "cli", "overview"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# irc-lens cli overview")


def test_explain_unknown_remediation_lists_known_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The remediation must enumerate real catalog paths, not point at the root page."""
    rc = main(["explain", "zzz-not-a-real-noun"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "known paths:" in err
    assert "irc-lens" in err  # at least the root noun should be listed
