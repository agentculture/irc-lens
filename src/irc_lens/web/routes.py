"""HTTP route handlers for irc-lens.

* ``GET /``        — render the three-pane index from Session state.
* ``POST /input``  — read the input line (JSON or form-encoded body),
  parse via ``parse_command``, dispatch through ``Session.execute``.
  Returns ``204`` on success, ``413`` if the body exceeds the
  bounded-memory limit (also enforced by ``client_max_size`` in
  ``make_app``), ``400`` for invalid JSON, ``503`` when the session
  is unhealthy or AgentIRC is unreachable.
* ``GET /events``  — open an SSE stream from
  ``Session.event_bus.subscribe()``; flushes each event through
  ``format_sse``. Closes the subscription cleanly on client disconnect.

Static files (``/static/*``) are wired via ``app.router.add_static`` in
:mod:`irc_lens.web.app`, not as a handler here.
"""

from __future__ import annotations

import json
import logging
import urllib.parse

from aiohttp import web

from irc_lens.commands import parse_command
from irc_lens.session import LensConnectionLost
from irc_lens.web.events import format_sse
from irc_lens.web.render import render_chat_log, render_index

logger = logging.getLogger(__name__)

# 4 KiB upper bound on the input body. Slash-commands and chat lines
# are both well under this; the cap is a bounded-memory contract from
# the spec and a cheap defence against accidental floods. The same
# value is passed to ``web.Application(client_max_size=...)`` in
# ``make_app`` so aiohttp rejects oversize requests *before* any
# handler runs (covers chunked / no-Content-Length transfers that
# would otherwise buffer the whole body before the in-handler check).
_MAX_INPUT_BODY = 4096

_UNHEALTHY_HINT = (
    "AgentIRC connection lost — restart irc-lens to reconnect "
    "(no auto-reconnect in v1)."
)


async def _resolve_session(request: web.Request):
    """Look up (or lazily open) the Session for this request's identity."""
    identity = request["identity"]
    registry = request.app["registry"]
    return await registry.get_or_open(identity)


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


async def get_index(request: web.Request) -> web.Response:
    """Render the three-pane page.

    When a current channel is set, pull HISTORY RECENT first so the chat
    pane isn't blank on a page reload. The Culture IRCd persists messages
    in SQLite and the query is cheap; on timeout or transport failure we
    degrade to an empty log rather than 500ing the page.
    """
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
            chat_log_html = render_chat_log(entries)
    body = render_index(session, chat_log_html=chat_log_html)
    return web.Response(text=body, content_type="text/html")


async def _extract_text(request: web.Request) -> tuple[str | None, web.Response | None]:
    """Pull the ``text`` field out of either a JSON or form-encoded body.

    Returns ``(text, None)`` on success and ``(None, error_response)`` on
    a body we can't parse. Empty body → ``("", None)`` (no-op upstream).
    """
    raw = await request.read()
    # `client_max_size` already rejects oversize requests at the framework
    # level (returns 413 before we ever get called), but we keep an
    # in-handler bound for clarity / defence in depth.
    if len(raw) > _MAX_INPUT_BODY:
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
    # `index.html.j2` form sends a `text=` field that way.
    form = await request.post()
    return str(form.get("text", "")), None


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
        xfp = request.headers.get("X-Forwarded-Proto")
        forwarded_scheme = (
            xfp.lower().split(",")[0].strip() if xfp else request.url.scheme
        )
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
    try:
        await session.execute(parsed)
    except LensConnectionLost as exc:
        return _connection_lost(str(exc))
    return web.Response(status=204)


async def get_events(request: web.Request) -> web.StreamResponse:
    """SSE stream — drains ``Session.event_bus`` until the client leaves."""
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


async def get_healthz(_request: web.Request) -> web.Response:  # NOSONAR S7503
    """Opaque health probe. No auth, no IRC state, no allowlist leak.

    The body is sync but the signature must be ``async def`` —
    aiohttp's router only accepts coroutine handlers. Sonar's S7503
    ("use async features or remove the keyword") doesn't know about
    that constraint; the inline ``# NOSONAR S7503`` silences it.
    """
    return web.json_response({"ok": True})
