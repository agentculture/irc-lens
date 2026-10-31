"""Default-deny access policy over :mod:`irc_lens.access_store`.

Pure functions: no IRC, no aiohttp. Every caller asks the same three
questions -- :func:`can_list`, :func:`can_read`, :func:`can_write` -- and
every answer is derived from the store's *current* rows, on every call
(nothing is cached), so revoking a grant takes effect on the next check.

Rules:

* An *approved* admin passes every check.
* Any other human needs an explicit ``channel`` grant. ``read`` allows
  list/read; ``write`` and ``ask`` allow list/read/write. An unknown level
  grants nothing.
* An agent has no rows of its own: :func:`agent_can_list` & co. resolve the
  credential to its sponsoring human and check *that human's* rows.
* Fail closed: an unknown, unapproved, or empty human, a missing store, or
  any error while reading the store or grants answers ``False``.
* Every refusal appends an ``access.deny`` audit row keyed by the email
  hash (never the plain address). Audit failure never turns a deny into an
  allow.
"""

from __future__ import annotations

from typing import Any, Callable

CHANNEL_KIND = "channel"
DENY_ACTION = "access.deny"

_LIST_LEVELS = frozenset({"read", "write", "ask"})
_READ_LEVELS = _LIST_LEVELS
_WRITE_LEVELS = frozenset({"write", "ask"})
_LEVELS = {"list": _LIST_LEVELS, "read": _READ_LEVELS, "write": _WRITE_LEVELS}


def _allowed(store: Any, human: str | None, channel: str, op: str) -> bool:
    """Raw decision; may raise on store errors (callers fail closed)."""
    if store is None or not human or not channel:
        return False
    user = store.get_user(human)
    if not user or user.get("state") != "approved":
        return False
    if user.get("role") == "admin":
        return True
    wanted = channel.casefold()
    for _kind, resource, level in store.grants_for(human, CHANNEL_KIND):
        if resource.casefold() == wanted:
            return level in _LEVELS[op]
    return False


def _record_denial(store: Any, human: str | None, channel: str, op: str) -> None:
    """Best-effort audit row for a refusal; the actor is hashed by the store."""
    try:
        actor = human if human and human.strip() else None
        with store._tx() as con:  # pylint: disable=protected-access
            store._audit(  # pylint: disable=protected-access
                con, actor, DENY_ACTION, f"{op}:{CHANNEL_KIND}:{channel}"
            )
    except Exception:  # noqa: BLE001 - audit must never flip a deny to allow
        pass


def _check(store: Any, human: str | None, channel: str, op: str) -> bool:
    try:
        ok = _allowed(store, human, channel, op) is True
    except Exception:  # noqa: BLE001 - unreadable store/grant => deny
        ok = False
    if not ok:
        _record_denial(store, human, channel, op)
    return ok


def can_list(human: str | None, channel: str, *, store: Any) -> bool:
    return _check(store, human, channel, "list")


def can_read(human: str | None, channel: str, *, store: Any) -> bool:
    return _check(store, human, channel, "read")


def can_write(human: str | None, channel: str, *, store: Any) -> bool:
    return _check(store, human, channel, "write")


def _agent_human(store: Any, token: str) -> str | None:
    try:
        found = store.verify_agent_credential(token)
    except Exception:  # noqa: BLE001
        return None
    return found[0] if found else None


def _agent_check(op: str) -> Callable[..., bool]:
    def check(token: str, channel: str, *, store: Any) -> bool:
        human = _agent_human(store, token)
        if human is None:
            _record_denial(store, None, channel, op)
            return False
        return _check(store, human, channel, op)

    check.__name__ = f"agent_can_{op}"
    check.__doc__ = (
        f"Agent {op} check: resolves token -> sponsoring human's current rows."
    )
    return check


agent_can_list = _agent_check("list")
agent_can_read = _agent_check("read")
agent_can_write = _agent_check("write")

__all__ = [
    "can_list",
    "can_read",
    "can_write",
    "agent_can_list",
    "agent_can_read",
    "agent_can_write",
]
