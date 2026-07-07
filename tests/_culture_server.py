"""Tiny aiohttp app that mimics culture's ``GET /residents.json`` for tests.

Mirrors the ``tests/_jwks_server.py`` pattern: an in-tree fake bound to
an ephemeral loopback port (no network egress) that tests point
``irc_lens.web.residents.fetch_residents`` at. Each test picks the one
scenario it needs with a ``serve_*`` call, then ``start()``s the
server — or skips ``start()``/calls :meth:`stop` to simulate a
connection refused.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web


class FakeCultureServer:
    """Serves ``/residents.json`` with a per-test-configured scenario.

    Public surface:
      - ``host`` / ``port`` / ``residents_url`` after :meth:`start`.
      - one ``serve_*`` call (before or after :meth:`start`) to pick the
        scenario; defaults to a supported payload with no residents.
      - :meth:`start` / :meth:`stop` lifecycle.
    """

    def __init__(self) -> None:
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._status: int = 200
        self._content_type: str = "application/json"
        self._body: bytes = b""
        self._delay_seconds: float = 0.0
        self.serve_supported()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def residents_url(self) -> str:
        return f"{self.base_url}/residents.json"

    def _set_json(self, status: int, payload: dict[str, Any]) -> None:
        self._status = status
        self._content_type = "application/json"
        self._body = json.dumps(payload).encode()

    def serve_supported(self, residents: list[dict[str, Any]] | None = None) -> None:
        """200, valid JSON, ``supported: true``."""
        self._set_json(
            200,
            {
                "supported": True,
                "generated_at": "2026-07-07T12:00:00Z",
                "residents": residents or [],
            },
        )

    def serve_unsupported(self) -> None:
        """200, valid JSON, ``supported: false`` — a known mesh state."""
        self._set_json(
            200,
            {
                "supported": False,
                "generated_at": "2026-07-07T12:00:00Z",
                "residents": [],
            },
        )

    def serve_unreachable(self) -> None:
        """503 with culture's structured ``{code, message, remediation}`` body."""
        self._set_json(
            503,
            {
                "code": 503,
                "message": "cannot connect to the culture server",
                "remediation": "check that the culture server is running",
            },
        )

    def serve_unreachable_garbled(self) -> None:
        """503 with a non-JSON body — the status alone must still classify
        as unreachable, per culture's contract that 503 always means
        "server unreachable or presence stream stalled"."""
        self._status = 503
        self._content_type = "text/plain"
        self._body = b"not json at all"

    def serve_internal_error(self) -> None:
        """500 with culture's structured error body."""
        self._set_json(
            500,
            {
                "code": 500,
                "message": "unexpected internal error",
                "remediation": "check the culture server logs",
            },
        )

    def serve_non_json(self) -> None:
        """200 with a body that isn't JSON at all."""
        self._status = 200
        self._content_type = "text/html"
        self._body = b"<html>not json</html>"

    def serve_missing_supported_key(self) -> None:
        """200, valid JSON, but no boolean ``supported`` key."""
        self._set_json(200, {"generated_at": "2026-07-07T12:00:00Z"})

    def serve_stalling(self, delay_seconds: float) -> None:
        """200 eventually, but only after sleeping ``delay_seconds`` —
        for timeout tests. Pair with a monkeypatched short client
        timeout so the test doesn't have to wait out the real one."""
        self._delay_seconds = delay_seconds
        self.serve_supported()

    async def start(self) -> None:
        app = web.Application()

        async def handler(_req: web.Request) -> web.Response:
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)
            return web.Response(
                body=self._body, status=self._status, content_type=self._content_type
            )

        app.router.add_get("/residents.json", handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, 0)
        await self._site.start()
        sockets = list(self._runner.addresses)
        if sockets:
            self.port = sockets[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
