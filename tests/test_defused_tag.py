"""A defused tag ("@", zero-width space, nick) renders red with a note.

The sandbox agents defuse a tag to another agent the guest didn't ask them
to involve (culture ``_defuse_peer_tags``): it reads like a tag but no agent
treats it as one, so the lens says so instead of letting it look real.
"""

from __future__ import annotations

from irc_lens.web.media import ZWSP, defused_tags, render_message_html
from irc_lens.web.render import render_fragment

DEFUSED = f"sbx-ann: start your message with @{ZWSP}sbx-bolt."


def _line(text: str) -> str:
    return render_fragment(
        "_chat_line.html.j2", msg={"nick": "sbx-ask", "text": text, "ts_display": "", "kind": "chat"}
    )


def test_defused_tags_found_in_order_without_repeats():
    assert defused_tags(f"@{ZWSP}sbx-bolt and @{ZWSP}sbx-x, @{ZWSP}sbx-bolt") == ["sbx-bolt", "sbx-x"]
    assert defused_tags("@sbx-bolt is a real tag") == []
    assert defused_tags("") == []


def test_defused_tag_is_red_and_keeps_the_zero_width_space():
    html = str(render_message_html(DEFUSED))
    assert f'class="lens-defused-tag" data-testid="defused-tag" title="Not a real tag">@{ZWSP}sbx-bolt</span>' in html


def test_line_with_a_defused_tag_gets_the_note():
    html = _line(DEFUSED)
    assert 'data-testid="defused-note"' in html
    assert "Not a tag: sbx-bolt won't see this. To ask sbx-bolt, start your message with @sbx-bolt." in html


def test_real_tags_and_plain_lines_are_untouched():
    for text in ("@sbx-bolt what do you think?", "hello", "see https://example.com/@x"):
        html = _line(text)
        assert "defused" not in html


def test_message_html_is_still_escaped():
    html = str(render_message_html(f"<b>@{ZWSP}sbx-bolt</b>"))
    assert "<b>" not in html
    assert "&lt;b&gt;" in html
    assert 'data-testid="defused-tag"' in html
