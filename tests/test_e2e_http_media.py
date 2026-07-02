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

from irc_lens.irc import Message
from irc_lens.session import Session

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


# ---------------------------------------------------------------------------
# Task t10 — the full media proof chain in ONE test.
#
# t8's tests above pin the first two links (upload → returned URL → PRIVMSG on
# the wire). The gap t10 fills is the *third* link: the URL that just left on
# the wire must also come back out as a rendered chat fragment carrying the
# lens-media embed markup for that URL — the reactive surface the browser
# actually paints. These tests close that loop entirely over loopback, with
# no external host: the `media_hosted_lens_client` fixture points the upload
# base and the session's embed prefixes at the TestServer's own origin, so an
# uploaded (lens-hosted) URL renders as a *direct* `<img>` embed rather than a
# click-to-load placeholder.
# ---------------------------------------------------------------------------


def _chat_frame_containing(buf: bytes, needle: bytes) -> bytes | None:
    """Return the first *complete* ``event: chat`` SSE frame in ``buf`` that
    contains ``needle``, or ``None`` if none has fully arrived yet.

    ``format_sse`` emits ``event: chat`` then one ``data:`` line per line of
    the rendered fragment, terminated by a blank line (``\\n\\n``). A chunk
    boundary can split a frame mid-payload, so only frames whose terminator
    has arrived are inspected — matching the frame-completeness guard
    ``tests/test_e2e_http.py`` uses for the ``mesh`` event.
    """
    start = 0
    while True:
        i = buf.find(b"event: chat", start)
        if i == -1:
            return None
        end = buf.find(b"\n\n", i)
        if end == -1:
            return None  # frame not fully arrived — caller reads more bytes
        frame = buf[i:end]
        if needle in frame:
            return frame
        start = end + 2


async def _collect_chat_media_frame(
    client: TestClient, needle: bytes, *, timeout: float = 3.0
) -> bytes:
    """Open ``GET /events`` and read until a ``chat`` frame carrying ``needle``
    lands, returning that frame. Raises ``AssertionError`` on timeout with the
    bytes seen so far, so a broken render surfaces as a clean diff."""
    resp = await client.get("/events")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    buf = b""
    try:
        async with asyncio.timeout(timeout):
            while True:
                frame = _chat_frame_containing(buf, needle)
                if frame is not None:
                    return frame
                chunk = await resp.content.read(1024)
                if not chunk:
                    raise AssertionError(
                        f"SSE stream closed before a chat frame with {needle!r} — "
                        f"received: {buf!r}"
                    )
                buf += chunk
    except TimeoutError as exc:
        raise AssertionError(
            f"timed out after {timeout}s waiting for a chat frame with {needle!r} — "
            f"received: {buf!r}"
        ) from exc
    finally:
        resp.close()


async def test_full_chain_upload_to_wire_to_lens_media_embed(
    media_hosted_lens_client: TestClient,
    agentirc_server: AgentIRCTestServer,
    lens_session: Session,
) -> None:
    """The whole media chain in one automated test, entirely over loopback:

    ``POST /upload`` (real bytes) → ``201`` capability URL → ``POST /input``
    that URL → the URL lands on the fake-IRCd wire as an ordinary ``PRIVMSG``
    → the local-echo ``chat`` SSE fragment for that send carries the
    lens-media *embed* markup (`data-testid="media-embed"` with `src=<url>`)
    for the uploaded, lens-hosted URL.

    This is exactly what a user upload does end to end: the same
    ``POST /input`` that puts the URL on the wire also publishes the chat
    fragment the browser paints. No external host is involved — the fixture
    pins the upload base and embed prefixes to the server's own origin.
    """
    client = media_hosted_lens_client

    # 1. Join a channel so free-text chat has an active target, and confirm
    #    the JOIN reached the wire.
    await client.post("/input", json={"text": "/join #chain"})
    await _wait_for_received(agentirc_server, "JOIN", "#chain")

    # 2. Upload real bytes; take the returned lens-hosted capability URL.
    form = aiohttp.FormData()
    form.add_field("file", PNG_BYTES, filename="chain.png", content_type="image/png")
    upload_resp = await client.post("/upload", data=form)
    assert upload_resp.status == 201
    url = (await upload_resp.json())["url"]
    assert "/media/" in url and url.endswith(".png")
    # The fixture repointed the upload base at the server origin, so the URL
    # is lens-hosted (prefix-matched) and will render as a direct embed.
    assert url.startswith(str(client.make_url("/")).rstrip("/") + "/media/")

    # 3. Open the SSE stream and wait until the handler has actually
    #    registered on the bus before publishing — closes the subscribe race.
    collector = asyncio.create_task(
        _collect_chat_media_frame(client, b'data-testid="media-embed"')
    )
    async with asyncio.timeout(1.0):
        while lens_session.event_bus.subscriber_count == 0:
            await asyncio.sleep(0.005)

    # 4. Send the URL as chat text; it must reach the wire as a PRIVMSG.
    input_resp = await client.post("/input", json={"text": url})
    assert input_resp.status == 204
    line = await _wait_for_received(agentirc_server, "PRIVMSG", "#chain")
    assert line.params == ["#chain", url]

    # 5. The chat fragment on the SSE stream carries the embed markup for the
    #    URL — the third link the t8 tests never asserted.
    frame = await collector
    assert b'data-testid="media-embed"' in frame
    assert f'src="{url}"'.encode() in frame
    assert b'data-testid="media-placeholder"' not in frame


async def test_inbound_media_privmsg_renders_embed_fragment(
    media_hosted_lens_client: TestClient,
    agentirc_server: AgentIRCTestServer,
    lens_session: Session,
) -> None:
    """The received-message half of the chain: a lens-hosted media URL that
    arrives *inbound* from a peer (not the lens's own echo) must render the
    embed fragment too.

    Exercises a genuinely different code path from the local-echo test above
    — ``Session.dispatch`` → ``_dispatch_privmsg`` rather than ``_exec_chat``
    — which the t8 suite never touches for media. Inbound traffic is injected
    via ``Session.dispatch`` (one of the two options the plan calls out;
    ``_dispatch_privmsg`` drops the lens's own nick, so a genuine peer sender
    is required here rather than replaying the wire echo)."""
    client = media_hosted_lens_client

    await client.post("/input", json={"text": "/join #inbound"})
    await _wait_for_received(agentirc_server, "JOIN", "#inbound")

    # Upload real bytes so the URL names a blob the /media/ route can serve;
    # the returned URL is lens-hosted (fixture-pinned prefix) → direct embed.
    form = aiohttp.FormData()
    form.add_field("file", PNG_BYTES, filename="peer.png", content_type="image/png")
    upload_resp = await client.post("/upload", data=form)
    assert upload_resp.status == 201
    url = (await upload_resp.json())["url"]

    collector = asyncio.create_task(
        _collect_chat_media_frame(client, b'data-testid="media-embed"')
    )
    async with asyncio.timeout(1.0):
        while lens_session.event_bus.subscriber_count == 0:
            await asyncio.sleep(0.005)

    # A peer posts the media URL into the active channel.
    await lens_session.dispatch(
        Message(prefix="peer!peer@test", command="PRIVMSG", params=["#inbound", url])
    )

    frame = await collector
    assert b'data-testid="media-embed"' in frame
    assert f'src="{url}"'.encode() in frame
