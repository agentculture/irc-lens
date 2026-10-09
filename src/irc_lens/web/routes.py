"""HTTP route handlers for irc-lens.

* ``GET /``        — render the three-pane index from Session state.
* ``POST /input``  — read the input line (JSON or form-encoded body),
  parse via ``parse_command``, dispatch through ``Session.execute``.
  Returns ``204`` on success, ``413`` if the body exceeds the
  bounded-memory limit (enforced in-handler — see the module docstring
  on ``_read_bounded_body``), ``400`` for invalid JSON, ``503`` when
  the session is unhealthy or AgentIRC is unreachable.
* ``POST /upload`` — identity-gated + origin-checked like ``/input``;
  streams a ``multipart/form-data`` ``file`` field into the
  :class:`~irc_lens.web.store.MediaStore`. ``413``/``400`` on
  too-large/bad-type, ``201`` with ``{"url", "kind"}`` on success.
* ``GET /media/{name}`` — capability-URL media serving. Auth-exempt
  (see the exemption lists in ``web/app.py`` / ``web/auth.py``) — the
  unguessable token in the path *is* the credential.
* ``GET /events``  — open an SSE stream from
  ``Session.event_bus.subscribe()``; flushes each event through
  ``format_sse``. Closes the subscription cleanly on client disconnect.
* ``GET /residents`` — standalone page rendering culture's resident
  presence resource view, fetched server-side. Always ``200``: every
  upstream failure renders a kind-specific notice, never an error page.

Static files (``/static/*``) are wired via ``app.router.add_static`` in
:mod:`irc_lens.web.app`, not as a handler here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from collections.abc import AsyncIterator

import aiohttp
from aiohttp import web

from irc_lens import metrics
from irc_lens.commands import (
    SANDBOX_COMMAND,
    CommandType,
    ParsedCommand,
    allowed_in_sandbox,
    parse_command,
)
from irc_lens.session import LensConnectionLost
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.entry import client_ip
from irc_lens.web.events import format_sse
from irc_lens.web.identity import TIER_ANONYMOUS, TIER_GUEST, Identity
from irc_lens.web.render import (
    render_chat_log,
    render_fragment,
    render_index,
    render_residents_page,
)
from irc_lens.web.residents import (
    ResidentsResult,
    fetch_residents,
    resolve_residents_url,
)
from irc_lens.web.sessions import (
    BACKEND_MESH,
    BACKEND_SANDBOX,
    GUEST_PRINCIPAL_PREFIX,
)
from irc_lens.web.store import CONTENT_TYPES, MediaTooLargeError, MediaTypeError

logger = logging.getLogger(__name__)

# 4 KiB upper bound on the input body. Slash-commands and chat lines
# are both well under this; the cap is a bounded-memory contract from
# the spec and a cheap defence against accidental floods. Enforced
# in-handler via `_read_bounded_body` — `client_max_size` on the
# Application is sized for media uploads (see `make_app`), so it no
# longer doubles as `/input`'s own cap the way it did before task t6.
_MAX_INPUT_BODY = 4096

# How many bytes `_read_bounded_body` (and `_iter_field_chunks` for
# uploads) pull per `.read()`/`.read_chunk()` call. Small enough to
# keep memory bounded while streaming; large enough not to thrash on
# syscalls for a multi-megabyte media upload.
_STREAM_CHUNK_SIZE = 65536

_UNHEALTHY_HINT = (
    "AgentIRC connection lost — restart irc-lens to reconnect "
    "(no auto-reconnect in v1)."
)

_MEDIA_DISABLED_HINT = (
    "media uploads are disabled — set `media.enabled: true` in the lens config"
)


#: Where a guest without a current consent record is sent (the consent step
#: is built by the entry-card task; this is the redirect target only).
CONSENT_PATH = "/consent"

#: Sandbox nick prefix for approved users who toggle into the sandbox.
SANDBOX_NICK_PREFIX = "sbx-"


def _json_error(status: int, error: str, hint: str, **extra) -> web.Response:
    return web.json_response({"error": error, "hint": hint, **extra}, status=status)


def sandbox_nick_for(config, identity: Identity) -> str:
    """``sbx-<real nick suffix>`` — distinct from the user's mesh nick."""
    prefix = f"{config.server_name}-"
    nick = identity.nick
    suffix = nick[len(prefix) :] if nick.startswith(prefix) else nick
    return f"{SANDBOX_NICK_PREFIX}{suffix}"


