"""Phase 9b — HTTP end-to-end tests.

Drives the lens's real ``aiohttp.web.Application`` (via
``conftest.py``'s ``lens_client``) against a thin AgentIRC test
server (``_agentirc_server.py``). Asserts the spec's user flows
(``GET /``, ``POST /input``, ``GET /events``) work against a
*connected* Session — not a stub-constructed one like
``test_web_skeleton.py``.

The test server is line-buffered and records every line the lens
sent, so assertions like "POST /input '/join #x' caused the
transport to write JOIN #x within 100 ms" are a single
``await wait_for_received(server, 'JOIN', '#x')`` call.
"""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient

from irc_lens.session import Session

from _agentirc_server import AgentIRCTestServer, _ReceivedLine


async def _wait_for_received(
    server: AgentIRCTestServer,
    command: str,
    *params: str,
    timeout: float = 1.0,
) -> _ReceivedLine:
    """Poll ``server.received`` until a matching line appears.

    Wraps the poll loop in :func:`asyncio.timeout` (Python 3.11+) so
    a stuck wire shows up as a clean ``AssertionError`` with the
    received history rather than as a runtime hang.
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


# ---------------------------------------------------------------------------
# GET / — connected lens renders the full three-pane shell.
# ---------------------------------------------------------------------------


async def test_get_index_returns_200_with_required_testids(lens_client: TestClient) -> None:
    """`GET /` against a real connected session returns 200 + every
    DOM contract id Phase 9c (Playwright) and the SSE handlers
    rely on."""
    resp = await lens_client.get("/")
    assert resp.status == 200
    body = await resp.text()
    for testid in (
        "chat-log",
        "sidebar",
        "info",
        "chat-input",
        "chat-submit",
        "view-indicator",
        "connection-status",
    ):
        assert f'data-testid="{testid}"' in body, f"missing #{testid}"


# ---------------------------------------------------------------------------
# POST /input — the user-driven write path.
# ---------------------------------------------------------------------------


async def test_post_input_join_writes_to_irc(
    lens_client: TestClient, agentirc_server: AgentIRCTestServer
) -> None:
    resp = await lens_client.post("/input", json={"text": "/join #ops"})
    assert resp.status == 204
    line = await _wait_for_received(agentirc_server, "JOIN", "#ops")
    assert line.command == "JOIN"


async def test_post_input_chat_writes_privmsg_to_irc(
    lens_client: TestClient, agentirc_server: AgentIRCTestServer
) -> None:
    # Join first so the lens has a current channel for free-text chat.
    await lens_client.post("/input", json={"text": "/join #ops"})
    await _wait_for_received(agentirc_server, "JOIN", "#ops")

    resp = await lens_client.post("/input", json={"text": "hello there"})
    assert resp.status == 204
    line = await _wait_for_received(agentirc_server, "PRIVMSG", "#ops")
    # PRIVMSG params are [target, text].
    assert line.params == ["#ops", "hello there"]


async def test_post_input_503_when_session_unhealthy(
    lens_client: TestClient, lens_session
) -> None:
    """Spec line 267: subsequent `POST /input` returns 503 once the
    session is unhealthy."""
    lens_session._healthy = False
    resp = await lens_client.post("/input", json={"text": "hello"})
    assert resp.status == 503
    body = await resp.json()
    assert "error" in body
    assert "hint" in body


async def test_post_input_413_on_oversize_body(lens_client: TestClient) -> None:
    """Bounded-memory contract: bodies > 4 KiB are rejected before
    `Session.execute` runs (PR #7 wired this via `client_max_size`
    on the Application + an in-handler check)."""
    big = "x" * 5000
    resp = await lens_client.post("/input", json={"text": big})
    assert resp.status == 413


# ---------------------------------------------------------------------------
# GET /events — the SSE read path. Stream a few bytes after a /join
# and assert a roster event arrives.
# ---------------------------------------------------------------------------


async def test_get_events_streams_roster_after_join(
    lens_client: TestClient,
    agentirc_server: AgentIRCTestServer,
    lens_session: Session,
) -> None:
    """Open SSE, then trigger a /join via POST /input. The roster
    event should land on the stream within ~1 s.

    Race-free: instead of sleeping a fixed amount to "ensure" the
    subscriber registered, we wait on
    ``lens_session.event_bus.subscriber_count`` so the JOIN cannot
    publish before the SSE handler is in the bus."""

    async def collect_event() -> bytes:
        resp = await lens_client.get("/events")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        buf = b""
        try:
            async with asyncio.timeout(2.0):
                while b"event: roster" not in buf:
                    chunk = await resp.content.read(1024)
                    if not chunk:
                        break
                    buf += chunk
        finally:
            resp.close()
        return buf

    collector = asyncio.create_task(collect_event())
    # Wait for the SSE handler to actually register on the bus
    # before publishing — closes the race the previous fixed-sleep
    # version had.
    async with asyncio.timeout(1.0):
        while lens_session.event_bus.subscriber_count == 0:
            await asyncio.sleep(0.005)
    join_resp = await lens_client.post("/input", json={"text": "/join #ops"})
    assert join_resp.status == 204
    await _wait_for_received(agentirc_server, "JOIN", "#ops")
    payload = await collector
    assert b"event: roster" in payload


# ---------------------------------------------------------------------------
# JSON-shape regression: error response shape (NOT the AfiError CLI
# triple — see PR #7 pushback memory).
# ---------------------------------------------------------------------------


async def test_post_input_error_response_uses_error_hint_shape(
    lens_client: TestClient, lens_session
) -> None:
    """503 body must be `{error, hint}` — *not* the AfiError
    `{code, message, remediation}` CLI shape. This is the spec
    pushback PR #7 ratified; pin it as a regression guard."""
    lens_session._healthy = False
    resp = await lens_client.post("/input", json={"text": "x"})
    body = await resp.json()
    assert set(body.keys()) == {"error", "hint"}
    assert isinstance(body["error"], str)
    assert isinstance(body["hint"], str)


# ---------------------------------------------------------------------------
# Test-server self-check: easy to spot if the fixture itself broke.
# ---------------------------------------------------------------------------


async def test_fixture_wires_handshake_and_serves_http(
    lens_client: TestClient, agentirc_server: AgentIRCTestServer
) -> None:
    """End-to-end fixture sanity: the lens's connect path sent
    NICK + USER to the test server (proves `Session.connect` ran),
    and the aiohttp Application is serving requests (proves
    `make_app` + `TestClient` are wired up)."""
    commands = [line.command for line in agentirc_server.received]
    assert "NICK" in commands
    assert "USER" in commands
    resp = await lens_client.get("/")
    assert resp.status == 200


async def test_post_input_form_encoded_body_also_works(lens_client: TestClient) -> None:
    """HTMX defaults to application/x-www-form-urlencoded — the
    content-negotiation path from PR #7 must still work end-to-end."""
    resp = await lens_client.post(
        "/input",
        data={"text": "/join #form"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status == 204


# ---------------------------------------------------------------------------
# Negative path: invalid JSON body.
# ---------------------------------------------------------------------------


async def test_post_input_400_on_bad_json(lens_client: TestClient) -> None:
    resp = await lens_client.post(
        "/input",
        data="this is not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
