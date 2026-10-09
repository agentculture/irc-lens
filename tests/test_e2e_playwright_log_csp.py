"""Real-browser regressions from the guest-mode browser test (deviation d6).

* The chat log opens at the NEWEST message and follows new lines only while
  the reader is at the bottom.
* No page logs a Content Security Policy violation (htmx indicator styles,
  hx-trigger event filters).
* Enter on a focused room row switches rooms (delegated keydown handler).

Opt-in like the rest of the playwright suite (``-m playwright``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from playwright.async_api import async_playwright, expect

from conftest import _serve_lens
from irc_lens.seed import apply_seed

pytestmark = pytest.mark.playwright

_T = 5000
_LINES = 80


def _url(client: TestClient, path: str = "/") -> str:
    return str(client.make_url(path))


@pytest_asyncio.fixture
async def long_history_client(
    lens_session, agentirc_server, tmp_path
) -> AsyncIterator[TestClient]:
    """A seeded lens whose #general history overflows the log pane."""
    rows = "\n".join(
        f'  - {{channel: "#general", nick: "alice", text: "line {i} '
        f'{"lorem ipsum " * 6}", timestamp: {1714000000 + i}}}'
        for i in range(_LINES)
    )
    seed = tmp_path / "long.yaml"
    seed.write_text(
        'joined_channels:\n  - "#general"\n  - "#ops"\n'
        f"preload_messages:\n{rows}\n"
        'roster:\n  - {nick: "alice", type: "human", online: true}\n'
        'current_channel: "#general"\n'
    )
    apply_seed(lens_session, seed)
    async for client in _serve_lens(lens_session, agentirc_server.host, agentirc_server.port):
        yield client


_GAP = "el => el.scrollHeight - el.scrollTop - el.clientHeight"


@pytest.mark.parametrize(
    "viewport", [{"width": 1280, "height": 800}, {"width": 375, "height": 700}]
)
async def test_log_opens_at_newest_message(long_history_client, viewport) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport=viewport)
            await page.goto(_url(long_history_client))
            log = page.locator("#chat-log")
            await expect(page.locator('[data-testid="chat-line"]')).to_have_count(
                _LINES, timeout=_T
            )
            overflow = await log.evaluate("el => el.scrollHeight - el.clientHeight")
            assert overflow > 400, "fixture must overflow the log pane"
            assert await log.evaluate(_GAP) <= 2
            await expect(page.locator('[data-testid="chat-line"]').last).to_be_in_viewport()
        finally:
            await browser.close()


async def test_log_follows_new_lines_only_when_at_bottom(long_history_client) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(_url(long_history_client))
            lines = page.locator('[data-testid="chat-line"]')
            await expect(lines).to_have_count(_LINES, timeout=_T)
            box = page.locator("#chat-input")
            log = page.locator("#chat-log")

            # At the bottom: a new line keeps the view pinned to it.
            await box.fill("follow me")
            await box.press("Enter")
            await expect(lines).to_have_count(_LINES + 1, timeout=_T)
            assert await log.evaluate(_GAP) <= 2

            # Scrolled up: a new line must NOT yank the reader.
            await log.evaluate("el => { el.scrollTop = 0; }")
            await page.wait_for_function("document.getElementById('chat-log').scrollTop === 0")
            await box.fill("do not yank")
            await box.press("Enter")
            await expect(lines).to_have_count(_LINES + 2, timeout=_T)
            assert await log.evaluate("el => el.scrollTop") == 0
        finally:
            await browser.close()


async def test_no_csp_console_errors_and_enter_switches_room(
    seeded_lens_client: TestClient,
) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            problems: list[str] = []
            page.on(
                "console",
                lambda m: problems.append(m.text) if m.type in ("error", "warning") else None,
            )
            page.on("pageerror", lambda e: problems.append(str(e)))
            await page.goto(_url(seeded_lens_client))
            ops = page.locator('[data-testid="sidebar-channel"][data-channel="#ops"]')
            await expect(ops).to_be_visible(timeout=_T)

            async with page.expect_request(
                lambda r: r.url.endswith("/input") and r.method == "POST", timeout=_T
            ) as req_info:
                await ops.focus()
                await page.keyboard.press("Enter")
            req = await req_info.value
            assert "switch" in (req.post_data or "")
            await expect(
                page.locator('[data-testid="sidebar-channel"][data-channel="#ops"]')
            ).to_have_attribute("aria-current", "true", timeout=_T)

            await page.wait_for_timeout(300)
            csp = [t for t in problems if "content security policy" in t.lower()]
            assert not csp, csp
        finally:
            await browser.close()
