"""Spec contract tests for the same-host Origin CSRF floor on ``POST /input``.

Per Phase 4 plan T4.3:
1. Origin absent  → NOT 403 (204 or 503 acceptable — depends on session state).
2. Origin matches Host → NOT 403.
3. Origin mismatches Host → 403 with JSON body containing ``"origin"`` in ``error``.

The floor deliberately allows Origin-absent requests so that curl,
cloudflared probes, and internal monitors continue to work unmodified.
Full CSRF-token protection is tracked in issue #27.
"""
from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient


@pytest.mark.asyncio
async def test_post_input_no_origin_allowed(lens_client: TestClient) -> None:
    """POST /input with no Origin header must NOT return 403.

    The spec guarantees Origin-absent requests pass through; the response
    will be 204 (success) or 503 (no healthy upstream) depending on
    AgentIRC state — both are acceptable. The important contract is that
    the CSRF floor is not triggered.
    """
    resp = await lens_client.post("/input", data={"text": "hello"})
    assert resp.status in (204, 503), (
        f"Expected 204 or 503 for Origin-absent request, got {resp.status}"
    )


@pytest.mark.asyncio
async def test_post_input_matching_origin_allowed(lens_client: TestClient) -> None:
    """POST /input with Origin matching the server host must NOT return 403.

    The aiohttp TestServer binds on 127.0.0.1:<port>. We construct the
    Origin as ``http://127.0.0.1:<port>`` so netloc comparison succeeds.
    """
    port = lens_client.port
    origin = f"http://127.0.0.1:{port}"
    resp = await lens_client.post(
        "/input",
        data={"text": "hello"},
        headers={"Origin": origin},
    )
    assert resp.status in (204, 503), (
        f"Expected 204 or 503 for matching-Origin request, got {resp.status}"
    )


@pytest.mark.asyncio
async def test_post_input_foreign_origin_rejected(lens_client: TestClient) -> None:
    """POST /input with a foreign Origin must return 403 with ``{error, hint}`` JSON.

    The error message must contain the word ``"origin"`` (case-insensitive)
    so the caller can identify the cause without parsing the hint.
    """
    resp = await lens_client.post(
        "/input",
        data={"text": "hello"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status == 403, (
        f"Expected 403 for foreign-Origin request, got {resp.status}"
    )
    body = await resp.json()
    assert "error" in body, f"Response body missing 'error' key: {body}"
    assert "origin" in body["error"].lower(), (
        f"Expected 'origin' in error message, got: {body['error']!r}"
    )
    assert "hint" in body, f"Response body missing 'hint' key: {body}"
    assert body["hint"], f"Expected non-empty 'hint', got: {body['hint']!r}"


@pytest.mark.asyncio
async def test_post_input_origin_default_port_omitted(lens_client: TestClient) -> None:
    """Origin with default port omitted for a foreign host still 403s.

    Verifies that default-port elision (no :80 in the Origin header)
    doesn't break the foreign-origin rejection path.  The lens fixture
    binds on a non-standard port so the comparison tuple always differs.
    """
    resp = await lens_client.post(
        "/input",
        data={"text": "hello"},
        headers={"Origin": "http://evil.example.com"},  # no port → default 80
    )
    assert resp.status == 403, (
        f"Expected 403 for default-port-omitted foreign Origin, got {resp.status}"
    )
    body = await resp.json()
    assert "error" in body
    assert "hint" in body
    assert body["hint"]


@pytest.mark.asyncio
async def test_post_input_origin_with_path_component(lens_client: TestClient) -> None:
    """Origin with a path component is allowed when the host+port matches.

    The Origin header normally doesn't include a path, but this defensive
    test confirms the helper only compares (hostname, port) — a spurious
    path in the header doesn't break the match.
    """
    port = lens_client.port
    origin = f"http://127.0.0.1:{port}/some/path"
    resp = await lens_client.post(
        "/input",
        data={"text": "hello"},
        headers={"Origin": origin},
    )
    assert resp.status in (204, 503), (
        f"Expected 204 or 503 for matching-host Origin-with-path, got {resp.status}"
    )


# omitted: test_post_input_origin_case_insensitive — the aiohttp TestServer
# binds on 127.0.0.1 (IP literal); constructing an Origin with an uppercase
# hostname like 'LOCALHOST' that maps to the same address would require DNS
# mocking or a second server bind.  The implementation handles it (hostname
# comparison is lowercased on both sides) but the fixture mechanics make an
# in-test assertion impractical.
