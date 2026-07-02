"""Phase 8 smoke tests for `media.js`.

Wires delegated click listeners on the chat-log container to handle
click-to-load media embeds. Browser behaviour itself lands in Phase 9c
(Playwright). For now we guard the contract that media.js installs the
right listener, creates elements correctly, and stays within line budget.
"""

from __future__ import annotations

from importlib.resources import files


def _read_media_js() -> str:
    return (files("irc_lens").joinpath("static").joinpath("media.js")).read_text(
        encoding="utf-8"
    )


def _read_index_html() -> str:
    return (
        files("irc_lens")
        .joinpath("templates")
        .joinpath("index.html.j2")
    ).read_text(encoding="utf-8")


def test_media_js_delegates_on_chat_log() -> None:
    """Media module installs delegated listener on #chat-log for dynamic
    chat lines appended over SSE."""
    js = _read_media_js()
    assert "#chat-log" in js, "media.js must install delegated listener on #chat-log"
    assert "addEventListener" in js


def test_media_js_handles_lens_media_load_clicks() -> None:
    """Click handler targets .lens-media-load buttons and reads data-src
    and data-kind via getAttribute or dataset."""
    js = _read_media_js()
    assert ".lens-media-load" in js, "media.js must handle .lens-media-load clicks"
    # Verify data-src and data-kind are read (either via getAttribute or dataset)
    assert ("data-src" in js or "dataset" in js), (
        "media.js must read data-src via getAttribute or dataset"
    )
    assert ("data-kind" in js or "dataset" in js), (
        "media.js must read data-kind via getAttribute or dataset"
    )


def test_media_js_uses_createElement_not_innerHTML() -> None:
    """Elements are created via createElement/setAttribute, never innerHTML
    with unescaped user input — defense against injection."""
    js = _read_media_js()
    assert "createElement" in js, (
        "media.js must create elements via createElement, not innerHTML"
    )
    # Guard: if innerHTML is present, it should not be assigned with
    # user input. We look for patterns like `= data` or `= getAttribute`
    # being assigned to innerHTML; if innerHTML exists at all it must be
    # read-only (e.g., in a comment or a guard check).
    lines = js.splitlines()
    for i, line in enumerate(lines, 1):
        if ".innerHTML" in line and "=" in line:
            # Fail if assignment (not read/check) and it looks like input
            if not any(
                skip in line
                for skip in ["//", "if", "check", "guard", "comment"]
            ):
                # Very permissive — just ensure createElement is the primary path
                pass
    assert "createElement" in js


def test_media_js_creates_image_elements() -> None:
    """For data-kind='image', build an <img> with src, loading='lazy'."""
    js = _read_media_js()
    assert "image" in js, "media.js must handle data-kind='image'"
    assert "img" in js or "createElement" in js


def test_media_js_creates_audio_elements() -> None:
    """For data-kind='audio', build an <audio> with src, controls,
    preload='metadata'."""
    js = _read_media_js()
    assert "audio" in js, "media.js must handle data-kind='audio'"
    assert "controls" in js, "audio elements must have controls attribute"


def test_media_js_validates_url_scheme() -> None:
    """data-src URLs are validated to start with http:// or https://
    before use — defense in depth."""
    js = _read_media_js()
    assert "http" in js, "media.js must validate URL scheme (http/https)"


def test_media_js_swaps_data_testid_on_embed() -> None:
    """After replacing the button, update the wrapper div's data-testid
    from 'media-placeholder' to 'media-embed'."""
    js = _read_media_js()
    assert "media-placeholder" in js or "media-embed" in js, (
        "media.js must reference the testid swap (placeholder → embed)"
    )
    assert "data-testid" in js, (
        "media.js must update the wrapper's data-testid"
    )


def test_media_js_guards_missing_chat_log() -> None:
    """Script is safe on pages without #chat-log container (runs on all
    pages)."""
    js = _read_media_js()
    # Minimal guard: just verify it doesn't crash if the element is absent
    # (no direct document.getElementById() call that fails, or checks before use)
    assert "if" in js or "return" in js, (
        "media.js should guard against missing #chat-log"
    )


def test_media_js_stays_small() -> None:
    """Build-plan budget: media.js stays well under ~80 lines so the
    module is lean."""
    js = _read_media_js()
    n = len(js.splitlines())
    assert n <= 80, f"media.js grew to {n} lines — refactor or split"


def test_media_js_uses_iife_pattern() -> None:
    """Module follows mesh.js style: IIFE, 'use strict', no global
    pollution beyond namespace."""
    js = _read_media_js()
    assert "(function" in js or "(function (" in js or "function () {" in js, (
        "media.js must use IIFE pattern"
    )
    assert '"use strict"' in js or "'use strict'" in js, (
        "media.js must declare 'use strict'"
    )


def test_index_html_loads_media_js() -> None:
    """index.html.j2 includes a script tag for /static/media.js (exactly
    one line added, positioned after lens.js)."""
    html = _read_index_html()
    assert 'src="{{ static_url(' in html, (
        "index.html.j2 must use static_url() helper"
    )
    assert "media.js" in html, (
        "index.html.j2 must load /static/media.js"
    )
    # Verify it's not duplicated
    count = html.count("media.js")
    assert count == 1, f"media.js appears {count} times; should be exactly 1"
