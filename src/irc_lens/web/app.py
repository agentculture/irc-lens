"""aiohttp ``Application`` factory for irc-lens.

Phase 3: dev-mode and cloudflare-access modes both wired. Takes a
:class:`LensConfig` and a session factory rather than a single connected
Session. Builds the per-principal :class:`SessionRegistry`, mounts the
appropriate identity middleware for the configured auth mode, and exposes
a ``/healthz`` endpoint.
"""
from __future__ import annotations

from importlib.resources import files

from aiohttp import web

from irc_lens._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import LensConfig
from irc_lens.web import routes
from irc_lens.web.auth import build_cloudflare_middleware
from irc_lens.web.identity import Identity
from irc_lens.web.routes import _MAX_INPUT_BODY
from irc_lens.web.sessions import SessionFactory, SessionRegistry


def _dev_identity_middleware(config: LensConfig):
    """Synthesize a fixed dev identity on every request.

    Real-world dev mode has a single human at the keyboard; the lens
    treats every request as them. CF mode (Phase 3) replaces this.
    """
    # Explicit checks rather than `assert`: `python -O` strips assertions,
    # and a config with auth_mode='dev' but missing dev_email/dev_nick
    # would silently produce an Identity(None, None, "dev") and fail
    # opaquely deep in session creation.
    if config.auth_mode != "dev":
        raise ValueError(
            f"_dev_identity_middleware called with auth_mode={config.auth_mode!r}"
        )
    if not config.dev_email or not config.dev_nick:
        raise ValueError(
            "auth.mode='dev' requires both auth.dev.email and auth.dev.nick"
        )
    identity = Identity(
        principal=config.dev_email,
        nick=config.dev_nick,
        raw_jwt_subject="dev",
    )

    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.path.startswith("/static/") or request.path == "/healthz":
            return await handler(request)
        request["identity"] = identity
        return await handler(request)

    return middleware


def make_app(config: LensConfig, session_factory: SessionFactory) -> web.Application:
    if config.auth_mode == "dev":
        middleware = _dev_identity_middleware(config)
    elif config.auth_mode == "cloudflare-access":
        middleware = build_cloudflare_middleware(config)
    else:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"auth.mode={config.auth_mode!r} is not supported",
            remediation="set `auth.mode:` to either `dev` or `cloudflare-access`",
        )

    app = web.Application(client_max_size=_MAX_INPUT_BODY, middlewares=[middleware])

    registry = SessionRegistry(factory=session_factory)
    app["registry"] = registry
    app["config"] = config

    app.router.add_get("/", routes.get_index)
    app.router.add_post("/input", routes.post_input)
    app.router.add_get("/events", routes.get_events)
    app.router.add_get("/healthz", routes.get_healthz)

    static_dir = files("irc_lens").joinpath("static")
    app.router.add_static(
        "/static/",
        path=str(static_dir),
        name="static",
        show_index=False,
        follow_symlinks=False,
    )

    return app