def _backend_for(request: web.Request) -> tuple[Identity, str]:
    """Pick (identity-for-registry, backend) from the VERIFIED tier only.

    Nothing from the query string, body, or headers participates: ``approved``
    -> real mesh (or the sandbox if that principal toggled it server-side),
    ``guest`` -> sandbox always, anything else -> 401.
    """
    identity: Identity = request["identity"]
    config = request.app["config"]
    if identity.is_approved:
        toggled = request.app.get("sandbox_toggle", ())
        if config.guest_enabled and identity.principal in toggled:
            sandbox_identity = identity._replace(
                nick=sandbox_nick_for(config, identity)
            )
            return sandbox_identity, BACKEND_SANDBOX
        return identity, BACKEND_MESH
    if identity.tier == TIER_GUEST and config.guest_enabled:
        return (
            identity._replace(
                principal=f"{GUEST_PRINCIPAL_PREFIX}{identity.principal}"
            ),
            BACKEND_SANDBOX,
        )
    raise web.HTTPUnauthorized(
        text=json.dumps(
            {
                "error": "approved sign-in required",
                "hint": "sign in through /login to reach the real mesh",
            }
        ),
        content_type="application/json",
    )


async def _resolve_session(request: web.Request):
    """Look up (or lazily open) the Session for this request's verified tier."""
    identity, backend = _backend_for(request)
    session = await request.app["registry"].get_or_open(identity, backend)
    # UI context for the header / palette, from the verified tier only.
    session.sandbox_available = (
        backend == BACKEND_MESH
        and identity.is_approved
        and request.app["config"].guest_enabled
    )
    return session


async def _consent_current(request: web.Request, email: str) -> bool:
    """True iff *email* has a consent record for the current Terms+Privacy.

    Fails closed: the legal module missing, or the version lookup failing,
    both mean "not consented".
    """
    import inspect

    try:
        from irc_lens import legal

        versions = await legal.current_legal_versions(request.app["config"])
        ok = legal.consent_is_current(request.app["guest_store"], email, versions)
        if inspect.isawaitable(ok):
            ok = await ok
        return bool(ok)
    except Exception:  # noqa: BLE001 — fail closed
        logger.exception("consent check failed; refusing guest")
        return False


async def _guest_consent_gate(
    request: web.Request, *, redirect: bool
) -> web.StreamResponse | None:
    """Obligation o8: a guest reaches sandbox routes only with current consent.

    Returns a response to send instead of the handler, or None to proceed.
    """
    identity: Identity = request["identity"]
    if identity.tier != TIER_GUEST:
        return None
    if await _consent_current(request, identity.principal):
        return None
    if redirect:
        return web.Response(status=302, headers={"Location": CONSENT_PATH})
    return _json_error(
        403,
        "consent required",
        "accept the current Terms and Privacy Policy first",
        redirect=CONSENT_PATH,
    )


async def _anonymous_or_gate(
    request: web.Request, *, redirect: bool
) -> web.StreamResponse | None:
    identity: Identity = request["identity"]
    if identity.tier == TIER_ANONYMOUS:
        return _json_error(
            401, "approved sign-in required", "enter through the guest entry card"
        )
    return await _guest_consent_gate(request, redirect=redirect)


async def post_sandbox_enter(request: web.Request) -> web.Response:
    """Approved users only: switch this principal's view to the sandbox."""
    return _set_sandbox_toggle(request, on=True)


