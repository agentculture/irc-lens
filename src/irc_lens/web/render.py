"""Jinja2 environment and render helpers for irc-lens.

Server-rendered HTML fragments are the entire reactive surface; both
``GET /`` (full page) and the Phase 5+ SSE stream emit Jinja2-rendered
markup. Templates live next to this module under
``irc_lens.templates`` and are loaded via ``PackageLoader`` so they
ship in the wheel without a separate ``MANIFEST.in``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader, select_autoescape

if TYPE_CHECKING:
    from irc_lens.session import Session


_env = Environment(
    loader=PackageLoader("irc_lens", "templates"),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_fragment(template: str, **ctx: Any) -> str:
    """Render a single Jinja2 template to a string.

    Phase 5+ uses this for SSE event payloads (`_chat_line.j2`,
    `_sidebar.j2`, `_info.j2`).
    """
    return _env.get_template(template).render(**ctx)


def render_index(session: "Session") -> str:
    """Render the full three-pane page from current Session state."""
    return _env.get_template("index.html.j2").render(session=session)
