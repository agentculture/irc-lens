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

import struct
import zlib

import pytest
from aiohttp.test_utils import TestClient
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.playwright

# Per-locator timeout. Local runs assert in <2s; CI runners can be
# noticeably slower under cold-cache chromium starts or hot
# parallel jobs, so leave generous margin. Single constant so the
# next adjustment is one edit.
_LOCATOR_TIMEOUT_MS = 5000


def _valid_png(width: int = 2, height: int = 2) -> bytes:
    """A *decodable* RGB PNG (task t10 media-embed proof).

    ``test_e2e_playwright_media.py`` (t8) uses a magic-bytes-only stub
    (``\\x89PNG…`` + padding) — enough for the store's magic-byte sniff,
    but the browser can't decode it, so ``<img>.naturalWidth`` stays 0.
    t10 must prove a lens-hosted image actually *loads* over loopback, so
    this builds a real single-IDAT PNG (RGB, filter-0 rows) whose bytes
    both pass the sniff *and* decode to a non-zero ``naturalWidth``.
    """

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    row = b"\x00" + b"\xff\x00\x00" * width  # filter byte 0 + red pixels
    idat = zlib.compress(row * height)
    return signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _valid_wav(sample_rate: int = 8000, n_samples: int = 800) -> bytes:
    """A *decodable* 8-bit mono PCM WAV (task t10 audio-embed proof).

    A real RIFF/WAVE container (fmt + data chunks) so the browser can load
    metadata over loopback and advance ``<audio>.readyState`` — proving the
    lens-hosted audio embed is playable, not just present. The bytes also
    satisfy the store's ``RIFF…WAVE`` magic-byte sniff.
    """
    pcm = b"\x80" * n_samples  # 8-bit unsigned PCM midpoint (silence)
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, sample_rate, sample_rate, 1, 8)
    data = struct.pack("<4sI", b"data", len(pcm)) + pcm
    riff_size = 4 + len(fmt) + len(data)
    return struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE") + fmt + data


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
                2, timeout=_LOCATOR_TIMEOUT_MS
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
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)
            await page.locator('[data-testid="chat-input"]').fill("browser hello")
            await page.locator('[data-testid="chat-input"]').press("Enter")
            await expect(chat_lines).to_have_count(3, timeout=_LOCATOR_TIMEOUT_MS)
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
                "lens-channel lens-channel--active", timeout=_LOCATOR_TIMEOUT_MS
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
            await expect(indicator).to_have_attribute("data-view", "chat", timeout=_LOCATOR_TIMEOUT_MS)
            await page.locator('[data-testid="chat-input"]').fill("/help")
            await page.locator('[data-testid="chat-input"]').press("Enter")
            await expect(indicator).to_have_attribute("data-view", "help", timeout=_LOCATOR_TIMEOUT_MS)
        finally:
            await browser.close()


async def test_mesh_view_shows_canvas_and_hides_log(seeded_lens_client: TestClient) -> None:
    """Typing ``/mesh`` switches to the live agent-mesh graph: the canvas
    pane becomes visible (the mesh.js renderer mounts it), the chat log
    hides, and the input form stays usable so the user can switch back."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            canvas = page.locator("#mesh-canvas")
            chat_log = page.locator('[data-testid="chat-log"]')
            # Mesh pane is hidden under the default chat view.
            await expect(canvas).to_be_hidden(timeout=_LOCATOR_TIMEOUT_MS)
            await page.locator('[data-testid="chat-input"]').fill("/mesh")
            await page.locator('[data-testid="chat-input"]').press("Enter")
            await expect(canvas).to_be_visible(timeout=_LOCATOR_TIMEOUT_MS)
            await expect(chat_log).to_be_hidden(timeout=_LOCATOR_TIMEOUT_MS)
            # The input form stays usable in mesh view — clicking a channel
            # (or /switch) is how the user returns to chat.
            await expect(page.locator('[data-testid="chat-input"]')).to_be_visible(
                timeout=_LOCATOR_TIMEOUT_MS
            )
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Task t10 — media-embed proof in the browser.
#
# t8's `test_e2e_playwright_media.py` proved the three upload *surfaces*
# (picker, paste, drag-drop) funnel through one send path, and — because its
# fixture Session has no `media_embed_prefixes` — only ever asserted the
# union `media-embed, media-placeholder` marker appears. It never proved a
# real embed *renders and loads*. These tests close that gap:
#
#   (a) a lens-hosted image renders a direct <img data-testid="media-embed">
#       that actually decodes (naturalWidth > 0) over loopback;
#   (b) a lens-hosted audio renders a direct <audio controls> that loads far
#       enough to signal readyState — i.e. it's playable, not just present;
#   (c) a *remote* media URL renders a click-to-load placeholder, and one
#       click swaps it to a loaded embed (the shipped media.js delegated
#       handler) — the only browser test of that swap.
#
# All three stay on loopback. (a)/(b) use `media_hosted_lens_client`, whose
# embed prefixes point at the server's own origin so an uploaded URL is
# lens-hosted. (c) uses `seeded_lens_client` (no prefixes ⇒ every absolute
# URL is "remote") and points at a same-server /static asset via a real
# absolute origin URL — a loopback URL that deliberately does not match the
# lens-hosted prefixes, per the plan's "no external host" constraint.
# ---------------------------------------------------------------------------


async def test_lens_hosted_image_renders_and_loads(
    media_hosted_lens_client: TestClient,
) -> None:
    """Upload a real PNG through the shipped attach-button path; the
    auto-sent lens-hosted URL renders a direct ``<img data-testid=
    "media-embed">`` in the chat log that the browser actually decodes
    (``naturalWidth > 0``) by fetching it back over loopback."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(media_hosted_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)

            await page.locator('[data-testid="media-file-input"]').set_input_files(
                {"name": "shot.png", "mimeType": "image/png", "buffer": _valid_png()}
            )

            # The auto-sent lens-hosted URL renders a *direct* embed, not a
            # placeholder — the prefix-matched branch t8 could never reach.
            await expect(chat_lines).to_have_count(3, timeout=_LOCATOR_TIMEOUT_MS)
            embed = chat_lines.last.locator('[data-testid="media-embed"]')
            await expect(embed).to_be_visible(timeout=_LOCATOR_TIMEOUT_MS)
            await expect(
                chat_lines.last.locator('[data-testid="media-placeholder"]')
            ).to_have_count(0, timeout=_LOCATOR_TIMEOUT_MS)

            # The <img> actually loaded: complete + non-zero decoded width,
            # proving the /media/ route served the bytes over loopback.
            await page.wait_for_function(
                """() => {
                  const lines = document.querySelectorAll('[data-testid="chat-line"]');
                  const last = lines[lines.length - 1];
                  const img = last && last.querySelector('[data-testid="media-embed"]');
                  return img && img.tagName === 'IMG' && img.complete && img.naturalWidth > 0;
                }""",
                timeout=_LOCATOR_TIMEOUT_MS,
            )
        finally:
            await browser.close()


