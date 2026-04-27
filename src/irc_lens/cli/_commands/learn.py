"""``irc-lens learn`` — the learnability affordance (shape-adapt).

Satisfies the agent-first rubric: >=200 chars and mentions purpose,
command map, exit codes, --json, explain.
"""

from __future__ import annotations

import argparse

from irc_lens import __version__
from irc_lens.cli._output import emit_result

_TEXT = """\
irc-lens — reactive web console for AgentIRC.

Purpose
-------
A standalone CLI that launches an aiohttp + HTMX + SSE web app over a
plain TCP AgentIRC connection. Server-rendered HTML fragments make the
DOM deterministic and Playwright-driveable, so a browser-automation
agent can administer any AgentIRC server without a human in the loop.
Pure client: no agent loop, no daemon, one process per browser tab.

Commands
--------
  irc-lens learn              Print this self-teaching prompt. Supports --json.
  irc-lens explain <path>...  Print markdown docs for any noun/verb path.
                              Supports --json.
  irc-lens overview [path]    Descriptive rollup across interface surfaces.
                              Unknown paths warn and exit 0. Supports --json.
  irc-lens cli overview       Same rollup, scoped to the cli noun.
  irc-lens serve              Launch the aiohttp web console against an AgentIRC server.
                              See `irc-lens serve --help` for flags.

Machine-readable output
-----------------------
Every command that produces a listing or report supports --json. Errors in
JSON mode emit {"code", "message", "remediation"} to stderr. Stdout and
stderr are never mixed.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3+ reserved

More detail
-----------
  irc-lens explain irc-lens
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "irc-lens",
        "version": __version__,
        "purpose": (
            "Reactive web console for AgentIRC. Server-rendered HTMX + SSE "
            "frontend so a Playwright-driven agent can administer any "
            "AgentIRC server. Pure client; no agent loop."
        ),
        "commands": [
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {
                "path": ["overview"],
                "summary": (
                    "Descriptive rollup across interface surfaces; unknown "
                    "paths warn and exit 0."
                ),
            },
            {
                "path": ["cli", "overview"],
                "summary": "Same rollup, scoped to the cli noun.",
            },
            {
                "path": ["serve"],
                "summary": (
                    "Launch the aiohttp web console against an AgentIRC server. "
                    "Required: --host, --port, --nick."
                ),
            },
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "json_support": True,
        "explain_pointer": "irc-lens explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
