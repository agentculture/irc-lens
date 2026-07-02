"""Task t10 — the AFI-rubric gate, run in-suite when ``afi`` is available.

``irc-lens`` conforms to the ``python-cli`` agent-first CLI pattern cited
from ``citation-cli`` (``afi``); ``afi cli verify`` is the authoritative
checker for the six rubric bundles (Structure / Learnability / JSON /
Errors / Explain / Overview — see ``CLAUDE.md``). The build plan's t10
proof chain requires that gate to run green *in the same suite* as the
e2e/Playwright proofs, so a regression in the CLI contract fails a normal
test run rather than only surfacing in a manual ``afi`` invocation.

``afi`` is a sibling-checkout tool, not a packaged dependency, so it may
not be on ``PATH`` (CI installs it separately; a fresh contributor may
not have it yet). When it's absent the test *skips* rather than fails —
the gate is opt-in on ``afi``'s presence, matching how the ``playwright``
marker keeps browser tests opt-in on a chromium install. When it *is*
present the verifier runs against the repo root and must exit ``0``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Repo root = the directory containing pyproject.toml (one up from tests/).
_REPO_ROOT = Path(__file__).resolve().parent.parent

# `afi cli verify` shells out to `<tool> --help`, `<tool> learn`, etc. for
# each rubric bundle; each is a fast in-process CLI call, but give generous
# headroom for a cold `uv`/import path on a loaded CI runner.
_VERIFY_TIMEOUT_S = 120


@pytest.mark.skipif(shutil.which("afi") is None, reason="afi not installed")
def test_afi_cli_verify_passes() -> None:
    """`afi cli verify` exits 0 against the repo root — the CLI still
    satisfies every AFI rubric bundle. Skipped when `afi` isn't on PATH."""
    result = subprocess.run(
        ["afi", "cli", "verify"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_VERIFY_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, (
        "afi cli verify failed (exit "
        f"{result.returncode}).\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
