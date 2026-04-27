"""Phase 9c — Playwright end-to-end tests (opt-in).

Drives a real chromium against the lens's `aiohttp.web.Application`
(via Phase 9b's `seeded_lens_client` fixture, which preloads
`tests/fixtures/basic.yaml` so every test starts from a known DOM).

Run via:

    uv run playwright install chromium      # one-time browser install
    uv run pytest -m playwright -v

A bare `pytest` run *skips* this module — `addopts = "-m 'not
playwright'"` in `pyproject.toml` keeps default test runs (and the
existing CI `test` job) free of browser overhead. The new CI
`playwright` job overrides via `pytest -m playwright`.

We use ``playwright.async_api`` (not the sync ``page`` fixture from
pytest-playwright) because the test stack already runs under
pytest-asyncio's auto loop — mixing the sync API into an active loop
trips ``RuntimeError: Cannot run the event loop while another loop
is running``. The async API integrates cleanly via ``async with
async_playwright() as p:``.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.playwright


def _url(client: TestClient, path: str = "/") -> str:
    """Resolve an absolute URL the headless browser can navigate to.

    `aiohttp.test_utils.TestClient.make_url` returns a ``yarl.URL``
    bound to the random port the test server picked; Playwright
    expects a string."""
    return str(client.make_url(path))


async def test_seeded_chat_lines_render(seeded_lens_client: TestClient) -> None:
    """Spec verification gate (build plan line 287): the seeded
    chat lines render with the correct ``data-testid="chat-line"``
    count. ``basic.yaml`` preloads two messages in #general."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            await expect(page.locator('[data-testid="chat-line"]')).to_have_count(
                2, timeout=2000
            )
        finally:
            await browser.close()


async def test_typing_chat_input_appends_chat_line(
    seeded_lens_client: TestClient,
) -> None:
    """Type into ``#chat-input`` and submit; the new chat-line must
    appear via the local-echo SSE path (HTMX form → POST /input →
    Session.execute → _publish_chat → SSE → lens.js append)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=2000)
            await page.locator('[data-testid="chat-input"]').fill("browser hello")
            await page.locator('[data-testid="chat-input"]').press("Enter")
            await expect(chat_lines).to_have_count(3, timeout=2000)
            await expect(chat_lines.last).to_contain_text("browser hello")
        finally:
            await browser.close()


async def test_active_channel_renders_with_active_class(
    seeded_lens_client: TestClient,
) -> None:
    """``basic.yaml`` pins ``#general`` as the current channel; the
    sidebar item for it must carry the ``lens-channel--active``
    class. (The build plan's "click to switch" assertion presupposes
    a sidebar click handler that doesn't exist in the shipped
    ``lens.js``; this adapted assertion proves the active-state
    wiring is live without inventing new product features.)"""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            active = page.locator(
                '[data-testid="sidebar-channel"][data-channel="#general"]'
            )
            await expect(active).to_have_class(
                "lens-channel lens-channel--active", timeout=2000
            )
        finally:
            await browser.close()


async def test_view_switch_via_help_command(seeded_lens_client: TestClient) -> None:
    """End-to-end view switch: type ``/help``, submit, the
    ``view-indicator`` must reflect the new view via the
    SSE ``info`` swap."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            indicator = page.locator('[data-testid="view-indicator"]')
            await expect(indicator).to_have_attribute("data-view", "chat", timeout=2000)
            await page.locator('[data-testid="chat-input"]').fill("/help")
            await page.locator('[data-testid="chat-input"]').press("Enter")
            await expect(indicator).to_have_attribute("data-view", "help", timeout=2000)
        finally:
            await browser.close()
