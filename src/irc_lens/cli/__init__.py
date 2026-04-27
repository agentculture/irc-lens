"""Unified CLI entry point for irc-lens.

Noun-based command groups and globals are registered here. Top-level globals
(``learn``, ``explain``) live under :mod:`irc_lens.cli._commands`; per-noun
groups follow the same pattern.

Error-propagation contract: every handler raises
:class:`irc_lens.cli._errors.AfiError` on failure; :func:`main` catches it
via :func:`_dispatch` and routes through :mod:`irc_lens.cli._output`.
Unknown exceptions are wrapped so no Python traceback leaks.
"""

from __future__ import annotations

import argparse
import sys

from irc_lens import __version__
from irc_lens.cli._commands import explain as _explain_cmd
from irc_lens.cli._commands import learn as _learn_cmd
from irc_lens.cli._commands import overview as _overview_cmd
from irc_lens.cli._commands import serve as _serve_cmd
from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.cli._output import emit_error


def _argv_requested_json(argv: list[str] | None) -> bool:
    """Detect ``--json`` on the raw argv before argparse has parsed it.

    Used by :class:`_ArgumentParser`'s parse-time error path: by the time
    argparse calls ``error()``, the namespace doesn't exist yet, so we
    can't read ``args.json``. Sniff the raw arg list instead so a user
    who passed ``--json`` still gets a JSON-shaped error.
    """
    return bool(argv) and "--json" in argv


# Captured at the start of each `main()` so `_ArgumentParser.error` can read it
# even though argparse hasn't built a namespace yet. Process-global is fine —
# `main()` is the only entry point and is single-threaded.
_PARSE_JSON_MODE: bool = False


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that emits errors via our structured format."""

    def error(self, message: str) -> None:  # type: ignore[override]
        err = AfiError(
            code=EXIT_USER_ERROR,
            message=message,
            remediation=f"run '{self.prog} --help' to see valid arguments",
        )
        emit_error(err, json_mode=_PARSE_JSON_MODE)
        raise SystemExit(err.code)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="irc-lens",
        description="irc-lens — agent-first CLI.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    _learn_cmd.register(sub)
    _explain_cmd.register(sub)
    _overview_cmd.register(sub)
    _serve_cmd.register(sub)

    # Noun groups. Every noun with action-verbs must also expose `overview`.
    cli_noun = sub.add_parser(
        "cli",
        help="Meta-introspection of the irc-lens CLI surface itself.",
    )
    cli_sub = cli_noun.add_subparsers(dest="cli_command")
    _overview_cmd.register_cli_noun_overview(cli_sub)
    # `irc-lens cli` with no verb prints the noun's own help instead of
    # raising AttributeError out of _dispatch.
    cli_noun.set_defaults(func=lambda _args: (cli_noun.print_help() or 0))

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    try:
        return args.func(args)
    except AfiError as err:
        emit_error(err, json_mode=json_mode)
        return err.code
    except Exception as err:  # noqa: BLE001 - last-resort
        wrapped = AfiError(
            code=EXIT_USER_ERROR,
            message=f"unexpected: {err.__class__.__name__}: {err}",
            remediation="file a bug",
        )
        emit_error(wrapped, json_mode=json_mode)
        return wrapped.code


def main(argv: list[str] | None = None) -> int:
    global _PARSE_JSON_MODE
    effective_argv = sys.argv[1:] if argv is None else argv
    _PARSE_JSON_MODE = _argv_requested_json(effective_argv)
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
    finally:
        _PARSE_JSON_MODE = False
    if args.command is None:
        parser.print_help()
        return 0
    return _dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
