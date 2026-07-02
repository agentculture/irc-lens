"""Task t8 — server-side proof of the "one send path" contract.

The build plan's first acceptance criterion for t8 (console upload UI)
requires that the attach button, drag-drop, and paste surfaces all POST
the file to ``/upload`` and then submit the returned URL through the
*existing* ``POST /input`` pipeline — never a second, bespoke send path.
``tests/test_media_js.py`` pins the client-side half of that contract
(``media.js`` calls ``form.requestSubmit()``, never fetches ``/input``
directly). This module pins the other half: the exact sequence the
browser performs — upload, then submit the returned URL as chat text —
must actually reach the IRC wire as an ordinary ``PRIVMSG``.

Kept in its own file (rather than appended to ``tests/test_e2e_http.py``,
which task t10 also extends) so the two tasks' TDD-gated worktrees don't
collide on the same file.
"""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp.test_utils import TestClient

from _agentirc_server import AgentIRCTestServer, _ReceivedLine

# Minimal but valid PNG: magic bytes + padding, matching the fixture
# `tests/test_upload_routes.py` (task t6) already established for the
# store's magic-byte sniff.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


async def _wait_for_received(
    server: AgentIRCTestServer,
    command: str,
    *params: str,
    timeout: float = 1.0,
) -> _ReceivedLine:
    """Poll ``server.received`` until a matching line appears.

    Copied from ``tests/test_e2e_http.py`` (same pattern, same
    rationale) rather than imported, to keep this file self-contained
    and avoid a cross-task import dependency on a file t10 also owns.
    """

    async def _poll() -> _ReceivedLine:
        while True:
            for line in server.received:
                if line.command == command and list(params) == line.params[: len(params)]:
                    return line
            await asyncio.sleep(0.01)

    try:
        async with asyncio.timeout(timeout):
            return await _poll()
    except TimeoutError as exc:
        raise AssertionError(
            f"timed out after {timeout}s waiting for {command} {list(params)} — "
            f"server received: {[(line.command, line.params) for line in server.received]}"
        ) from exc


async def test_upload_then_input_sends_url_as_privmsg(
    lens_client: TestClient, agentirc_server: AgentIRCTestServer
) -> None:
    """Reproduce exactly what ``media.js``'s ``uploadAndSend()`` does at
    the HTTP layer: POST /upload, take the returned capability URL, then
    POST it to /input as chat text (the same request shape
    ``form.requestSubmit()`` produces for a typed message). The URL must
    land on the wire as an ordinary PRIVMSG — proving upload and typed
    chat share one pipeline, not two."""
    await lens_client.post("/input", json={"text": "/join #media"})
    await _wait_for_received(agentirc_server, "JOIN", "#media")

    form = aiohttp.FormData()
    form.add_field("file", PNG_BYTES, filename="pic.png", content_type="image/png")
    upload_resp = await lens_client.post("/upload", data=form)
    assert upload_resp.status == 201
    upload_body = await upload_resp.json()
    url = upload_body["url"]
    assert upload_body["kind"] == "image"
    assert url.endswith(".png")

    input_resp = await lens_client.post("/input", json={"text": url})
    assert input_resp.status == 204

    line = await _wait_for_received(agentirc_server, "PRIVMSG", "#media")
    assert line.params == ["#media", url]


async def test_upload_then_input_matches_form_encoded_submission(
    lens_client: TestClient, agentirc_server: AgentIRCTestServer
) -> None:
    """`form.requestSubmit()` on `#chat-form` (no `hx-encoding`
    override) submits `application/x-www-form-urlencoded`, exactly like
    a typed message — not JSON. Pin that the uploaded URL also flows
    correctly over that exact content type, since that's what actually
    leaves the browser."""
    await lens_client.post("/input", json={"text": "/join #media2"})
    await _wait_for_received(agentirc_server, "JOIN", "#media2")

    form = aiohttp.FormData()
    form.add_field("file", PNG_BYTES, filename="pic2.png", content_type="image/png")
    upload_resp = await lens_client.post("/upload", data=form)
    assert upload_resp.status == 201
    url = (await upload_resp.json())["url"]

    input_resp = await lens_client.post(
        "/input",
        data={"text": url},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert input_resp.status == 204

    line = await _wait_for_received(agentirc_server, "PRIVMSG", "#media2")
    assert line.params == ["#media2", url]
