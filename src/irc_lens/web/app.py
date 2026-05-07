"""aiohttp ``Application`` factory for irc-lens.

Phase 2: takes a :class:`LensConfig` and a session factory rather than
a single connected Session. Builds the per-principal
:class:`SessionRegistry`, mounts the dev-mode identity middleware, and
exposes a `/healthz` endpoint.

CF-mode middleware lands in Phase 3.
"""
from __future__ import annotations

from importlib.resources import files

from aiohttp import web

from irc_lens.config import LensConfig
from irc_lens.web import routes
from irc_lens.web.identity import Identity
from irc_lens.web.routes import _MAX_INPUT_BODY
from irc_lens.web.sessions import SessionFactory, SessionRegistry


def _dev_identity_middleware(config: LensConfig):
    """Synthesize a fixed dev identity on every request.

    Real-world dev mode has a single human at the keyboard; the lens
    treats every request as them. CF mode (Phase 3) replaces this.
    """
    assert config.auth_mode == "dev"
    assert config.dev_email is not None
    assert config.dev_nick is not None
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
    if config.auth_mode != "dev":
        # Phase 3 will branch here. Until then, callers must pass dev mode.
        raise RuntimeError(
            f"make_app: only auth.mode='dev' is wired in Phase 2, got {config.auth_mode!r}"
        )

    middleware = _dev_identity_middleware(config)
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
