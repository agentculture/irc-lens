"""`render_residents_page` rendering tests (residents-presence-page plan,
task t3).

Exercises `irc_lens.web.render.render_residents_page` directly — the
route (task t4) isn't built yet, so this pins the render function's
contract independently, against the exact payload shape documented in
`docs/resident-presence.md` (culture's canonical `serialize_residents`
JSON schema) and the markup contract pinned in
`docs/specs/2026-07-07-residents-presence-page.md` /
`docs/plans/2026-07-07-residents-presence-page.md`.

Six things pinned here:

1. A fully-populated resident row surfaces every promised column
   (nick, server, state, since, task, tokens in/out, budget %, flags).
2. Every nullable field (server, state, since, task, tokens_in/out,
   budget_used_pct) dashes cleanly — no exception, no blank cell.
3. Rows are defensively re-sorted by nick at render time regardless of
   input order.
4. `budget_warning`/`presumed_hung` rows carry the pinned
   `budget-warning`/`presumed-hung` CSS classes, and only those rows.
5. The three degrade kinds (`unsupported`, `unreachable`, `unavailable`)
   each render their exact pinned notice text under
   `data-testid="residents-notice"`, on a full HTML document, never a
   table.
6. The rendered HTML never leaks the upstream URL/host/port —
   "127.0.0.1" and "/residents.json" appear nowhere in any output.
"""

from __future__ import annotations

import re

import pytest

from irc_lens.web.render import render_residents_page


def _row_class(html: str, nick: str) -> str:
    """Return the `class="..."` value of the `<tr>` whose first `<td>`
    is *nick* — lets a test assert per-row markup without depending on
    exact whitespace between the table's rows."""
    match = re.search(rf'<tr class="([^"]*)">\s*<td>{re.escape(nick)}</td>', html)
    assert match, f"no resident row found for nick {nick!r} in:\n{html}"
    return match.group(1)


# ---------------------------------------------------------------------------
# (a) fully-populated resident row
# ---------------------------------------------------------------------------


def test_fully_populated_resident_row_shows_every_column() -> None:
    payload = {
        "supported": True,
        "generated_at": "2026-07-07T12:00:00Z",
        "residents": [
            {
                "nick": "spark-claude",
                "server": "spark",
                "state": "thinking",
                "since": "2026-07-07T11:00:00Z",
                "task": "review PR #471",
                "tokens_in": 900,
                "tokens_out": 100,
                "presumed_hung": False,
                "last_refresh": "2026-07-07T11:59:30Z",
                "token_budget": 1000,
                "budget_used_pct": 100.0,
                "budget_warning": True,
            }
        ],
    }

    out = render_residents_page("supported", payload)

    assert 'data-testid="residents-table"' in out
    assert "spark-claude" in out
    assert "spark" in out
    assert "thinking" in out
    assert "2026-07-07T11:00:00Z" in out
    assert "review PR #471" in out
    assert "900/100" in out
    assert "100%" in out
    assert "BUDGET" in out
    assert "2026-07-07T12:00:00Z" in out  # generated_at footer


# ---------------------------------------------------------------------------
# (b) all-nullables resident
# ---------------------------------------------------------------------------


def test_all_nullable_fields_render_as_dashes() -> None:
    payload = {
        "supported": True,
        "generated_at": "2026-07-07T12:00:00Z",
        "residents": [
            {
                "nick": "thor-codex",
                "server": None,
                "state": None,
                "since": None,
                "task": None,
                "tokens_in": None,
                "tokens_out": None,
                "presumed_hung": False,
                "last_refresh": None,
                "token_budget": None,
                "budget_used_pct": None,
                "budget_warning": None,
            }
        ],
    }

    out = render_residents_page("supported", payload)

    assert "thor-codex" in out
    # 8 columns total (nick, server, state, since, task, tokens, budget,
    # flags); only nick is populated here, so the remaining 7 must dash.
    assert out.count("<td>-</td>") == 7


# ---------------------------------------------------------------------------
# (c) rows sort by nick regardless of input order
# ---------------------------------------------------------------------------


