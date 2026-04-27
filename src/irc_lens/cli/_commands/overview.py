"""``irc-lens overview [path]`` — rollup across interface surfaces.

The ``overview`` verb is a descriptive rollup, not a verifier. When the
target path is missing or unrecognized, it must still exit 0 with a
warning section — hard-failing is ``afi cli verify``'s job.

The rubric expects:

* a top-level ``overview`` verb (this module).
* ``overview --json`` returning a structured payload (``subject``,
  ``path``, ``sections``).
* an ``overview`` verb under every noun group that has action-verbs
  (registered alongside the noun's parser; see :mod:`irc_lens.cli.__init__`).
* graceful handling of unknown ``path`` arguments — exit 0 with a
  warning section in the output.
"""

from __future__ import annotations

import argparse

from irc_lens import __version__
from irc_lens.cli._output import emit_result

# Surface descriptors keyed by subject. The text body is markdown; the
# JSON shape is the same content reorganised into a structured payload.
_SECTIONS: dict[str, list[dict[str, object]]] = {
    "all": [
        {
            "heading": "irc-lens",
            "body_md": (
                "Reactive web console for AgentIRC. Pure client; one "
                "process per browser tab. Server-rendered HTML fragments "
                "delivered via SSE keep the DOM Playwright-driveable."
            ),
            "findings": [
                {"key": "version", "value": __version__},
                {"key": "transport", "value": "tcp+irc"},
                {"key": "frontend", "value": "aiohttp+jinja2+htmx+sse"},
            ],
        },
        {
            "heading": "Globals",
            "body_md": (
                "- `irc-lens learn` — structured self-teaching prompt.\n"
                "- `irc-lens explain <path>` — markdown docs for any noun/verb.\n"
                "- `irc-lens overview [path]` — this rollup.\n"
                "- `irc-lens serve` — launches the web console "
                "(lands in a later phase)."
            ),
            "findings": [
                {"verb": "learn"},
                {"verb": "explain"},
                {"verb": "overview"},
                {"verb": "serve", "status": "pending"},
            ],
        },
        {
            "heading": "Nouns",
            "body_md": (
                "- `cli` — meta-introspection of the CLI surface itself; "
                "exposes `overview`."
            ),
            "findings": [
                {"noun": "cli", "verbs": ["overview"]},
            ],
        },
    ],
    "cli": [
        {
            "heading": "irc-lens cli",
            "body_md": (
                "The `cli` noun is a meta-surface: it describes the CLI "
                "itself rather than the running web console. New CLI-meta "
                "verbs land under this noun."
            ),
            "findings": [
                {"verb": "overview", "summary": "Rollup of the CLI surface."},
            ],
        },
    ],
}


def _bad_path_section(raw_path: str) -> dict[str, object]:
    return {
        "heading": "Unknown path",
        "body_md": (
            f"warning: no overview entry matches `{raw_path}`. "
            "Try `irc-lens overview` (no args) or `irc-lens overview cli`."
        ),
        "findings": [{"warning": "no_match", "input": raw_path}],
    }


def _build_payload(path_tokens: tuple[str, ...]) -> dict[str, object]:
    if not path_tokens:
        return {"subject": "all", "path": None, "sections": _SECTIONS["all"]}
    raw_path = " ".join(path_tokens)
    head = path_tokens[0]
    # Subjects are modelled as flat keys today; any path deeper than one
    # token is necessarily an unknown sub-subject and must warn.
    if len(path_tokens) == 1 and head in _SECTIONS:
        return {
            "subject": head,
            "path": raw_path,
            "sections": _SECTIONS[head],
        }
    # Graceful: zero-target report + warning, exit 0. If the head is a
    # known subject, anchor the rollup there so the warning sits next to
    # the section the user was probably trying to drill into.
    if head in _SECTIONS:
        subject = head
        anchor_sections = _SECTIONS[head]
    else:
        subject = "all"
        anchor_sections = _SECTIONS["all"]
    return {
        "subject": subject,
        "path": raw_path,
        "sections": [_bad_path_section(raw_path), *anchor_sections],
    }


def _render_text(payload: dict[str, object]) -> str:
    subject = payload["subject"]
    path = payload["path"]
    header_target = path if path else "<root>"
    lines = [f"# overview: {subject} — {header_target}", ""]
    for section in payload["sections"]:  # type: ignore[union-attr]
        lines.append(f"## {section['heading']}")
        lines.append("")
        lines.append(str(section["body_md"]).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_overview(args: argparse.Namespace) -> int:
    path_tokens = tuple(args.path) if args.path else ()
    payload = _build_payload(path_tokens)
    if getattr(args, "json", False):
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_text(payload), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "overview",
        help="Rollup across irc-lens's interface surfaces (descriptive, not verifying).",
    )
    p.add_argument(
        "path",
        nargs="*",
        help="Optional subject path; unknown paths produce a warning, not an error.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview)


def register_cli_noun_overview(sub: argparse._SubParsersAction) -> None:
    """Register the `overview` verb under the `cli` noun group."""
    p = sub.add_parser(
        "overview",
        help="Rollup of the irc-lens CLI surface.",
    )
    p.add_argument(
        "path",
        nargs="*",
        help="Optional subject path under cli; unknown paths produce a warning.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")

    def _cmd(args: argparse.Namespace) -> int:
        # Anchor on the `cli` subject; `_build_payload` warns on extras.
        args.path = ["cli", *(args.path or [])]
        return cmd_overview(args)

    p.set_defaults(func=_cmd)
