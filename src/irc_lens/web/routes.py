"""HTTP route handlers for irc-lens.

* ``GET /``        — render the three-pane index from Session state.
* ``POST /input``  — parse the JSON body via ``parse_command`` and
  dispatch through ``Session.execute``. Returns ``204`` on success,
  ``413`` if the body exceeds the bounded-memory limit, ``400`` for
  invalid JSON, ``503`` when the underlying IRC connection is gone.
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

# 4 KiB upper bound on the JSON body. Slash-commands and chat lines
# are both well under this; the cap is a bounded-memory contract from
# the spec and a cheap defence against accidental floods.
_MAX_INPUT_BODY = 4096


async def get_index(request: web.Request) -> web.Response:
    session = request.app["session"]
    body = render_index(session)
    return web.Response(text=body, content_type="text/html")


async def post_input(request: web.Request) -> web.Response:
    """Parse one user input line and dispatch it through the session."""
    # Cheap check via Content-Length when the client supplied it; saves
    # us reading a multi-megabyte body just to reject it.
    if request.content_length is not None and request.content_length > _MAX_INPUT_BODY:
        return web.json_response(
            {"error": "input too large", "hint": f"max {_MAX_INPUT_BODY} bytes"},
            status=413,
        )
    raw = await request.read()
    if len(raw) > _MAX_INPUT_BODY:
        return web.json_response(
            {"error": "input too large", "hint": f"max {_MAX_INPUT_BODY} bytes"},
            status=413,
        )
    if not raw:
        return web.Response(status=204)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        return web.json_response(
            {"error": "invalid JSON body", "hint": str(exc)},
            status=400,
        )
    text = body.get("text", "") if isinstance(body, dict) else ""
    parsed = parse_command(text)
    session = request.app["session"]
    try:
        await session.execute(parsed)
    except LensConnectionLost as exc:
        return web.json_response(
            {
                "error": str(exc),
                "hint": (
                    "AgentIRC connection lost — restart irc-lens to reconnect "
                    "(no auto-reconnect in v1)."
                ),
            },
            status=503,
        )
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
            except (ConnectionResetError, ConnectionError):
                # Client closed the SSE connection — unwind cleanly
                # rather than letting the error escape into aiohttp's
                # access log as an unhandled exception.
                break
    finally:
        sub.close()
    return response
