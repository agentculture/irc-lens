"""Task t9 — Playwright smoke: mic recording via MediaRecorder.

Companion to ``tests/test_e2e_playwright_media.py`` (same fixture stack,
same async-Playwright conventions — see ``docs/playwright.md``). Kept in
its own file for the same reason t8's upload coverage is: separate
files let independently-built worktrees land without touching the same
lines.

Chromium is launched with ``--use-fake-ui-for-media-stream`` and
``--use-fake-device-for-media-stream`` so
``navigator.mediaDevices.getUserMedia({audio: true})`` resolves against
a synthetic device — no OS permission prompt, no real microphone
required in CI. Neither ``tests/conftest.py`` nor
``test_e2e_playwright_media.py`` share a browser fixture (every test
launches its own ``p.chromium.launch()`` inline), so these extra args
are passed directly on the local launch call rather than editing
``conftest.py``.

Drives the record button through idle -> recording -> uploading -> idle
end to end: click once to start, wait long enough for a couple of
MediaRecorder ``dataavailable`` events, click again to stop. The
stopped recording goes through the SAME ``uploadAndSend`` path task t8
built (POST ``/upload``, then submit the returned URL through
``#chat-form`` exactly like a typed message), which we observe here by
asserting a new chat line whose text contains ``/media/`` — the
capability URL the upload route returns.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.playwright

_LOCATOR_TIMEOUT_MS = 5000

_FAKE_MEDIA_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
]


def _url(client: TestClient, path: str = "/") -> str:
    """Resolve an absolute URL the headless browser can navigate to —
    mirrors the identically-named helper in test_e2e_playwright_media.py."""
    return str(client.make_url(path))


async def test_record_button_captures_and_auto_sends(
    seeded_lens_client: TestClient,
) -> None:
    """Click record, wait ~1.5s of fake mic audio, click again to stop;
    the finished recording is uploaded and auto-sent through the same
    #chat-form pipeline a typed message uses."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=_FAKE_MEDIA_ARGS)
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)

            record_button = page.locator('[data-testid="media-record"]')
            await record_button.click()
            await page.wait_for_timeout(1500)
            await record_button.click()

            await expect(chat_lines).to_have_count(3, timeout=_LOCATOR_TIMEOUT_MS)
            await expect(chat_lines.last).to_contain_text(
                "/media/", timeout=_LOCATOR_TIMEOUT_MS
            )
        finally:
            await browser.close()
