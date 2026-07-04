"""Task t8 — Playwright coverage: picker, drag-drop, paste, auto-send.

Companion to ``tests/test_e2e_playwright.py`` (same fixture stack, same
async-Playwright conventions — see ``docs/playwright.md``). Kept in its
own file rather than appended to that module because task t10 also
extends ``tests/test_e2e_playwright.py``; separate files let the two
TDD-gated worktrees land without touching the same lines.

Drives the real upload UI end to end: the hidden file input (the
attach-button path), a synthesized clipboard paste, and a synthesized
drag-drop onto ``#chat-log``. All three must funnel through the SAME
send path — POST ``/upload``, then submit the returned URL through
``#chat-form`` exactly like a typed message — which we observe
indirectly here (new chat line appears, ``#chat-input`` clears via the
same HTMX ``afterRequest`` 204 handling a typed message uses) and
directly at the HTTP layer in ``tests/test_e2e_http_media.py``.

The fixture ``Session`` built by ``tests/conftest.py`` never wires
``media_embed_prefixes`` (only the real ``irc_lens.web.app`` factory
used outside tests does), so an uploaded lens-hosted URL always renders
as the click-to-load placeholder here, never a direct embed. The build
plan's acceptance criterion is explicit that either is acceptable ("a
chat line with the media embed/placeholder appears"), so assertions
below check for the union rather than assuming one or the other.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.playwright

_LOCATOR_TIMEOUT_MS = 5000

# Minimal but valid PNG (magic bytes + padding) — same fixture shape as
# tests/test_upload_routes.py (task t6) uses for the store's
# magic-byte sniff.
_PNG_BYTES = bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

_MEDIA_MARKER_SELECTOR = '[data-testid="media-embed"], [data-testid="media-placeholder"]'


def _url(client: TestClient, path: str = "/") -> str:
    """Resolve an absolute URL the headless browser can navigate to —
    mirrors the identically-named helper in test_e2e_playwright.py."""
    return str(client.make_url(path))


async def test_attach_button_picker_uploads_and_auto_sends(
    seeded_lens_client: TestClient,
) -> None:
    """Picking a file via the hidden input behind the attach button
    uploads it, then auto-submits the returned URL through the same
    #chat-form/#chat-input pipeline a typed message uses."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)

            await page.locator('[data-testid="media-file-input"]').set_input_files(
                {"name": "pic.png", "mimeType": "image/png", "buffer": _PNG_BYTES}
            )

            await expect(chat_lines).to_have_count(3, timeout=_LOCATOR_TIMEOUT_MS)
            await expect(chat_lines.last.locator(_MEDIA_MARKER_SELECTOR)).to_be_visible(
                timeout=_LOCATOR_TIMEOUT_MS
            )
            # One send path: the URL travelled through #chat-input, so
            # the rendered line's text carries it (linkify wraps it).
            await expect(chat_lines.last).to_contain_text("/media/", timeout=_LOCATOR_TIMEOUT_MS)
            # HTMX clears #chat-input on the 204 the same way it does
            # for a typed message — proving requestSubmit() went
            # through the real pipeline, not a side channel.
            await expect(page.locator('[data-testid="chat-input"]')).to_have_value(
                "", timeout=_LOCATOR_TIMEOUT_MS
            )
        finally:
            await browser.close()


async def test_paste_image_uploads_and_auto_sends(
    seeded_lens_client: TestClient,
) -> None:
    """Pasting an image while focused on #chat-input uploads it and
    auto-sends via the same pipeline as the attach button."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)

            await page.locator('[data-testid="chat-input"]').click()
            await page.evaluate(
                """(bytes) => {
                  const file = new File([new Uint8Array(bytes)], 'pasted.png', { type: 'image/png' });
                  const dt = new DataTransfer();
                  dt.items.add(file);
                  const input = document.getElementById('chat-input');
                  const event = new ClipboardEvent('paste', { bubbles: true, cancelable: true });
                  Object.defineProperty(event, 'clipboardData', { value: dt });
                  input.dispatchEvent(event);
                }""",
                list(_PNG_BYTES),
            )

            await expect(chat_lines).to_have_count(3, timeout=_LOCATOR_TIMEOUT_MS)
            await expect(chat_lines.last.locator(_MEDIA_MARKER_SELECTOR)).to_be_visible(
                timeout=_LOCATOR_TIMEOUT_MS
            )
        finally:
            await browser.close()


async def test_drag_drop_onto_chat_log_uploads_and_auto_sends(
    seeded_lens_client: TestClient,
) -> None:
    """Dropping a file onto #chat-log uploads it and auto-sends via the
    same pipeline as the attach button. dragover/drop are synthesized
    (no real OS drag session is available under Playwright), per
    docs/playwright.md's guidance to construct DataTransfer in
    page.evaluate."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)

            await page.evaluate(
                """(bytes) => {
                  const file = new File([new Uint8Array(bytes)], 'dropped.png', { type: 'image/png' });
                  const dt = new DataTransfer();
                  dt.items.add(file);
                  const chatLog = document.getElementById('chat-log');
                  const over = new DragEvent('dragover', { bubbles: true, cancelable: true });
                  Object.defineProperty(over, 'dataTransfer', { value: dt });
                  chatLog.dispatchEvent(over);
                  const drop = new DragEvent('drop', { bubbles: true, cancelable: true });
                  Object.defineProperty(drop, 'dataTransfer', { value: dt });
                  chatLog.dispatchEvent(drop);
                }""",
                list(_PNG_BYTES),
            )

            await expect(chat_lines).to_have_count(3, timeout=_LOCATOR_TIMEOUT_MS)
            await expect(chat_lines.last.locator(_MEDIA_MARKER_SELECTOR)).to_be_visible(
                timeout=_LOCATOR_TIMEOUT_MS
            )
        finally:
            await browser.close()


async def test_upload_failure_surfaces_toast_not_a_chat_line(
    seeded_lens_client: TestClient,
) -> None:
    """An unsupported file type 400s from /upload; the {error, hint}
    payload surfaces via the existing #toast-region toast pattern, and
    no chat line is added — the failed upload never reaches /input."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)

            await page.locator('[data-testid="media-file-input"]').set_input_files(
                {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hello world"}
            )

            toast = page.locator("#toast-region .lens-toast")
            await expect(toast).to_be_visible(timeout=_LOCATOR_TIMEOUT_MS)
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)
        finally:
            await browser.close()
