"""Unit tests for `irc_lens.web.media` (media-support PR 1, task t1).

Covers the three functions the design doc's "Rendering path" section
carves out for this module:

1. `classify_url` — extension-based image/audio/link classification
   (`docs/superpowers/specs/2026-07-02-media-support-design.md`).
2. `render_message_html` — escape-first, then linkify. All safety
   rides on `markupsafe.escape`; the only markup this function injects
   is built from already-escaped components, and only `http`/`https`
   URLs are ever turned into anchors.
3. `compose_media_message` — the outbound half: plain URL text (plus
   optional caption), guaranteed to fit AgentIRC's
   `MAX_INBOUND_LINE = 8192` byte cap.

This task owns only `src/irc_lens/web/media.py` and this test file —
it does not touch `render.py`, templates, or `session.py` (that is
task t2's job). `render_message_html` must still be a drop-in-safe
value under Jinja2 autoescape (a `markupsafe.Markup`), so a couple of
tests pin that directly against the shared `_env` used by
`render.py`.
"""

from __future__ import annotations

import pytest
from markupsafe import Markup

from irc_lens.web.media import (
    MAX_INBOUND_LINE,
    classify_url,
    compose_media_message,
    render_message_html,
)

# ---------------------------------------------------------------------------
# classify_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["png", "jpg", "jpeg", "gif", "webp"])
def test_classify_url_recognizes_image_extensions(ext: str) -> None:
    assert classify_url(f"http://example.com/pic.{ext}") == "image"


@pytest.mark.parametrize("ext", ["PNG", "Jpg", "JPEG", "GIF", "WebP"])
def test_classify_url_image_extension_is_case_insensitive(ext: str) -> None:
    assert classify_url(f"http://example.com/pic.{ext}") == "image"


@pytest.mark.parametrize("ext", ["mp3", "ogg", "wav", "webm", "m4a", "flac"])
def test_classify_url_recognizes_audio_extensions(ext: str) -> None:
    assert classify_url(f"http://example.com/clip.{ext}") == "audio"


@pytest.mark.parametrize("ext", ["MP3", "Ogg", "WAV", "WEBM", "M4A", "FLAC"])
def test_classify_url_audio_extension_is_case_insensitive(ext: str) -> None:
    assert classify_url(f"http://example.com/clip.{ext}") == "audio"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/page.html",
        "http://example.com/doc.pdf",
        "http://example.com/archive.zip",
        "http://example.com/no-extension-at-all",
        "http://example.com/",
        "http://example.com",
    ],
)
def test_classify_url_returns_link_for_everything_else(url: str) -> None:
    assert classify_url(url) == "link"


def test_classify_url_ignores_query_string_and_fragment() -> None:
    assert classify_url("http://example.com/pic.png?w=100&h=200") == "image"
    assert classify_url("http://example.com/clip.mp3#t=30") == "audio"
    assert classify_url("http://example.com/pic.png?file=x.pdf") == "image"


def test_classify_url_result_is_one_of_the_three_literals() -> None:
    for url in (
        "http://example.com/a.png",
        "http://example.com/a.mp3",
        "http://example.com/a.txt",
    ):
        assert classify_url(url) in ("image", "audio", "link")


def test_classify_url_svg_is_not_classified_as_image() -> None:
    """SVG is explicitly excluded from the v1 allowlist (design doc
    "Non-goals": scriptable-document XSS risk); it classifies as a
    plain link."""
    assert classify_url("http://example.com/pic.svg") == "link"


# ---------------------------------------------------------------------------
# render_message_html — escape-first-then-linkify
# ---------------------------------------------------------------------------


def test_render_message_html_returns_markup() -> None:
    out = render_message_html("hello world")
    assert isinstance(out, Markup)


def test_render_message_html_escapes_html_in_plain_text() -> None:
    out = render_message_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_message_html_escapes_html_around_a_url() -> None:
    out = render_message_html("<b>look</b> http://example.com/x.png <i>nice</i>")
    assert "<b>" not in out
    assert "<i>" not in out
    assert "&lt;b&gt;" in out
    assert "&lt;i&gt;" in out


