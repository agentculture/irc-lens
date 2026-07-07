"""aiohttp ``Application`` factory for irc-lens.

Phase 3: dev-mode and cloudflare-access modes both wired. Takes a
:class:`LensConfig` and a session factory rather than a single connected
Session. Builds the per-principal :class:`SessionRegistry`, mounts the
appropriate identity middleware for the configured auth mode, and exposes
a ``/healthz`` endpoint.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from aiohttp import web

from irc_lens._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import LensConfig
from irc_lens.web import routes
from irc_lens.web.auth import build_cloudflare_middleware
from irc_lens.web.front import mount_agent_front
from irc_lens.web.identity import Identity
from irc_lens.web.render import precompute_static_hashes
from irc_lens.web.sessions import SessionFactory, SessionRegistry
from irc_lens.web.store import MediaStore

# Headroom added on top of `config.media_max_file_bytes` when sizing
# `client_max_size` (see `make_app`). Multipart framing (boundary
# markers, the "file" field's own headers) adds a little overhead
# beyond the raw file bytes; this keeps a well-formed upload right at
# the per-file cap from being rejected by the framework layer before
# `MediaStore.save`'s own streaming cap ever gets a chance to run.
_CLIENT_MAX_SIZE_MEDIA_HEADROOM = 65536


_SECURITY_HEADERS_CSP = (
    "default-src 'self'; script-src 'self'; img-src 'self' https: http:; "
    "media-src 'self' https: http:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)


def _apply_security_headers(response: web.StreamResponse) -> None:
    """Stamp the baseline security headers onto *response* in place.

    ``X-Content-Type-Options`` and ``Referrer-Policy`` apply to every
    response; ``Content-Security-Policy`` is scoped to HTML documents —
    the directives (``script-src``, ``object-src``, ...) police markup,
    and adding them to JSON/static-asset responses would just be noise.
    See docs/superpowers/specs/2026-07-02-media-support-design.md
    ("Security headers").
    """
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if (response.content_type or "").lower() == "text/html":
        response.headers["Content-Security-Policy"] = _SECURITY_HEADERS_CSP


@web.middleware
async def _security_headers_middleware(request: web.Request, handler):
    """Attach baseline security headers to every response.

    Wraps the identity middleware (see ``middlewares=`` in ``make_app``)
    so the headers land even on auth denials (401/403) and other
    early-exit responses. A static-file 404 surfaces as a *raised*
    ``HTTPException`` rather than a returned ``Response`` (aiohttp's
    ``StaticResource`` internals) — the ``except`` arm below catches
    that path too, so nosniff/referrer-policy aren't lost on the
    framework-level error page.

    ``GET /events`` (the SSE stream) calls ``response.prepare()``
    itself deep inside the handler, before this middleware regains
    control — by the time we could mutate its headers they're already
    on the wire, so this middleware is a no-op there by construction.
    That route sets its own Content-Type/Cache-Control/X-Accel-Buffering
    and is left alone, matching the design doc.
    """
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _apply_security_headers(exc)
        raise
    _apply_security_headers(response)
    return response


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
        if (
            request.path.startswith("/static/")
            or request.path == "/healthz"
            or request.path.startswith("/media/")
        ):
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

    # Sized for the media cap (task t6), not the 4 KiB `/input` contract —
    # `POST /input` enforces its own bound in-handler (see
    # `routes._read_bounded_body`) precisely because this is no longer
    # small enough to do that job for it. `media_max_file_bytes` is
    # always a valid, validated int regardless of `media_enabled`
    # (`_validate_media_section` computes it unconditionally), so this
    # is safe to compute even when media is disabled.
    app = web.Application(
        client_max_size=config.media_max_file_bytes + _CLIENT_MAX_SIZE_MEDIA_HEADROOM,
        middlewares=[_security_headers_middleware, middleware],
    )

    registry = SessionRegistry(factory=session_factory)
    app["registry"] = registry
    app["config"] = config

    if config.media_enabled:
        app["media_store"] = MediaStore(
            root=Path(config.media_dir),
            max_file_bytes=config.media_max_file_bytes,
            max_store_bytes=config.media_max_store_bytes,
        )
        # Advertised base URL for capability links returned by
        # `POST /upload`. `media_public_base_url` (when set) is the
        # operator-declared reachable address (needed once a peer on
        # another machine has to fetch the blob); otherwise fall back
        # to this instance's own bind/port, which is at least correct
        # for same-host / same-LAN consumers.
        app["media_base"] = (
            config.media_public_base_url.rstrip("/")
            if config.media_public_base_url
            else f"http://{config.web_bind}:{config.web_port}"  # NOSONAR — loopback default; TLS terminates at cloudflared in CF mode
        )

    app.router.add_get("/", routes.get_index)
    app.router.add_post("/input", routes.post_input)
    app.router.add_post("/upload", routes.post_upload)
    app.router.add_get("/media/{name}", routes.get_media)
    app.router.add_get("/events", routes.get_events)
    app.router.add_get("/residents", routes.get_residents)
    app.router.add_get("/healthz", routes.get_healthz)

    static_dir = files("irc_lens").joinpath("static")
    app.router.add_static(
        "/static/",
        path=str(static_dir),
        name="static",
        show_index=False,
        follow_symlinks=False,
    )

    # Mount the agentfront HTTP surface (index / llms.txt / sitemap.xml /
    # front / doc slugs) under the /agent prefix, behind this app's auth
    # middleware. Builds the WSGI callable once here — not per request. The
    # /agent routes are deliberately NOT added to either exempt list above,
    # so CF mode requires a valid JWT on the prefix (decisions c21/c30).
    mount_agent_front(app)

    # Pre-warm asset-hash cache so the first GET / doesn't pay file I/O
    # synchronously inside the async handler (per Qodo PR #40 review).
    precompute_static_hashes()

    return app
