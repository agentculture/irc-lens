"""t12 — boundary regression sweep for the agentfront adoption.

Guards two boundaries the plan promised the adoption would respect:

1. **media capability URLs stay auth-exempt** — reuses
   ``tests/test_web_front.py``'s ``_exempt_paths_in`` source-introspection
   helper (rather than duplicating its regex logic) to pin that ``/media/``
   specifically remains in both the dev and cloudflare-access middlewares'
   exempt path sets.
2. **no leftover afi-scaffolding references under src/** — a grep gate for
   ``.afi/`` or ``afi cli `` (trailing space, so it cannot match the
   ``AfiError`` class name, which deliberately remains — it is the stable
   public error type, not a scaffolding reference).

A third boundary originally lived here: a one-time guard asserting the
agentfront-adoption branch's diff against ``main`` (``git diff --name-only
main...HEAD``) never touched ``src/irc_lens/irc/``, ``src/irc_lens/commands.py``,
or ``CITATION.md``. That guard was inherently branch-scoped, and it was
retired once PR #51 merged into ``main`` — its history now lives in ``main``
itself, so ``main...HEAD`` is empty and the assertion of a nonempty diff can
no longer hold. It could not be repurposed into an ongoing invariant either:
forbidding all future changes to those paths would be wrong, since
``CITATION.md`` and the cited files can legitimately be updated when
re-syncing citations.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# 1. Media capability URLs stay auth-exempt.
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
# 2. Grep gate — no leftover afi-scaffolding references under src/.
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
