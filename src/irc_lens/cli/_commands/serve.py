"""``irc-lens serve`` — launch the aiohttp web console.

Phase 4 ships the skeleton: parse flags, construct ``Session``,
fail-fast on connect, then ``aiohttp.web.run_app``. The Phase 5 wiring
(SSE bus, parser dispatch on POST /input) is invisible from here —
``make_app`` just gets a richer Session.

Spec contract enforced:

* ``--host`` / ``--port`` / ``--nick`` are required.
* ``--bind 0.0.0.0`` prints a loud stderr warning (no auth in v1).
* AgentIRC unreachable → ``error:`` + ``hint:`` on stderr, exit 1,
  aiohttp never binds.
* Web port already in use → exit 2 (env error per the policy in
  ``CLAUDE.md``).
* ``--seed`` is accepted but deferred to Phase 8 (logged as a
  diagnostic); the path isn't read here.
* ``--log-json`` switches stderr logging to one JSON object per line.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser

from aiohttp import web

from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError
from irc_lens.cli._output import emit_diagnostic
from irc_lens.session import LensConnectionLost, Session
from irc_lens.web import make_app


class _JsonLineFormatter(logging.Formatter):
    """One JSON object per line on stderr (mirrors culture's --log-json)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
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


def cmd_serve(args: argparse.Namespace) -> int:
    if args.bind == "0.0.0.0":
        emit_diagnostic(
            "warning: --bind 0.0.0.0 exposes the lens with NO authentication "
            "(v1 has no auth). Use --bind 127.0.0.1 unless you know what "
            "you're doing."
        )

    _configure_logging(args.log_json)

    if args.seed:
        emit_diagnostic(
            f"--seed {args.seed}: deferred to a later phase; flag accepted, "
            "no fixture loaded yet."
        )

    session = Session(host=args.host, port=args.port, nick=args.nick, icon=args.icon)

    # Connect synchronously via aiohttp's loop helper so failure
    # surfaces BEFORE we ever bind the web port. The contract is
    # "AgentIRC unreachable → exit 1, aiohttp never starts".
    import asyncio

    try:
        asyncio.run(session.connect())
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

    url = f"http://{args.bind}:{args.web_port}/"
    emit_diagnostic(f"irc-lens serving on {url}")

    if args.open:
        try:
            webbrowser.open(url)
        except webbrowser.Error as exc:
            emit_diagnostic(f"warning: --open failed: {exc}")

    app = make_app(session)
    try:
        web.run_app(
            app,
            host=args.bind,
            port=args.web_port,
            print=None,  # we already printed our own banner
            handle_signals=True,
        )
    except OSError as exc:
        # Port in use is the canonical failure here — exit 2 per the
        # spec's exit-code policy (env error).
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"cannot bind web port {args.bind}:{args.web_port}: {exc}",
            remediation=(
                "pick a different --web-port, or stop whatever is already "
                "bound to this port"
            ),
        ) from exc
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "serve",
        help="Launch the aiohttp web console against an AgentIRC server.",
    )
    p.add_argument("--host", required=True, help="AgentIRC server host.")
    p.add_argument("--port", required=True, type=int, help="AgentIRC server port.")
    p.add_argument("--nick", required=True, help="Nick to register on AgentIRC.")
    p.add_argument(
        "--web-port",
        type=int,
        default=8765,
        help="Local HTTP port for the lens UI (default: 8765).",
    )
    p.add_argument(
        "--bind",
        default="127.0.0.1",
        help=(
            "Bind address for the local web app (default: 127.0.0.1). "
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
            "Path to a YAML fixture preloading view state for tests "
            "(loader lands in a later phase; flag accepted now)."
        ),
    )
    p.add_argument(
        "--log-json",
        action="store_true",
        help="Emit stderr logs as one JSON object per line.",
    )
    p.set_defaults(func=cmd_serve)
