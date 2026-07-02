"""Chat-line media-embed rendering tests (media-support PR 1, task t2).

Exercises the `.lens-media` block that `_chat_line.html.j2` renders
under the text span when a message contains an image/audio URL — see
`docs/superpowers/specs/2026-07-02-media-support-design.md` ("Rendering
path"). Builds on task t1's `irc_lens.web.media` (`classify_url`,
`render_message_html`), which this task does not modify.

Three things pinned here:

1. **Embed shape** — lens-hosted URLs (matching a `media_embed_prefixes`
   entry, or a relative `/media/...` path) always render a direct
   `<img loading="lazy">` / `<audio controls preload="metadata">`
   (`data-testid="media-embed"`); remote URLs render per
   `media_remote_embeds` (`click` → placeholder button
   `data-testid="media-placeholder"` with `data-src`/`data-kind`;
   `auto` → direct embed; `off` → no `.lens-media` block at all).
2. **DOM contract unchanged** — `data-testid="chat-line"` (and the
   nick/text testids) keep rendering exactly as `test_render.py`
   already pins, with or without a media block alongside them.
3. **Escape-safety** — the media block is template-built from escaped
   values only; an adversarial URL (quote/angle-bracket breakout
   attempt) must never produce a live unescaped attribute.
"""

from __future__ import annotations

import asyncio

from irc_lens.session import Session
from irc_lens.web.render import render_chat_log, render_fragment

# ---------------------------------------------------------------------------
# Direct embeds — lens-hosted (prefix match)
# ---------------------------------------------------------------------------


def test_chat_line_renders_lens_hosted_image_embed() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "check this http://lens.local/media/abc123.png",
            "ts_display": "00:00:00",
        },
        media_embed_prefixes=("http://lens.local/media/",),
        media_remote_embeds="click",
    )
    assert 'data-testid="chat-line"' in out  # DOM contract unchanged
    assert 'class="lens-media"' in out
    assert 'data-testid="media-embed"' in out
    assert "<img" in out
    assert 'loading="lazy"' in out
    assert 'src="http://lens.local/media/abc123.png"' in out
    assert 'data-testid="media-placeholder"' not in out


def test_chat_line_renders_lens_hosted_audio_embed() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "bob",
            "text": "listen http://lens.local/media/clip.mp3",
            "ts_display": "00:00:00",
        },
        media_embed_prefixes=("http://lens.local/media/",),
    )
    assert "<audio" in out
    assert "controls" in out
    assert 'preload="metadata"' in out
    assert 'data-testid="media-embed"' in out
    assert 'src="http://lens.local/media/clip.mp3"' in out


# ---------------------------------------------------------------------------
# Remote media — click / auto / off
# ---------------------------------------------------------------------------


def test_chat_line_remote_image_click_mode_renders_placeholder() -> None:
    """`media_remote_embeds="click"` (the default) — placeholder button,
    no direct embed."""
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "http://other.example.com/pic.png",
            "ts_display": "00:00:00",
        },
    )
    assert 'data-testid="media-placeholder"' in out
    assert 'class="lens-media-load"' in out
    assert 'data-src="http://other.example.com/pic.png"' in out
    assert 'data-kind="image"' in out
    assert 'data-testid="media-embed"' not in out


def test_chat_line_remote_audio_click_mode_renders_placeholder() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "http://other.example.com/clip.mp3",
            "ts_display": "00:00:00",
        },
        media_remote_embeds="click",
    )
    assert 'data-testid="media-placeholder"' in out
    assert 'data-kind="audio"' in out


def test_chat_line_remote_image_auto_mode_embeds_directly() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "http://other.example.com/pic.png",
            "ts_display": "00:00:00",
        },
        media_remote_embeds="auto",
    )
    assert 'data-testid="media-embed"' in out
    assert 'data-testid="media-placeholder"' not in out
    assert "<img" in out
    assert 'src="http://other.example.com/pic.png"' in out


def test_chat_line_remote_image_off_mode_renders_no_media_block() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "http://other.example.com/pic.png",
            "ts_display": "00:00:00",
        },
        media_remote_embeds="off",
    )
    assert 'class="lens-media"' not in out
    assert 'data-testid="media-embed"' not in out
    assert 'data-testid="media-placeholder"' not in out
    # Plain-link rendering (via the `linkify` filter) is untouched by the
    # media block being suppressed.
    assert '<a href="http://other.example.com/pic.png"' in out


def test_chat_line_lens_hosted_wins_over_off_mode() -> None:
    """Lens-hosted media always embeds directly, even when
    `media_remote_embeds="off"` — `off` only governs *remote* media."""
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "http://lens.local/media/abc.png",
            "ts_display": "00:00:00",
        },
        media_embed_prefixes=("http://lens.local/media/",),
        media_remote_embeds="off",
    )
    assert 'data-testid="media-embed"' in out


# ---------------------------------------------------------------------------
# Relative /media/ paths always count as lens-hosted
# ---------------------------------------------------------------------------


def test_chat_line_relative_media_path_is_lens_hosted() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "see /media/token123.png",
            "ts_display": "00:00:00",
        },
        # No prefixes configured and mode is "off" — the relative
        # /media/ rule must still win.
        media_embed_prefixes=(),
        media_remote_embeds="off",
    )
    assert 'data-testid="media-embed"' in out
    assert 'src="/media/token123.png"' in out