def test_render_message_html_linkifies_http_url() -> None:
    out = render_message_html("see http://example.com/pic.png here")
    assert 'href="http://example.com/pic.png"' in out
    assert "<a " in out


def test_render_message_html_linkifies_https_url() -> None:
    out = render_message_html("see https://example.com/pic.png here")
    assert 'href="https://example.com/pic.png"' in out


def test_render_message_html_anchor_has_target_blank_and_noopener() -> None:
    out = render_message_html("http://example.com/a.mp3")
    assert 'target="_blank"' in out
    assert 'rel="noopener noreferrer"' in out


def test_render_message_html_javascript_url_stays_inert_text() -> None:
    """javascript: URLs must never be linkified — they pass through as
    plain (escaped) text with no anchor wrapping them."""
    out = render_message_html("javascript:alert(document.cookie)")
    assert "<a " not in out
    assert "href=" not in out
    assert "javascript:alert(document.cookie)" in out


def test_render_message_html_data_url_stays_inert_text() -> None:
    out = render_message_html("data:text/html,<script>alert(1)</script>")
    assert "<a " not in out
    assert "href=" not in out
    # The embedded script tag must still be escaped even though the
    # data: URL itself isn't linkified — escaping is unconditional.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


@pytest.mark.parametrize("scheme", ["javascript", "data", "vbscript", "file"])
def test_render_message_html_only_http_and_https_schemes_are_linkified(
    scheme: str,
) -> None:
    out = render_message_html(f"{scheme}://example.com/a.png ")
    assert "<a " not in out


def test_render_message_html_quotes_inside_url_do_not_break_out_of_attribute() -> None:
    """Adversarial: a URL carrying a literal double quote must not be
    able to close the `href="..."` attribute early and inject a new
    attribute/tag. Escape-first guarantees the quote is an entity
    before the anchor is ever built, so every literal `"` in the
    output must be one of ours (href/target/rel), not one smuggled in
    from the source text."""
    out = str(render_message_html('http://example.com/x?a="onmouseover="alert(1)'))
    # The only literal quote characters allowed are the three
    # attributes this function itself emits per anchor: href="...",
    # target="_blank", rel="noopener noreferrer".
    assert out.count('"') == 6 * out.count("<a ")
    # The quote from the source text survives only as an entity.
    assert "&#34;onmouseover=" in out
    assert "<script" not in out


def test_render_message_html_quote_and_angle_bracket_combo_in_url() -> None:
    """A more direct breakout attempt: text that looks like it is
    trying to close an attribute/tag and open a new element."""
    text = "<img src=x onerror=alert(1)>"
    out = str(render_message_html(text))
    assert "<img" not in out
    assert "onerror=" in out  # present only as inert escaped text
    assert "&lt;img" in out
    assert "&gt;" in out


def test_render_message_html_multiple_urls_all_linkified() -> None:
    out = render_message_html(
        "first http://example.com/a.png then http://example.com/b.mp3"
    )
    assert out.count("<a ") == 2
    assert 'href="http://example.com/a.png"' in out
    assert 'href="http://example.com/b.mp3"' in out


def test_render_message_html_plain_text_without_url_is_unchanged_by_linkify() -> None:
    out = render_message_html("just chatting, no links here")
    assert "<a " not in out
    assert "just chatting, no links here" in out


def test_render_message_html_empty_string() -> None:
    out = render_message_html("")
    assert out == ""
    assert isinstance(out, Markup)


def test_render_message_html_is_safe_under_jinja_autoescape() -> None:
    """`render_message_html` must return a `Markup` so embedding its
    result via `{{ }}` in an autoescaped Jinja2 template does not
    double-escape the anchor markup it built. Mirrors the autoescape
    guard in `test_render.py` but exercises this module's own output
    directly, matching the shared `_env` policy (`select_autoescape`
    on `["html", "html.j2"]`)."""
    from jinja2 import Environment, select_autoescape

    env = Environment(autoescape=select_autoescape(["html", "html.j2"]))
    template = env.from_string("{{ body }}")
    rendered = template.render(body=render_message_html("http://example.com/a.png"))
    assert 'href="http://example.com/a.png"' in rendered
    assert "&lt;a" not in rendered


