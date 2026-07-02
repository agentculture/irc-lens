"""``irc-lens cli`` — the CLI-surface introspection noun (registry-backed).

Registered on the agentfront App as a grouped tool (``cli overview``) via
:func:`register_into`, mirroring the colleague blueprint. Its presence keeps the
agent-first convention that *any noun with action-verbs also exposes*
``overview``, and it gives the rendered surface three things for free:

* ``irc-lens cli overview`` — the CLI-surface rollup (dual text/JSON output).
* ``irc-lens cli`` (no verb) — agentfront's grouped-noun listing (exit 0).
* ``irc-lens explain cli overview`` — the tool's registered ``doc`` page.

Distinct from the global ``overview``, which rolls up the whole tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from irc_lens.cli._meta import build_cli_overview

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentfront.app import App

_CLI_OVERVIEW_DOC = """\
# irc-lens cli overview

Describe the irc-lens CLI surface itself — the verbs it exposes and the
conventions every verb honours (``--json`` everywhere, a strict stdout/stderr
split, the 0/1/2 exit-code policy). Distinct from the global ``overview``,
which rolls up the whole tool rather than the CLI meta-surface.
"""


def register_into(app: "App") -> None:
    """Register the ``cli`` introspection noun on the agentfront App registry."""

    def _overview() -> object:
        # Built fresh from the App so the rollup can never drift from the
        # registry the rest of the surface reads.
        return build_cli_overview(app)

    app.group("cli").tool(
        _overview,
        name="overview",
        description="Describe the irc-lens CLI surface.",
        doc=_CLI_OVERVIEW_DOC,
    )
