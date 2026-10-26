"""A sandbox agent's defused tag ("@", zero-width space, nick) renders red with a note.

The sandbox agents defuse a tag to another agent the guest didn't ask them
to involve (culture ``_defuse_peer_tags``): it reads like a tag but no agent
treats it as one, so the lens says so instead of letting it look real. Only
lines from an agent in the room are marked, only the room's agent nicks
count, and never inside a URL.
"""

from __future__ import annotations

from irc_lens.session import EntityItem, Session
from irc_lens.web.media import ZWSP, render_chat_text, render_message_html
from irc_lens.web.render import render_chat_log, render_fragment

AGENTS = ("sbx-ask", "sbx-bolt")
DEFUSED = f"sbx-ann: start your message with @{ZWSP}sbx-bolt."


def _line(text: str, nick: str = "sbx-ask", agents=AGENTS, kind: str = "chat") -> str:
    return render_fragment(
        "_chat_line.html.j2",
        msg={"nick": nick, "text": text, "ts_display": "", "kind": kind},
        agent_nicks=agents,
    )


def test_agent_line_marks_the_tag_red_and_adds_the_note():
    html = _line(DEFUSED)
    assert (
        f'class="lens-defused-tag" data-testid="defused-tag" title="Not a real tag">@{ZWSP}sbx-bolt</span>'
        in html
    )
    assert (
        "Not a tag: sbx-bolt won't see this. To ask it, start your message with @sbx-bolt." in html
    )


def test_several_defused_agents_each_get_their_instruction():
    html = _line(f"try @{ZWSP}sbx-bolt or @{ZWSP}sbx-ask", nick="sbx-x", agents=(*AGENTS, "sbx-x"))
    assert "Not a tag: sbx-bolt and sbx-ask won't see this." in html
    assert "To ask one of them, start your message with @sbx-bolt or @sbx-ask." in html


def test_guest_lines_are_never_marked():
    """Review finding: a guest's pasted "@<ZWSP>sbx-ask" does reach sbx-ask
    (a bare mention), so it must not be told it won't."""
    html = _line(f"@{ZWSP}sbx-ask hi", nick="sbx-ann")
    assert "defused" not in html


def test_no_marking_without_room_agents_or_on_action_lines():
    assert "defused" not in _line(DEFUSED, agents=())
    assert "defused" not in render_fragment(
        "_chat_line.html.j2", msg={"nick": "sbx-ask", "text": DEFUSED, "ts_display": "", "kind": "chat"}
    )
    assert "defused" not in _line(DEFUSED, kind="action")


def test_only_room_agent_nicks_and_the_agents_word_boundary():
    """Review finding: "@<ZWSP>sbx-bolt-2" was defused by the agents as
    sbx-bolt (their check is @nick\\b), and a non-agent nick is not marked."""
    markup, nicks = render_chat_text(f"@{ZWSP}sbx-bolt-2 and @{ZWSP}sbx-ann", AGENTS)
    assert nicks == ["sbx-bolt"]
    assert str(markup).count('data-testid="defused-tag"') == 1


def test_never_inside_a_url():
    """Review finding: the span was injected into an <a href>, breaking it."""
    markup, nicks = render_chat_text(f"see https://github.com/@{ZWSP}sbx-bolt now", AGENTS)
    html = str(markup)
    assert nicks == []
    assert "lens-defused-tag" not in html
    assert '<a href="https://github.com/@' in html


def test_text_around_urls_is_still_marked_and_escaped():
    markup, nicks = render_chat_text(f"<b> @{ZWSP}sbx-bolt https://x.dev/a @{ZWSP}sbx-ask", AGENTS)
    html = str(markup)
    assert nicks == ["sbx-bolt", "sbx-ask"]
    assert "&lt;b&gt;" in html
    assert html.count('data-testid="defused-tag"') == 2
    assert '<a href="https://x.dev/a"' in html


def test_plain_render_message_html_is_unchanged():
    assert "defused" not in str(render_message_html(DEFUSED))


def test_history_render_and_session_scope():
    entries = [{"nick": "sbx-ask", "text": DEFUSED, "timestamp": 0}]
    assert 'data-testid="defused-note"' in render_chat_log(entries, agent_nicks=AGENTS)
    assert 'data-testid="defused-note"' not in render_chat_log(entries)
    s = Session(host="127.0.0.1", port=6668, nick="sbx-ann")
    s.set_roster([EntityItem(nick="sbx-ask", type="agent"), EntityItem(nick="sbx-ann", type="human")])
    assert s.defuse_agents() == ()  # not a sandbox session
    s.thinking_agent = "sbx-ask"
    assert s.defuse_agents() == ("sbx-ask",)
