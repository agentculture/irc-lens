"""`irc-lens guests` noun group (extensible: later tasks add verbs here)."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError

_HELP = "Manage guest-mode data (export, passwd, ban, unban, list, flags)."
DEFAULT_FLAG_LOG = "~/.culture/sandbox/flags.jsonl"


def _resolve_store_path(args: argparse.Namespace) -> Path:
    if getattr(args, "store", None):
        return Path(args.store)
    from irc_lens.config import resolve_config

    cfg = resolve_config(getattr(args, "config", None))
    return Path(cfg.guest_store_path)


def _open_store(args: argparse.Namespace):
    from irc_lens.guest_store import GuestStore

    path = _resolve_store_path(args)
    if not path.exists():
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"guest store not found at {path}",
            remediation="pass --store, or set guest_mode.store_path in the config",
        )
    return GuestStore(path)


def _warn_if_unapproved(args: argparse.Namespace, email: str) -> None:
    try:
        from irc_lens.config import resolve_config

        cfg = resolve_config(getattr(args, "config", None))
    except Exception:  # noqa: BLE001 -- the warning is best effort
        print(
            "warning: could not read the config to check the approved list",
            file=sys.stderr,
        )
        return
    if email not in {e.lower() for e in cfg.allowed_emails}:
        print(
            f"warning: {email} is not in allowed_emails; sign-in also "
            "requires the allowlist",
            file=sys.stderr,
        )


def _read_password(args: argparse.Namespace) -> str:
    """Password from stdin (--stdin) or a TTY prompt; never from argv."""
    if args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != password:
            raise AfiError(
                code=EXIT_USER_ERROR,
                message="passwords do not match",
                remediation="run the command again",
            )
    if not password:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message="empty password",
            remediation="provide a non-empty password",
        )
    return password


def cmd_guests_passwd(args: argparse.Namespace) -> int:
    email = args.email.strip().lower()
    store = _open_store(args)
    password = _read_password(args)
    _warn_if_unapproved(args, email)
    store.set_password(email, password)
    print(f"password set for {email}")
    return 0


def _is_email(target: str) -> bool:
    return "@" in target


def _flag_records(store, flag_log: Path) -> list[dict]:
    """Store flags (ids ``s<n>``) and sandbox flag-log lines (``j<line>``)."""
    records: list[dict] = []
    for fid, email, kind, detail, ts in store.list_flags_with_id():
        records.append(
            {
                "id": f"s{fid}",
                "source": "store",
                "email": email,
                "kind": kind,
                "detail": detail,
                "ts": ts,
            }
        )
    if flag_log.exists():
        for lineno, line in enumerate(flag_log.read_text().splitlines(), 1):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            records.append(
                {
                    "id": f"j{lineno}",
                    "source": "flag-log",
                    "nick": rec.get("nick"),
                    "kind": "nsfw",
                    "detail": rec.get("reason"),
                    "excerpt": rec.get("excerpt"),
                    "ts": rec.get("ts"),
                }
            )
    return records


def _flag_log_path(args: argparse.Namespace) -> Path:
    return Path(args.flag_log or DEFAULT_FLAG_LOG).expanduser()


def cmd_guests_ban(args: argparse.Namespace) -> int:
    store = _open_store(args)
    if args.flag:
        if args.target:
            raise AfiError(
                code=EXIT_USER_ERROR,
                message="give either a target or --flag, not both",
                remediation="irc-lens guests ban --flag <id>",
            )
        rec = next(
            (
                r
                for r in _flag_records(store, _flag_log_path(args))
                if r["id"] == args.flag
            ),
            None,
        )
        if rec is None:
            raise AfiError(
                code=EXIT_USER_ERROR,
                message=f"no flag with id {args.flag!r}",
                remediation="list ids with `irc-lens guests flags`",
            )
        email = rec.get("email")
        if not email:
            nick = (rec.get("nick") or "").lower()
            email = next(
                (e for e, n, _ip in store.list_guests() if n.lower() == nick), None
            )
        if not email:
            raise AfiError(
                code=EXIT_USER_ERROR,
                message=f"flag {args.flag} names no known guest (nick {rec.get('nick')!r})",
                remediation="ban by address instead: irc-lens guests ban <email|ip>",
            )
        store.ban(email=email, reason=args.reason or f"flag {args.flag}")
        print(f"banned {email} (flag {args.flag})")
        return 0
    if not args.target:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message="ban needs an <email|ip> or --flag <id>",
            remediation="irc-lens guests ban <email|ip>",
        )
    target = args.target.strip().lower()
    if _is_email(target):
        store.ban(email=target, reason=args.reason)
    else:
        store.ban(ip=target, reason=args.reason)
    print(f"banned {target}")
    return 0


def cmd_guests_unban(args: argparse.Namespace) -> int:
    store = _open_store(args)
    target = args.target.strip().lower()
    n = store.unban(**({"email": target} if _is_email(target) else {"ip": target}))
    print(f"unbanned {target} ({n} ban(s) removed)")
    return 0


def cmd_guests_list(args: argparse.Namespace) -> int:
    store = _open_store(args)
    for email, nick, ip in store.list_guests():
        mark = " [banned]" if store.is_banned(email, ip) else ""
        print(f"{nick}\t{email}\t{ip}{mark}")
    for email, ip, reason, ts in store.list_bans():
        print(f"ban\t{email or '-'}\t{ip or '-'}\t{reason or '-'}\t{ts}")
    return 0


def cmd_guests_flags(args: argparse.Namespace) -> int:
    store = _open_store(args)
    for r in _flag_records(store, _flag_log_path(args)):
        who = r.get("email") or r.get("nick") or "-"
        print(f"{r['id']}\t{r['source']}\t{who}\t{r['kind']}\t{r.get('detail') or '-'}")
    return 0


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

        def common(p: argparse.ArgumentParser) -> None:
            p.add_argument("--store", default=None, help="Guest store path override.")
            p.add_argument("--config", default=None, help="Config file path.")

        pw = sub.add_parser("passwd", help="Set an approved user's password.")
        pw.add_argument("email")
        pw.add_argument(
            "--stdin", action="store_true", help="Read the password from stdin."
        )
        common(pw)
        pw.set_defaults(func=cmd_guests_passwd)

        ban = sub.add_parser("ban", help="Ban an email or IP (or --flag <id>).")
        ban.add_argument("target", nargs="?", default=None, help="Email or IP.")
        ban.add_argument("--flag", default=None, help="Ban the guest a flag names.")
        ban.add_argument("--reason", default=None)
        ban.add_argument("--flag-log", default=None, help="Sandbox flag log JSONL.")
        common(ban)
        ban.set_defaults(func=cmd_guests_ban)

        unban = sub.add_parser("unban", help="Lift a ban on an email or IP.")
        unban.add_argument("target")
        common(unban)
        unban.set_defaults(func=cmd_guests_unban)

        lst = sub.add_parser("list", help="List guests and bans.")
        common(lst)
        lst.set_defaults(func=cmd_guests_list)

        flg = sub.add_parser("flags", help="List flags (store + sandbox flag log).")
        flg.add_argument("--flag-log", default=None, help="Sandbox flag log JSONL.")
        common(flg)
        flg.set_defaults(func=cmd_guests_flags)

    def handle_bare(_args: argparse.Namespace) -> int:
        parser = holder.get("parser")
        if parser is not None:
            parser.print_help()
        return 0

    app.add_command("guests", handler=handle_bare, help=_HELP, configure=configure)
