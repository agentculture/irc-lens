"""``irc-lens serve`` — launch the aiohttp web console.

Phase 4 ships the skeleton: parse flags, construct ``Session``,
fail-fast on connect, then ``aiohttp.web.run_app``. The Phase 5 wiring
(SSE bus, parser dispatch on POST /input) is invisible from here —
``make_app`` just gets a richer Session.

Spec contract enforced:

* ``--nick`` is required (identity is the user's choice — no safe default).
* ``--host`` / ``--port`` default to ``127.0.0.1`` / ``6667`` so a bare
  ``irc-lens serve --nick <name>`` reaches a local AgentIRC out of the
  box. Override either flag to point at a remote server.
* ``--bind 0.0.0.0`` prints a loud stderr warning (no auth in v1).
* AgentIRC unreachable → ``error:`` + ``hint:`` on stderr, exit 1,
  aiohttp never binds.
* Web port already in use → exit 2 (env error per the policy in
  ``CLAUDE.md``).
* ``--seed`` overlays a YAML fixture onto Session state after
  ``connect()`` — see :mod:`irc_lens.seed` for the schema.
* ``--log-json`` switches stderr logging to one JSON object per line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import webbrowser
from pathlib import Path

from aiohttp import web

from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError
from irc_lens.cli._output import emit_diagnostic
from irc_lens.session import LensConnectionLost, Session
from irc_lens.web import make_app

# `irc_lens.seed` is imported function-locally inside `_serve_async`
# to avoid a real-but-latent module-load cycle:
#   seed.py -> cli._errors -> cli/__init__.py (which eagerly imports
#   serve.py) -> serve.py -> seed.py (partially initialized)
# Phase 9c's `seeded_lens_client` fixture is the first place that
# imports `irc_lens.seed` at module-load time, which uncovered this.
# Keep the cycle broken at the production-code import edge.


class _JsonLineFormatter(logging.Formatter):
    """One JSON object per line on stderr (mirrors culture's --log-json).

    Tracebacks are intentionally omitted — the spec mandates "no Python
    traceback ever leaks", which applies to JSON-line output too.
    Exceptions are summarised by class+message via ``logger.error`` /
    the dispatcher's ``AfiError`` translation; full tracebacks are for
    interactive debugging via ``logger.exception`` and the default
    text formatter, not for the agent-facing JSON channel.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            payload["exc"] = {
                "type": exc.__class__.__name__,
                "msg": str(exc),
            }
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging(log_json: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if log_json:
        handler.setFormatter(_JsonLineFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    # Replace any existing handlers so successive `serve` invocations in
    # tests don't accumulate.
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def _display_url(bind: str, port: int) -> str:
    """The URL we PRINT and ``--open`` against.

    When binding to ``0.0.0.0`` (any interface), the user-facing URL has
    to be a routable address — ``http://0.0.0.0:port/`` is not a valid
    browser target on most systems. Substitute ``127.0.0.1`` for the
    display only; the bind address itself is unchanged.
    """
    host = "127.0.0.1" if bind in ("0.0.0.0", "::") else bind
    return f"http://{host}:{port}/"


async def _serve_async(args: argparse.Namespace) -> None:
    """Run connect → bind → forever inside one event loop.

    Doing the IRC connect in a separate ``asyncio.run`` would create
    background read tasks tied to a loop that exits before
    ``aiohttp.web.run_app`` starts — the IRC connection would die
    before the web UI ever serves a request. This coroutine keeps
    everything on one loop until shutdown.
    """
    session = Session(host=args.host, port=args.port, nick=args.nick, icon=args.icon)
    try:
        await session.connect()
    except LensConnectionLost as exc:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"cannot reach AgentIRC at {args.host}:{args.port}: {exc}",
            remediation=(
                "verify the AgentIRC server is running and reachable, then "
                "retry. e.g. `culture server start --name local && culture "
                "server status local`"
            ),
        ) from exc

    if args.seed:
        # Spec line 261: connection is real; seed only overlays UI
        # state. apply_seed raises AfiError on shape errors which the
        # dispatcher renders as `error:` + `hint:`. Broad except so
        # connection cleanup runs on every failure path — leaking a
        # connected IRC session past process exit would orphan state
        # in the AgentIRC server. BaseException (KeyboardInterrupt,
        # SystemExit) still propagates untouched. The function-local
        # import lives INSIDE the try so an import-time failure on
        # `irc_lens.seed` (or any module it loads) also triggers the
        # disconnect cleanup branch.
        try:
            from irc_lens.seed import apply_seed  # see module top comment

            apply_seed(session, Path(args.seed))
        except Exception:
            await session.disconnect()
            raise

    app = make_app(session)
    runner = web.AppRunner(app, handle_signals=True)
    await runner.setup()
    site = web.TCPSite(runner, host=args.bind, port=args.web_port)
    try:
        await site.start()
    except OSError as exc:
        await session.disconnect()
        await runner.cleanup()
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"cannot bind web port {args.bind}:{args.web_port}: {exc}",
            remediation=(
                "pick a different --web-port, or stop whatever is already "
                "bound to this port"
            ),
        ) from exc

    url = _display_url(args.bind, args.web_port)
    emit_diagnostic(f"irc-lens serving on {url}")
    if args.open:
        try:
            webbrowser.open(url)
        except webbrowser.Error as exc:
            emit_diagnostic(f"warning: --open failed: {exc}")

    # Sleep forever until the runtime cancels us (SIGINT / SIGTERM via
    # AppRunner.handle_signals=True, or the test harness cancelling the
    # task).
    try:
        await asyncio.Event().wait()
    finally:
        await session.disconnect()
        await runner.cleanup()


def cmd_serve(args: argparse.Namespace) -> int:
    if args.bind == "0.0.0.0":
        emit_diagnostic(
            "warning: --bind 0.0.0.0 exposes the lens with NO authentication "
            "(v1 has no auth). Use --bind 127.0.0.1 unless you know what "
            "you're doing."
        )

    _configure_logging(args.log_json)

    try:
        asyncio.run(_serve_async(args))
    except KeyboardInterrupt:
        # Ctrl-C is the supported shutdown per the spec; exit 0.
        emit_diagnostic("irc-lens shutdown")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "serve",
        help="Launch the aiohttp web console against an AgentIRC server.",
        description=(
            "Launch the aiohttp web console against an AgentIRC server. "
            "Defaults target a local culture server on 127.0.0.1:6667 — only "
            "--nick is required for the common case."
        ),
        epilog=(
            "examples:\n"
            "  irc-lens serve --nick lens\n"
            "      Connect to a local AgentIRC (127.0.0.1:6667) and serve the\n"
            "      web console on http://127.0.0.1:8765/.\n"
            "  irc-lens serve --nick lens --open\n"
            "      Same, and auto-launch your default browser at the URL.\n"
            "  irc-lens serve --host irc.example.org --port 6667 --nick ops\n"
            "      Point at a remote AgentIRC server.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # `%(default)s` lets argparse render the actual default at help-time,
    # so the rendered "(default: …)" string can never drift from the
    # `default=` value. Guarded by
    # tests/test_serve_cli.py::test_serve_help_renders_defaults_from_argparse.
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="AgentIRC server host (default: %(default)s).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=6667,
        help="AgentIRC server port (default: %(default)s).",
    )
    p.add_argument(
        "--nick",
        required=True,
        help="Nick to register on AgentIRC (e.g. --nick lens).",
    )
    p.add_argument(
        "--web-port",
        type=int,
        default=8765,
        help="Local HTTP port for the lens UI (default: %(default)s).",
    )
    p.add_argument(
        "--bind",
        default="127.0.0.1",
        help=(
            "Bind address for the local web app (default: %(default)s). "
            "Using 0.0.0.0 prints a warning — there is no auth in v1."
        ),
    )
    p.add_argument("--icon", default=None, help="Optional emoji passed to AgentIRC ICON.")
    p.add_argument(
        "--open",
        action="store_true",
        help="Auto-launch the default browser to the lens URL after binding.",
    )
    p.add_argument(
        "--seed",
        default=None,
        help=(
            "Path to a YAML fixture preloading view state for tests. "
            "See irc_lens/seed.py for the schema."
        ),
    )
    p.add_argument(
        "--log-json",
        action="store_true",
        help="Emit stderr logs as one JSON object per line.",
    )
    p.set_defaults(func=cmd_serve)
