"""Task t10 — in-process drift gates replace the external ``afi`` verifier.

``tests/test_afi_verify.py`` shelled out to a sibling-checkout binary
(``afi cli verify``) that may not be on ``PATH``, so its one test skipped
whenever ``afi`` was absent — a gate that can silently go dark. t3 rebuilt
the CLI as a single ``agentfront.app.App`` registry (``irc_lens.cli.build_app``)
rendered into every surface, and ``agentfront.testing`` ships the exact
same proof `agentfront` runs on itself, in-process, with no subprocess and
no external binary. This file is that gate's consumer-side home: it
deletes the opt-in/skip axis entirely (every test here runs in the default
pytest selection, unconditionally) and adds the redundant CLI-surface smoke
coverage the harness is meant to protect.

**Why two different call paths for the meta-verbs (see t3):** t3 found that
agentfront 0.20.0's stock ``overview``/``doctor`` meta-verbs can't yet
produce the richer ``{subject, sections}`` / ``{healthy, checks}`` JSON
shapes irc-lens's rubric requires, and agentfront reserves both names on the
``App`` so they can't be re-registered as ordinary tools. ``irc_lens.cli.main``
therefore intercepts ``overview`` and ``doctor`` and derives their output from
the same App registry via :mod:`irc_lens.cli._meta`, while every other verb
(including the ``learn``/``explain`` meta-verbs) is rendered straight through
``agentfront.cli_surface.run_cli``. So: ``learn``/``explain`` are smoked here
via ``agentfront.testing.run_cli(app, [...])`` — the actual CI-facing harness
path — and ``overview``/``doctor`` are smoked via ``irc_lens.cli.main([...])``
with ``capsys``, since ``run_cli`` never sees those two verbs in the real
entry point. ``tests/test_cli.py`` already covers ``overview``/``doctor``
through ``main()``; the coverage here is deliberately redundant with (not a
replacement for) that file, because the point of this file is proving the
``agentfront.testing`` harness — the thing CI actually gates on — sees the
same behaviour.
"""

from __future__ import annotations

import json

import pytest

from agentfront import App
from agentfront.testing import assert_surfaces_agree, run_cli
from irc_lens.cli import build_app, main

# ---------------------------------------------------------------------------
# The one-liner: every surface agrees with the registry. Zero skip
# conditions, no subprocess, no external binary — unlike the deleted
# test_afi_verify.py, this cannot silently go dark.
# ---------------------------------------------------------------------------


def test_surfaces_agree() -> None:
    """``assert_surfaces_agree(build_app())`` — irc-lens's CLI/HTTP/MCP/TAUI
    surfaces all enumerate the same docs/tools as the registry, and the HTTP
    ``/front`` markdown agrees with the TAUI markdown tier. Raises
    ``AssertionError`` naming the disagreeing pair on drift; passes clean."""
    assert_surfaces_agree(build_app())


# ---------------------------------------------------------------------------
# The gate provably bites: a deliberate one-surface drift on a scratch App
# must raise AssertionError naming the disagreeing pair.
#
# irc-lens's own App registry has no public mutator that can desynchronize a
# surface from the registry (see agentfront's own
# tests/integration/test_anti_drift.py::test_no_public_side_channel_to_inject_into_a_surface
# — add_doc/add_docs_dir/add_command/@app.tool are the only mutators, and
# every surface reads the registry live). So provoking real drift through
# the public API is impossible by design; that impossibility is the feature,
# not a gap in this test. To prove the gate still *bites* when a surface
# genuinely disagrees, we monkeypatch the internal per-surface probe
# (agentfront.serve._mcp_command_paths) the same way agentfront's own
# test_front_agreement.py breaks the HTTP /front route to test
# http_front_agrees — patching the seam between "surface queries the
# registry" and "surface reports what it found", not the registry itself.
# ---------------------------------------------------------------------------


def _scratch_app() -> App:
    app = App(name="drift-scratch", version="1.0")

    @app.tool
    def widget(x: int) -> int:
        """A single scratch tool."""
        return x

    return app


def test_drift_gate_bites_on_deliberate_surface_disagreement(monkeypatch) -> None:
    app = _scratch_app()
    # Baseline: an honest scratch app agrees with itself.
    assert_surfaces_agree(app)

    import agentfront.serve as serve

    # Simulate the MCP surface silently dropping every tool it would
    # otherwise report — a one-surface drift with the registry untouched.
    monkeypatch.setattr(serve, "_mcp_command_paths", lambda _app: set())

    with pytest.raises(AssertionError) as excinfo:
        assert_surfaces_agree(app)

    message = str(excinfo.value)
    # The gate must name the disagreeing pair (mcp_tools vs registry_tools)
    # and the actual missing entry, not just "surfaces disagree".
    assert "mcp_tools" in message
    assert "registry_tools" in message
    assert "widget" in message


# ---------------------------------------------------------------------------
# run_cli smoke tests — learn / explain, through the real CI-facing harness.
# ---------------------------------------------------------------------------


def test_run_cli_learn_meets_rubric() -> None:
    result = run_cli(build_app(), ["learn"])
    assert result.exit_code == 0
    assert result.stderr == ""
    assert len(result.stdout) >= 200
    lowered = result.stdout.lower()
    for marker in ["purpose", "commands", "exit", "--json", "explain"]:
        assert marker in lowered


def test_run_cli_learn_json_parseable_clean_stderr() -> None:
    result = run_cli(build_app(), ["learn", "--json"])
    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["name"] == "irc-lens"


def test_run_cli_explain_exits_zero() -> None:
    result = run_cli(build_app(), ["explain"])
    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.startswith("#")


def test_run_cli_bogus_verb_exits_nonzero_with_hint_no_traceback() -> None:
    result = run_cli(build_app(), ["nope-not-a-verb"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "error:" in result.stderr
    assert "hint:" in result.stderr
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# main()-with-capsys smoke tests — overview / doctor, the two meta-verbs t3
# routes outside run_cli (see the module docstring for why).
# ---------------------------------------------------------------------------


def test_main_overview_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["overview"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("# overview:")
    assert err == ""


def test_main_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["overview", "--json"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["subject"] == "all"
    assert isinstance(payload["sections"], list) and payload["sections"]


def test_main_doctor_exits_zero_when_healthy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["doctor"]) == 0
    out, err = capsys.readouterr()
    assert "healthy" in out.lower()
    assert "Traceback" not in err


def test_main_doctor_json_clean_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--json"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["healthy"] is True
    assert isinstance(payload["checks"], list) and payload["checks"]
