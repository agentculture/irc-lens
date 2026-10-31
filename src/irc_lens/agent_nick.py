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

``testsrv-bob`` -> ``testsrv-pa_-bob``; ``sbx-gus`` -> ``sbx-pa_-gus``.

The nick is stored with the agent's credential when it is issued
(``web/agent_auth.py``). Ownership is never inferred from nick text: the
lens maps a credential to its human from its own state. Task t13 owns this
scheme and may refine it; callers must go through :func:`agent_nick_for`.
"""

from __future__ import annotations

#: Marks the personal-agent namespace right after the ``<server>-`` prefix.
#: Sandbox agents can recognise visitors by the prefix ``<server>-pa_-``.
AGENT_NICK_INFIX = "pa_-"


def agent_nick_for(human_nick: str) -> str:
    """The personal agent's nick for the human whose nick is *human_nick*.

    Raises ``ValueError`` for a nick without a ``<server>-`` prefix or with
    nothing after it (no human nick the lens hands out looks like that).
    """
    server, sep, rest = human_nick.partition("-")
    if not sep or not server or not rest:
        raise ValueError("human nick has no '<server>-' prefix")
    return f"{server}-{AGENT_NICK_INFIX}{rest}"


def is_agent_nick(nick: str) -> bool:
    """True iff *nick* lies in the personal-agent namespace.

    A namespace test only: it says nothing about *whose* agent a nick is.
    Ownership always comes from the lens's credential rows
    (``web/agent_sessions.py``), never from this text.
    """
    _server, sep, rest = (nick or "").partition("-")
    return bool(sep and _server and rest.startswith(AGENT_NICK_INFIX)) and len(
        rest
    ) > len(AGENT_NICK_INFIX)


__all__ = ["AGENT_NICK_INFIX", "agent_nick_for", "is_agent_nick"]
