"""HTTP tests for the Phase 5 SSE wiring.

What's covered:

* ``format_sse`` produces the right wire bytes (single line, multi-line,
  empty payload).
* ``GET /events`` opens a real stream that delivers events published
  into ``Session.event_bus`` in real time, and unwinds cleanly when
  the client closes.
* ``POST /input`` translates ``LensConnectionLost`` to HTTP 503.
* ``POST /input`` rejects oversize bodies with HTTP 413 (bounded-memory
  contract from the spec).
* ``POST /input`` rejects malformed JSON with HTTP 400.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.commands import ParsedCommand
from irc_lens.session import LensConnectionLost, Session, SessionEvent
from irc_lens.web import make_app
from irc_lens.web.events import format_sse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    return Session(host="127.0.0.1", port=6667, nick="lens-test")


@pytest.fixture
async def client(session: Session) -> TestClient:
    app = make_app(session)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


# ---------------------------------------------------------------------------
# format_sse
# ---------------------------------------------------------------------------


def test_format_sse_single_line() -> None:
    out = format_sse(SessionEvent(name="chat", data="hello"))
    assert out == b"event: chat\ndata: hello\n\n"


def test_format_sse_multi_line() -> None:
    """Each \\n in payload becomes a separate `data:` line."""
    out = format_sse(SessionEvent(name="roster", data="<ul>\n  <li>x</li>\n</ul>"))
    assert out == b"event: roster\ndata: <ul>\ndata:   <li>x</li>\ndata: </ul>\n\n"


def test_format_sse_empty_payload() -> None:
    """Empty data still emits one `data:` line so the terminator holds."""
    out = format_sse(SessionEvent(name="error", data=""))
    assert out == b"event: error\ndata:\n\n"


def test_format_sse_handles_crlf() -> None:
    """\\r\\n must be normalised the same as \\n."""
    out = format_sse(SessionEvent(name="chat", data="line1\r\nline2"))
    assert out == b"event: chat\ndata: line1\ndata: line2\n\n"


# ---------------------------------------------------------------------------
# GET /events — real subscriber loop
# ---------------------------------------------------------------------------


async def test_get_events_streams_published_event(
    session: Session, client: TestClient
) -> None:
    """Publishing into the bus surfaces on an open SSE connection."""
    resp = await client.get("/events")
    try:
        assert resp.status == 200
        # Yield once so the route's `async for` actually parks on the
        # subscriber queue before we publish — otherwise the publish
        # races the subscribe-then-await sequencing.
        await asyncio.sleep(0.01)
        session.event_bus.publish(SessionEvent(name="chat", data="hi there"))
        chunk = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), timeout=1.0)
        assert chunk == b"event: chat\ndata: hi there\n\n"
    finally:
        resp.close()


async def test_get_events_subscriber_unregisters_on_close(
    session: Session, client: TestClient
) -> None:
    """Closing the client tears down the subscription so the bus
    doesn't retain a dangling per-tab queue."""
    resp = await client.get("/events")
    await asyncio.sleep(0.01)
    assert session.event_bus.subscriber_count == 1
    resp.close()
    # Allow the route's `finally` to run.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if session.event_bus.subscriber_count == 0:
            break
    assert session.event_bus.subscriber_count == 0


# ---------------------------------------------------------------------------
# POST /input — error translation
# ---------------------------------------------------------------------------


async def test_post_input_503_on_lens_connection_lost(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_parsed: ParsedCommand) -> None:
        raise LensConnectionLost("broken pipe")

    monkeypatch.setattr(session, "execute", boom)
    app = make_app(session)
    async with TestClient(TestServer(app)) as c:
        resp = await c.post("/input", json={"text": "/join #x"})
        assert resp.status == 503
        body = await resp.json()
        assert body["error"] == "broken pipe"
        assert "hint" in body


async def test_post_input_413_on_oversize_body(client: TestClient) -> None:
    """Oversize JSON body — `client_max_size` on the Application drops
    it at the framework layer (returns 413 before the handler even
    runs), which is what we want for the bounded-memory contract.
    Body shape isn't asserted because the framework-level error has
    its own template, not our `{error, hint}` JSON."""
    big = {"text": "x" * 5000}
    resp = await client.post("/input", json=big)
    assert resp.status == 413


async def test_post_input_413_on_chunked_oversize_body(client: TestClient) -> None:
    """Chunked transfer (no Content-Length) > cap — also 413.

    Without `client_max_size`, the previous implementation would have
    buffered the whole body before checking size; the framework cap
    closes that hole.
    """

    async def chunked() -> object:
        # Five 1 KiB chunks = 5 KiB > 4 KiB cap.
        for _ in range(5):
            yield b"x" * 1024

    resp = await client.post(
        "/input",
        data=chunked(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 413


async def test_post_input_400_on_invalid_json(client: TestClient) -> None:
    """Bare bytes that aren't JSON → 400 with a hint."""
    resp = await client.post(
        "/input",
        data=b"this is not JSON",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid JSON body"


async def test_post_input_204_on_empty_text(client: TestClient) -> None:
    """Empty text is a no-op, not an error — the UI may submit a blank
    field on accidental Enter."""
    resp = await client.post("/input", json={"text": ""})
    assert resp.status == 204


async def test_post_input_accepts_form_encoded_body(
    session: Session, client: TestClient
) -> None:
    """HTMX submits forms as `application/x-www-form-urlencoded` by
    default; the shipped `index.html.j2` form sends `text=...` that
    way. The handler must accept it (not 400 on missing JSON)."""
    resp = await client.post(
        "/input",
        data={"text": "/join #ops"},
        # aiohttp's TestClient sets the form-encoded Content-Type
        # automatically for `data=dict`.
    )
    assert resp.status == 204
    # Side-effect check: /join #ops should have advanced session state.
    assert "#ops" in session.joined_channels
    assert session.current_channel == "#ops"


async def test_post_input_503_when_session_unhealthy(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the AgentIRC pipe is gone, subsequent input must fail fast
    rather than silently no-op through `_writer is None` (spec line 267)."""
    session._healthy = False

    called = False

    async def should_not_run(_parsed) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(session, "execute", should_not_run)

    app = make_app(session)
    async with TestClient(TestServer(app)) as c:
        resp = await c.post("/input", json={"text": "/join #ops"})
        assert resp.status == 503
        body = await resp.json()
        assert "error" in body
        assert "hint" in body
    assert called is False, "execute() must not be called once unhealthy"
