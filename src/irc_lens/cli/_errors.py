"""AfiError and exit-code policy (stable-contract — copy verbatim).

Every failure inside irc-lens raises :class:`AfiError`. The CLI entry
point catches it and exits with :attr:`AfiError.code`. Guarantees:

* no Python traceback leaks to stderr;
* every error has shape ``{code, message, remediation}``;
* the exit-code policy is centralised.

The implementation lives in :mod:`irc_lens._errors` so that non-CLI
modules (e.g. ``irc_lens.config``) can import it without creating a
circular dependency through ``irc_lens.cli``. Everything is re-exported
here so all existing ``from irc_lens.cli._errors import ...`` call
sites continue to work unchanged.
"""
from __future__ import annotations

from irc_lens._errors import (  # noqa: F401 — re-exports for the stable-contract API
    EXIT_ENV_ERROR,
    EXIT_SUCCESS,
    EXIT_USER_ERROR,
    AfiError,
)

__all__ = ["EXIT_SUCCESS", "EXIT_USER_ERROR", "EXIT_ENV_ERROR", "AfiError"]
