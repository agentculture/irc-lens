"""HTTP route handlers for irc-lens.

Phase 4 surface (skeleton):

* ``GET /``        — render the three-pane index from Session state.
* ``POST /input``  — accept a JSON command body and return 204
  (Phase 5 wires it through `Session.execute`).
* ``GET /events``  — SSE stub: emit one ``chat`` event then EOF
  (Phase 5 swaps in the real `SessionEventBus.subscribe()` loop).

Static files (`/static/*`) are wired via `app.router.add_static` in
:mod:`irc_lens.web.app`, not as a handler here.
"""

from __future__ import annotations

import logging

from aiohttp import web

from irc_lens.web.render import render_index

logger = logging.getLogger(__name__)


async def get_index(request: web.Request) -> web.Response:
    session = request.app["session"]
    body = render_index(session)
    return web.Response(text=body, content_type="text/html")


async def post_input(request: web.Request) -> web.Response:
    """Stub: accept and discard a JSON command body, return 204.

    The 204 keeps HTMX from trying to swap anything from the response —
    all visible updates flow through SSE. Phase 5 parses the body via
    `parse_command` and dispatches through `Session.execute`.
    """
    # Drain the body so the client sees a clean close even if it sent
    # JSON. Don't validate shape yet — that lands with the dispatcher.
    if request.body_exists:
        await request.read()
    return web.Response(status=204)


async def get_events(request: web.Request) -> web.StreamResponse:
    """SSE stub: emit one ``chat`` event saying ``irc-lens online``, EOF.

    Phase 5 replaces this with `bus.subscribe()` -> stream until client
    disconnect. Keeping the stub one-shot for Phase 4 means the manual
    smoke (`curl -N /events`) returns deterministically without
    blocking.
    """
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    await response.write(b"event: chat\ndata: irc-lens online\n\n")
    await response.write_eof()
    return response
