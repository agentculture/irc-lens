"""t8 — MCP surface: the ``mcp`` host verb + ``call_mcp`` catalog coverage.

Three layers of coverage, per the plan's acceptance criteria:

1. **The host verb itself** (unit-level): ``mcp`` is registered via
   ``app.add_command``, does not collide with a reserved meta-verb or an
   existing command/tool name, and its handler maps ``KeyboardInterrupt``
   to a clean exit (mirrors ``serve``'s convention) and a missing ``mcp``
   extra to a structured :class:`AfiError` rather than a raw traceback.
2. **The stdio e2e round trip**: spawn ``uv run irc-lens mcp`` as a real
   subprocess and speak the MCP protocol against it (initialize,
   tools/list, tools/call on the single ``run`` tool) via the ``mcp``
   client SDK — the ONE place in this file that imports ``mcp``, per the
   plan's explicit carve-out (the no-``mcp``-import constraint applies
   only to the ``call_mcp`` catalog test below).
3. **The in-process catalog test**: ``agentfront.testing.call_mcp`` —
   which never imports ``mcp`` — dispatches every tool ``build_app()``
   registers and asserts each payload is exactly ``{"result": ...}`` XOR
   ``{"error": {code, message, remediation}}``. A missing config makes
   every live AgentIRC verb (t5) fail deterministically into the error
   triple; that satisfies "result-or-error-triple" for those, while
   ``cli overview`` (needing no config/network) exercises the success
   side. ``channels`` additionally gets a real happy-path round trip
   against the in-tree fake AgentIRC server (t5's
   ``tests/_agentirc_server.py``), landing an actual ``{"result": [...]}``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import pytest
from agentfront.testing import call_mcp

from irc_lens.cli import build_app
from irc_lens.cli._errors import EXIT_ENV_ERROR, AfiError

from _agentirc_server import ThreadedAgentIRCTestServer

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. The `mcp` host verb — registration + handler behaviour
# ---------------------------------------------------------------------------


def _mcp_handler():
    app = build_app()
    cmd = next(c for c in app.list_commands() if c.name == "mcp")
    return cmd.handler


def test_mcp_registered_as_host_command_no_collision() -> None:
    """``mcp`` is a host command, distinct from every reserved meta-verb,
    other host command, and top-level tool name (a collision would have
    raised ``DuplicateError`` inside ``build_app()`` itself)."""
    app = build_app()
    command_names = {c.name for c in app.list_commands()}
    assert "mcp" in command_names

    reserved = {"learn", "explain", "overview", "doctor"}
    tool_names = {t.name for t in app.list_tools() if not t.group}
    assert not (tool_names & {"mcp"})
    assert not (reserved & {"mcp"})


def test_cmd_mcp_keyboard_interrupt_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C / client-disconnect during ``serve_stdio`` is the supported
    shutdown path (mirrors ``serve.py::cmd_serve``): exit 0, no traceback."""
    import agentfront.mcp_surface as mcp_surface

    def _boom(_app: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_surface, "serve_stdio", _boom)
    handler = _mcp_handler()
    assert handler(argparse.Namespace()) == 0


