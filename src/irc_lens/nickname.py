"""Nickname rules shared by guests and approved users.

A guest picks a nickname on the entry form (``web/entry.py``, shown as
``sbx-<nickname>``); with email permissions on, an approved user picks one
too (``web/nickname.py``, shown as ``<server>-<nickname>``). Both go through
:func:`sanitize_nickname` here, so the character rules exist once.

A nickname is always chosen by its person (or set by an admin). It is never
derived from an email address: an input holding ``@`` is refused outright,
so typing an address cannot smuggle one in.

The personal-agent namespace (``irc_lens.agent_nick``) is ``<server>-pa_-``.
A nickname keeps only ``[a-z0-9_]``, so ``<server>-<nickname>`` never holds
the ``-`` that namespace needs after ``pa_``; approved nicknames starting
with ``pa_`` are refused as well, so no human nick even looks like an agent.
"""

from __future__ import annotations

import re

NICK_MIN, NICK_MAX = 2, 16
#: Nicknames nobody may take (``ask`` is the sandbox agent, ``sbx-ask``).
RESERVED_NICKNAMES = frozenset({"ask"})
#: An approved nickname may not start with this (the agent namespace marker).
AGENT_PREFIX = "pa_"

REFUSAL_INVALID = "invalid"
REFUSAL_RESERVED = "reserved"
REFUSAL_TAKEN = "taken"

# No "-": approved users' sandbox nicks are sbx-op-<x> (d7).
_NICK_DROP = re.compile(r"[^a-z0-9_]")


def sanitize_nickname(raw: str) -> str | None:
    """Lowercase, keep ``[a-z0-9_]``; None unless 2..16 chars remain."""
    nick = _NICK_DROP.sub("", (raw or "").lower())
    if not NICK_MIN <= len(nick) <= NICK_MAX:
        return None
    return nick


def approved_refusal(raw: str) -> str | None:
    """Why *raw* cannot be an approved user's nickname, or None if it can.

    Only the rules that need no state: characters and length, no address,
    reserved names and the agent namespace. Uniqueness is the store's job.
    """
    if "@" in (raw or ""):
        return REFUSAL_INVALID
    nick = sanitize_nickname(raw)
    if nick is None:
        return REFUSAL_INVALID
    if nick in RESERVED_NICKNAMES or nick.startswith(AGENT_PREFIX):
        return REFUSAL_RESERVED
    return None


def normalize_approved(raw: str) -> str | None:
    """The stored form of an acceptable approved nickname, else None."""
    if approved_refusal(raw) is not None:
        return None
    return sanitize_nickname(raw)


def mesh_nick(server_name: str, nick: str) -> str:
    """An approved user's IRC nick: ``<server>-<nickname>``."""
    return f"{server_name}-{nick}"


__all__ = [
    "AGENT_PREFIX",
    "NICK_MAX",
    "NICK_MIN",
    "REFUSAL_INVALID",
    "REFUSAL_RESERVED",
    "REFUSAL_TAKEN",
    "RESERVED_NICKNAMES",
    "approved_refusal",
    "mesh_nick",
    "normalize_approved",
    "sanitize_nickname",
]
