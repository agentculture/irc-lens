"""Phase 7 smoke tests for `lens.js`.

Browser behaviour itself lands in Phase 9c (Playwright). For now we
just guard the contract that the SSE / form glue references the
right event names and DOM ids — Phase 9c will catch the actual
runtime regressions.
"""

from __future__ import annotations

from importlib.resources import files


def _read_lens_js() -> str:
    return (files("irc_lens").joinpath("static").joinpath("lens.js")).read_text(
        encoding="utf-8"
    )


def test_lens_js_subscribes_to_all_six_event_types() -> None:
    js = _read_lens_js()
    # `log` was added alongside history-on-join: replaces #chat-log
    # innerHTML so the chat pane swaps content on /switch and shows
    # server-side backlog after /join.
    for name in ("chat", "log", "roster", "info", "view", "error"):
        assert f'addEventListener("{name}"' in js, (
            f"lens.js missing handler for SSE event {name!r}"
        )


def test_lens_js_targets_dom_contract_ids() -> None:
    js = _read_lens_js()
    for id_ in ("chat-log", "sidebar", "info", "toast-region", "chat-input", "chat-form"):
        assert f'"{id_}"' in js, f"lens.js missing reference to #{id_}"


def test_lens_js_clears_input_on_204_via_htmx_hook() -> None:
    """HTMX form submission: 204 → clear input; 5xx → toast."""
    js = _read_lens_js()
    assert "htmx:afterRequest" in js
    assert "204" in js
    assert "input.value" in js


def test_lens_js_opens_event_source_at_events_path() -> None:
    js = _read_lens_js()
    assert 'new EventSource("/events")' in js


def test_lens_js_stays_small() -> None:
    """Build-plan budget: keep the inline glue small. The original
    plan said ≤ 50 lines; the cap is now 100 to make room for the
    review-driven additions on PR #9 (DOM cap, accessible toasts,
    transport-error guard, src.onopen reconnect handling). If this
    trips, factor logic into a helper module rather than letting the
    inline glue grow into a framework."""
    js = _read_lens_js()
    n = len(js.splitlines())
    assert n <= 100, f"lens.js grew to {n} lines — refactor into a module"


def test_lens_css_carries_phase_7_additions() -> None:
    """Cheap regression guard for the Phase 7 CSS extensions."""
    css = (files("irc_lens").joinpath("static").joinpath("lens.css")).read_text(
        encoding="utf-8"
    )
    # Chat-line timestamp, info-pane typography, toast styling.
    assert ".lens-chat-ts" in css
    assert ".lens-toast" in css
    assert ".lens-info dl" in css
    # View / connection state cues driven by `lens.js` via body data-attrs.
    assert 'body[data-view=' in css
    assert 'body[data-conn=' in css
