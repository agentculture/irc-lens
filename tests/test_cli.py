"""Smoke tests for irc-lens's CLI (shape-adapt from afi cli cite python-cli)."""

from __future__ import annotations

import json

import pytest

from irc_lens import __version__
from irc_lens.cli import main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    # Rendered CLI: main() returns an exit code rather than raising SystemExit
    # (run_cli translates argparse's exits into return values); --version is
    # handled ahead of run_cli.
    assert main(["--version"]) == 0
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
    # Derived learn --json keys the tool name as "name" (was "tool").
    assert payload["name"] == "irc-lens"


def test_explain_self(capsys: pytest.CaptureFixture[str]) -> None:
    # Bare `explain` (no path) renders the root page; the derived `explain`
    # resolves registered tool paths, not a synthetic "irc-lens" path.
    assert main(["explain"]) == 0
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
    # run_cli returns the exit code (no SystemExit) but still emits the
    # structured error:/hint: pair with no traceback.
    rc = main(["nope-not-a-verb"])
    assert rc != 0
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


def test_doctor_exits_zero_when_healthy(capsys: pytest.CaptureFixture[str]) -> None:
    """`doctor` audits the derived surfaces; a healthy app exits 0."""
    rc = main(["doctor"])
    out, err = capsys.readouterr()
    assert rc == 0, f"doctor must exit 0 when healthy; stderr={err!r}"
    assert "healthy" in out.lower()
    assert "Traceback" not in err


def test_doctor_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """`doctor --json` carries a healthy bool + a checks list; stderr is clean."""
    rc = main(["doctor", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert err == "", f"doctor --json stderr must be clean; got {err!r}"
    payload = json.loads(out)
    assert payload["healthy"] is True
    assert isinstance(payload["checks"], list) and payload["checks"]
    for check in payload["checks"]:
        assert {"name", "status", "remediation"} <= set(check)


def test_overview_bad_flag_errors_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bogus flag to the intercepted `overview` verb still routes through the
    error:/hint: contract with no traceback."""
    rc = main(["overview", "--nonsense-flag"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "error:" in err and "hint:" in err
    assert "Traceback" not in err


def test_doctor_bad_flag_errors_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["doctor", "--nonsense-flag"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "error:" in err and "hint:" in err
    assert "Traceback" not in err


def test_overview_and_doctor_help_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--help` on the intercepted meta-verbs prints usage and exits 0."""
    assert main(["overview", "--help"]) == 0
    assert main(["doctor", "--help"]) == 0


def test_doctor_failed_check_carries_remediation() -> None:
    """A failed check must carry a non-empty remediation, and any failure flips
    the aggregate `healthy` bool to False."""
    from agentfront.doctor_live import Check

    from irc_lens.cli._meta import _doctor_payload

    payload = _doctor_payload(
        "irc-lens v0",
        [
            Check(name="ok-check", status="ok", remediation=""),
            Check(name="broken", status="fail", remediation="do the thing"),
        ],
    )
    assert payload["healthy"] is False
    failed = [c for c in payload["checks"] if not c["passed"]]
    assert failed, "expected at least one failed check"
    assert all(c["remediation"] for c in failed)


def test_cli_noun_overview(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cli", "overview"]) == 0
    out = capsys.readouterr().out
    assert "cli" in out.lower()


def test_cli_noun_overview_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cli", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "cli"


def test_learn_text_lists_overview(capsys: pytest.CaptureFixture[str]) -> None:
    """Derived learn TEXT surfaces the meta-verbs (in the description) and the
    registered `cli overview` tool."""
    assert main(["learn"]) == 0
    out = capsys.readouterr().out
    assert "overview" in out
    assert "cli overview" in out


def test_learn_json_lists_overview(capsys: pytest.CaptureFixture[str]) -> None:
    """Derived learn --json lists registered tools by path; `cli overview` is
    registered (the meta-verbs are generated, not registry tools)."""
    assert main(["learn", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    paths = [tuple(t["path"]) for t in payload["tools"]]
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
    rc = main(["--json", "nope-not-a-verb"])
    assert rc != 0
    err = capsys.readouterr().err
    payload = json.loads(err)  # JSON-mode errors emit to stderr per the rubric
    assert payload["code"] != 0
    assert "remediation" in payload


def test_cli_overview_extra_path_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`cli overview <extra>` is a leaf verb in the derived surface, so an extra
    positional is a parse error (error:/hint:, no traceback) — the global
    `overview` is the graceful, path-tolerant descriptive rollup."""
    rc = main(["cli", "overview", "definitely-not-a-real-subpath"])
    out, err = capsys.readouterr()
    assert rc != 0
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_explain_cli_overview_resolves(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `cli overview` noun-verb is registered, so it must be explainable."""
    assert main(["explain", "cli", "overview"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# irc-lens cli overview")


def test_explain_unknown_remediation_points_at_overview(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The derived `explain` remediation points at the discovery verb so an
    agent can find the real registered paths."""
    rc = main(["explain", "zzz-not-a-real-noun"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "hint:" in err
    assert "overview" in err  # points at `irc-lens overview` for discovery
