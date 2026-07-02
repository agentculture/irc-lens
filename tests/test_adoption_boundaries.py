"""t12 — boundary regression sweep for the agentfront adoption.

Guards three boundaries the plan promised the adoption would respect:

1. **the diff stays out of the IRC transport / command core** — the whole
   agentfront-adoption branch (``main...HEAD``) touches no file under
   ``src/irc_lens/irc/``, none equal to ``src/irc_lens/commands.py``, and
   none equal to ``CITATION.md``. Checked via ``git diff --name-only`` from
   a subprocess — a repo-history assertion is inherently git-bound, so
   shelling out here (rather than reaching for a git library) is the
   pragmatic choice. Skipped only when ``git merge-base`` itself fails
   (e.g. a shallow checkout with no local ``main`` ref) — that failure mode
   is exactly what CI's ``actions/checkout`` (full history) does not hit,
   so the skip is a local/degraded-environment escape hatch, not a way for
   the gate to go quietly dark in CI.
2. **media capability URLs stay auth-exempt** — reuses
   ``tests/test_web_front.py``'s ``_exempt_paths_in`` source-introspection
   helper (rather than duplicating its regex logic) to pin that ``/media/``
   specifically remains in both the dev and cloudflare-access middlewares'
   exempt path sets.
3. **no leftover afi-scaffolding references under src/** — a grep gate for
   ``.afi/`` or ``afi cli `` (trailing space, so it cannot match the
   ``AfiError`` class name, which deliberately remains — it is the stable
   public error type, not a scaffolding reference).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# 1. Adoption diff touches no forbidden path.
# ---------------------------------------------------------------------------


def _merge_base_available() -> bool:
    result = subprocess.run(
        ["git", "merge-base", "main", "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.skipif(
    not _merge_base_available(),
    reason=(
        "git history unavailable (no local `main` ref reachable from HEAD, "
        "e.g. a shallow checkout) — CI's actions/checkout uses full history, "
        "so this history assertion runs there even when skipped locally"
    ),
)
def test_adoption_diff_does_not_touch_forbidden_paths() -> None:
    result = subprocess.run(
        ["git", "diff", "--name-only", "main...HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert changed, "expected the agentfront-adoption branch to have a nonempty diff against main"

    violations = [
        path
        for path in changed
        if path.startswith("src/irc_lens/irc/")
        or path == "src/irc_lens/commands.py"
        or path == "CITATION.md"
    ]
    assert not violations, f"adoption diff touches forbidden paths: {violations}"


# ---------------------------------------------------------------------------
# 2. Media capability URLs stay auth-exempt.
# ---------------------------------------------------------------------------


def test_media_capability_urls_stay_auth_exempt() -> None:
    """Same source-introspection technique
    ``tests/test_web_front.py::test_exempt_lists_contain_exactly_static_healthz_media``
    already applies — reused here via that file's ``_exempt_paths_in``
    helper (not duplicated) to pin specifically that ``/media/`` stays in
    both middlewares' auth-exempt path sets after the adoption."""
    from test_web_front import _exempt_paths_in

    from irc_lens.web import app as app_module
    from irc_lens.web import auth as auth_module

    dev_exempt = _exempt_paths_in(app_module._dev_identity_middleware)
    cf_exempt = _exempt_paths_in(auth_module.build_cloudflare_middleware)

    assert "/media/" in dev_exempt, f"media capability URLs must stay auth-exempt (dev): {dev_exempt}"
    assert "/media/" in cf_exempt, f"media capability URLs must stay auth-exempt (cf): {cf_exempt}"


# ---------------------------------------------------------------------------
# 3. Grep gate — no leftover afi-scaffolding references under src/.
# ---------------------------------------------------------------------------

# Precisely scoped so it cannot match the `AfiError` class name: `afi cli `
# requires a trailing space (a scaffolding-command reference, e.g. `afi cli
# verify`), and `.afi/` requires the path-prefix slash (a reference to the
# `.afi/reference/` citation directory). Neither substring appears in
# `AfiError`.
_AFI_SCAFFOLDING_RE = re.compile(r"\.afi/|afi cli ")

# Extensions worth grepping as text; skips binary assets (png/ico) under
# src/irc_lens/static/.
_TEXT_SUFFIXES = {".py", ".md", ".j2", ".css", ".js", ".toml", ".cfg", ".ini", ".yaml", ".yml"}


def test_no_afi_scaffolding_references_under_src() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC_DIR.rglob("*")):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        if _AFI_SCAFFOLDING_RE.search(text):
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, f"afi-scaffolding references remain under src/: {offenders}"


def test_scaffolding_pattern_does_not_match_afi_error() -> None:
    """Regression guard for the pattern itself: `AfiError` (and its
    lowercase-word neighbours) must never match, so the gate above cannot
    accidentally flag the class irc-lens deliberately kept."""
    for benign in ("AfiError", "raise AfiError(", "class AfiError(AgentfrontError):"):
        assert not _AFI_SCAFFOLDING_RE.search(benign), f"pattern falsely matched {benign!r}"
