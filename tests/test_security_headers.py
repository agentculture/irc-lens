"""Security headers middleware tests (t3 — CSP + nosniff + referrer-policy).

Pins the contract from
``docs/superpowers/specs/2026-07-02-media-support-design.md`` ("Security
headers" section):

* ``Content-Security-Policy`` on HTML responses only, with the exact
  directive string from the design doc (``script-src 'self'``,
  ``object-src 'none'``, broad ``img-src``/``media-src`` since mesh peers
  advertise plain-HTTP LAN media URLs).
* ``X-Content-Type-Options: nosniff`` and ``Referrer-Policy: no-referrer``
  on every response — HTML, JSON, static assets, and error responses
  alike, including the framework-level static-file 404.
* The SSE stream (``GET /events``) keeps setting its own headers
  (``Content-Type``/``Cache-Control``/``X-Accel-Buffering``) untouched —
  it calls ``response.prepare()`` itself before the middleware regains
  control, so those headers are already on the wire.

Follows the fixture pattern from ``tests/test_web_events.py`` /
``tests/helpers.py``: a bare (unconnected) ``Session`` is enough since
none of these assertions touch AgentIRC state.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.session import Session

from helpers import make_app_for as _make_app_for


_EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; img-src 'self' https: http:; "
    "media-src 'self' https: http:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)


@pytest.fixture
def session() -> Session:
    return Session(host="127.0.0.1", port=6667, nick="lens-test")


@pytest.fixture
async def client(session: Session) -> TestClient:
    app = _make_app_for(session)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


# ---------------------------------------------------------------------------
# CSP on HTML
# ---------------------------------------------------------------------------


async def test_get_index_has_exact_csp_header(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert resp.headers["Content-Security-Policy"] == _EXPECTED_CSP


async def test_csp_pins_load_bearing_directives(client: TestClient) -> None:
    """Pin ``script-src``/``object-src`` (the actual attack-surface
    guards) and the broad ``img-src``/``media-src`` individually, so a
    future CSP edit can't silently loosen the load-bearing directives
    while a full-string diff still looks "close enough"."""
    resp = await client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "img-src 'self' https: http:" in csp
    assert "media-src 'self' https: http:" in csp


# ---------------------------------------------------------------------------
# nosniff + referrer-policy on ALL responses; CSP scoped to HTML only
# ---------------------------------------------------------------------------


async def test_get_index_has_nosniff_and_referrer_policy(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


async def test_healthz_has_baseline_headers_but_no_csp(client: TestClient) -> None:
    """``/healthz`` is JSON, not HTML — nosniff/referrer-policy still
    apply; CSP does not (there's no markup on this response to police)."""
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" not in resp.headers


async def test_static_asset_has_baseline_headers_but_no_csp(client: TestClient) -> None:
    resp = await client.get("/static/lens.css")
    assert resp.status == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" not in resp.headers


async def test_static_404_still_has_baseline_headers(client: TestClient) -> None:
    """aiohttp's ``StaticResource`` raises ``HTTPNotFound`` (an
    exception, not a returned ``Response``) for a missing file — this
    exercises the middleware's except-arm, guarding against the
    baseline headers silently dropping off the framework-level error
    page."""
    resp = await client.get("/static/does-not-exist.js")
    assert resp.status == 404
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


async def test_post_input_400_has_baseline_headers(client: TestClient) -> None:
    resp = await client.post(
        "/input",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


async def test_post_input_413_has_baseline_headers(client: TestClient) -> None:
    resp = await client.post("/input", json={"text": "x" * 5000})
    assert resp.status == 413
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


# ---------------------------------------------------------------------------
# SSE stream headers stay exactly as routes.py sets them
# ---------------------------------------------------------------------------


async def test_sse_stream_headers_unmodified(client: TestClient) -> None:
    """The security headers middleware must not clobber (or fail to
    apply around) the SSE route's own Content-Type/Cache-Control/
    X-Accel-Buffering headers. ``get_events`` calls
    ``response.prepare()`` itself, deep inside the handler, before this
    middleware's post-handler code ever runs — those headers are
    already committed to the wire by then, so this is a regression
    guard on routes.py's own header set, not a nosniff/CSP assertion."""
    resp = await client.get("/events")
    try:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["X-Accel-Buffering"] == "no"
    finally:
        resp.close()
