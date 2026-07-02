"""Registry-derived meta-verbs that agentfront 0.20.0 can't shape for us yet.

The rendered CLI (see :mod:`irc_lens.cli`) leans on agentfront's free
``learn`` and ``explain`` meta-verbs verbatim — they already satisfy the
agent-first rubric. Two meta-verbs need richer output than the stock ones
emit, and agentfront reserves their names (``App._RESERVED_META_VERBS``) so
they cannot be re-registered through the App:

* ``overview --json`` must carry ``{subject, sections}`` — agentfront's stock
  ``overview --json`` returns a bare list of nouns.
* ``doctor --json`` must carry ``{healthy, checks}`` (failed checks carrying a
  remediation) — agentfront's stock ``doctor`` has no ``--json`` at all.

So :func:`irc_lens.cli.main` routes ``overview`` and ``doctor`` here and
delegates every other verb to ``agentfront.cli_surface.run_cli``. Both handlers
still *derive from the one App registry* (they read ``list_commands`` /
``list_tools`` / ``list_docs`` and run ``agentfront.doctor_live`` against the
same App), so there is no second source of truth. A follow-up task may fold
these back into agentfront once its stock meta-verbs grow the richer shapes.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from typing import TYPE_CHECKING, Any, Optional

from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.cli._output import emit_error, emit_result

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentfront._registry import ToolEntry
    from agentfront.app import App
    from agentfront.doctor_live import Check

#: Shared placeholder for an empty registry-derived section (commands, nouns,
#: top-level tools, docs) — keeps the four ``_*_md`` builders below from
#: duplicating the same literal.
_NONE_REGISTERED = "_(none registered)_"

# One line per meta-verb, mirrored into overview's rollup so an agent reading
# ``overview`` sees the generated surface it can't discover from the registry
# alone (the meta-verbs are not registry tools).
_META_VERBS: tuple[tuple[str, str], ...] = (
    ("learn", "Structured self-teaching prompt for agent consumers. Supports --json."),
    ("explain", "Markdown docs for a registered noun/verb path. Supports --json."),
    ("overview", "Descriptive rollup across the CLI's surfaces. Supports --json."),
    ("doctor", "Runtime readiness check over the derived surfaces. Supports --json."),
)


class _DualView(dict):
    """A result that renders as pretty text yet serialises as its payload.

    Text mode goes through :func:`emit_result`'s ``str(data)`` branch (so the
    human sees the rendered markdown); ``--json`` mode goes through
    ``json.dump`` which treats this ``dict`` subclass as its payload. One return
    value therefore satisfies both the text and ``--json`` surfaces — including
    agentfront's stock grouped-tool dispatcher, which has no per-mode hook.
    """

    def __init__(self, payload: dict[str, Any], text: str) -> None:
        super().__init__(payload)
        self._text = text

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self._text


class _MetaParser(argparse.ArgumentParser):
    """ArgumentParser that routes parse-time exits through the error contract.

    Mirrors the rendered-CLI contract: parse errors emit ``error:`` / ``hint:``
    (honouring ``--json``) and unwind via a private exception rather than
    ``sys.exit``, so ``main`` returns an exit code with no traceback leak.
    """

    class _Exit(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(code)
            self.code = code

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._json_mode = False

    def error(self, message: str) -> Any:  # type: ignore[override]
        err = AfiError(
            code=EXIT_USER_ERROR,
            message=message,
            remediation=f"run '{self.prog} --help' to see valid arguments",
        )
        emit_error(err, json_mode=self._json_mode)
        raise self._Exit(EXIT_USER_ERROR)

    def exit(self, status: int = 0, message: Optional[str] = None) -> Any:  # type: ignore[override]
        if message:
            self._print_message(message)
        raise self._Exit(status)


# ---------------------------------------------------------------------------
# Section builders (registry-derived)
# ---------------------------------------------------------------------------


def _commands_md(app: "App") -> str:
    cmds = app.list_commands()
    if not cmds:
        return _NONE_REGISTERED
    return "\n".join(f"- `{app.name} {c.name}` — {c.help}" for c in cmds)


def _nouns(app: "App") -> dict[str, list[str]]:
    """Map each top-level noun (grouped-tool head) to its child verb names."""
    nouns: dict[str, list[str]] = {}
    for tool in app.list_tools():
        if tool.group:
            nouns.setdefault(tool.group[0], []).append(tool.name)
    return nouns


def _nouns_md(app: "App") -> str:
    nouns = _nouns(app)
    if not nouns:
        return _NONE_REGISTERED
    return "\n".join(
        f"- `{noun}` — verbs: {', '.join(verbs)}" for noun, verbs in nouns.items()
    )


def _top_level_tools(app: "App") -> list["ToolEntry"]:
    """Ungrouped (bare-verb) tools — e.g. t5's live-verb catalog
    (``send``/``join``/...). Distinct from :func:`_nouns`, which only
    covers *grouped* tools; before t5 registered the first ungrouped
    tools, irc-lens had none, so this gap was invisible."""
    return [t for t in app.list_tools() if not t.group]


def _top_level_tools_md(app: "App") -> str:
    tools = _top_level_tools(app)
    if not tools:
        return _NONE_REGISTERED
    return "\n".join(f"- `{app.name} {t.name}` — {t.description}" for t in tools)


def _docs_md(app: "App") -> str:
    docs = app.list_docs()
    if not docs:
        return _NONE_REGISTERED
    return "\n".join(f"- `{d.slug}` — {d.title}" for d in docs)


def _meta_md(app: "App") -> str:
    return "\n".join(f"- `{app.name} {name}` — {desc}" for name, desc in _META_VERBS)


def _all_sections(app: "App") -> list[dict[str, str]]:
    return [
        {"heading": app.name, "body_md": (app.description or "").strip()},
        {"heading": "Meta-verbs", "body_md": _meta_md(app)},
        {"heading": "Commands", "body_md": _commands_md(app)},
        {"heading": "Tools", "body_md": _top_level_tools_md(app)},
        {"heading": "Nouns", "body_md": _nouns_md(app)},
        {"heading": "Docs", "body_md": _docs_md(app)},
    ]


def _scoped_sections(app: "App", head: str) -> Optional[list[dict[str, str]]]:
    """Sections for a single known subject, or ``None`` when *head* is unknown."""
    nouns = _nouns(app)
    if head in nouns:
        verbs = "\n".join(f"- `{app.name} {head} {v}`" for v in nouns[head])
        return [{"heading": f"{app.name} {head}", "body_md": verbs}]
    for cmd in app.list_commands():
        if cmd.name == head:
            return [{"heading": f"{app.name} {head}", "body_md": cmd.help}]
    for tool in _top_level_tools(app):
        if tool.name == head:
            return [
                {
                    "heading": f"{app.name} {head}",
                    "body_md": tool.description or "_(no description)_",
                }
            ]
    return None


def _warning_section(raw_path: str) -> dict[str, str]:
    return {
        "heading": "Unknown path",
        "body_md": (
            f"warning: no overview entry matches `{raw_path}`. "
            f"Try `irc-lens overview` (no args) or `irc-lens overview cli`."
        ),
    }


def cli_surface_sections(app: "App") -> list[dict[str, str]]:
    """Sections describing the CLI surface itself (the ``cli overview`` verb)."""
    verbs = _meta_md(app) + "\n" + _commands_md(app)
    return [
        {"heading": "Verbs", "body_md": verbs},
        {
            "heading": "Conventions",
            "body_md": (
                "- every command supports --json\n"
                "- results to stdout, errors/diagnostics to stderr (never mixed)\n"
                "- exit codes: 0 success, 1 user error, 2 environment error, 3+ reserved"
            ),
        },
    ]


def _render_text(subject: str, path: Optional[str], sections: list[dict[str, str]]) -> str:
    header = path if path else "<root>"
    lines = [f"# overview: {subject} — {header}", ""]
    for section in sections:
        lines.append(f"## {section['heading']}")
        lines.append("")
        lines.append(str(section.get("body_md", "")).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _overview_view(subject: str, path: Optional[str], sections: list[dict[str, str]]) -> _DualView:
    return _DualView(
        {"subject": subject, "path": path, "sections": sections},
        _render_text(subject, path, sections),
    )


def build_overview(app: "App", path_tokens: list[str]) -> _DualView:
    """Build the descriptive overview payload for *path_tokens* (registry-derived).

    No path → the full ``all`` rollup. A single known subject → that subject's
    sections. Anything else is graceful: a warning section anchored on the best
    match, still exit-0 (overview is descriptive, not a verifier).
    """
    if not path_tokens:
        return _overview_view("all", None, _all_sections(app))
    raw = " ".join(path_tokens)
    head = path_tokens[0]
    scoped = _scoped_sections(app, head)
    if len(path_tokens) == 1 and scoped is not None:
        return _overview_view(head, raw, scoped)
    subject = head if scoped is not None else "all"
    anchor = scoped if scoped is not None else _all_sections(app)
    return _overview_view(subject, raw, [_warning_section(raw), *anchor])


def build_cli_overview(app: "App") -> _DualView:
    """The ``cli overview`` grouped-tool payload (CLI-surface introspection)."""
    sections = cli_surface_sections(app)
    return _overview_view("cli", "cli", sections)


def overview_command(app: "App", rest: list[str]) -> int:
    """Handle ``irc-lens overview [path...] [--json]``."""
    json_mode = "--json" in rest
    parser = _MetaParser(
        prog="irc-lens overview",
        description="Descriptive rollup across irc-lens's surfaces (never hard-fails).",
    )
    parser._json_mode = json_mode
    parser.add_argument(
        "path",
        nargs="*",
        help="Optional subject path; unknown paths warn (exit 0), not error.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")
    try:
        args = parser.parse_args(rest)
    except _MetaParser._Exit as exc:
        return exc.code
    view = build_overview(app, list(args.path))
    emit_result(view, json_mode=args.json)
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _doctor_payload(subject: str, checks: list["Check"]) -> dict[str, Any]:
    """Rubric-shaped report: ``{subject, healthy, checks:[{...remediation}]}``."""
    from agentfront.doctor_live import healthy

    return {
        "subject": subject,
        "healthy": healthy(checks),
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "passed": c.status != "fail",
                "remediation": c.remediation,
            }
            for c in checks
        ],
    }


def _render_doctor_text(subject: str, payload: dict[str, Any]) -> str:
    status = "healthy" if payload["healthy"] else "unhealthy"
    lines = [f"# doctor: {subject} — {status}", ""]
    for check in payload["checks"]:
        mark = "ok" if check["passed"] else "FAIL"
        lines.append(f"[{mark}] {check['name']}: {check['status']}")
        if not check["passed"] and check["remediation"]:
            lines.append(f"  hint: {check['remediation']}")
    return "\n".join(lines).rstrip() + "\n"


def build_doctor(app: "App") -> _DualView:
    """Run the runtime doctor over *app*'s derived surfaces (registry-derived).

    ``agentfront.doctor_live.run_doctor`` audits the App's live HTTP/CLI
    surfaces in-process; its learn-check invokes the ``learn`` verb, which
    prints, so both output streams are captured and discarded — only this
    handler's own report reaches stdout, keeping ``--json`` stderr clean.
    """
    from agentfront.doctor_live import run_doctor

    subject = f"{app.name} v{app.version}"
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        checks = run_doctor(app)
    payload = _doctor_payload(subject, checks)
    return _DualView(payload, _render_doctor_text(subject, payload))


def doctor_command(app: "App", rest: list[str]) -> int:
    """Handle ``irc-lens doctor [--json]`` — exit 0 when healthy, else 1."""
    json_mode = "--json" in rest
    parser = _MetaParser(
        prog="irc-lens doctor",
        description="Runtime readiness check over the derived surfaces.",
    )
    parser._json_mode = json_mode
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")
    try:
        args = parser.parse_args(rest)
    except _MetaParser._Exit as exc:
        return exc.code
    view = build_doctor(app)
    emit_result(view, json_mode=args.json)
    return 0 if view["healthy"] else 1
