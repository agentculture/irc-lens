"""Jinja2 environment and render helpers for irc-lens.

Server-rendered HTML fragments are the entire reactive surface; both
``GET /`` (full page) and the Phase 5+ SSE stream emit Jinja2-rendered
markup. Templates live next to this module under
``irc_lens.templates`` and are loaded via ``PackageLoader`` so they
ship in the wheel without a separate ``MANIFEST.in``.
"""

from __future__ import annotations

import hashlib
import re
import time
from importlib.resources import files
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from jinja2 import Environment, PackageLoader, select_autoescape

from irc_lens.web.media import classify_url, render_message_html

if TYPE_CHECKING:
    from irc_lens.session import Session


_asset_hash_cache: dict[str, str] = {}


def static_url(name: str) -> str:
    """Return ``/static/<name>?v=<hash>`` for cache-busting.

    Hashes the file's bytes once per process and reuses the result.
    Restarting the lens picks up edits; the changed hash forces
    browsers to refetch instead of serving the disk-cached copy.

    Pre-warm via :func:`precompute_static_hashes` at startup to avoid
    blocking file I/O on the event loop during the first request; the
    lazy path here remains as a fallback for assets added after
    startup or in tests with mocked package layouts.
    """
    cached = _asset_hash_cache.get(name)
    if cached is None:
        path = files("irc_lens").joinpath("static").joinpath(name)
        cached = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
        _asset_hash_cache[name] = cached
    return f"/static/{name}?v={cached}"


def precompute_static_hashes() -> None:
    """Eagerly fill :data:`_asset_hash_cache` for every file under
    ``irc_lens/static``. Called once from ``web.app.make_app`` so the
    first ``GET /`` doesn't hash assets synchronously inside an async
    handler.

    Failures are swallowed: if the static layout isn't a real
    filesystem (zipimport, partial test fixtures), :func:`static_url`
    will retry lazily and surface the error there.
    """
    try:
        _walk_static(files("irc_lens").joinpath("static"), "")
    except Exception:  # pragma: no cover — defensive, see docstring
        pass


def _walk_static(node: Any, prefix: str) -> None:
    for entry in node.iterdir():
        rel = f"{prefix}{entry.name}" if not prefix else f"{prefix}/{entry.name}"
        if entry.is_file():
            static_url(rel)
        elif entry.is_dir():
            _walk_static(entry, rel)


def _strftime(value: Any, fmt: str = "%H:%M:%S") -> str:
    """Jinja2 filter: format a UNIX timestamp.

    Used by `_chat_line.html.j2` for the initial-render path, which
    receives `BufferedMessage` instances that carry a raw `timestamp`
    (float). Live SSE publishes pre-format the string (`ts_display`)
    in Python so the wire payload is byte-stable.
    """
    if value is None:
        return ""
    return time.strftime(fmt, time.localtime(float(value)))


# Candidate media URLs inside a chat message's raw text: either an
# absolute http(s) URL (mirrors `media.py`'s own `_URL_RE`) or a
# relative `/media/...` path. Per the design doc's "Rendering path", a
# relative `/media/` path always counts as lens-hosted even without a
# matching `media_embed_prefixes` entry — see `media_items` below.
_MEDIA_CANDIDATE_RE = re.compile(r"(?:https?://\S+|/media/\S+)", re.IGNORECASE)

# One entry in the lens-hosted/trusted-origin allowlist:
# `(scheme, hostname, port)`, all pre-normalized to lowercase
# (scheme/hostname) by the builder — see
# `cli/_commands/serve.py::_media_session_kwargs`. `port is None` is a
# wildcard meaning "match this host on any port" — used for
# `media.trusted_hosts` entries that carry no explicit `:port`, since a
# trusted *external* media host's port has nothing to do with the
# lens's own web port. `media.public_base_url` always widens to an
# exact `(scheme, host, port)` triple instead, because that value names
# the lens's own advertised address and its port is always known.
MediaOrigin = tuple[str, str, "int | None"]


