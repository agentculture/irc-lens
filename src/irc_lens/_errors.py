"""Shared AfiError and exit-code constants.

This module is intentionally placed at the top of the ``irc_lens``
package so it can be imported by both ``irc_lens.config`` and
``irc_lens.cli`` without creating a circular dependency.

The exit-code constants and ``AfiError`` are mapped directly onto
``agentfront.errors``: the ``EXIT_*`` values are re-exported from
agentfront rather than re-declared, and ``AfiError`` *subclasses*
``agentfront.errors.AgentfrontError`` (same ``(code, message,
remediation)`` fields, same ``to_dict()`` shape) instead of aliasing
it. Subclassing means any dispatcher that catches ``AgentfrontError``
— including agentfront's own — catches ``AfiError`` instances
natively, which is what lets irc-lens's CLI dispatcher migrate onto
agentfront's in a later step without an error-translation layer in
between.

``irc_lens.cli._errors`` re-exports everything from here so
all existing imports of ``irc_lens.cli._errors`` continue to work.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentfront.errors import AgentfrontError
from agentfront.errors import (  # noqa: F401 — re-exported exit-code constants
    EXIT_ENV_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
)


@dataclass
class AfiError(AgentfrontError):
    """Structured error with a remediation hint for agents.

    Subclasses :class:`agentfront.errors.AgentfrontError` verbatim (no
    new fields, no overridden behaviour) so that irc-lens's own
    ``{code, message, remediation}`` error shape *is* agentfront's, and
    any ``except AgentfrontError`` clause — including agentfront's own
    dispatcher — catches ``AfiError`` instances without irc-lens having
    to translate between the two.
    """
