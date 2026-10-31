"""The input gate: one policy for every line a human -- or their agent -- sends.

Extracted from ``POST /input`` (``routes.post_input``) so the personal
agent's MCP path (``web/agent_sessions.py``) re-enters the *same* checks
instead of bypassing them (o15):

* the ``/delete`` redirect: deletion is a page, never an IRC line;
* the per-human message rate limit (guests: ``guest_rate_messages_per_min``
  over 60 s, counted per human email *and* per caller IP; every non-empty
  input counts, refused commands included, so probing is never free). A
  guest and their agent share the human's bucket;
* the sandbox command allowlist (:func:`irc_lens.commands.allowed_in_sandbox`);
* for agents only: admin-only commands are refused (an agent is never an
  admin, whoever its human is);
* the input log: a guest's chat lines -- and their agent's, kind
  ``agent_message`` -- are recorded under the human's email, so export,
  deletion and the retention sweep cover both.

The gate decides; callers render. ``routes.post_input`` maps a
:class:`Refusal` onto its ``{error, hint}`` HTTP responses unchanged; the
agent path turns it into a tool error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from irc_lens import metrics
from irc_lens.commands import (
    DELETE_COMMAND,
    CommandType,
    ParsedCommand,
    allowed_in_sandbox,
    is_admin_only,
)
from irc_lens.web.identity import TIER_GUEST
from irc_lens.web.sessions import BACKEND_SANDBOX

#: Rate-limit bucket / window for guest chat input.
MESSAGE_RATE_KIND = "message"
MESSAGE_RATE_WINDOW_S = 60

#: Input-log kinds: the human's own chat line, and their agent's.
INPUT_KIND_HUMAN = "message"
INPUT_KIND_AGENT = "agent_message"

#: Refusal reasons (stable strings; callers branch on them).
REASON_DELETE = "delete-page"
REASON_RATE = "rate-limited"
REASON_NOT_ALLOWED = "not-allowed"
REASON_ADMIN_ONLY = "admin-only"


@dataclass(frozen=True)
class InputActor:
    """Who is sending, from VERIFIED state only (never from the input).

    ``email`` is the human's address (the guest-store key); ``tier`` the
    human's tier; ``backend`` the backend their session was routed to;
    ``ip`` the caller's address (browser or agent); ``via_agent`` marks the
    human's personal agent.
    """

    tier: str
    email: str
    backend: str
    ip: str
    via_agent: bool = False


@dataclass(frozen=True)
class Refusal:
    status: int
    reason: str
    error: str
    hint: str
    headers: dict[str, str] = field(default_factory=dict)


def _rate_limited(store: Any, limit: int, actor: InputActor) -> bool:
    """Check the per-email and per-IP buckets, then count this message."""
    keys = (f"e:{actor.email}", f"i:{actor.ip}")
    limited = any(
        store.rate_limited(
            MESSAGE_RATE_KIND, k, limit=limit, window=MESSAGE_RATE_WINDOW_S
        )
        for k in keys
    )
    if not limited:
        for k in keys:
            store.record_attempt(MESSAGE_RATE_KIND, k)
    return limited


def check(
    app: Any, actor: InputActor, parsed: ParsedCommand, text: str
) -> Refusal | None:
    """Run the gate; a :class:`Refusal` to send instead, or None to proceed.

    Order matters and matches the original handler: the ``/delete``
    redirect, then the rate limit (so a refused command still counts), then
    the allowlist.
    """
    if actor.via_agent and is_admin_only(parsed):
        return Refusal(403, REASON_ADMIN_ONLY, "agents never run admin commands", "")
    if actor.backend != BACKEND_SANDBOX:
        return None
    guest = actor.tier == TIER_GUEST
    if guest and text.strip().lower() == DELETE_COMMAND:
        return Refusal(
            204,
            REASON_DELETE,
            "/delete is a page, not a message",
            "the human deletes their data from the deletion page",
        )
    if guest and _rate_limited(
        app["guest_store"], app["config"].guest_rate_messages_per_min, actor
    ):
        metrics.get_metrics().rate_limited()
        return Refusal(
            429,
            REASON_RATE,
            "Slow down",
            "Too many messages",
            {"Retry-After": str(MESSAGE_RATE_WINDOW_S)},
        )
    if not allowed_in_sandbox(parsed, preview=not guest):
        return Refusal(403, REASON_NOT_ALLOWED, "Not in guest view", "Try /help")
    return None


def record(app: Any, actor: InputActor, parsed: ParsedCommand, text: str) -> None:
    """The input log: a guest's (or their agent's) chat line, after it ran.

    Keyed by the human's email -- export, deletion and retention read it
    back from there.
    """
    if actor.tier != TIER_GUEST or parsed.type != CommandType.CHAT:
        return
    kind = INPUT_KIND_AGENT if actor.via_agent else INPUT_KIND_HUMAN
    app["guest_store"].record_input(actor.email, kind=kind, payload=text)


__all__ = [
    "INPUT_KIND_AGENT",
    "INPUT_KIND_HUMAN",
    "MESSAGE_RATE_KIND",
    "MESSAGE_RATE_WINDOW_S",
    "InputActor",
    "Refusal",
    "check",
    "record",
]
