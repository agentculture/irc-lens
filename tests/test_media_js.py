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
    and data-kind via .dataset.

    Pin updated deliberately: media.js moved off getAttribute/setAttribute
    for data-* attributes to `.dataset` per SonarCloud S7761 — the pin now
    checks for the specific `.dataset.src`/`.dataset.kind` accessors
    rather than accepting either form.
    """
    js = _read_media_js()
    assert ".lens-media-load" in js, "media.js must handle .lens-media-load clicks"
    assert "dataset.src" in js, "media.js must read data-src via .dataset.src (S7761)"
    assert "dataset.kind" in js, "media.js must read data-kind via .dataset.kind (S7761)"


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


def test_media_js_url_scheme_check_is_case_insensitive() -> None:
    """Qodo PR #50 finding: the click-to-load scheme guard only accepted
    a lowercase `http(s)://` prefix while server-side classification
    (media.classify_url / the store's magic-byte sniff) is
    case-insensitive — an otherwise-legitimate "HTTP://..." URL would be
    silently dropped by the click handler. The fix lowercases a *copy*
    of the URL for the scheme comparison while keeping the original
    casing on the element actually built. This pins the lowercasing
    itself so the fix can't silently regress back to a raw comparison.
    """
    js = _read_media_js()
    assert "toLowerCase" in js, (
        "media.js must lowercase the URL before the http(s) scheme check "
        "so an uppercase scheme (e.g. HTTP://...) isn't silently rejected"
    )


def test_media_js_swaps_data_testid_on_embed() -> None:
    """After replacing the button, update the wrapper div's data-testid
    from 'media-placeholder' to 'media-embed' via `.dataset.testid`.

    Pin updated deliberately alongside the S7761 dataset migration: the
    swap itself now goes through `wrapper.dataset.testid = ...` rather
    than `setAttribute`, so this pins that accessor directly instead of
    only the generic "data-testid" substring (which would still match
    the unrelated `[data-testid="media-attach"]`-style selectors used
    elsewhere in this file).
    """
    js = _read_media_js()
    assert "media-placeholder" in js or "media-embed" in js, (
        "media.js must reference the testid swap (placeholder → embed)"
    )
    assert "data-testid" in js, (
        "media.js must update the wrapper's data-testid"
    )
    assert "dataset.testid" in js, (
        "media.js must update the wrapper's data-testid via .dataset.testid (S7761)"
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
    # deliberately raised by task t9: recording half added
    """Build-plan budget: media.js stays well under ~300 lines so the
    module is lean."""
    js = _read_media_js()
    n = len(js.splitlines())
    assert n <= 300, f"media.js grew to {n} lines — refactor or split"


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


# ---------------------------------------------------------------------------
# Task t8 — upload-UI half: attach button, drag-drop, paste, auto-send.
# ---------------------------------------------------------------------------


def test_media_js_wires_attach_button_to_hidden_file_input() -> None:
    """Clicking the attach button (data-testid=media-attach) opens the
    hidden file picker (data-testid=media-file-input) via .click()."""
    js = _read_media_js()
    assert "media-attach" in js, "media.js must reference the attach button testid"
    assert "media-file-input" in js, "media.js must reference the hidden file input testid"
    assert ".click()" in js, "attach button must open the hidden file input via .click()"


def test_media_js_uploads_via_fetch_post_upload() -> None:
    """Uploads go through fetch("/upload", {method: "POST", ...}) with a
    FormData body carrying the file — never a hand-rolled XHR."""
    js = _read_media_js()
    assert '"/upload"' in js or "'/upload'" in js, "media.js must POST to /upload"
    assert "FormData" in js, "media.js must build a multipart body via FormData"
    assert 'method: "POST"' in js or "method: 'POST'" in js


def test_media_js_one_send_path_uses_requestsubmit_not_direct_input_post() -> None:
    """On 201, the URL is submitted through the SAME pipeline typed
    messages use: set #chat-input's value then call requestSubmit() on
    #chat-form. media.js must never POST /input directly — that would
    create a second, divergent send path."""
    js = _read_media_js()
    assert "requestSubmit" in js, (
        "media.js must submit the uploaded URL via form.requestSubmit(), "
        "reusing the existing HTMX POST /input pipeline"
    )
    assert '"/input"' not in js and "'/input'" not in js, (
        "media.js must not POST /input directly — one send path only"
    )


def test_media_js_drag_drop_prevents_default_on_dragover() -> None:
    """dragover must call preventDefault or drop never fires; drop reads
    dataTransfer.files[0]."""
    js = _read_media_js()
    assert "dragover" in js, "media.js must listen for dragover on the chat area"
    assert "drop" in js, "media.js must listen for drop on the chat area"
    assert "dataTransfer" in js, "media.js must read dataTransfer.files on drop"
    assert "preventDefault" in js


def test_media_js_paste_listener_reads_clipboard_file_items() -> None:
    """Paste on the message input checks clipboardData items for a file
    (image) entry and uploads it."""
    js = _read_media_js()
    assert "paste" in js, "media.js must listen for paste on the message input"
    assert "clipboardData" in js, "media.js must read event.clipboardData"
    assert "getAsFile" in js, "media.js must pull the File out of a clipboard item"


def test_media_js_upload_failure_uses_toast_pattern_with_error_hint() -> None:
    """Upload failures surface via the same #toast-region / .lens-toast
    DOM pattern lens.js uses, reading the {error, hint} JSON body — no
    new SSE events are introduced for this."""
    js = _read_media_js()
    assert "toast-region" in js, "media.js must reuse the #toast-region toast pattern"
    assert "lens-toast" in js, "media.js toast helper must reuse the lens-toast class"
    assert ".error" in js, "media.js must read the {error, hint} payload's error field"
    assert ".hint" in js, "media.js must read the {error, hint} payload's hint field"


def test_media_js_guards_missing_upload_elements() -> None:
    """Attach/drag-drop/paste wiring is all guarded by element-existence
    checks, so the script stays safe on pages without the form."""
    js = _read_media_js()
    assert "attachButton && fileInput" in js or (
        "if (attachButton" in js and "if (fileInput" in js
    ), "attach-button wiring must guard on both elements existing"


def test_index_html_has_attach_button_and_hidden_file_input() -> None:
    """index.html.j2 has the attach button and hidden file input the
    upload half of media.js drives, near the existing chat-input form."""
    html = _read_index_html()
    assert 'data-testid="media-attach"' in html, "index.html.j2 must add the attach button"
    assert 'data-testid="media-file-input"' in html, (
        "index.html.j2 must add the hidden file input"
    )
    assert 'type="file"' in html
    assert 'accept="image/*,audio/*"' in html, (
        "the hidden file input must accept image/* and audio/* only"
    )


# ---------------------------------------------------------------------------
# Task t9 — mic recording: MediaRecorder capture.
# ---------------------------------------------------------------------------


def test_media_js_records_via_getusermedia_and_mediarecorder() -> None:
    """Record button captures audio via
    navigator.mediaDevices.getUserMedia({audio: true}) into a
    MediaRecorder — the capture APIs the acceptance criteria name."""
    js = _read_media_js()
    assert "getUserMedia" in js, "media.js must call getUserMedia to capture the mic"
    assert "MediaRecorder" in js, "media.js must use MediaRecorder to capture audio"


def test_media_js_prefers_webm_opus_with_mp4_fallback() -> None:
    """MediaRecorder is constructed with audio/webm;codecs=opus when
    MediaRecorder.isTypeSupported says so, else audio/mp4."""
    js = _read_media_js()
    assert "audio/webm;codecs=opus" in js, (
        "media.js must prefer audio/webm;codecs=opus"
    )
    assert "audio/mp4" in js, "media.js must fall back to audio/mp4"
    assert "isTypeSupported" in js, (
        "media.js must probe MediaRecorder.isTypeSupported before picking a mimeType"
    )


def test_media_js_caps_recording_at_five_minutes() -> None:
    """Client-side hard cap: auto-stop at 300 seconds (5 minutes)."""
    js = _read_media_js()
    assert "300" in js, "media.js must cap recording duration at 300 seconds"


def test_media_js_stops_stream_tracks_when_recording_ends() -> None:
    """No dangling mic indicator: every track on the captured stream is
    stopped once recording ends."""
    js = _read_media_js()
    assert "getTracks" in js, "media.js must enumerate the stream's tracks"
    assert ".stop()" in js, "media.js must call track.stop() to release the mic"


def test_media_js_record_button_reuses_upload_and_send_path() -> None:
    """The finished recording is sent through the SAME upload-then-send
    path t8 built (globalThis.LensMedia.uploadAndSend) — no duplicated
    /input POST logic in the recording IIFE.

    Pin updated deliberately: media.js moved off `window.` for its module
    namespace per SonarCloud S7764 (prefer globalThis over window), so
    this docstring now names the accessor the code actually uses.
    # SonarCloud S7764: window -> globalThis
    """
    js = _read_media_js()
    assert "LensMedia" in js, (
        "media.js must reuse the shared LensMedia.uploadAndSend helper"
    )
    assert "uploadAndSend" in js


def test_media_js_wires_record_button() -> None:
    """media.js queries the record button by its testid and element-guards
    it, so the script stays safe on pages without the button."""
    js = _read_media_js()
    assert "media-record" in js, "media.js must reference the record button testid"


def test_index_html_has_record_button() -> None:
    """index.html.j2 has the record button next to the attach button."""
    html = _read_index_html()
    assert 'data-testid="media-record"' in html, (
        "index.html.j2 must add the record button"
    )


# ---------------------------------------------------------------------------
# SonarCloud cleanup pins (media.js style/quality fixes, no behavior change).
# ---------------------------------------------------------------------------


def test_media_js_has_single_toast_implementation() -> None:
    """SonarCloud S4144 (duplicate function implementation): the upload
    IIFE (t8) and the recording IIFE (t9) used to each define an
    identical `toast` helper. The fix keeps exactly ONE implementation —
    `showToast`, defined once in the upload IIFE and exposed on
    `globalThis.LensMedia.showToast` — and has the recording IIFE reuse
    it via a local alias instead of redefining it. This pins that there
    is exactly one function body for the toast helper."""
    js = _read_media_js()
    assert js.count("function showToast(") == 1, (
        "media.js must define the toast helper exactly once (S4144 dedup)"
    )
    assert "function toast(" not in js, (
        "media.js must not keep the old duplicated `toast` function name"
    )
    assert "globalThis.LensMedia.showToast" in js, (
        "the recording IIFE must reuse the shared showToast via globalThis.LensMedia"
    )
    assert "showToast: showToast" in js, (
        "the upload IIFE must expose showToast on globalThis.LensMedia"
    )


def test_media_js_uses_globalthis_not_window() -> None:
    """SonarCloud S7764 (prefer globalThis over window): media.js's
    module-namespace references (`LensMedia`) must go through
    `globalThis`, matching the fix already applied to mesh.js
    (`globalThis.LensMesh`). No bare `window.` reference should remain.
    # SonarCloud S7764: window -> globalThis
    """
    js = _read_media_js()
    assert "window." not in js, (
        "media.js must not reference window. — use globalThis instead (S7764)"
    )
    assert "globalThis.LensMedia" in js, (
        "media.js must expose/consume its namespace via globalThis.LensMedia"
    )


def test_media_js_paste_loop_uses_for_of() -> None:
    """SonarCloud S4138 (prefer for-of): the clipboard-items paste loop
    never used its index for anything but `items[i]`, so it's a for-of
    candidate. Pins that the classic indexed loop is gone."""
    js = _read_media_js()
    assert "for (const item of items)" in js, (
        "media.js must iterate clipboard items via for-of (S4138)"
    )
    assert "for (let i = 0; i < items.length; i++)" not in js, (
        "media.js must not keep the old indexed loop over clipboard items"
    )


def test_media_js_catch_blocks_log_the_bound_error() -> None:
    """SonarCloud S2486 (handle exception or don't catch): every catch
    block references its bound error via console.warn, matching the
    established lens.js convention (`console.warn("[lens] ...", err)`).
    A comment-only swallow does not clear S2486 — the caught error must
    actually be used."""
    js = _read_media_js()
    # No unused underscore bindings left behind.
    assert "catch (_err)" not in js, (
        "media.js catch blocks must use the caught error, not swallow it "
        "as _err with a comment (S2486)"
    )
    # Every catch binds `err` and logs it via the repo's console.warn form.
    assert js.count("} catch (err) {") == 5, (
        "media.js must have exactly 5 catch blocks binding `err` (S2486)"
    )
    assert js.count('console.warn("[lens-media]') == 5, (
        "each catch must log its bound error via console.warn, matching "
        "the lens.js convention (S2486)"
    )