async def test_lens_hosted_audio_renders_and_is_playable(
    media_hosted_lens_client: TestClient,
) -> None:
    """Upload a real WAV; the auto-sent lens-hosted URL renders a direct
    ``<audio controls preload="metadata" data-testid="media-embed">`` whose
    ``readyState`` advances to at least ``HAVE_METADATA`` — the browser
    fetched and parsed the file over loopback, so the element is playable,
    not merely present."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(media_hosted_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)

            await page.locator('[data-testid="media-file-input"]').set_input_files(
                {"name": "clip.wav", "mimeType": "audio/wav", "buffer": _valid_wav()}
            )

            await expect(chat_lines).to_have_count(3, timeout=_LOCATOR_TIMEOUT_MS)
            audio = chat_lines.last.locator('audio[data-testid="media-embed"]')
            await expect(audio).to_have_count(1, timeout=_LOCATOR_TIMEOUT_MS)

            # Playable signal: controls present AND metadata loaded from
            # loopback (readyState >= HAVE_METADATA). Explicitly `.load()`
            # first so the assertion doesn't depend on chromium's autoload
            # heuristics for a preload="metadata" element.
            await page.wait_for_function(
                """() => {
                  const lines = document.querySelectorAll('[data-testid="chat-line"]');
                  const last = lines[lines.length - 1];
                  const a = last && last.querySelector('audio[data-testid="media-embed"]');
                  if (!a || !a.controls) return false;
                  if (a.readyState < 1 && !a.__t10Kicked) { a.__t10Kicked = true; a.load(); }
                  return a.readyState >= 1;
                }""",
                timeout=_LOCATOR_TIMEOUT_MS,
            )
        finally:
            await browser.close()


async def test_remote_media_placeholder_click_to_load_swaps_to_embed(
    seeded_lens_client: TestClient,
) -> None:
    """A *remote* media URL renders a click-to-load placeholder; one click
    swaps it to a loaded ``<img data-testid="media-embed">`` via the shipped
    ``media.js`` delegated handler — the only browser test of that swap
    (t7 pins it with grep-level assertions only).

    "Remote" here is a loopback URL that deliberately does not match the
    lens-hosted prefixes: the seeded fixture wires no prefixes, so an
    absolute same-origin ``/static`` asset URL classifies as remote yet
    still loads over loopback after the click — no external host, per the
    plan."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(_url(seeded_lens_client))
            chat_lines = page.locator('[data-testid="chat-line"]')
            await expect(chat_lines).to_have_count(2, timeout=_LOCATOR_TIMEOUT_MS)

            # A real, reachable, absolute loopback URL that is NOT lens-hosted
            # (it's /static, not /media, and no embed prefixes are configured)
            # so it renders as a click-to-load placeholder.
            remote_url = await page.evaluate(
                "() => location.origin + '/static/culture-logo.png'"
            )
            await page.locator('[data-testid="chat-input"]').fill(remote_url)
            await page.locator('[data-testid="chat-input"]').press("Enter")

            await expect(chat_lines).to_have_count(3, timeout=_LOCATOR_TIMEOUT_MS)
            placeholder = chat_lines.last.locator('[data-testid="media-placeholder"]')
            await expect(placeholder).to_be_visible(timeout=_LOCATOR_TIMEOUT_MS)
            # Not yet an embed — the swap is click-driven.
            await expect(chat_lines.last.locator('[data-testid="media-embed"]')).to_have_count(
                0, timeout=_LOCATOR_TIMEOUT_MS
            )

            await placeholder.click()

            # media.js replaced the button with a loaded <img> and relabelled
            # the marker media-placeholder → media-embed.
            embed = chat_lines.last.locator('img[data-testid="media-embed"]')
            await expect(embed).to_be_visible(timeout=_LOCATOR_TIMEOUT_MS)
            await expect(
                chat_lines.last.locator('[data-testid="media-placeholder"]')
            ).to_have_count(0, timeout=_LOCATOR_TIMEOUT_MS)
            await page.wait_for_function(
                """() => {
                  const lines = document.querySelectorAll('[data-testid="chat-line"]');
                  const last = lines[lines.length - 1];
                  const img = last && last.querySelector('img[data-testid="media-embed"]');
                  return img && img.complete && img.naturalWidth > 0;
                }""",
                timeout=_LOCATOR_TIMEOUT_MS,
            )
        finally:
            await browser.close()