async def post_sandbox_leave(request: web.Request) -> web.Response:
    """Approved users only: switch back to the real mesh (no re-auth)."""
    return _set_sandbox_toggle(request, on=False)


def _set_sandbox_toggle(request: web.Request, *, on: bool) -> web.Response:  # NOSONAR
    identity: Identity = request["identity"]
    if not identity.is_approved:
        return _json_error(
            401, "approved sign-in required", "sandbox toggle is for approved users"
        )
    toggled: set[str] = request.app["sandbox_toggle"]
    if on:
        toggled.add(identity.principal)
    else:
        toggled.discard(identity.principal)
    # HX-Refresh: the htmx buttons reload the page into the other view.
    return web.json_response(
        {"backend": BACKEND_SANDBOX if on else BACKEND_MESH},
        status=200,
        headers={"HX-Refresh": "true"},
    )


def _too_large() -> web.Response:
    return web.json_response(
        {"error": "input too large", "hint": f"max {_MAX_INPUT_BODY} bytes"},
        status=413,
    )


def _connection_lost(message: str) -> web.Response:
    return web.json_response(
        {"error": message, "hint": _UNHEALTHY_HINT},
        status=503,
    )


def _origin_ok(request: web.Request) -> bool:
    """CSRF defense-in-depth: verify Origin matches the request host.

    Compares ``(hostname.lower(), port_or_default_for_scheme)`` tuples
    rather than raw header strings so that case, default-port elision,
    and IPv6 bracket formatting don't cause false 403s.

    - Origin absent → allow (curl, cloudflared probes, internal monitors).
    - Origin present → match → allow; mismatch → deny.

    A proper CSRF-token scheme is tracked in issue #27 and will replace
    this floor in a later phase.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    parsed = urllib.parse.urlparse(origin)
    if not parsed.hostname:
        return False
    origin_host = parsed.hostname.lower()
    origin_port = _effective_port(parsed.port, parsed.scheme)
    request_host = (request.url.host or "").lower()
    request_port = _request_effective_port(request)
    return (origin_host, origin_port) == (request_host, request_port)


def _request_effective_port(request: web.Request) -> int:
    """Return the public-facing port for Origin comparison.

    When ``X-Forwarded-Proto`` is set (we're behind a TLS-terminating
    proxy), ``request.url.port`` reflects the proxy hop, not the
    client's view. Resolve the public-side port by preference:
    explicit ``X-Forwarded-Port`` > explicit port in the ``Host``
    header > default port for the forwarded scheme. The ``Host``-
    explicit case keeps deployments on non-standard public ports (e.g.
    ``https://example.com:8443``) from 403'ing on every POST.

    Without XFP, fall back to the actual URL.

    Interim heuristic: trusts a header any local process can forge.
    Replacement tracked in #39.
    """
    xfp = request.headers.get("X-Forwarded-Proto")
    if not xfp:
        return _effective_port(request.url.port, request.url.scheme)
    forwarded_scheme = xfp.lower().split(",")[0].strip()
    xfport = request.headers.get("X-Forwarded-Port")
    if xfport:
        try:
            return int(xfport.split(",")[0].strip())
        except ValueError:
            pass
    explicit = request.url.explicit_port
    if explicit is not None:
        return explicit
    return _effective_port(None, forwarded_scheme)


def _effective_port(port: int | None, scheme: str) -> int:
    """Resolve an explicit port, defaulting to the scheme's standard port."""
    if port is not None:
        return port
    return 443 if scheme == "https" else 80


def _origin_denied_response(request: web.Request) -> web.Response:
    """Shared 403 body + log line for a failed ``_origin_ok`` check.

    Used by both ``post_input`` and ``post_upload`` — the CSRF defense
    floor described in ``_origin_ok``'s docstring applies identically
    to both mutating POST endpoints.
    """
    xfp = request.headers.get("X-Forwarded-Proto")
    forwarded_scheme = xfp.lower().split(",")[0].strip() if xfp else request.url.scheme
    logger.warning(
        "origin_mismatch origin=%s request_host=%s request_port=%s "
        "scheme=%s method=%s path=%s",
        request.headers.get("Origin"),
        (request.url.host or "").lower(),
        _request_effective_port(request),
        forwarded_scheme,
        request.method,
        request.path,
    )
    return web.json_response(
        {
            "error": "Origin does not match request host",
            "hint": "this is a CSRF defense; submit from the lens UI itself",
        },
        status=403,
    )


def _media_disabled() -> web.Response:
    return web.json_response(
        {"error": "media is disabled", "hint": _MEDIA_DISABLED_HINT},
        status=404,
    )


def _media_not_found() -> web.Response:
    return web.json_response(
        {
            "error": "unknown media token",
            "hint": "the upload may have expired, been evicted, or the URL is wrong",
        },
        status=404,
    )


async def _read_bounded_body(request: web.Request) -> bytes | None:
    """Read the request body via ``request.content``, bailing out as
    soon as more than ``_MAX_INPUT_BODY`` bytes have arrived.

    ``client_max_size`` on the Application is now sized for media
    uploads (see ``make_app``), so it no longer bounds ``/input``'s own
    4 KiB contract by itself — a plain ``await request.read()`` would
    happily buffer a multi-megabyte chunked (no ``Content-Length``)
    body in memory before any in-handler check ran. Streaming through
    ``request.content`` in bounded chunks with a running counter keeps
    memory use capped at roughly ``_MAX_INPUT_BODY +
    _STREAM_CHUNK_SIZE`` regardless of how large the framework-level
    cap is. Returns ``None`` on overflow (caller returns ``_too_large()``).
    """
    total = 0
    parts: list[bytes] = []
    while True:
        chunk = await request.content.read(_STREAM_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_INPUT_BODY:
            return None
        parts.append(chunk)
    return b"".join(parts)


async def _iter_field_chunks(field: aiohttp.BodyPartReader) -> AsyncIterator[bytes]:
    """Stream a multipart field's body in bounded chunks.

    Feeds :meth:`MediaStore.save`'s ``chunks`` parameter — the store
    enforces the per-file cap while consuming this iterator, so an
    oversize upload is rejected mid-stream rather than after being
    buffered whole.
    """
    while True:
        chunk = await field.read_chunk(size=_STREAM_CHUNK_SIZE)
        if not chunk:
            break
        yield chunk


@allows_anonymous
async def get_index(request: web.Request) -> web.Response:
    """Render the three-pane page (or, for anonymous visitors, the entry card).

    When a current channel is set, pull HISTORY RECENT first so the chat
    pane isn't blank on a page reload. The Culture IRCd persists messages
    in SQLite and the query is cheap; on timeout or transport failure we
    degrade to an empty log rather than 500ing the page.
    """
    if request["identity"].tier == TIER_ANONYMOUS:
        # Imported lazily: the entry card is its own module (guest mode).
        from irc_lens.web import entry

        return await entry.get_entry(request)
    gated = await _guest_consent_gate(request, redirect=True)
    if gated is not None:
        return gated
    session = await _resolve_session(request)
    # `chat_log_html=None` lets `render_index` fall back to the
    # `MessageBuffer` — which is the seed-loader path. We only override
    # to a string when the live IRCd query actually ran, so `--seed`
    # preloaded fixtures still render on the initial page render.
    chat_log_html: str | None = None
    channel = session.current_channel
    # Both `connected` (post-welcome) and `healthy` (no broken pipe) must
    # hold — a pre-welcome session would block for QUERY_TIMEOUT (10s) on
    # the HISTORY query because AgentIRC ignores custom verbs from
    # unregistered clients.
    if channel and session.healthy and session.connected:
        try:
            entries = await session.history(channel, limit=50)
        except LensConnectionLost:
            # Page render shouldn't break on transport loss; the
            # connection-status indicator + 503 on next /input will
            # surface the problem to the user. Fall back to the buffer
            # rather than forcing a blank pane.
            entries = None
        except Exception:
            logger.exception("history fetch for %s during GET / failed", channel)
            entries = None
        if entries is not None:
            # Thread the session's media-embed state through exactly as
            # `session.py`'s `_publish_chat`/`_fetch_and_publish_history`
            # and `render_index`'s own buffer-fallback branch do — a
            # gap flagged by t2: this explicit history-fetch branch
            # previously called `render_chat_log(entries)` with no
            # kwargs, so a page reload after a live HISTORY query would
            # silently drop `.lens-media` embeds that a reload hitting
            # the buffer-fallback branch would have rendered.
            chat_log_html = render_chat_log(
                entries,
                media_embed_prefixes=session.media_embed_prefixes,
                media_remote_embeds=session.media_remote_embeds,
            )
    _, backend = _backend_for(request)
    presence = metrics.get_presence().state() if backend == BACKEND_SANDBOX else None
    body = render_index(session, chat_log_html=chat_log_html, presence=presence)
    return web.Response(text=body, content_type="text/html")


@allows_anonymous
async def get_presence(request: web.Request) -> web.Response:
    """Agent-state fragment for sandbox sessions (polled by htmx).

    Guest and sandbox-preview sessions only; a real-mesh session gets an
    empty fragment. Guests need it too, so it is not owner/approved gated,
    but it still requires a verified identity (and consent for guests).
    """
    gated = await _anonymous_or_gate(request, redirect=False)
    if gated is not None:
        return gated
    _, backend = _backend_for(request)
    presence = metrics.get_presence().state() if backend == BACKEND_SANDBOX else None
    body = render_fragment("_presence.html.j2", presence=presence)
    return web.Response(
        text=body, content_type="text/html", headers={"Cache-Control": "no-store"}
    )


async def _extract_text(request: web.Request) -> tuple[str | None, web.Response | None]:
    """Pull the ``text`` field out of either a JSON or form-encoded body.

    Returns ``(text, None)`` on success and ``(None, error_response)`` on
    a body we can't parse. Empty body → ``("", None)`` (no-op upstream).
    """
    raw = await _read_bounded_body(request)
    if raw is None:
        return None, _too_large()
    if not raw:
        return "", None
    content_type = (request.content_type or "").lower()
    if content_type == "application/json":
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, web.json_response(
                {"error": "invalid JSON body", "hint": str(exc)},
                status=400,
            )
        text = body.get("text", "") if isinstance(body, dict) else ""
        return str(text), None
    # Default: treat as form-encoded. HTMX submits form fields with
    # `application/x-www-form-urlencoded` by default; the shipped
    # `index.html.j2` form sends a `text=` field that way. Parsed
    # ourselves (mirroring aiohttp's own `Request.post()` urlencoded
    # branch) rather than via `request.post()`, which would try to
    # re-read `request.content` — already fully drained by
    # `_read_bounded_body` above.
    charset = request.charset or "utf-8"
    form = dict(
        urllib.parse.parse_qsl(
            raw.decode(charset, errors="replace"),
            keep_blank_values=True,
            encoding=charset,
        )
    )
    return str(form.get("text", "")), None


@allows_anonymous
async def post_input(request: web.Request) -> web.Response:
    """Parse one user input line and dispatch it through the session."""
    # Cheap header-only check first; saves a body read when the client
    # was honest about the Content-Length.
    if request.content_length is not None and request.content_length > _MAX_INPUT_BODY:
        return _too_large()

    # CSRF defense floor: reject cross-origin POSTs. Origin-absent requests
    # (curl, cloudflared probes, internal monitors) pass through unchanged.
    # A full CSRF-token scheme is tracked in issue #27.
    if not _origin_ok(request):
        return _origin_denied_response(request)

    gated = await _anonymous_or_gate(request, redirect=False)
    if gated is not None:
        return gated

    session = await _resolve_session(request)
    # Health gate before parsing: once the AgentIRC pipe is gone, the
    # spec mandates 503 on subsequent input rather than silently
    # no-oping (which is what `IRCTransport.send_raw` would do — its
    # `_writer` is None after disconnect).
    if not session.healthy:
        return _connection_lost("session unhealthy")

    text, err = await _extract_text(request)
    if err is not None:
        return err
    if not text:
        return web.Response(status=204)

    parsed = parse_command(text)
    refused = _sandbox_policy(request, text, parsed)
    if refused is not None:
        return refused
    if _is_sandbox_enter(request, parsed, text):
        request.app["sandbox_toggle"].add(request["identity"].principal)
        return web.Response(status=204, headers={"HX-Refresh": "true"})
    try:
        await session.execute(parsed)
    except LensConnectionLost as exc:
        return _connection_lost(str(exc))
    identity: Identity = request["identity"]
    if identity.tier == TIER_GUEST and parsed.type == CommandType.CHAT:
        # The export task reads these back for the guest's data export.
        request.app["guest_store"].record_input(
            identity.principal, kind="message", payload=text
        )
    return web.Response(status=204)


#: Rate-limit bucket / window for guest chat input (config:
#: ``guest_mode.rate_limits.messages_per_min``).
_MESSAGE_RATE_KIND = "message"
_MESSAGE_RATE_WINDOW_S = 60


def _is_sandbox_enter(request: web.Request, parsed: ParsedCommand, text: str) -> bool:
    """``/sandbox`` typed by an approved user on the mesh (guest mode on)."""
    identity: Identity = request["identity"]
    return (
        parsed.type == CommandType.UNKNOWN
        and text.strip().lower() == SANDBOX_COMMAND
        and identity.is_approved
        and request.app["config"].guest_enabled
        and identity.principal not in request.app["sandbox_toggle"]
    )


def _sandbox_policy(
    request: web.Request, text: str, parsed: ParsedCommand
) -> web.Response | None:
    """Server-side guest policy for ``POST /input`` (approved deviation d4).

    Applies to sessions the VERIFIED tier routed to the sandbox:

    * (b) guests only: per-guest message rate limit
      (``guest_rate_messages_per_min``) -> 429. Counted per guest email and
      per IP over a 60 s window; every non-empty input counts, refused
      commands included, so probing cannot be free.
    * (a) allowlist: only commands that work in the sandbox
      (:data:`irc_lens.commands.SANDBOX_ALLOWED`) are run; anything else is
      refused with 403 before it can reach IRC.
    """
    identity: Identity = request["identity"]
    if _is_sandbox_enter(request, parsed, text):
        return None
    _, backend = _backend_for(request)
    if backend != BACKEND_SANDBOX:
        return None
    if identity.tier == TIER_GUEST and _message_rate_limited(request, identity):
        metrics.get_metrics().rate_limited()
        return web.json_response(
            {"error": "Slow down", "hint": "Too many messages"},
            status=429,
            headers={"Retry-After": str(_MESSAGE_RATE_WINDOW_S)},
        )
    if not allowed_in_sandbox(parsed):
        return _json_error(403, "Not in guest view", "Try /help")
    return None


def _message_rate_limited(request: web.Request, identity: Identity) -> bool:
    """Check per-email and per-IP buckets, then count this message."""
    store = request.app["guest_store"]
    limit = request.app["config"].guest_rate_messages_per_min
    keys = (f"e:{identity.principal}", f"i:{client_ip(request)}")
    limited = any(
        store.rate_limited(
            _MESSAGE_RATE_KIND, k, limit=limit, window=_MESSAGE_RATE_WINDOW_S
        )
        for k in keys
    )
    if not limited:
        for k in keys:
            store.record_attempt(_MESSAGE_RATE_KIND, k)
    return limited


async def post_upload(request: web.Request) -> web.Response:
    """Stream a ``multipart/form-data`` ``file`` field into the media
    store and return its capability URL.

    Identity-gated exactly like ``post_input``: the auth middleware has
    already stashed ``request["identity"]`` before this handler runs
    (``/upload`` is not on either middleware's exemption list), so no
    explicit auth check happens here — only the read of
    ``request["identity"]`` itself, mirroring ``_resolve_session``.
    Origin-checked with the same ``_origin_ok`` CSRF floor as
    ``post_input``. See docs/superpowers/specs/
    2026-07-02-media-support-design.md ("Upload path").
    """
    config = request.app["config"]
    if not config.media_enabled:
        return _media_disabled()

    if not _origin_ok(request):
        return _origin_denied_response(request)

    if not (request.content_type or "").lower().startswith("multipart/"):
        return web.json_response(
            {
                "error": "expected a multipart/form-data body",
                "hint": 'POST the file as multipart/form-data with a "file" field',
            },
            status=400,
        )

    try:
        reader = await request.multipart()
    except ValueError:
        return web.json_response(
            {
                "error": "malformed multipart body",
                "hint": "the request must be multipart/form-data with a boundary",
            },
            status=400,
        )

    field: aiohttp.BodyPartReader | None = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if isinstance(part, aiohttp.BodyPartReader) and part.name == "file":
            field = part
            break

    if field is None:
        return web.json_response(
            {
                "error": 'missing "file" field',
                "hint": 'upload the file under a multipart field named "file"',
            },
            status=400,
        )

    identity = request["identity"]
    store = request.app["media_store"]
    filename = field.filename or ""
    try:
        stored = await store.save(
            identity.principal, filename, _iter_field_chunks(field)
        )
    except MediaTooLargeError as exc:
        return web.json_response({"error": exc.message, "hint": exc.hint}, status=413)
    except MediaTypeError as exc:
        return web.json_response({"error": exc.message, "hint": exc.hint}, status=400)

    base = request.app["media_base"]
    return web.json_response(
        {"url": f"{base}/media/{stored.token}.{stored.ext}", "kind": stored.kind},
        status=201,
    )


async def get_media(request: web.Request) -> web.Response:
    """Serve a previously uploaded blob by its ``<token>.<ext>`` capability.

    Auth-exempt by design — see the exemption lists in ``web/app.py``
    (dev mode) and ``web/auth.py`` (cloudflare-access mode). The
    unguessable token *is* the credential (design doc: "Why capability
    URLs on /media/"), so no ``request["identity"]`` lookup happens
    here and none is required.

    ``store.resolve`` walks the store directory tree (``iterdir`` +
    ``is_file``/``stat``) — real blocking filesystem work, not a token
    await — so it's offloaded via ``asyncio.to_thread`` rather than
    called directly on the event loop (SonarCloud S7503: an ``async
    def`` handler should actually do async/awaited work).
    """
    config = request.app["config"]
    if not config.media_enabled:
        return _media_disabled()

    store = request.app["media_store"]
    name = request.match_info["name"]
    path = await asyncio.to_thread(store.resolve, name)
    if path is None:
        return _media_not_found()

    ext = name.rsplit(".", 1)[-1].lower()
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
            # Private (not shared-cache) since the token itself is the
            # capability — an intermediary caching this response keyed
            # only on URL would be fine, but "private" keeps a shared
            # proxy from persisting the blob past the token's lifetime.
            # Immutable: the token names an exact, never-mutated blob,
            # so a client never needs to revalidate a cached copy.
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Disposition": "inline",
        },
    )