def test_cmd_mcp_missing_mcp_extra_raises_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the optional ``mcp`` extra not being installed (evicting
    it from ``sys.modules`` the same way agentfront's own
    ``test_mcp_server_without_mcp_extra_raises_friendly_error`` does) and
    asserts the handler raises a structured ``AfiError`` — never a bare
    ``ModuleNotFoundError`` traceback — naming the extra to install."""
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.delitem(sys.modules, "agentfront.mcp_surface", raising=False)

    handler = _mcp_handler()
    with pytest.raises(AfiError) as excinfo:
        handler(argparse.Namespace())
    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "mcp" in excinfo.value.message
    assert "agentfront[mcp]" in excinfo.value.remediation


# ---------------------------------------------------------------------------
# 2. Stdio e2e round trip — real subprocess, real MCP protocol.
#
# This is the ONLY test in this file that imports `mcp`; the import is
# function-local so the module stays importable (and collectible) even in
# an environment without the optional `mcp` extra.
# ---------------------------------------------------------------------------


async def test_mcp_stdio_run_round_trip() -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def _round_trip() -> None:
        params = StdioServerParameters(
            command="uv",
            args=["run", "irc-lens", "mcp"],
            cwd=str(_REPO_ROOT),
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                assert init_result.serverInfo.name == "irc-lens"

                tools = await session.list_tools()
                assert [t.name for t in tools.tools] == ["run"]

                # `cli overview` needs no IRC server / config — ideal for
                # an e2e round trip with no external state to stand up.
                result = await session.call_tool(
                    "run", {"command": ["cli", "overview"], "args": {}}
                )
                assert result.isError is False
                payload = result.structuredContent
                assert payload is not None and "result" in payload
                assert payload["result"]["subject"] == "cli"
                assert "sections" in payload["result"]

    # A sane timeout so a protocol/handshake bug fails fast instead of
    # wedging the suite.
    await asyncio.wait_for(_round_trip(), timeout=30)


# ---------------------------------------------------------------------------
# 3. `call_mcp` over the full catalog — no `mcp` import anywhere below.
# ---------------------------------------------------------------------------

_CATALOG_APP = build_app()
_CATALOG = _CATALOG_APP.list_tools()

_PLACEHOLDER_BY_JSON_TYPE = {
    "string": "x",
    "integer": 1,
    "number": 1,
    "boolean": True,
    "array": [],
    "object": {},
}


def _minimal_args(entry) -> dict:
    """Build the smallest args dict that satisfies *entry*'s required
    properties — one placeholder value per JSON-schema type, derived from
    the tool's own registered ``input_schema`` rather than a hardcoded
    per-tool-name table, so a new tool automatically gets covered too."""
    props = entry.input_schema.get("properties", {})
    required = entry.input_schema.get("required", [])
    return {
        name: _PLACEHOLDER_BY_JSON_TYPE.get(props.get(name, {}).get("type"), "x")
        for name in required
    }


def _tool_id(entry) -> str:
    return "-".join((*entry.group, entry.name))


@pytest.fixture
def no_config_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point config resolution at a directory with no config file, so
    every t5 live-verb tool fails deterministically at the same
    "missing config" step (before any channel/argument validation) —
    the acceptance criteria's explicitly-sanctioned error-triple outcome
    for "live verbs invoked without a config"."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config-dir"))


@pytest.mark.usefixtures("no_config_env")
@pytest.mark.parametrize("entry", _CATALOG, ids=_tool_id)
def test_call_mcp_every_registered_tool_returns_result_or_error_triple(entry) -> None:
    command = [*entry.group, entry.name]
    payload = call_mcp(_CATALOG_APP, command, _minimal_args(entry))

    assert set(payload) in ({"result"}, {"error"})
    if "error" in payload:
        err = payload["error"]
        assert set(err) == {"code", "message", "remediation"}
        assert isinstance(err["code"], int)
        assert isinstance(err["message"], str) and err["message"]
        assert isinstance(err["remediation"], str) and err["remediation"]


# ---------------------------------------------------------------------------
# `channels` also gets a real happy-path round trip against the fake
# AgentIRC server, landing an actual `{"result": [...]}` — not just the
# error triple every other live verb settles for above.
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
        "    nick: lens-mcp\n"
        "    email: dev@local\n"
        "server:\n"
        "  name: testsrv\n"
        f"  host: {fake_server.host}\n"
        f"  port: {fake_server.port}\n"
    )
    return cfg


def test_call_mcp_channels_happy_path_round_trip(dev_config_path) -> None:
    app = build_app()
    payload = call_mcp(app, ["channels"], {"config": str(dev_config_path)})
    assert payload == {"result": []}  # nothing joined yet on a fresh fake server
