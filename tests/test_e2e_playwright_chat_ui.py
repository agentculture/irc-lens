"""Chat UI uplift (t13) in a real browser: command palette, phone layout,
focus rings. Opt-in like the rest of the playwright suite (``-m playwright``).
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.playwright

_T = 5000


def _url(client: TestClient) -> str:
    return str(client.make_url("/"))


async def test_slash_opens_palette_above_input_and_filters(
    seeded_lens_client: TestClient,
) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(_url(seeded_lens_client))
            palette = page.locator('[data-testid="command-palette"]')
            box = page.locator("#chat-input")
            await expect(palette).to_be_hidden(timeout=_T)
            await box.fill("/")
            await expect(palette).to_be_visible(timeout=_T)
            rows = palette.locator("li:visible")
            await expect(rows).to_have_count(
                9, timeout=_T
            )  # approved, guest mode off: no /sandbox
            # opens ABOVE the input
            pb = await palette.bounding_box()
            ib = await box.bounding_box()
            assert pb["y"] + pb["height"] <= ib["y"] + 1
            await box.fill("/jo")
            await expect(rows).to_have_count(1, timeout=_T)
            await expect(rows.first).to_contain_text("Open a room")
            await box.press("Tab")  # completes the command
            await expect(box).to_have_value("/join ")
            await expect(palette).to_be_hidden(timeout=_T)
            await box.fill("hello")
            await expect(palette).to_be_hidden(timeout=_T)
        finally:
            await browser.close()


async def test_palette_keyboard_select_and_escape(
    seeded_lens_client: TestClient,
) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            box = page.locator("#chat-input")
            await box.fill("/")
            await box.press("ArrowDown")
            await box.press("ArrowDown")
            await box.press("Enter")  # picks the 2nd row (/who), no submit
            await expect(box).to_have_value("/who ")
            await box.fill("/")
            await box.press("Escape")
            await expect(page.locator('[data-testid="command-palette"]')).to_be_hidden(
                timeout=_T
            )
        finally:
            await browser.close()


async def test_phone_375_no_horizontal_scroll_and_touch_targets(
    seeded_lens_client: TestClient,
) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 375, "height": 760})
            await page.goto(_url(seeded_lens_client))
            await expect(page.locator('[data-testid="chat-line"]').first).to_be_visible(
                timeout=_T
            )
            for open_palette in (False, True):
                if open_palette:
                    await page.locator("#chat-input").fill("/")
                    await expect(
                        page.locator('[data-testid="command-palette"]')
                    ).to_be_visible(timeout=_T)
                widths = await page.evaluate(
                    "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
                )
                assert widths[0] <= widths[1], widths
            await page.locator("#chat-input").fill("")
            toggle = page.locator('[data-testid="rooms-toggle"]')
            tb = await toggle.bounding_box()
            assert tb["width"] >= 44
            assert tb["height"] >= 44
            sidebar = page.locator("#sidebar")
            await expect(sidebar).to_be_hidden()
            await toggle.click()
            await expect(sidebar).to_be_visible(timeout=_T)
            await expect(toggle).to_have_attribute("aria-expanded", "true")
            await page.keyboard.press("Escape")
            await expect(sidebar).to_be_hidden(timeout=_T)
        finally:
            await browser.close()


async def test_focus_rings_visible(seeded_lens_client: TestClient) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            checked = 0
            for _ in range(12):
                await page.keyboard.press("Tab")
                style = await page.evaluate(
                    "(() => { const e = document.activeElement;"
                    " if (!e || e === document.body) return null;"
                    " const s = getComputedStyle(e);"
                    " return [s.outlineStyle, parseFloat(s.outlineWidth)]; })()"
                )
                if style is None:
                    continue
                checked += 1
                assert style[0] != "none"
                assert style[1] >= 2, style
            assert checked >= 5
        finally:
            await browser.close()