@allows_anonymous
async def get_events(request: web.Request) -> web.StreamResponse:
    """SSE stream — drains ``Session.event_bus`` until the client leaves."""
    gated = await _anonymous_or_gate(request, redirect=False)
    if gated is not None:
        return gated
    session = await _resolve_session(request)
    sub = session.event_bus.subscribe()
    # If this session is already in the mesh view (e.g. a page reload while
    # viewing the graph), push the current snapshot straight away so the
    # canvas paints without waiting for the next refresher tick. Gated on
    # the view so clients looking at chat/help/etc don't receive mesh
    # events they won't render.
    if session.view == "mesh":
        session.request_mesh_refresh()
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            # `no-store` (not `no-cache`) — SSE responses must never be
            # held by intermediaries, since clients reconnect on
            # connection drop and a cached response would replay stale
            # state and never close.
            "Cache-Control": "no-store",
            # Disable proxy buffering (nginx, et al.) so events flush
            # in real time instead of being held until the response
            # body grows large enough to break a buffer threshold.
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    try:
        async for event in sub.events():
            try:
                await response.write(format_sse(event))
            except ConnectionError:
                # Client closed the SSE connection. `ConnectionResetError`,
                # `BrokenPipeError`, and friends are all `ConnectionError`
                # subclasses — the bare parent catches every variant
                # without an S5713-flagged ladder.
                break
    finally:
        sub.close()
    return response


