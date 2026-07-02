"""irc-lens CLI — rendered from one agentfront ``App`` registry.

The CLI is *assembled*, not hand-maintained: :func:`build_app` constructs a
single :class:`agentfront.app.App`, registers a few doc pages, and calls each
command module's ``register_into(app)`` hook (the colleague blueprint — "import,
don't duplicate"). :func:`main` then dispatches an argv against that App via
``agentfront.cli_surface.run_cli``, which generates the agent-first surface for
free: ``learn`` / ``explain`` meta-verbs, per-verb ``--json``, structured
``error:`` / ``hint:`` diagnostics on stderr, and no traceback leak. The same
App also backs the (future) MCP and HTTP surfaces, so they cannot drift.

Two meta-verbs are the exception. ``overview`` must emit ``{subject, sections}``
and ``doctor`` must emit ``{healthy, checks}`` under ``--json`` — richer shapes
than agentfront 0.20.0's stock meta-verbs produce, and their names are reserved
so they cannot be re-registered on the App. :func:`main` therefore routes those
two verbs to :mod:`irc_lens.cli._meta`, which derives its output from the *same*
App registry, and delegates everything else to ``run_cli``.

The ``[project.scripts]`` entry (``irc-lens = "irc_lens.cli:main"``) is
unchanged; only the internals moved from bespoke argparse scaffolding to the
rendered path.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from irc_lens import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    import argparse

    from agentfront.app import App

_DESCRIPTION = (
    "irc-lens — reactive web console for AgentIRC. Purpose: launch an "
    "aiohttp + HTMX + SSE web app over a plain TCP AgentIRC connection so a "
    "browser-automation agent can administer any AgentIRC server without a "
    "human in the loop — a pure client, no agent loop, one process per browser "
    "tab. Commands: `serve` launches the console and `config` manages the "
    "config file; the meta-verbs learn, explain, overview, and doctor are "
    "generated from this registry. Exit codes: 0 success, 1 user-input error, "
    "2 environment/setup error, 3+ reserved. Every command supports --json "
    "(results to stdout, errors to stderr, never mixed); run "
    "'irc-lens explain <path>' for per-command docs."
)


def _iter_command_modules() -> tuple[object, ...]:
    """Yield each command module exposing a ``register_into(app)`` hook.

    The list is **explicit** (no dynamic import). ``serve`` and ``config`` are
    host commands; ``cli`` is the CLI-introspection noun. The ``overview`` and
    ``doctor`` meta-verbs are *not* here — agentfront reserves their names, so
    they are routed by :func:`main` to :mod:`irc_lens.cli._meta` instead.
    """
    from irc_lens.cli._commands import cli_noun, config_cmd, serve

    return (serve, config_cmd, cli_noun)


def build_app() -> "App":
    """Assemble the irc-lens :class:`agentfront.app.App` from the registry.

    Registers the purpose-authored doc pages (see :mod:`irc_lens.front_docs`),
    then invokes every command module's ``register_into(app)`` hook.
    Side-effect-free beyond constructing the App.
    """
    from agentfront.app import App

    from irc_lens.front_docs import register_docs

    app = App(name="irc-lens", version=__version__, description=_DESCRIPTION)
    register_docs(app)
    for module in _iter_command_modules():
        register_into = getattr(module, "register_into", None)
        if register_into is not None:
            register_into(app)
    return app


def _build_parser() -> "argparse.ArgumentParser":
    """Return the rendered argparse parser for the assembled App.

    Retained for tests that introspect the ``serve`` subparser's argparse
    actions; the live entry point (:func:`main`) dispatches through ``run_cli``,
    not this parser.
    """
    from agentfront.cli_surface import make_cli

    return make_cli(build_app())


def main(argv: list[str] | None = None) -> int:
    """Dispatch *argv* against the assembled irc-lens App.

    Everything routes through ``agentfront.cli_surface.run_cli`` except the
    ``overview`` and ``doctor`` meta-verbs (whose ``--json`` contracts agentfront
    0.20.0's stock verbs can't yet satisfy, and whose names are reserved) and the
    ``--version`` flag; those are handled here against the same App.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    app = build_app()

    if args[:1] in (["--version"], ["-V"]):
        sys.stdout.write(f"irc-lens {__version__}\n")
        return 0
    if args[:1] == ["overview"]:
        from irc_lens.cli._meta import overview_command

        return overview_command(app, args[1:])
    if args[:1] == ["doctor"]:
        from irc_lens.cli._meta import doctor_command

        return doctor_command(app, args[1:])

    from agentfront.cli_surface import run_cli

    return run_cli(app, args)


if __name__ == "__main__":
    sys.exit(main())