def test_render_message_html_double_escaping_would_be_caught() -> None:
    """Sanity check for the test above: a plain (non-Markup) string
    equal to the same HTML *would* get double-escaped by autoescape,
    proving the previous test is actually exercising the Markup path
    and not passing vacuously."""
    from jinja2 import Environment, select_autoescape

    env = Environment(autoescape=select_autoescape(["html", "html.j2"]))
    template = env.from_string("{{ body }}")
    raw_html = str(render_message_html("http://example.com/a.png"))
    rendered = template.render(body=raw_html)
    assert "&lt;a" in rendered


# ---------------------------------------------------------------------------
# compose_media_message — outbound plain-URL text, MAX_INBOUND_LINE-safe
# ---------------------------------------------------------------------------


def test_compose_media_message_url_only() -> None:
    assert (
        compose_media_message("http://example.com/a.png") == "http://example.com/a.png"
    )


def test_compose_media_message_with_caption() -> None:
    out = compose_media_message("http://example.com/a.png", "check this out")
    assert out == "check this out http://example.com/a.png"


def test_compose_media_message_strips_surrounding_whitespace() -> None:
    out = compose_media_message("  http://example.com/a.png  ", "  hi  ")
    assert out == "hi http://example.com/a.png"


def test_compose_media_message_stays_under_max_inbound_line_no_caption() -> None:
    out = compose_media_message("http://example.com/a.png")
    assert len(out.encode("utf-8")) < MAX_INBOUND_LINE


def test_compose_media_message_stays_under_max_inbound_line_with_huge_caption() -> None:
    """The wire format is just the URL plus optional text (design
    doc's "Wire format" section); a runaway caption must never push
    the composed line over AgentIRC's 8192-byte inbound cap. The URL
    itself is load-bearing and must survive intact; the caption is
    what gets truncated."""
    huge_caption = "x" * 50_000
    url = "http://example.com/a.png"
    out = compose_media_message(url, huge_caption)
    encoded = out.encode("utf-8")
    assert len(encoded) < MAX_INBOUND_LINE
    assert url in out


def test_compose_media_message_huge_caption_plus_multibyte_stays_under_cap() -> None:
    """Multi-byte UTF-8 caption text must not be sliced mid-codepoint
    when truncated to fit the byte budget."""
    huge_caption = "éèê" * 20_000  # 2-byte-per-char text
    url = "https://example.com/clip.mp3"
    out = compose_media_message(url, huge_caption)
    encoded = out.encode("utf-8")
    assert len(encoded) < MAX_INBOUND_LINE
    assert url in out
    # Must still be valid UTF-8 (would raise if a codepoint was cut).
    encoded.decode("utf-8")


def test_compose_media_message_degenerate_oversized_url_alone() -> None:
    """Even in the pathological case of a URL that alone exceeds the
    cap, the composer must not raise and must still respect the byte
    budget (there is no way to "fix" an oversized URL, but the
    function must degrade safely rather than emit an over-cap line or
    throw)."""
    oversized_url = "http://example.com/" + ("a" * 9000) + ".png"
    out = compose_media_message(oversized_url)
    assert len(out.encode("utf-8")) <= MAX_INBOUND_LINE


def test_compose_media_message_result_never_exceeds_cap_property() -> None:
    """Broader sweep across caption lengths — every composed line
    stays under the 8192-byte cap regardless of input size."""
    url = "http://example.com/a.png"
    for length in (0, 1, 100, 1000, 8000, 8192, 20000):
        out = compose_media_message(url, "y" * length)
        assert len(out.encode("utf-8")) < MAX_INBOUND_LINE


def test_max_inbound_line_constant_matches_agentirc() -> None:
    """Pins the value against AgentIRC's `MAX_INBOUND_LINE`
    (`agentirc/_internal/constants.py`); irc-lens vendors its own IRC
    client so this is a literal, not an import, but the two must
    agree."""
    assert MAX_INBOUND_LINE == 8192
