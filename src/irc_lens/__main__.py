"""Entry point for ``python -m irc_lens``."""

from __future__ import annotations

import sys

from irc_lens.cli import main

if __name__ == "__main__":
    sys.exit(main())
