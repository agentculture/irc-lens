"""``irc-lens mcp`` — serve the agentfront MCP surface over stdio (t8).

Registers a host command (``add_command``, mirroring ``serve``/``config``)
whose handler blocks the process serving :meth:`agentfront.app.App.mcp_server`
over stdio via :func:`agentfront.mcp_surface.serve_stdio`. That surface
exposes exactly one MCP tool, ``run``, accepting
``{"command": [...], "args": {...}}`` — dispatching against the *same*
registry (the ``app`` closed over by :func:`register_into`) the CLI and
(future) TAUI surfaces read, so the tool catalog can never drift between
surfaces.

``mcp`` is not a reserved meta-verb (``learn``/``explain``/``overview``/
``doctor``) and does not collide with any host command or top-level tool
name, so ``App.add_command`` accepts it without a ``DuplicateError``.

Failure mapping
-----------------
* The ``mcp`` extra (the ``mcp`` PyPI package) missing — surfaced as a
  friendly :class:`AfiError` (``EXIT_ENV_ERROR``: the environment is
  missing a dependency it should have) rather than a raw traceback from
  the SDK's own lazy import. Irrelevant in practice for irc-lens, which
  declares ``agentfront[mcp]`` as a hard dependency (see
  ``pyproject.toml``), but this keeps the command's own contract
  self-contained rather than relying on that external fact.
* ``KeyboardInterrupt`` (Ctrl-C, or the client disconnecting and the
  stdio pipe closing) — the supported shutdown path, matching ``serve``'s
  own convention (see ``serve.py::cmd_serve``): caught and exits 0, no
  traceback.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from irc_lens.cli._errors import EXIT_ENV_ERROR, AfiError
from irc_lens.cli._output import emit_diagnostic

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentfront.app import App

_MCP_HELP = "Serve the agentfront MCP surface (the 'run' tool) over stdio."


def _configure_mcp(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Serve irc-lens's registry as an MCP server over stdio. Exposes exactly "
        "one MCP tool, 'run', accepting {'command': [...], 'args': {...}} — the "
        "same command catalog 'irc-lens learn'/'irc-lens explain' describe. "
        "Intended to be launched by an MCP client (e.g. an agent harness), not "
        "run interactively; it blocks reading/writing stdio until the client "
        "disconnects or the process receives Ctrl-C."
    )
    p.epilog = (
        "examples:\n"
        "  irc-lens mcp\n"
        "      Serve the MCP 'run' tool over stdio. A connecting client speaks\n"
        "      the MCP stdio protocol (initialize, tools/list, tools/call).\n"
    )
    p.formatter_class = argparse.RawDescriptionHelpFormatter


def register_into(app: "App") -> None:
    """Register ``mcp`` as a host command serving *app* over MCP stdio."""

    def cmd_mcp(_args: argparse.Namespace) -> int:
        try:
            from agentfront.mcp_surface import serve_stdio
        except ModuleNotFoundError as exc:
            # Only translate the *mcp* import failure; anything else naming a
            # different missing module is a real bug and must not be masked
            # (mirrors the same guard agentfront's own App.mcp_server() uses).
            if (getattr(exc, "name", "") or "").split(".", 1)[0] != "mcp":
                raise
            raise AfiError(
                code=EXIT_ENV_ERROR,
                message=(
                    "the MCP surface needs the optional 'mcp' dependency, "
                    "which is not installed"
                ),
                remediation=(
                    "install it with: uv add 'agentfront[mcp]'  "
                    "(or: pip install 'agentfront[mcp]')"
                ),
            ) from exc

        try:
            serve_stdio(app)
        except KeyboardInterrupt:
            # Ctrl-C / client disconnect is the supported shutdown path,
            # matching `serve`'s own convention — exit 0, no traceback.
            emit_diagnostic("irc-lens mcp shutdown")
        return 0

    app.add_command(
        "mcp",
        handler=cmd_mcp,
        help=_MCP_HELP,
        configure=_configure_mcp,
    )
