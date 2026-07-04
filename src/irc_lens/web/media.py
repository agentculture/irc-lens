"""URL classification and escape-first linkify engine for media support.

`docs/superpowers/specs/2026-07-02-media-support-design.md` ("Rendering
path" / "Wire format") is the spec this module implements:

- `classify_url` — extension-allowlist classification (`image` /
  `audio` / `link`), used by the (later) chat-line template to decide
  whether a message's URL gets an inline embed or a placeholder card.
- `render_message_html` — **escape-first, then linkify**. The whole
  input is passed through `markupsafe.escape` before anything else
  happens; the only markup this function ever injects is `<a>` tags it
  builds itself from already-escaped components. Only `http`/`https`
  URLs are recognized — `javascript:`, `data:`, and any other scheme
  are never turned into anchors, so they stay inert (escaped) text.
- `compose_media_message` — the outbound half: the plain-text PRIVMSG
  body for an uploaded media URL. The wire format is just the URL,
  optionally preceded by free text; this guarantees the composed line
  never exceeds AgentIRC's `MAX_INBOUND_LINE` (8192 bytes), truncating
  an oversized caption rather than risk the line being rejected by the
  wire.

This module deliberately does not touch templates or `session.py` —
wiring `render_message_html` into `_chat_line.html.j2` and
`compose_media_message` into the upload/send path is a separate task.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from markupsafe import Markup, escape

# Extension allowlists — see "Rendering path" in the design doc. Kept
# tight and deliberately excludes SVG (see "Non-goals": a raster-only
# allowlist sidesteps the scriptable-document XSS risk of serving SVG
# from our own auth-exempt `/media/` origin).
_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
_AUDIO_EXTENSIONS = frozenset({"mp3", "ogg", "wav", "webm", "m4a", "flac"})

# `agentirc/_internal/constants.py::MAX_INBOUND_LINE`. irc-lens vendors
# its own IRC client and has no runtime dependency on the `agentirc`
# package, so the value is pinned here as a literal rather than
# imported; `test_media.py` pins it back against the upstream constant.
MAX_INBOUND_LINE = 8192

# Only http/https are ever linkified. The literal scheme prefix in the
# pattern *is* the allowlist — `javascript:`, `data:`, `vbscript:`,
# etc. simply never match and pass through render_message_html as
# plain escaped text. Matches greedily up to the next whitespace; by
# the time this regex runs the text has already been through
# `markupsafe.escape`, so it will never see a raw `<`, `>`, `'`, or
# `"` — those became entities first.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def classify_url(url: str) -> str:
    """Classify `url` as ``"image"``, ``"audio"``, or ``"link"``.

    Classification is by file-extension allowlist only (v1 does not
    unfurl arbitrary web pages — see the design doc's "Non-goals").
    The extension is read from the URL's path component, so query
    strings and fragments never affect the result (`a.png?file=x.pdf`
    classifies as `image`, not `link`). Matching is case-insensitive.
    A URL with no extension, or an extension outside both allowlists,
    classifies as `"link"`.
    """
    path = urlsplit(url).path
    if "." not in path:
        return "link"
    ext = path.rsplit(".", 1)[-1].lower()
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _AUDIO_EXTENSIONS:
        return "audio"
    return "link"


def _anchor(match: "re.Match[str]") -> str:
    """Build an `<a>` tag for one regex match against already-escaped
    text. `url` below is a substring of the escaped input — it can
    only contain literal `"`/`<`/`>` characters if `markupsafe.escape`
    already turned the originals into entities, so it is safe to drop
    directly into the `href` attribute without re-escaping."""
    url = match.group(0)
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'


def render_message_html(text: str) -> Markup:
    """Escape `text`, then linkify any `http`/`https` URLs within it.

    Escape-first-then-linkify: `text` is escaped through
    `markupsafe.escape` *before* URL detection runs, so every
    character from the original input that could open a tag or an
    attribute has already become an HTML entity. The regex then only
    ever matches within that escaped string, and the only new markup
    introduced is the `<a>` wrapper this function builds itself — so
    there is no path for attacker-controlled text (HTML in the
    message, or a quote/angle-bracket smuggled inside a URL) to break
    out of the anchor or inject new markup.

    Returns a `markupsafe.Markup` so this is safe to drop straight
    into an autoescaped Jinja2 template with `{{ }}` — passing the
    plain `str()` of the result through `{{ }}` would double-escape
    the anchor markup this function built.
    """
    escaped = str(escape(text))
    linked = _URL_RE.sub(_anchor, escaped)
    return Markup(linked)


def compose_media_message(url: str, caption: str = "") -> str:
    """Compose the plain-text PRIVMSG body for an uploaded media URL.

    Per the design doc's "Wire format" section, the message text is
    simply the URL, optionally preceded by free text — no protocol
    markup. The result is guaranteed to encode to fewer than
    `MAX_INBOUND_LINE` (8192) UTF-8 bytes: AgentIRC *rejects* (does
    not truncate) inbound lines over that size, so silently producing
    an over-cap line here would mean the media message never reaches
    the wire at all.

    The URL is the load-bearing part and is never truncated; an
    oversized caption is cut to fit the remaining byte budget (on a
    UTF-8 codepoint boundary, so multi-byte characters are never
    split). In the degenerate case where the URL alone is at or over
    the cap, the caption is dropped and the URL itself is truncated on
    a codepoint boundary as a last resort — this function never
    raises and never returns a line at or over the cap.
    """
    url = url.strip()
    caption = caption.strip()

    message = f"{caption} {url}" if caption else url
    if len(message.encode("utf-8")) < MAX_INBOUND_LINE:
        return message

    url_bytes = url.encode("utf-8")
    if len(url_bytes) >= MAX_INBOUND_LINE:
        return _truncate_utf8(url_bytes, MAX_INBOUND_LINE - 1)

    # Reserve room for the URL plus the separating space, and one more
    # byte of slack so the composed line is strictly under the cap
    # (not merely at it) — whatever remains is the caption's budget.
    budget = MAX_INBOUND_LINE - len(url_bytes) - 2
    truncated_caption = _truncate_utf8(caption.encode("utf-8"), budget).rstrip()
    if truncated_caption:
        return f"{truncated_caption} {url}"
    return url


def _truncate_utf8(data: bytes, max_bytes: int) -> str:
    """Truncate `data` to at most `max_bytes` bytes without splitting a
    UTF-8 codepoint, and decode the result."""
    if max_bytes <= 0:
        return ""
    chunk = data[:max_bytes]
    return chunk.decode("utf-8", errors="ignore")