def test_rows_are_sorted_by_nick_regardless_of_input_order() -> None:
    payload = {
        "supported": True,
        "generated_at": "2026-07-07T12:00:00Z",
        "residents": [
            {"nick": "zed-agent", "server": None, "state": None, "since": None},
            {"nick": "alpha-agent", "server": None, "state": None, "since": None},
        ],
    }

    out = render_residents_page("supported", payload)

    assert out.index("alpha-agent") < out.index("zed-agent")


# ---------------------------------------------------------------------------
# (d) budget-warning / presumed-hung markup
# ---------------------------------------------------------------------------


def test_budget_warning_and_presumed_hung_markup_appear_exactly_when_flagged() -> None:
    payload = {
        "supported": True,
        "generated_at": "2026-07-07T12:00:00Z",
        "residents": [
            {"nick": "a-hung", "presumed_hung": True, "budget_warning": False},
            {"nick": "b-plain", "presumed_hung": False, "budget_warning": False},
            {"nick": "c-warn", "presumed_hung": False, "budget_warning": True},
            {"nick": "d-both", "presumed_hung": True, "budget_warning": True},
        ],
    }

    out = render_residents_page("supported", payload)

    assert _row_class(out, "a-hung") == "resident-row presumed-hung"
    assert _row_class(out, "b-plain") == "resident-row"
    assert _row_class(out, "c-warn") == "resident-row budget-warning"
    assert _row_class(out, "d-both") == "resident-row presumed-hung budget-warning"


# ---------------------------------------------------------------------------
# (e) the three notice kinds, pinned text + testid, still a full page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, expected_text",
    [
        (
            "unsupported",
            "Presence is pending the agentirc release (agentirc#53).",
        ),
        (
            "unreachable",
            "IRCd down: the culture server is unreachable.",
        ),
        (
            "unavailable",
            "Resource view unavailable: the culture overview server is not "
            "reachable or not configured.",
        ),
    ],
)
def test_notice_kinds_render_pinned_text_and_testid(
    kind: str, expected_text: str
) -> None:
    out = render_residents_page(kind, None)

    assert 'data-testid="residents-notice"' in out
    assert expected_text in out
    # The notice is page content, never an exception path: still a full
    # standalone document, and never a table (there is no payload).
    assert "<!doctype html>" in out
    assert "<title>residents — irc-lens</title>" in out
    assert 'data-testid="residents-table"' not in out


# ---------------------------------------------------------------------------
# (f) never leak the upstream URL/host/port
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["unsupported", "unreachable", "unavailable"])
def test_notice_kinds_never_leak_upstream_details(kind: str) -> None:
    out = render_residents_page(kind, None)

    assert "127.0.0.1" not in out
    assert "/residents.json" not in out


def test_supported_kind_never_leaks_upstream_details() -> None:
    payload = {
        "supported": True,
        "generated_at": "2026-07-07T12:00:00Z",
        "residents": [
            {
                "nick": "spark-claude",
                "server": "spark",
                "state": "thinking",
                "since": "2026-07-07T11:00:00Z",
                "task": "review PR #471",
                "tokens_in": 900,
                "tokens_out": 100,
                "presumed_hung": False,
                "budget_used_pct": None,
                "budget_warning": None,
            }
        ],
    }

    out = render_residents_page("supported", payload)

    assert "127.0.0.1" not in out
    assert "/residents.json" not in out


# ---------------------------------------------------------------------------
# (g) supported + empty residents list
# ---------------------------------------------------------------------------


def test_supported_empty_list_renders_no_residents_connected() -> None:
    payload = {
        "supported": True,
        "generated_at": "2026-07-07T12:00:00Z",
        "residents": [],
    }

    out = render_residents_page("supported", payload)

    assert 'data-testid="residents-table"' in out
    assert "No residents connected." in out


def test_messy_scalars_inside_rows_render_without_raising() -> None:
    # Version-skewed serializers can put wrong scalar types inside an
    # otherwise well-shaped row; the renderer coerces rather than
    # raising (PR #54 review, comment 3533556372): non-string nicks
    # sort via str(), a non-numeric budget_used_pct renders a dash.
    html = render_residents_page(
        "supported",
        {
            "generated_at": "2026-07-07T12:00:00Z",
            "residents": [
                {"nick": 1, "budget_used_pct": "not-a-number"},
                {"nick": "spark-a", "budget_used_pct": True},
            ],
        },
    )
    assert 'data-testid="residents-table"' in html
    assert "not-a-number" not in html