def _candidate_origin(url: str) -> tuple[str, str, int] | None:
    """Parse *url* into a normalized `(scheme, hostname, port)` triple,
    or `None` when it has no parseable hostname (e.g. a relative path —
    callers handle the `/media/...` relative case separately).

    Uses `urlsplit` rather than string-prefix matching specifically so
    that userinfo (`user@host`) and subdomain-suffix tricks can't spoof
    a trusted host: `urlsplit(...).hostname` strips any `user@` prefix
    entirely (so `https://trusted.com@evil.org/x` resolves to hostname
    `evil.org`, never `trusted.com`), and comparing the *exact* hostname
    (not a prefix of it) means `https://trusted.com.evil.org/x` — whose
    hostname is the single label `trusted.com.evil.org` — can never
    match an allowlisted `trusted.com` entry the way
    `url.startswith("https://trusted.com")` incorrectly would have.
    """
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        return None
    scheme = parsed.scheme.lower()
    if parsed.port is not None:
        port = parsed.port
    elif scheme == "https":
        port = 443
    else:
        port = 80
    return (scheme, hostname.lower(), port)


def _origin_matches(candidate: tuple[str, str, int], allowed: "MediaOrigin") -> bool:
    """True when *candidate* (an exact origin) satisfies one allowlist
    row. `allowed`'s port may be `None`, meaning "any port" — see
    `MediaOrigin`'s docstring."""
    c_scheme, c_host, c_port = candidate
    a_scheme, a_host, a_port = allowed
    if c_scheme != a_scheme or c_host != a_host:
        return False
    return a_port is None or a_port == c_port


def _is_hosted_origin(url: str, embed_origins: tuple["MediaOrigin", ...]) -> bool:
    """True iff *url*'s `(scheme, hostname, port)` matches one row of
    *embed_origins* — see `MediaOrigin` for the allowlist shape and
    `_candidate_origin`/`_origin_matches` for the comparison itself.
    """
    if not embed_origins:
        return False
    candidate = _candidate_origin(url)
    if candidate is None:
        return False
    return any(_origin_matches(candidate, allowed) for allowed in embed_origins)


def media_items(
    text: str,
    embed_prefixes: tuple["MediaOrigin", ...] = (),
    remote_mode: str = "click",
) -> list[dict[str, str | bool]]:
    """Extract the `.lens-media` block entries for one chat message.

    Scans *text* (the raw, unescaped message body) for image/audio
    URLs via `media.classify_url` and decides, per URL, whether it
    renders as a direct embed or a click-to-load placeholder:

    - **Lens-hosted** — the URL's `(scheme, hostname, port)` matches one
      of *embed_prefixes* (a tuple of `MediaOrigin` rows derived from
      `media.public_base_url` / `media.trusted_hosts` by the session
      glue — see `cli/_commands/serve.py::_media_session_kwargs`) or the
      URL is a relative `/media/...` path — always embeds directly
      (`item["direct"] = True`), regardless of *remote_mode*. The
      parameter is still named `embed_prefixes` for a minimal diff
      across `Session`/`render_chat_log`'s existing kwarg plumbing, but
      it no longer holds URL-string prefixes: a naive
      `url.startswith(prefix)` check is bypassable by a subdomain-suffix
      trick (`https://trusted.com.evil.org/...`) or a userinfo trick
      (`https://trusted.com@evil.org/...`), since both start with the
      trusted string without the URL actually resolving to that host —
      see `_is_hosted_origin`.
    - **Remote** — anything else. `remote_mode="auto"` embeds
      directly; `"click"` (the default) marks the item for a
      click-to-load placeholder (`item["direct"] = False`);
      `"off"` drops the item from the result entirely (message text
      still renders as a plain link via `render_message_html`, but no
      `.lens-media` entry is produced for it).

    `"link"`-classified URLs (no recognized image/audio extension)
    never produce an entry. Called from `_chat_line.html.j2` as a
    Jinja global — see the `_env.globals["media_items"]` registration
    below. Values are plain `str`/`bool`; the template interpolates
    `item.url` via `{{ }}` so Jinja's autoescape — not this function —
    is what protects the `src="..."`/`data-src="..."` attributes from
    an adversarial URL (see the XSS-in-URL test in
    `tests/test_render_media.py`).
    """
    items: list[dict[str, str | bool]] = []
    for match in _MEDIA_CANDIDATE_RE.finditer(text or ""):
        url = match.group(0)
        kind = classify_url(url)
        if kind not in ("image", "audio"):
            continue
        hosted = url.startswith("/media/") or _is_hosted_origin(url, embed_prefixes)
        if hosted:
            direct = True
        elif remote_mode == "off":
            continue
        else:
            direct = remote_mode == "auto"
        items.append({"kind": kind, "url": url, "direct": direct})
    return items


