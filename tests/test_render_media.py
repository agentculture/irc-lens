"""Chat-line media-embed rendering tests (media-support PR 1, task t2).

Exercises the `.lens-media` block that `_chat_line.html.j2` renders
under the text span when a message contains an image/audio URL — see
`docs/superpowers/specs/2026-07-02-media-support-design.md` ("Rendering
path"). Builds on task t1's `irc_lens.web.media` (`classify_url`,
`render_message_html`), which this task does not modify.

Four things pinned here:

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
4. **Trusted-host matching is origin-exact, not prefix-based** — fixed
   after a Qodo review finding on PR #50: `media_embed_prefixes` used
   to hold URL-string prefixes matched via `url.startswith(prefix)`,
   which a subdomain-suffix trick (`https://trusted.com.evil.org/x`) or
   a userinfo trick (`https://trusted.com@evil.org/x`) could bypass —
   both start with the trusted string without the URL actually
   resolving to that host. `media_embed_prefixes` now holds
   `(scheme, hostname, port)` origin tuples (`web/render.py`'s
   `MediaOrigin`) compared via `urllib.parse.urlsplit`, not
   `str.startswith`. Every fixture in this file that used to pass a
   URL-string prefix (e.g. `"http://lens.local/media/"`) now passes the
   equivalent origin tuple (`("http", "lens.local", 80)`) — see the
   "Direct embeds" section below and the adversarial cases in
   "Trusted-host matching is origin-exact, not prefix-based" further
   down.
"""

from __future__ import annotations

import asyncio

from irc_lens.session import Session
from irc_lens.web.render import media_items, render_chat_log, render_fragment

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
        media_embed_prefixes=(("http", "lens.local", 80),),
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
        media_embed_prefixes=(("http", "lens.local", 80),),
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
        media_embed_prefixes=(("http", "lens.local", 80),),
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
        media_embed_prefixes=(("http", "lens.local", 80),),
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
        media_embed_prefixes=(("http", "lens.local", 80),),
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
        media_embed_prefixes=(("http", "lens.local", 80),),
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


# ---------------------------------------------------------------------------
# Trusted-host matching is origin-exact, not prefix-based
#
# Qodo PR #50 finding (src/irc_lens/web/render.py:135 area): the old
# `url.startswith(prefix)` check let an attacker-controlled URL that merely
# *starts with* a trusted prefix string bypass click-to-load, even when it
# doesn't actually resolve to the trusted host. `_TRUSTED` below mirrors what
# `serve.py::_media_session_kwargs` would derive for
# `media.public_base_url: "https://trusted.com"` — an exact-port origin
# `("https", "trusted.com", 443)`.
# ---------------------------------------------------------------------------

_TRUSTED: tuple[tuple[str, str, int], ...] = (("https", "trusted.com", 443),)


def test_subdomain_suffix_trick_is_not_trusted() -> None:
    """`https://trusted.com.evil.org/x.png` starts with the trusted
    prefix string but its actual hostname is `trusted.com.evil.org` —
    a completely different (attacker-owned) host. Must render a
    click-to-load placeholder, never a direct embed."""
    items = media_items(
        "https://trusted.com.evil.org/x.png", embed_prefixes=_TRUSTED, remote_mode="click"
    )
    assert len(items) == 1
    assert items[0]["direct"] is False


def test_userinfo_trick_on_message_url_is_not_trusted() -> None:
    """`https://trusted.com@evil.org/x.png` — `trusted.com` is userinfo
    here, not the host; `urlsplit(...).hostname` resolves it to
    `evil.org`. Must not be treated as lens-hosted."""
    items = media_items(
        "https://trusted.com@evil.org/x.png", embed_prefixes=_TRUSTED, remote_mode="click"
    )
    assert len(items) == 1
    assert items[0]["direct"] is False


def test_userinfo_trick_on_public_base_url_host_is_not_trusted() -> None:
    """Same userinfo trick, but the *legitimate* host is embedded as
    userinfo ahead of an attacker's real host — e.g. an operator whose
    `media.public_base_url` host is `lens.example.com` being targeted
    by `https://lens.example.com@evil.org/x.png`. The credential-looking
    prefix must not fool the origin match."""
    origins = (("https", "lens.example.com", 443),)
    items = media_items(
        "https://lens.example.com@evil.org/x.png", embed_prefixes=origins, remote_mode="click"
    )
    assert len(items) == 1
    assert items[0]["direct"] is False


def test_uppercase_host_on_trusted_url_is_still_trusted() -> None:
    """Hostname comparison is case-insensitive — `HTTPS://TRUSTED.COM/x.png`
    must still match the (lowercase-stored) trusted origin."""
    items = media_items(
        "https://TRUSTED.COM/x.png", embed_prefixes=_TRUSTED, remote_mode="click"
    )
    assert len(items) == 1
    assert items[0]["direct"] is True


def test_port_mismatch_is_not_trusted() -> None:
    """An exact-port allowlist entry (as `media.public_base_url` always
    produces) must not match a request against the same host on a
    different port."""
    items = media_items(
        "https://trusted.com:9999/x.png", embed_prefixes=_TRUSTED, remote_mode="click"
    )
    assert len(items) == 1
    assert items[0]["direct"] is False


def test_trusted_hosts_any_port_entry_matches_nonstandard_port() -> None:
    """A `media.trusted_hosts` entry with no explicit `:port` widens to
    "any port" (per `serve.py::_trusted_host_origins`) — this is the
    counterpart to the exact-port case above, using the `port=None`
    wildcard row directly."""
    wildcard_port: tuple[tuple[str, str, int | None], ...] = (("https", "cdn.example.com", None),)
    items = media_items(
        "https://cdn.example.com:9999/x.png", embed_prefixes=wildcard_port, remote_mode="click"
    )
    assert len(items) == 1
    assert items[0]["direct"] is True


def test_render_lens_hosted_prefix_fixture_rejects_subdomain_suffix_bypass() -> None:
    """End-to-end version of the subdomain-suffix trick through the real
    template render path (not just `media_items` directly) — the
    adversarial URL must render a click-to-load placeholder, never a
    direct `data-testid="media-embed"`."""
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "alice",
            "text": "https://lens.local.evil.org/media/x.png",
            "ts_display": "00:00:00",
        },
        media_embed_prefixes=(("https", "lens.local", 443),),
        media_remote_embeds="click",
    )
    assert 'data-testid="media-placeholder"' in out
    assert 'data-testid="media-embed"' not in out
