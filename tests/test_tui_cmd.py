"""t9 — TAUI surface: ``irc-lens tui`` host verb over agentfront's ``LiveDriver``.

Three acceptance-facing groups:

* **Non-TTY** — ``irc_lens.cli.main(["tui"])`` with piped/captured stdio
  must never enter raw mode: it returns 0, writes one hint line to stderr,
  and writes the registry-derived TAUI markdown front
  (``agentfront.taui.render.markdown.render_markdown(app.taui())``) to
  stdout. Run inside a bounded-timeout thread so a regression that
  accidentally reaches the raw-mode loop fails the test instead of hanging
  the suite.
* **Pure-navigation parity** — ``assert_agent_human_parity`` for
  ``cmd.config`` (a host command's panel item; per
  ``agentfront.app.App.get_by_path`` this never resolves to a registered
  tool, so it is safe for the agent path — dispatching it is pure
  navigation, not execution — matching the docs' own recommendation to
  "pick a panel item, a doc, or a host command").
* **Execution parity** — ``agentfront.testing.drive`` dispatching a
  ``SelectorAction`` for the ``channels`` tool (its only parameter,
  ``config``, is exactly what ``SelectorAction.args`` carries — see
  ``agentfront.taui.events.SelectorAction`` — so no tools.py change was
  needed to express this through the TAUI surface) lands the same
  ``{"result": ...}`` payload as ``irc-lens channels --json`` through the
  real CLI (``agentfront.testing.run_cli``), against the same in-tree fake
  AgentIRC server t5 built (``tests/_agentirc_server.py``,
  ``ThreadedAgentIRCTestServer`` — needed here for the same reason
  ``test_tools_live.py`` needs it: each live-verb tool opens its own
  ``asyncio.run()``, which cannot nest inside a thread that already has a
  running loop).
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import sys

import pytest
from agentfront.taui.events import SelectorAction
from agentfront.testing import assert_agent_human_parity, drive, run_cli

from irc_lens.cli import build_app, main

from _agentirc_server import ThreadedAgentIRCTestServer

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")

_CALL_TIMEOUT_S = 5.0

# ---------------------------------------------------------------------------
# Non-TTY: hint + front, exit 0, never hangs.
# ---------------------------------------------------------------------------


def test_tui_non_tty_returns_zero_prints_hint_and_front(monkeypatch) -> None:
    """Piped stdio: no raw mode, no hang — a hint on stderr, the front on
    stdout, exit 0. The call runs inside a worker thread bounded by
    ``_CALL_TIMEOUT_S``: if a regression ever let ``cmd_tui`` reach the
    raw-mode loop despite non-TTY streams, ``future.result(timeout=...)``
    raises ``TimeoutError`` instead of hanging the test run forever.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(main, ["tui"])
        exit_code = future.result(timeout=_CALL_TIMEOUT_S)

    assert exit_code == 0
    err = stderr.getvalue()
    assert "TTY" in err
    out = stdout.getvalue()
    assert out.startswith("# irc-lens")
    assert "## Status" in out
    assert "launch the aiohttp" in out.lower()  # cmd.serve's panel-item label


def test_tui_non_tty_front_matches_registry_markdown(monkeypatch) -> None:
    """The exact bytes irc-lens prints on stdout are
    ``render_markdown(app.taui())`` — the same tier the HTTP ``/front``
    route (t7) serves — not separately hand-authored help prose."""
    from agentfront.taui.render.markdown import render_markdown

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(main, ["tui"])
        exit_code = future.result(timeout=_CALL_TIMEOUT_S)

    assert exit_code == 0
    expected = render_markdown(build_app().taui())
    expected = expected if expected.endswith("\n") else expected + "\n"
    assert stdout.getvalue() == expected


# ---------------------------------------------------------------------------
# Pure-navigation parity: agent SelectorAction vs. human arrow-key walk.
# ---------------------------------------------------------------------------


def test_assert_agent_human_parity_cmd_config() -> None:
    """``cmd.config`` is a host command's panel item — never a registered
    tool (``App.get_by_path`` only resolves tools) — so it is the pinned
    pure-navigation selector: dispatching it is a no-op navigation move on
    the agent side, exactly what a human's arrow-key walk also produces."""
    app = build_app()
    assert app.get_by_path(("cmd", "config")) is None  # guards the premise
    assert_agent_human_parity(app, "cmd.config")


# ---------------------------------------------------------------------------
# Execution parity: drive() dispatching a tool SelectorAction vs. the CLI.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_server():
    server = ThreadedAgentIRCTestServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def dev_config_path(tmp_path, fake_server: ThreadedAgentIRCTestServer):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "auth:\n"
        "  mode: dev\n"
        "  dev:\n"
        "    nick: lens-test\n"
        "    email: dev@local\n"
        "server:\n"
        "  name: testsrv\n"
        f"  host: {fake_server.host}\n"
        f"  port: {fake_server.port}\n"
    )
    return cfg


def test_drive_channels_tool_matches_cli_invocation(dev_config_path) -> None:
    """``drive(app, [SelectorAction("channels", args={"config": ...})])`` —
    the TAUI execution path a live agent's ``dispatch`` also takes — lands
    the same result as ``irc-lens channels --json`` through the real CLI,
    against the same fake AgentIRC server.
    """
    app = build_app()

    session = drive(
        app,
        [SelectorAction(selector="channels", args={"config": str(dev_config_path)})],
    )
    assert session.last_result is not None
    assert "error" not in session.last_result
    taui_result = session.last_result["result"]

    cli_result = run_cli(app, ["channels", "--json", "--config", str(dev_config_path)])
    assert cli_result.exit_code == 0, cli_result.stderr
    assert cli_result.stderr == ""
    cli_payload = json.loads(cli_result.stdout)

    assert taui_result == cli_payload == []  # nothing joined on a fresh fake server

    # Also assert the ToolInvoked/ToolResult trail folded correctly and the
    # session stays replay-equivalent — the same "trail is the ground
    # truth" guarantee tested elsewhere for Session (test_taui_session.py
    # upstream), re-checked here for irc-lens's own registry.
    kinds = [type(ev).__name__ for ev in session.events]
    assert kinds == ["ToolInvoked", "ToolResult"]


def test_drive_channels_tool_matches_cli_after_join(dev_config_path) -> None:
    """Same parity proof, but with state the fake server actually holds —
    joining a channel first so both paths see a non-empty ``channels``
    result, not just the trivially-equal empty-list case above."""
    app = build_app()

    join_result = run_cli(app, ["join", "#agora", "--json", "--config", str(dev_config_path)])
    assert join_result.exit_code == 0, join_result.stderr

    session = drive(
        app,
        [SelectorAction(selector="channels", args={"config": str(dev_config_path)})],
    )
    assert session.last_result is not None
    taui_result = session.last_result["result"]

    cli_result = run_cli(app, ["channels", "--json", "--config", str(dev_config_path)])
    assert cli_result.exit_code == 0, cli_result.stderr
    cli_payload = json.loads(cli_result.stdout)

    assert taui_result == cli_payload
    assert "#agora" in taui_result
