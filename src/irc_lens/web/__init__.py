"""aiohttp web app for irc-lens.

Phase 4 ships the skeleton: ``make_app(session)`` returns a configured
``aiohttp.web.Application`` with ``GET /``, ``POST /input`` (204 stub),
``GET /events`` (one-shot SSE stub), and ``GET /static/*``. Phase 5
replaces the SSE stub with the real ``SessionEventBus.subscribe()``
loop and wires ``POST /input`` through ``Session.execute``.
"""

from __future__ import annotations

from irc_lens.web.app import make_app

__all__ = ["make_app"]