async def get_residents(request: web.Request) -> web.Response:
    """Render the standalone residents page (culture's resource view).

    Server-side fetch by design: the browser talks only to the console,
    and culture's overview endpoint (loopback-only, ephemeral port —
    resolved via ``resolve_residents_url``) is queried from this
    process. The overview name falls back to ``server.name`` when
    ``culture.overview_name`` is unset, matching culture's
    ``overview-{server_name}.port`` pidfile convention.

    Deliberately NOT the ``{error, hint}`` JSON contract of the POST
    endpoints: every outcome — unreachable IRCd, presence not yet
    supported, unconfigured or dead overview server — renders HTTP 200
    with a kind-specific notice. The graceful-degrade requirement
    (docs/specs/2026-07-07-residents-presence-page.md) exists because a
    down mesh is exactly when the operator opens this page; a 5xx here
    would repeat the 2026-07-03 console-500 incident.

    Touches no ``Session`` — this page reads culture's HTTP surface,
    not the AgentIRC connection.
    """
    config = request.app["config"]
    # The resolver reads the overview port file — a blocking read that
    # stays off the event loop like every other file read in this app
    # (the `store.resolve` / `precompute_static_hashes` convention).
    url = await asyncio.to_thread(
        resolve_residents_url,
        config.culture_residents_url,
        config.culture_overview_name or config.server_name,
    )
    if url is None:
        result = ResidentsResult("unavailable", None)
    else:
        result = await fetch_residents(url)
    try:
        body = render_residents_page(result.kind, result.payload)
    except Exception:
        # Backstop for the never-an-error-page contract: a payload that
        # slipped past fetch-layer validation still degrades to the
        # unavailable notice rather than a 500.
        logger.exception("residents render failed; degrading to notice")
        body = render_residents_page("unavailable", None)
    return web.Response(text=body, content_type="text/html")


async def get_healthz(_request: web.Request) -> web.Response:  # NOSONAR S7503
    """Opaque health probe. No auth, no IRC state, no allowlist leak.

    The body is sync but the signature must be ``async def`` —
    aiohttp's router only accepts coroutine handlers. Sonar's S7503
    ("use async features or remove the keyword") doesn't know about
    that constraint; the inline ``# NOSONAR S7503`` silences it.
    """
    return web.json_response({"ok": True})


async def get_owner_metrics(request: web.Request) -> web.Response:  # NOSONAR S7503
    """Owner-only guest-mode counters + sandbox-agent presence (JSON).

    Approved tier only. Deliberately NOT ``allows_anonymous``: the auth
    middleware already 401s anonymous requests; the tier check here also
    refuses a ``guest`` identity with the same standard denial.
    """
    identity = request.get("identity")
    if identity is None or not identity.is_approved:
        return web.json_response(
            {
                "error": "approved sign-in required",
                "hint": "sign in through /login to reach the real mesh",
            },
            status=401,
        )
    return web.json_response(
        {
            "counters": metrics.get_metrics().snapshot(),
            "agent": metrics.get_presence().state(),
        }
    )
