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


def render_index(session: "Session") -> str:
    """Render the full three-pane page from current Session state."""
    return _env.get_template("index.html.j2").render(session=session)
