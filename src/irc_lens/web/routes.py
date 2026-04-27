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

from aiohttp import web

from irc_lens.commands import parse_command
from irc_lens.session import LensConnectionLost
from irc_lens.web.events import format_sse
from irc_lens.web.render import render_index

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


async def get_index(request: web.Request) -> web.Response:
    session = request.app["session"]
    body = render_index(session)
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

    session = request.app["session"]
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
    session = request.app["session"]
    sub = session.event_bus.subscribe()
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