_env = Environment(
    loader=PackageLoader("irc_lens", "templates"),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["strftime"] = _strftime
_env.filters["linkify"] = render_message_html
_env.globals["static_url"] = static_url
_env.globals["media_items"] = media_items


def render_fragment(template: str, **ctx: Any) -> str:
    """Render a single Jinja2 template to a string.

    Phase 5+ uses this for SSE event payloads (`_chat_line.j2`,
    `_sidebar.j2`, `_info.j2`).
    """
    return _env.get_template(template).render(**ctx)


def _normalize_history_entry(entry: Any) -> dict:
    """Coerce a `Session.history()` row or `BufferedMessage` into the
    `{nick, text, ts_display}` shape the chat-line template expects.

    History rows from the IRCd carry `timestamp` as a string (raw IRC
    param); we parse to float and format. BufferedMessage instances
    carry a numeric `timestamp` already; the strftime filter handles
    them at render time, so we just pass through the raw fields.
    """
    if isinstance(entry, dict):
        nick = entry.get("nick", "")
        text = entry.get("text", "")
        kind = entry.get("kind", "chat")
        # HISTORY rows from the IRCd carry raw PRIVMSG text including
        # any CTCP ACTION wrapping; surface as kind="action" so the
        # template renders the `* nick text` form (matches live dispatch).
        if (
            kind == "chat"
            and isinstance(text, str)
            and text.startswith("\x01ACTION ")
            and text.endswith("\x01")
        ):
            text = text[len("\x01ACTION ") : -1]
            kind = "action"
        ts_display = entry.get("ts_display")
        if ts_display is None:
            raw = entry.get("timestamp")
            try:
                ts = float(raw) if raw is not None else None
                ts_display = (
                    time.strftime("%H:%M:%S", time.localtime(ts)) if ts is not None else ""
                )
            except (ValueError, TypeError):
                ts_display = ""
        return {"nick": nick, "text": text, "ts_display": ts_display, "kind": kind}
    # BufferedMessage path: leave numeric timestamp; the template's
    # strftime filter will format it.
    return {
        "nick": getattr(entry, "nick", ""),
        "text": getattr(entry, "text", ""),
        "timestamp": getattr(entry, "timestamp", None),
        "kind": "chat",
    }


def render_chat_log(
    entries: list,
    *,
    media_embed_prefixes: tuple["MediaOrigin", ...] = (),
    media_remote_embeds: str = "click",
) -> str:
    """Render multiple chat lines as a single HTML blob for innerHTML
    replacement of `#chat-log`. Used by the `log` SSE event publish on
    /join and /switch (history-on-channel-context-change) and by the
    initial `GET /` server render so a page reload doesn't go blank.

    `media_embed_prefixes`/`media_remote_embeds` mirror `Session`'s
    optional media state (see `session.py`) and are threaded straight
    into each `_chat_line.html.j2` render so history/log fragments get
    the same `.lens-media` treatment as live `chat` events. Both
    default to the values that make the block behave as it would with
    no media configuration at all — see `media_items` above.
    `media_embed_prefixes` holds `MediaOrigin` rows, not URL-string
    prefixes — see `media_items`'s docstring for why.
    """
    template = _env.get_template("_chat_line.html.j2")
    parts = [
        template.render(
            msg=_normalize_history_entry(e),
            media_embed_prefixes=media_embed_prefixes,
            media_remote_embeds=media_remote_embeds,
        )
        for e in entries
    ]
    return "".join(parts)


def render_index(session: "Session", *, chat_log_html: str | None = None) -> str:
    """Render the full three-pane page from current Session state.

    `chat_log_html` is the pre-rendered chat-log content for the active
    channel — typically server-side history fetched by `GET /` from the
    IRCd's HISTORY RECENT. When None (the default), fall back to the
    `MessageBuffer` entries for `current_channel`. The buffer fallback
    matters for the `--seed` flow and for unit tests that drive Session
    state without a live IRC connection: there is no IRCd to query.
    """
    if chat_log_html is None:
        if session.current_channel:
            entries = session.buffer.read(session.current_channel, limit=200)
            chat_log_html = render_chat_log(
                entries,
                media_embed_prefixes=session.media_embed_prefixes,
                media_remote_embeds=session.media_remote_embeds,
            )
        else:
            chat_log_html = ""
    return _env.get_template("index.html.j2").render(
        session=session, chat_log_html=chat_log_html
    )
