"""Tests for the purpose-authored agent doc pages (t6).

The pages are registered via ``irc_lens.front_docs.register_docs``, which
``build_app()`` calls. They are fresh, agent-addressed prose *about* the
running tool (what it is, how to drive it, how to invoke it) — never a
copy of a file under ``docs/``, which is the human-facing reference tree
and is cited by name, not reproduced.

The copy guard (``test_doc_pages_are_not_copies_of_docs_tree``) is the load-
bearing assertion here: it proves independence from ``docs/`` by whitespace-
normalizing both a doc body and a ``docs/**/*.md`` file and checking that
neither is a substring of the other. Doc-slug parity with the CLI ``explain``
surface is intentionally *not* re-asserted here — that is the job of the
surface-agreement gate another same-wave task adds, so this file does not
maintain a second, driftable list of slugs.
"""

from __future__ import annotations

import re
from pathlib import Path

from irc_lens.cli import build_app

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _REPO_ROOT / "docs"

# Each required topic must be covered by at least one registered doc slug.
# Keyed by topic for readable failure messages; the acceptance criterion is
# topic coverage, not any particular slug spelling.
_REQUIRED_TOPICS: dict[str, tuple[str, ...]] = {
    "what irc-lens is": ("about", "index"),
    "how to drive the chat console": ("console",),
    "the tool catalog": ("tools",),
    "exit codes and --json conventions": ("conventions",),
}


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip the ends.

    Whitespace-insensitive comparison catches a copy that has been
    reflowed/re-indented but is otherwise the same text, while leaving
    genuinely distinct prose alone.
    """
    return re.sub(r"\s+", " ", text).strip()


def _registered_docs() -> dict[str, object]:
    app = build_app()
    return {entry.slug: entry for entry in app.list_docs()}


def test_required_topics_have_a_registered_doc() -> None:
    docs = _registered_docs()
    for topic, candidate_slugs in _REQUIRED_TOPICS.items():
        assert any(slug in docs for slug in candidate_slugs), (
            f"no registered doc covers required topic {topic!r} "
            f"(expected one of slugs {candidate_slugs!r}); "
            f"registered slugs={sorted(docs)}"
        )


def test_doc_bodies_are_non_trivial() -> None:
    docs = _registered_docs()
    assert docs, "expected build_app() to register at least one doc"
    for slug, entry in docs.items():
        assert len(entry.text) > 200, (
            f"doc {slug!r} body is only {len(entry.text)} chars; "
            "purpose-authored pages must be substantive, not stubs"
        )


def test_doc_pages_are_not_copies_of_docs_tree() -> None:
    """Neither a registered doc body nor any ``docs/**/*.md`` file may be a
    whitespace-normalized substring of the other.

    This is the copy guard: a verbatim copy (even one that has been
    re-indented or re-wrapped) trips it in one direction or the other.
    Independently authored prose that merely covers similar ground —
    the actual ask here — does not, because the two texts never agree
    for their *entire* length.
    """
    docs = _registered_docs()
    assert docs, "expected build_app() to register at least one doc"

    md_files = sorted(_DOCS_DIR.rglob("*.md"))
    assert md_files, "expected docs/ to contain markdown files to compare against"

    doc_norms = {slug: _normalize(entry.text) for slug, entry in docs.items()}
    file_norms = {path: _normalize(path.read_text(encoding="utf-8")) for path in md_files}

    for slug, doc_norm in doc_norms.items():
        for path, file_norm in file_norms.items():
            rel = path.relative_to(_REPO_ROOT)
            assert doc_norm not in file_norm, (
                f"registered doc {slug!r} is a substring of {rel} — "
                "author fresh prose about the running tool instead of copying docs/"
            )
            assert file_norm not in doc_norm, (
                f"{rel} is a substring of registered doc {slug!r} — "
                "author fresh prose about the running tool instead of copying docs/"
            )


def test_register_docs_is_idempotent_per_app_instance() -> None:
    """Calling build_app() twice must not raise (each call gets a fresh App,
    so add_doc's duplicate-slug guard never fires across builds)."""
    first = {d.slug for d in build_app().list_docs()}
    second = {d.slug for d in build_app().list_docs()}
    assert first == second
    assert first  # non-empty
