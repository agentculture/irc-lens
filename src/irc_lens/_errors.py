"""Shared AfiError and exit-code constants.

This module is intentionally placed at the top of the ``irc_lens``
package so it can be imported by both ``irc_lens.config`` and
``irc_lens.cli`` without creating a circular dependency.

``irc_lens.cli._errors`` re-exports everything from here so
all existing imports of ``irc_lens.cli._errors`` continue to work.
"""
from __future__ import annotations

from dataclasses import dataclass

EXIT_SUCCESS = 0
EXIT_USER_ERROR = 1
EXIT_ENV_ERROR = 2


@dataclass
class AfiError(Exception):
    """Structured error with a remediation hint for agents."""

    code: int
    message: str
    remediation: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }
