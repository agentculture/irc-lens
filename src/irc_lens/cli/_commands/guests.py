"""`irc-lens guests` noun group (extensible: later tasks add verbs here)."""

from __future__ import annotations

import argparse
from pathlib import Path

from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError

_HELP = "Manage guest-mode data (export, ...)."


def _resolve_store_path(args: argparse.Namespace) -> Path:
    if getattr(args, "store", None):
        return Path(args.store)
    from irc_lens.config import resolve_config

    cfg = resolve_config(getattr(args, "config", None))
    return Path(cfg.guest_store_path)


def cmd_guests_export(args: argparse.Namespace) -> int:
    if not args.redacted:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message="unredacted guest export is not offered",
            remediation="pass --redacted",
        )
    from irc_lens.export import export_redacted
    from irc_lens.guest_store import GuestStore

    path = _resolve_store_path(args)
    if not path.exists():
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"guest store not found at {path}",
            remediation="pass --store, or set guest_mode.store_path in the config",
        )
    text = export_redacted(GuestStore(path), fmt=args.format)
    if args.out:
        out = Path(args.out)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
        except OSError as exc:
            raise AfiError(
                code=EXIT_ENV_ERROR,
                message=f"could not write export to {out}: {exc}",
                remediation="check directory permissions or pick a different --out",
            ) from exc
    else:
        print(text, end="")
    return 0


def register_into(app) -> None:
    """Register ``guests`` (a noun; ``export`` today, more verbs later)."""
    holder: dict[str, argparse.ArgumentParser] = {}

    def configure(cfg: argparse.ArgumentParser) -> None:
        holder["parser"] = cfg
        sub = cfg.add_subparsers(dest="guests_command")

        exp = sub.add_parser("export", help="Export guest transcripts (PII-redacted).")
        exp.add_argument(
            "--redacted",
            action="store_true",
            help="Required: strip emails, IPs, nicks and free-text PII.",
        )
        exp.add_argument("--format", choices=["jsonl", "md"], default="jsonl")
        exp.add_argument("--out", default=None, help="Write to FILE instead of stdout.")
        exp.add_argument("--store", default=None, help="Guest store path override.")
        exp.add_argument("--config", default=None, help="Config file path.")
        exp.set_defaults(func=cmd_guests_export)

    def handle_bare(_args: argparse.Namespace) -> int:
        parser = holder.get("parser")
        if parser is not None:
            parser.print_help()
        return 0

    app.add_command("guests", handler=handle_bare, help=_HELP, configure=configure)
