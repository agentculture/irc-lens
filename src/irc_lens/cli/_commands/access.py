"""Access verbs: users, grants, agents and extend over the access store.

Every verb opens the sqlite file, makes one store call and exits, so a
running console (which reads the store on every request) sees the change
without a restart. No verb prints an email address it was not given on the
command line: output names only the address the operator typed, counts,
nicks, nicknames, channels and levels.
"""

from __future__ import annotations

import argparse
import time

from irc_lens.cli._commands.guests import _resolve_store_path
from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError

ROLES = ("member", "admin")
LEVELS = ("read", "write", "ask")
CHANNEL_KIND = "channel"
STATE_APPROVED = "approved"
STATE_REMOVED = "removed"
SECONDS_PER_DAY = 86400
#: A guest's agent credential is keyed by this prefix plus the email.
GUEST_KEY_PREFIX = "guest:"


def _open_store(args: argparse.Namespace):
    from irc_lens.access_store import AccessStore

    path = _resolve_store_path(args)
    if not path.exists():
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"access store not found at {path}",
            remediation="pass --store, or set guest_mode.store_path in the config",
        )
    return AccessStore(path)


def _email(args: argparse.Namespace) -> str:
    email = args.email.strip().lower()
    if "@" not in email:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"{args.email!r} is not an email address",
            remediation="pass the user's email address",
        )
    return email


def _need_user(store, email: str) -> dict:
    user = store.get_user(email)
    if user is None:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"no such user: {email}",
            remediation="add them first: irc-lens users add <email>",
        )
    return user


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"unknown role {role!r}",
            remediation=f"use one of: {', '.join(ROLES)}",
        )


def _set_user(store, email: str, role: str, state: str) -> None:
    """Set the user, refusing a change that would leave no approved admin
    (the store's one atomic guard, shared with /admin)."""
    from irc_lens.access_store import LastAdminError

    try:
        store.set_user_keeping_admin(email, role=role, state=state, actor=None)
    except LastAdminError as exc:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message="at least one approved admin must remain",
            remediation="make another user an admin first: "
            "irc-lens users add <email> --role admin",
        ) from exc


def _check_nickname(raw: str) -> str:
    """The stored form of *raw*, or exit 1 saying why it is refused."""
    from irc_lens import nickname

    refusal = nickname.approved_refusal(raw)
    if refusal is not None:
        raise _nickname_error(refusal)
    return nickname.sanitize_nickname(raw) or ""


def _nickname_error(reason: str) -> AfiError:
    return AfiError(
        code=EXIT_USER_ERROR,
        message=f"nickname refused: {reason}",
        remediation=(
            "2-16 letters, digits or _, not an address, not a reserved name, "
            "not starting with pa_, and not another user's nickname"
        ),
    )


def _set_nickname(store, email: str, nick: str) -> str:
    from irc_lens.access_store import NicknameError

    try:
        return store.set_nickname(email, nick, actor=None)
    except NicknameError as exc:
        raise _nickname_error(exc.reason) from exc


def cmd_users_add(args: argparse.Namespace) -> int:
    _check_role(args.role)
    email = _email(args)
    nick = _check_nickname(args.nick) if args.nick is not None else None
    store = _open_store(args)
    if nick is not None and _nickname_owner(store, nick) not in (None, email):
        raise _nickname_error("taken")
    _set_user(store, email, args.role, STATE_APPROVED)
    if nick is not None:
        nick = _set_nickname(store, email, nick)
        print(f"added {email} as {args.role}, nickname {nick}")
        return 0
    print(f"added {email} as {args.role}")
    return 0


def _nickname_owner(store, nick: str) -> str | None:
    """The address already holding *nick* (any case), else None."""
    for user in store.list_users():
        if (user["nickname"] or "").casefold() == nick.casefold():
            return user["email"]
    return None


def cmd_users_nick(args: argparse.Namespace) -> int:
    """Set or rename a user's nickname (their IRC nick is <server>-<nickname>).

    A running lens closes the user's sessions and agent connection on its
    next sweep or request; their agent credential must be re-issued.
    """
    email = _email(args)
    nick = _check_nickname(args.nickname)
    store = _open_store(args)
    _need_user(store, email)
    nick = _set_nickname(store, email, nick)
    print(f"{email} is now known as {nick}")
    return 0


def cmd_users_role(args: argparse.Namespace) -> int:
    _check_role(args.role)
    email = _email(args)
    store = _open_store(args)
    user = _need_user(store, email)
    _set_user(store, email, args.role, user["state"])
    print(f"{email} is now {args.role}")
    return 0


def cmd_users_remove(args: argparse.Namespace) -> int:
    email = _email(args)
    store = _open_store(args)
    user = _need_user(store, email)
    _set_user(store, email, user["role"], STATE_REMOVED)
    n = store.revoke_agent_credential(email, actor=None)
    print(f"removed {email} ({n} agent credential(s) revoked)")
    return 0


def cmd_grants_add(args: argparse.Namespace) -> int:
    if args.level not in LEVELS:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"unknown level {args.level!r}",
            remediation=f"use one of: {', '.join(LEVELS)}",
        )
    email = _email(args)
    store = _open_store(args)
    store.set_grant(email, CHANNEL_KIND, args.channel, args.level, actor=None)
    print(f"granted {args.level} on {args.channel} to {email}")
    return 0


