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
from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.cli._output import emit_error


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that emits errors via our structured format."""

    def error(self, message: str) -> None:  # type: ignore[override]
        err = AfiError(
            code=EXIT_USER_ERROR,
            message=message,
            remediation=f"run '{self.prog} --help' to see valid arguments",
        )
        emit_error(err, json_mode=False)
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

    # Noun groups. Every noun with action-verbs must also expose `overview`.
    cli_noun = sub.add_parser(
        "cli",
        help="Meta-introspection of the irc-lens CLI surface itself.",
    )
    cli_sub = cli_noun.add_subparsers(dest="cli_command")
    _overview_cmd.register_cli_noun_overview(cli_sub)

    # Register additional noun groups here:
    #   from irc_lens.cli._commands import serve as _serve_group
    #   _serve_group.register(sub)

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
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return _dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