# ---------------------------------------------------------------------------
# No media block for plain (non-image/audio) links
# ---------------------------------------------------------------------------


def test_chat_line_plain_link_renders_no_media_block() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "see http://example.com/page.html",
            "ts_display": "00:00:00",
        },
    )
    assert 'class="lens-media"' not in out
    assert 'data-testid="media-embed"' not in out
    assert 'data-testid="media-placeholder"' not in out
    assert '<a href="http://example.com/page.html"' in out


def test_chat_line_no_url_at_all_renders_no_media_block() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={"nick": "alice", "text": "just chatting", "ts_display": "00:00:00"},
    )
    assert 'class="lens-media"' not in out


# ---------------------------------------------------------------------------
# XSS-in-URL stays escaped
# ---------------------------------------------------------------------------


def test_chat_line_media_url_xss_attempt_stays_escaped() -> None:
    """Adversarial URL carrying a literal double quote must not break
    out of the `src="..."` attribute the media block builds — the
    quote must survive only as an HTML entity, matching the same
    escape-first contract `test_media.py` pins for `render_message_html`."""
    malicious = 'http://lens.local/media/x.png?a="onmouseover="alert(1)'
    out = render_fragment(
        "_chat_line.html.j2",
        msg={"nick": "alice", "text": malicious, "ts_display": "00:00:00"},
        media_embed_prefixes=("http://lens.local/media/",),
    )
    assert 'data-testid="media-embed"' in out
    assert 'onmouseover="alert(1)"' not in out
    assert "&#34;" in out
    assert "<script" not in out


def test_chat_line_media_placeholder_xss_attempt_stays_escaped() -> None:
    """Same adversarial shape, but through the click-to-load placeholder's
    `data-src` attribute (remote, not lens-hosted)."""
    malicious = 'http://evil.example.com/x.png?a="onmouseover="alert(1)'
    out = render_fragment(
        "_chat_line.html.j2",
        msg={"nick": "alice", "text": malicious, "ts_display": "00:00:00"},
    )
    assert 'data-testid="media-placeholder"' in out
    assert 'onmouseover="alert(1)"' not in out
    assert "&#34;" in out
    assert "<script" not in out


# ---------------------------------------------------------------------------
# render_chat_log threads the media kwargs through every entry
# ---------------------------------------------------------------------------


def test_render_chat_log_threads_media_kwargs_through_entries() -> None:
    entries = [
        {
            "nick": "alice",
            "text": "http://lens.local/media/a.png",
            "timestamp": "1700000000",
        },
        {"nick": "bob", "text": "no media here", "timestamp": "1700000005"},
    ]
    out = render_chat_log(
        entries,
        media_embed_prefixes=("http://lens.local/media/",),
        media_remote_embeds="click",
    )
    assert out.count('data-testid="chat-line"') == 2
    assert 'data-testid="media-embed"' in out


def test_render_chat_log_defaults_match_no_media_config() -> None:
    """Omitting the media kwargs entirely (existing callers) must not
    crash and must fall back to click-mode / no lens-hosted prefixes —
    i.e. a remote image URL still renders a placeholder, not a bare
    KeyError/UndefinedError."""
    entries = [
        {"nick": "alice", "text": "http://example.com/a.png", "timestamp": "1700000000"}
    ]
    out = render_chat_log(entries)
    assert 'data-testid="media-placeholder"' in out


# ---------------------------------------------------------------------------
# Session glue — the two optional kwargs flow into published `chat` events
# ---------------------------------------------------------------------------


def test_session_defaults_have_no_lens_hosted_prefixes_and_click_mode() -> None:
    session = Session(host="127.0.0.1", port=6667, nick="lens-test")
    assert session.media_embed_prefixes == ()
    assert session.media_remote_embeds == "click"


def test_session_publish_chat_includes_media_embed_for_configured_prefix() -> None:
    session = Session(
        host="127.0.0.1",
        port=6667,
        nick="lens-test",
        media_embed_prefixes=("http://lens.local/media/",),
        media_remote_embeds="click",
    )
    session.set_current_channel("#ops")
    sub = session.event_bus.subscribe()
    try:
        asyncio.run(_publish_directly(session, "http://lens.local/media/pic.png"))
        events = sub.drain_nowait()
    finally:
        sub.close()
    chat_events = [e for e in events if e.name == "chat"]
    assert chat_events, "expected at least one chat event"
    assert 'data-testid="media-embed"' in chat_events[-1].data


async def _publish_directly(session: Session, text: str) -> None:
    """`_exec_chat` requires a live send path; `_publish_chat` is the
    piece this task actually wires media through, so call it directly
    rather than dragging in a fake IRC transport."""
    session._publish_chat(session.nick, text)


def test_session_publish_chat_remote_url_renders_placeholder_by_default() -> None:
    session = Session(host="127.0.0.1", port=6667, nick="lens-test")
    sub = session.event_bus.subscribe()
    try:
        asyncio.run(_publish_directly(session, "http://example.com/pic.png"))
        events = sub.drain_nowait()
    finally:
        sub.close()
    chat_events = [e for e in events if e.name == "chat"]
    assert 'data-testid="media-placeholder"' in chat_events[-1].data
