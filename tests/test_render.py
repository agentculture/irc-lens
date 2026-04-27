"""Template-rendering unit tests (Phase 6).

Exercises `render_fragment` directly so the per-fragment shape is
locked independently of the SSE / aiohttp surface. Three things we
guard against here:

1. **DOM contract drift** — every fragment that the spec's testid
   table promises must keep emitting that `data-testid`. Phase 7's
   browser glue and Phase 9c's Playwright tests will both grep for
   these attributes.
2. **Per-view content** — `_info.html.j2` branches on `session.view`;
   each branch must render identifiable content (so the SSE
   consumer can tell the four views apart).
3. **Autoescape** — Jinja2's autoescape policy is set on
   `[".html", ".html.j2"]`; nick/text containing `<` must not
   render as raw HTML in the chat fragment (this is the same XSS
   path that PR #6 closed by renaming `.j2` → `.html.j2`).
"""

from __future__ import annotations

import time

import pytest

from irc_lens.session import EntityItem, Session
from irc_lens.web.render import render_fragment


@pytest.fixture
def session() -> Session:
    return Session(host="127.0.0.1", port=6667, nick="lens-test")


# ---------------------------------------------------------------------------
# _chat_line.html.j2
# ---------------------------------------------------------------------------


def test_chat_line_renders_all_required_testids() -> None:
    out = render_fragment(
        "_chat_line.html.j2",
        msg={"nick": "alice", "text": "hello", "ts_display": "12:34:56"},
    )
    assert 'data-testid="chat-line"' in out
    assert 'data-testid="chat-line-nick"' in out
    assert 'data-testid="chat-line-text"' in out
    assert "12:34:56" in out
    assert "alice" in out
    assert "hello" in out


def test_chat_line_falls_back_to_strftime_for_initial_render() -> None:
    """Initial render path: msg is a `BufferedMessage`-shaped object
    with `.timestamp` (float), no `.ts_display`. The template uses
    the `strftime` filter, which formats in local time — assert
    against that same formatting so this test is timezone-stable."""

    ts = time.time()
    expected = time.strftime("%H:%M:%S", time.localtime(ts))

    class _Buf:
        nick = "bob"
        text = "from history"
        timestamp = ts

    out = render_fragment("_chat_line.html.j2", msg=_Buf())
    assert expected in out
    assert "bob" in out


def test_chat_line_escapes_html_in_nick_and_text() -> None:
    """Autoescape must defeat the obvious XSS path through chat fields."""
    out = render_fragment(
        "_chat_line.html.j2",
        msg={
            "nick": "<script>x</script>",
            "text": "<img onerror=y>",
            "ts_display": "00:00:00",
        },
    )
    assert "<script>" not in out
    assert "<img" not in out
    assert "&lt;script&gt;" in out


# ---------------------------------------------------------------------------
# _info.html.j2 — per-view branches
# ---------------------------------------------------------------------------


def test_info_chat_view_shows_active_channel(session: Session) -> None:
    session.set_current_channel("#general")
    out = render_fragment("_info.html.j2", session=session)
    assert 'data-testid="view-indicator"' in out
    assert 'data-view="chat"' in out
    assert "#general" in out


def test_info_chat_view_empty_state(session: Session) -> None:
    out = render_fragment("_info.html.j2", session=session)
    assert "No active channel" in out


def test_info_help_view_lists_slash_commands(session: Session) -> None:
    session.set_view("help")
    out = render_fragment("_info.html.j2", session=session)
    assert 'data-view="help"' in out
    assert "Slash commands" in out
    for cmd in ("/join", "/part", "/send", "/help", "/overview", "/status"):
        assert cmd in out, f"help view missing {cmd}"


def test_info_overview_view_lists_joined_channels(session: Session) -> None:
    session.joined_channels.add("#ops")
    session.joined_channels.add("#dev")
    session.set_current_channel("#ops")
    session.set_view("overview")
    out = render_fragment("_info.html.j2", session=session)
    assert 'data-view="overview"' in out
    assert "Joined channels" in out
    assert "#dev" in out
    assert "#ops" in out
    assert "(active)" in out  # marks current_channel


def test_info_overview_empty_state(session: Session) -> None:
    session.set_view("overview")
    out = render_fragment("_info.html.j2", session=session)
    assert "No channels joined" in out


def test_info_status_view_shows_session_metadata(session: Session) -> None:
    session.set_view("status")
    out = render_fragment("_info.html.j2", session=session)
    assert 'data-view="status"' in out
    assert "Session status" in out
    assert "lens-test" in out  # nick
    assert "127.0.0.1" in out  # host
    assert "6667" in out  # port


# ---------------------------------------------------------------------------
# _sidebar.html.j2 — testid contract
# ---------------------------------------------------------------------------


def test_sidebar_pins_testid_contract(session: Session) -> None:
    session.joined_channels.add("#ops")
    session.set_current_channel("#ops")
    session.set_roster([EntityItem(nick="alice", type="human")])
    out = render_fragment("_sidebar.html.j2", session=session)
    assert 'data-testid="sidebar-channel"' in out
    assert 'data-channel="#ops"' in out
    assert 'data-testid="sidebar-entity"' in out
    assert 'data-nick="alice"' in out
