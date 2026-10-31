"""The personal-agent nick scheme: one function, one place.

A human's personal agent connects under a nick derived from its human's
nick, so it is visibly tied to them, and that nick lives in a reserved
namespace that no human nick can enter:

* AgentIRC requires every nick to start with ``<server>-``, so the agent
  keeps its human's server prefix (the mesh name for an approved user, the
  sandbox name for a guest).
* After the prefix comes :data:`AGENT_NICK_INFIX` (``pa_-``). A derived mesh
  nick (``identity.derive_nick``) keeps only ``[a-z0-9-]``, so it never holds
  ``_``; a guest nickname (``entry.sanitize_nickname``) keeps only
  ``[a-z0-9_]``, so it never holds ``-`` after the prefix. A nick holding
  both can be neither.

``testsrv-bob`` -> ``testsrv-pa_-bob``; ``sbx-gus`` -> ``sbx-pa_-gus``. The
server name is the configured one, passed in by the caller: it may hold
``-`` itself (``spark-1-bob`` -> ``spark-1-pa_-bob``).

The nick is stored with the agent's credential when it is issued
(``web/agent_auth.py``). Ownership is never inferred from nick text: the
lens maps a credential to its human from its own state. Task t13 owns this
scheme and may refine it; callers must go through :func:`agent_nick_for`.
"""

from __future__ import annotations

from typing import Any

#: Marks the personal-agent namespace right after the ``<server>-`` prefix.
#: Sandbox agents can recognise visitors by the prefix ``<server>-pa_-``.
AGENT_NICK_INFIX = "pa_-"


def agent_nick_for(human_nick: str, server_name: str) -> str:
    """The personal agent's nick for the human whose nick is *human_nick*.

    *server_name* is the configured name of the server the human's nick
    lives on (``server.name`` for the mesh, the sandbox's name for a
    guest). It is passed in, never guessed from the nick: a server name may
    itself contain ``-`` (``spark-1-bob`` -> ``spark-1-pa_-bob``).

    Raises ``ValueError`` for an empty *server_name*, or a nick that does
    not start with ``<server_name>-`` or has nothing after it (no human nick
    the lens hands out looks like that).
    """
    prefix = f"{server_name}-"
    if not server_name or not human_nick.startswith(prefix):
        raise ValueError("human nick has no '<server>-' prefix")
    rest = human_nick[len(prefix) :]
    if not rest:
        raise ValueError("human nick has nothing after its '<server>-' prefix")
    return f"{prefix}{AGENT_NICK_INFIX}{rest}"


def is_agent_nick(nick: str, server_name: str) -> bool:
    """True iff *nick* lies in *server_name*'s personal-agent namespace.

    A namespace test only: it says nothing about *whose* agent a nick is.
    Ownership always comes from the lens's credential rows
    (``web/agent_sessions.py``), never from this text.
    """
    prefix = f"{server_name}-{AGENT_NICK_INFIX}"
    nick = nick or ""
    return bool(server_name) and nick.startswith(prefix) and len(nick) > len(prefix)


def server_name_for(config: Any, *, guest: bool) -> str:
    """The configured name of the server a human's nick lives on.

    The sandbox's name (``guest_mode.sandbox.name``) for a guest, the mesh's
    (``server.name``) for everyone else. Read from *config*, never from a
    nick.
    """
    return config.guest_sandbox_name if guest else config.server_name


__all__ = ["AGENT_NICK_INFIX", "agent_nick_for", "is_agent_nick", "server_name_for"]
