"""HTTP-level tests for the Phase 4 aiohttp skeleton.

The full event-bus / parser-dispatch wiring lands in Phase 5; this file
just guards the route surface, the DOM contract testids, and the
vendored asset paths. We construct a `Session` directly without
calling `connect()` (no live AgentIRC) — `make_app` doesn't need a
connected session for any of the Phase 4 routes.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.session import EntityItem, Session
from irc_lens.web import make_app


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
# GET /
# ---------------------------------------------------------------------------


# Wrapper testids that MUST exist on every load (per the spec's DOM
# contract section). Per-message / per-channel testids only appear when
# state populates, so they're not in this list.
REQUIRED_TESTIDS = (
    "chat-input",
    "chat-submit",
    "chat-log",
    "sidebar",
    "info",
    "view-indicator",
    "connection-status",
)


async def test_get_index_returns_200_html(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.content_type == "text/html"


async def test_get_index_has_all_required_testids(client: TestClient) -> None:
    resp = await client.get("/")
    body = await resp.text()
    for testid in REQUIRED_TESTIDS:
        assert f'data-testid="{testid}"' in body, (
            f"missing required testid: {testid}\nbody snippet:\n{body[:600]}"
        )


async def test_get_index_references_vendored_htmx(client: TestClient) -> None:
    body = await (await client.get("/")).text()
    assert "/static/vendor/htmx.min.js" in body
    assert "/static/vendor/sse.js" in body
    assert "/static/lens.js" in body
    assert "/static/lens.css" in body
    # No CDN URLs leaked.
    assert "unpkg.com" not in body
    assert "cdn.jsdelivr.net" not in body


async def test_get_index_renders_sidebar_state(session: Session) -> None:
    """Populated state lights up sidebar-channel + sidebar-entity testids."""
    session.joined_channels.add("#ops")
    session.set_current_channel("#ops")
    session.set_roster([EntityItem(nick="alice", type="human")])

    app = make_app(session)
    async with TestClient(TestServer(app)) as c:
        body = await (await c.get("/")).text()
    assert 'data-testid="sidebar-channel"' in body
    assert 'data-channel="#ops"' in body
    assert 'data-testid="sidebar-entity"' in body
    assert 'data-nick="alice"' in body


async def test_get_index_view_indicator_reflects_state(client: TestClient) -> None:
    body = await (await client.get("/")).text()
    # Default view is "chat".
    assert 'data-view="chat"' in body


async def test_get_index_connection_status_reflects_health(client: TestClient) -> None:
    body = await (await client.get("/")).text()
    # Pristine session: healthy=True, connected=False — render "connected"
    # text from the healthy CSS class. We don't assert exact text, just
    # that the wrapper exists; visual treatment is Phase 7.
    assert 'data-testid="connection-status"' in body


# ---------------------------------------------------------------------------
# POST /input
# ---------------------------------------------------------------------------


async def test_post_input_returns_204(client: TestClient) -> None:
    resp = await client.post("/input", json={"text": "/join #ops"})
    assert resp.status == 204
    assert (await resp.text()) == ""


async def test_post_input_204_on_empty_body(client: TestClient) -> None:
    resp = await client.post("/input")
    assert resp.status == 204


# ---------------------------------------------------------------------------
# GET /events
# ---------------------------------------------------------------------------


async def test_get_events_returns_event_stream(client: TestClient) -> None:
    resp = await client.get("/events")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")


async def test_get_events_emits_one_chat_then_eof(client: TestClient) -> None:
    resp = await client.get("/events")
    body = await resp.read()
    assert body == b"event: chat\ndata: irc-lens online\n\n", (
        f"expected one chat event then EOF; got {body!r}"
    )


# ---------------------------------------------------------------------------
# GET /static/*
# ---------------------------------------------------------------------------


async def test_static_lens_css_served(client: TestClient) -> None:
    resp = await client.get("/static/lens.css")
    assert resp.status == 200
    body = await resp.text()
    assert ".lens-grid" in body  # sanity: actual stylesheet, not 404 page


async def test_static_lens_js_served(client: TestClient) -> None:
    resp = await client.get("/static/lens.js")
    assert resp.status == 200
    body = await resp.text()
    assert "EventSource" in body


async def test_static_vendored_htmx_served(client: TestClient) -> None:
    resp = await client.get("/static/vendor/htmx.min.js")
    assert resp.status == 200
    # Pinned htmx.org bundle starts with `var htmx=function()` per the
    # 2.0.4 minified header.
    body = await resp.text()
    assert body.startswith("var htmx=")


async def test_static_vendored_sse_served(client: TestClient) -> None:
    resp = await client.get("/static/vendor/sse.js")
    assert resp.status == 200
    body = await resp.text()
    # sse.js opens with the htmx-ext-sse banner comment.
    assert "Server Sent Events" in body[:200] or "sse" in body[:200].lower()


async def test_static_404_for_missing_file(client: TestClient) -> None:
    resp = await client.get("/static/does-not-exist.js")
    assert resp.status == 404


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def test_make_app_stashes_session(session: Session) -> None:
    app = make_app(session)
    assert app["session"] is session


def test_make_app_registers_expected_routes(session: Session) -> None:
    app = make_app(session)
    paths = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("GET", "/") in paths
    assert ("POST", "/input") in paths
    assert ("GET", "/events") in paths
    # Static is a prefix-mounted resource; check by name.
    assert any(r.name == "static" for r in app.router.resources() if r.name)
