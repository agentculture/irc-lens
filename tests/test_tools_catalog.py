"""t5 — the tool catalog must not silently drop a web-only verb.

This is the "diff test" the plan's acceptance criteria call for: it
introspects ``Session._exec_dispatch`` (the same dict property the web
console's ``Session.execute`` reads to route ``POST /input`` commands —
see ``session.py`` lines ~581-603) and asserts the agentfront registry's
top-level tool names equal exactly ``dispatch table keys minus
NON_TOOL_DISPATCH_VERBS``. A verb that lands in the dispatch table without
either a matching tool or an entry in the documented exclusion set fails
this test — it cannot silently go missing from the live-verb catalog.
"""

from __future__ import annotations

from irc_lens.cli import build_app
from irc_lens.commands import CommandType
from irc_lens.session import Session
from irc_lens.tools import NON_TOOL_DISPATCH_VERBS

# The plan's own example list (send, join, part, read, channels, who,
# mesh, switch, topic, me, icon) — pinned here too so a change to either
# the dispatch table or the exclusion set that accidentally drops one of
# these named verbs fails loudly with a readable set difference, not just
# "registered != expected".
_PLAN_NAMED_VERBS = {
    "send",
    "join",
    "part",
    "read",
    "channels",
    "who",
    "mesh",
    "switch",
    "topic",
    "me",
    "icon",
}


def _dispatch_table_verbs() -> set[CommandType]:
    """The introspection point: ``Session._exec_dispatch``'s key set.

    A bare ``Session`` (no real connection — the property only reads
    bound methods off ``self``) is enough; constructing one does no I/O.
    """
    probe = Session(host="127.0.0.1", port=0, nick="catalog-probe")
    return set(probe._exec_dispatch)


def test_every_dispatch_verb_is_either_a_tool_or_a_documented_exclusion() -> None:
    dispatch_verbs = _dispatch_table_verbs()
    # Sanity: the exclusion set must itself be verbs the dispatch table
    # actually contains — an exclusion for a verb that was removed (or
    # renamed) upstream would silently stop meaning anything.
    unknown_exclusions = NON_TOOL_DISPATCH_VERBS - dispatch_verbs
    assert not unknown_exclusions, (
        f"NON_TOOL_DISPATCH_VERBS names verbs not present in "
        f"Session._exec_dispatch: {unknown_exclusions}"
    )

    expected_tool_names = {
        verb.name.lower() for verb in dispatch_verbs if verb not in NON_TOOL_DISPATCH_VERBS
    }
    assert expected_tool_names == _PLAN_NAMED_VERBS, (
        "the plan's named verb list and the dispatch-table-minus-exclusions "
        f"set have drifted: expected={_PLAN_NAMED_VERBS} actual={expected_tool_names}"
    )

    app = build_app()
    registered_top_level = {t.name for t in app.list_tools() if not t.group}

    missing = expected_tool_names - registered_top_level
    extra = registered_top_level - expected_tool_names
    assert not missing and not extra, (
        f"tool catalog drifted from the console dispatch table: "
        f"missing={missing} extra={extra}"
    )


def test_no_tool_collides_with_a_reserved_meta_verb_or_host_command() -> None:
    """Belt-and-braces: building the app must not raise, and none of the
    live-verb tool names may shadow agentfront's reserved meta-verbs or
    irc-lens's own host commands — a collision would raise
    ``DuplicateError`` at registration time (see ``App.add_command`` /
    ``Registry.add_tool``), so a clean ``build_app()`` already proves this,
    but assert the disjointness explicitly for a readable failure."""
    app = build_app()
    reserved = {"learn", "explain", "overview", "doctor"}
    host_commands = {cmd.name for cmd in app.list_commands()}
    tool_names = {t.name for t in app.list_tools() if not t.group}

    assert not (tool_names & reserved)
    assert not (tool_names & host_commands)
