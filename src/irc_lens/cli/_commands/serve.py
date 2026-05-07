"""``irc-lens serve`` — launch the aiohttp web console.

Loads a ``LensConfig`` from ``--config`` (or ``default_config_path()`` if
it exists). When no config file is found, falls back to building a synthetic
dev ``LensConfig`` from the CLI args — this preserves backward compatibility
with the existing ``irc-lens serve --nick lens`` invocation. Phase 4 (T4.4)
makes the config file required.

In ``auth.mode: dev``, opens one Session at startup against the configured
AgentIRC and pre-seeds the registry. In ``auth.mode: cloudflare-access``,
warms the JWKS endpoint at startup (fail-fast on unreachable Cloudflare →
``EXIT_ENV_ERROR``) and lazy-opens per-user Sessions on first authenticated
request.

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
from dataclasses import replace
from pathlib import Path

from aiohttp import web

from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError
from irc_lens.cli._output import emit_diagnostic
from irc_lens.config import LensConfig, default_config_path, load_config
from irc_lens.session import LensConnectionLost, Session
from irc_lens.web import make_app
from irc_lens.web.auth import warm_jwks
from irc_lens.web.sessions import SessionFactory, disconnect_all

logger = logging.getLogger("irc_lens.serve")

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_LOOPBACK_DEFAULT = "127.0.0.1"

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


def _validate_cli_against_config(
    config: LensConfig,
    nick: str | None,
    bind: str | None,
) -> LensConfig:
    """Apply CF-mode CLI rules. Returns a possibly-coerced config.

    In dev mode this is a no-op.  In cloudflare-access mode:
    - ``--nick`` is rejected (nick is derived per user from the JWT).
    - A non-loopback ``--bind`` is coerced to ``127.0.0.1`` with a WARNING,
      because cloudflared terminates locally and a public bind would bypass auth.
    """
    if config.auth_mode != "cloudflare-access":
        return config
    if nick is not None:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message="--nick is not valid when auth.mode is cloudflare-access",
            remediation=(
                "remove --nick — in CF mode the nick is derived per "
                "authenticated user from auth.allowed_emails"
            ),
        )
    effective_bind = bind if bind is not None else config.web_bind
    if effective_bind not in _LOOPBACK:
        logger.warning(
            "web.bind=%s is not loopback; coerced to %s because "
            "cloudflared terminates locally",
            effective_bind,
            _LOOPBACK_DEFAULT,
        )
        return replace(config, web_bind=_LOOPBACK_DEFAULT)
    return config


def _build_dev_config_from_args(args: argparse.Namespace) -> LensConfig:
    """Build a synthetic dev-mode ``LensConfig`` from the CLI arguments.

    This is the Phase-3 backward-compatibility fallback: when no config file
    is found at ``--config`` (or ``default_config_path()``), the serve command
    constructs a minimal ``LensConfig`` from the legacy ``--host``, ``--port``,
    ``--nick``, ``--bind``, and ``--web-port`` flags so that the existing
    ``irc-lens serve --nick lens`` invocation keeps working without a config
    file. Phase 4 (T4.4) removes this fallback and makes ``--config`` (or the
    default path) required.
    """
    if args.nick is None:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message="the following arguments are required: --nick",
            remediation=(
                "try 'irc-lens serve --nick <name>' (e.g. --nick lens); "
                "run 'irc-lens serve --help' for all flags"
            ),
        )
    dev_email = f"{args.nick}@local"
    web_bind = args.bind if args.bind is not None else _LOOPBACK_DEFAULT
    return LensConfig(
        auth_mode="dev",
        dev_nick=args.nick,
        dev_email=dev_email,
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="lens",
        server_host=args.host,
        server_port=args.port,
        web_bind=web_bind,
        web_port=args.web_port,
    )


def _resolve_config(args: argparse.Namespace) -> LensConfig:
    """Load a LensConfig from `--config` / default path, or fall back
    to the synthetic dev config from CLI args.

    Phase 4 (T4.4) removes the fallback so the file becomes required.

    If the user *explicitly* passes ``--config <path>``, the path must
    exist — silently dropping to the synthetic dev config on a missed
    typo would risk starting an unauthenticated server when the user
    intended a CF-mode deploy. The fallback only applies when no
    ``--config`` was passed AND the default location is empty.
    """
    if args.config:
        explicit = Path(args.config)
        if not explicit.exists():
            raise AfiError(
                code=EXIT_USER_ERROR,
                message=f"--config {args.config!r} does not exist",
                remediation=(
                    "fix the path, or run `irc-lens config init "
                    f"--path {args.config}` to drop a starter config there"
                ),
            )
        return load_config(explicit)
    default = default_config_path()
    if default.exists():
        return load_config(default)
    return _build_dev_config_from_args(args)


async def _connect_dev_session(args: argparse.Namespace, config: LensConfig) -> Session:
    """Open the dev-mode IRC session: connect → wait_for_welcome → seed.

    Translates `LensConnectionLost` into the canonical `AfiError`
    pair (host/port unreachable vs. nick rejected) and propagates
    `apply_seed`'s errors after disconnecting cleanly.
    """
    session = Session(
        host=config.server_host,
        port=config.server_port,
        nick=config.dev_nick,
        icon=args.icon,
    )
    try:
        await session.connect()
    except LensConnectionLost as exc:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"cannot reach AgentIRC at {config.server_host}:{config.server_port}: {exc}",
            remediation=(
                "verify the AgentIRC server is running and reachable, then "
                "retry. e.g. `culture server start --name local && culture "
                "server status local`"
            ),
        ) from exc
    # Block until 001 RPL_WELCOME (or 432/433 rejection). AgentIRC
    # enforces a server-name prefix on nicks (e.g. `spark-`); without
    # this gate a rejected nick produces a silently broken session
    # where every query times out at 10 s and the chat pane stays empty.
    try:
        await session.wait_for_welcome()
    except LensConnectionLost as exc:
        await session.disconnect()
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"AgentIRC registration failed: {exc}",
            remediation=(
                "AgentIRC enforces a server-name prefix on nicks (e.g. "
                "`spark-foo` for a server named `spark`). Pass a nick "
                "matching that prefix via --nick, or check the server's "
                "config for the expected prefix."
            ),
        ) from exc
    if args.seed:
        # apply_seed raises AfiError on shape errors which the dispatcher
        # renders as `error:` + `hint:`. Broad except so connection
        # cleanup runs on every failure path — leaking a connected IRC
        # session past process exit would orphan state in the AgentIRC
        # server. BaseException (KeyboardInterrupt, SystemExit) still
        # propagates untouched. Function-local import to keep the same
        # cycle break documented at the module top.
        try:
            from irc_lens.seed import apply_seed
            apply_seed(session, Path(args.seed))
        except Exception:
            await session.disconnect()
            raise
    return session


async def _warm_cf_or_raise(config: LensConfig) -> None:
    """Pre-flight the Cloudflare JWKS endpoint. Fail-fast at startup
    so a misconfigured CF deploy can't bind a port that returns 502
    on every request."""
    try:
        await warm_jwks(config)
    except Exception as exc:
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"could not reach Cloudflare JWKS at {config.cf_team_domain}: {exc}",
            remediation="verify network egress and team_domain spelling",
        ) from exc


async def _build_app(
    args: argparse.Namespace, config: LensConfig
) -> tuple[web.Application, Session | None]:
    """Construct the aiohttp Application for the active auth mode.

    Returns the app plus, in dev mode, the pre-seeded `Session` (so
    the bind-failure path can clean it up). CF mode returns `None`
    for the session: per-user sessions open lazily on first
    authenticated request.
    """
    if config.auth_mode == "dev":
        session = await _connect_dev_session(args, config)
        factory: SessionFactory = lambda _nick: session  # noqa: E731
        app = make_app(config, factory)
        # Pre-seed so the dev-mode middleware's fixed identity
        # (config.dev_email) resolves immediately without re-connecting.
        app["registry"].register(config.dev_email, session)
        return app, session
    if config.auth_mode == "cloudflare-access":
        # No session at startup — per-user sessions open lazily on
        # first authenticated request.
        await _warm_cf_or_raise(config)

        def cf_factory(nick: str) -> Session:
            return Session(
                host=config.server_host, port=config.server_port, nick=nick
            )

        app = make_app(config, cf_factory)
        # No pre-seed; --nick / --seed / --icon are ignored (Phase 4
        # T4.1 rejects --nick with a hard error in CF mode).
        return app, None
    # Future-mode guard: load_config only allows "dev" or
    # "cloudflare-access", but a caller bypassing load_config (or a
    # future mode added without updating this branch) lands here. Fail
    # loud rather than silently routing through the CF path.
    raise AfiError(
        code=EXIT_USER_ERROR,
        message=f"unsupported auth.mode={config.auth_mode!r}",
        remediation="set `auth.mode:` to either `dev` or `cloudflare-access`",
    )


async def _bind_site_or_raise(
    runner: web.AppRunner, config: LensConfig, dev_session: Session | None
) -> None:
    """Start the TCPSite or raise `AfiError(EXIT_ENV_ERROR)`.

    On `OSError` (port in use, etc.), tear down the runner AND any
    dev-mode session that's already connected, so the process doesn't
    exit holding open fds.
    """
    site = web.TCPSite(runner, host=config.web_bind, port=config.web_port)
    try:
        await site.start()
    except OSError as exc:
        if dev_session is not None:
            await dev_session.disconnect()
        await runner.cleanup()
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"cannot bind web port {config.web_bind}:{config.web_port}: {exc}",
            remediation=(
                "pick a different --web-port, or stop whatever is already "
                "bound to this port"
            ),
        ) from exc


def _maybe_open_browser(args: argparse.Namespace, url: str) -> None:
    if not args.open:
        return
    try:
        webbrowser.open(url)
    except webbrowser.Error as exc:
        emit_diagnostic(f"warning: --open failed: {exc}")


async def _serve_async(args: argparse.Namespace) -> None:
    """Run connect → bind → forever inside one event loop.

    Doing the IRC connect in a separate ``asyncio.run`` would create
    background read tasks tied to a loop that exits before
    ``aiohttp.web.run_app`` starts — the IRC connection would die
    before the web UI ever serves a request. This coroutine keeps
    everything on one loop until shutdown.
    """
    config = _resolve_config(args)
    config = _validate_cli_against_config(config, nick=args.nick, bind=args.bind)
    app, dev_session = await _build_app(args, config)
    runner = web.AppRunner(app, handle_signals=True)
    await runner.setup()
    await _bind_site_or_raise(runner, config, dev_session)
    url = _display_url(config.web_bind, config.web_port)
    emit_diagnostic(f"irc-lens serving on {url}")
    _maybe_open_browser(args, url)
    # Sleep forever until the runtime cancels us (SIGINT / SIGTERM via
    # AppRunner.handle_signals=True, or the test harness cancelling
    # the task).
    try:
        await asyncio.Event().wait()
    finally:
        # Disconnect every registered Session (covers dev pre-seeded
        # session and any lazily-opened per-user CF sessions alike).
        # return_exceptions=True so one failure doesn't strand the others.
        await disconnect_all(app["registry"])
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
    p.add_argument(
        "--config",
        default=None,
        help=(
            "Path to irc-lens config.yaml (default: ~/.config/irc-lens/config.yaml). "
            "When absent, builds a synthetic dev config from the other CLI flags."
        ),
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
        default=None,
        help=(
            "Nick to register on AgentIRC (e.g. --nick lens). "
            "Required in dev mode (no config file). "
            "Invalid in cloudflare-access mode — nick is derived from the JWT."
        ),
    )
    p.add_argument(
        "--web-port",
        type=int,
        default=8765,
        help="Local HTTP port for the lens UI (default: %(default)s).",
    )
    p.add_argument(
        "--bind",
        default=None,
        help=(
            "Bind address for the local web app "
            "(default: web.bind from config, or 127.0.0.1). "
            "Using 0.0.0.0 prints a warning — there is no auth in v1. "
            "In cloudflare-access mode a non-loopback bind is coerced to 127.0.0.1."
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