def cmd_grants_list(args: argparse.Namespace) -> int:
    email = _email(args)
    for kind, resource, level in _open_store(args).grants_for(email):
        print(f"{kind}\t{resource}\t{level}")
    return 0


def cmd_grants_revoke(args: argparse.Namespace) -> int:
    email = _email(args)
    if not _open_store(args).remove_grant(
        email, CHANNEL_KIND, args.channel, actor=None
    ):
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"no such grant: {args.channel} for {email}",
            remediation="channel names match exactly, case included; "
            "see: irc-lens grants list <email>",
        )
    print(f"revoked {args.channel} from {email}")
    return 0


def cmd_agents_list(args: argparse.Namespace) -> int:
    email = _email(args)
    store = _open_store(args)
    active = store.active_credential_count(email)
    until = store.extension_until(email)
    ext = "-" if until is None else str(until)
    print(f"{email}\tactive_credentials={active}\textension_until={ext}")
    return 0


def cmd_agents_revoke(args: argparse.Namespace) -> int:
    """Revoke *email*'s agent under both keys: approved (``<email>``) and
    guest (``guest:<email>``), so a guest's agent is reachable too."""
    email = _email(args)
    store = _open_store(args)
    n = store.revoke_agent_credential(email, actor=None)
    n += store.revoke_agent_credential(GUEST_KEY_PREFIX + email, actor=None)
    print(f"revoked {n} agent credential(s) for {email}")
    return 0


def cmd_extend(args: argparse.Namespace) -> int:
    email = _email(args)
    if args.clear:
        if args.days is not None:
            raise AfiError(
                code=EXIT_USER_ERROR,
                message="give either --days or --clear, not both",
                remediation="irc-lens extend <email> --days N",
            )
        _open_store(args).clear_extension(email, actor=None)
        print(f"cleared extension for {email}")
        return 0
    if args.days is None or args.days <= 0:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message="extend needs --days N (N > 0) or --clear",
            remediation="irc-lens extend <email> --days 7",
        )
    store = _open_store(args)
    until = int(time.time()) + args.days * SECONDS_PER_DAY
    store.set_extension(email, until=until, actor=None)
    print(f"extended {email} until {until}")
    return 0


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--store", default=None, help="Access store path override.")
    p.add_argument("--config", default=None, help="Config file path.")


def _noun(app, name: str, help_text: str, build) -> None:
    holder: dict[str, argparse.ArgumentParser] = {}

    def configure(cfg: argparse.ArgumentParser) -> None:
        holder["parser"] = cfg
        build(cfg.add_subparsers(dest=f"{name}_command"))

    def handle_bare(_args: argparse.Namespace) -> int:
        if "parser" in holder:
            holder["parser"].print_help()
        return 0

    app.add_command(name, handler=handle_bare, help=help_text, configure=configure)


def _verb(sub, name: str, help_text: str, func, *positionals: str):
    p = sub.add_parser(name, help=help_text)
    for pos in positionals:
        p.add_argument(pos)
    _common(p)
    p.set_defaults(func=func)
    return p


def _build_users(sub) -> None:
    add = _verb(sub, "add", "Approve a user.", cmd_users_add, "email")
    add.add_argument("--role", default="member", help="member (default) or admin.")
    add.add_argument(
        "--nick",
        default=None,
        help="The nickname others see (else the user picks one on first sign-in).",
    )
    _verb(sub, "remove", "Remove a user.", cmd_users_remove, "email")
    _verb(sub, "role", "Set a user's role.", cmd_users_role, "email", "role")
    _verb(
        sub,
        "nick",
        "Set or rename a user's nickname.",
        cmd_users_nick,
        "email",
        "nickname",
    )


def _build_grants(sub) -> None:
    add = _verb(sub, "add", "Grant channel access.", cmd_grants_add, "email", "channel")
    add.add_argument("--level", default="read", help="read (default), write or ask.")
    _verb(sub, "list", "List a user's grants.", cmd_grants_list, "email")
    _verb(
        sub, "revoke", "Revoke a channel grant.", cmd_grants_revoke, "email", "channel"
    )


def _build_agents(sub) -> None:
    _verb(sub, "list", "Show a user's agent status.", cmd_agents_list, "email")
    _verb(
        sub,
        "revoke",
        "Revoke a person's agent (approved and guest credentials).",
        cmd_agents_revoke,
        "email",
    )


def register_into(app) -> None:
    """Register ``users``, ``grants``, ``agents`` and ``extend``."""
    _noun(
        app,
        "users",
        "Manage approved users, roles and nicknames (add, remove, role, nick).",
        _build_users,
    )
    _noun(app, "grants", "Manage channel grants (add, list, revoke).", _build_grants)
    _noun(app, "agents", "Manage sponsored agents (list, revoke).", _build_agents)

    def configure_extend(cfg: argparse.ArgumentParser) -> None:
        cfg.add_argument("email")
        cfg.add_argument("--days", type=int, default=None, help="Extend by N days.")
        cfg.add_argument("--clear", action="store_true", help="Remove the extension.")
        _common(cfg)
        cfg.set_defaults(func=cmd_extend)

    app.add_command(
        "extend",
        handler=cmd_extend,
        help="Extend a user's agent lifetime (--days N | --clear).",
        configure=configure_extend,
    )
