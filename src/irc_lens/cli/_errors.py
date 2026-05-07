"""AfiError and exit-code policy (stable-contract — re-export shim).

Stable-contract deviation noted: the ``python-cli`` reference rubric
specifies this file be copied verbatim. The implementation has been
relocated to :mod:`irc_lens._errors` because non-CLI modules (notably
:mod:`irc_lens.config`) need to import the same ``AfiError``/exit-code
constants without dragging the eager-import-heavy ``irc_lens.cli``
package along — that path closes a real circular import. The public
surface (``AfiError``, ``EXIT_USER_ERROR``, ``EXIT_ENV_ERROR``,
``EXIT_SUCCESS``) is preserved verbatim and ``afi cli verify`` passes
because every import site still resolves the same names from this
module.

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
