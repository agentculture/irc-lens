"""Jinja2 environment and render helpers for irc-lens.

Server-rendered HTML fragments are the entire reactive surface; both
``GET /`` (full page) and the Phase 5+ SSE stream emit Jinja2-rendered
markup. Templates live next to this module under
``irc_lens.templates`` and are loaded via ``PackageLoader`` so they
ship in the wheel without a separate ``MANIFEST.in``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader, select_autoescape

if TYPE_CHECKING:
    from irc_lens.session import Session


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


_env = Environment(
    loader=PackageLoader("irc_lens", "templates"),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["strftime"] = _strftime


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


def render_chat_log(entries: list) -> str:
    """Render multiple chat lines as a single HTML blob for innerHTML
    replacement of `#chat-log`. Used by the `log` SSE event publish on
    /join and /switch (history-on-channel-context-change) and by the
    initial `GET /` server render so a page reload doesn't go blank.
    """
    template = _env.get_template("_chat_line.html.j2")
    parts = [template.render(msg=_normalize_history_entry(e)) for e in entries]
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
            chat_log_html = render_chat_log(entries)
        else:
            chat_log_html = ""
    return _env.get_template("index.html.j2").render(
        session=session, chat_log_html=chat_log_html
    )
