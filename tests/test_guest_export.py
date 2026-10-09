"""PII-stripped export of guest data (task t12, obligation o14)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from irc_lens.cli import main
from irc_lens.export import export_redacted, scrub_text
from irc_lens.guest_store import GuestStore

# Every one of these must be absent from any redacted output.
PII = [
    "alice.wonder@example.com",
    "alice.wonder",
    "bob_the_builder@corp.example.org",
    "203.0.113.42",
    "198.51.100.7",
    "2001:db8:85a3::8a2e:370:7334",
    "+1 (415) 555-0134",
    "+44 20 7946 0958",
    "050-123-4567",
    "0501234567",
    "sbx-alice",
    "sbx-bobby",
    "Alice",
    "Wonder",
    "Priya Raman",
    "Raman",
    "Dmitri",
]


@pytest.fixture
def store(tmp_path: Path) -> GuestStore:
    s = GuestStore(tmp_path / "guests.db")
    s.record_guest("alice.wonder@example.com", "sbx-alice", "203.0.113.42")
    s.record_guest("bob_the_builder@corp.example.org", "sbx-bobby", "198.51.100.7")
    s.record_input(
        "alice.wonder@example.com",
        kind="message",
        payload="hi, mail me at alice.wonder@example.com or call +1 (415) 555-0134",
    )
    s.record_input(
        "alice.wonder@example.com",
        kind="message",
        payload="my name is Priya Raman, ip 2001:db8:85a3::8a2e:370:7334 and 203.0.113.42",
    )
    s.record_input(
        "bob_the_builder@corp.example.org",
        kind="message",
        payload="sbx-alice said hello; ring 050-123-4567 / 0501234567 / +44 20 7946 0958",
    )
    s.record_input(
        "bob_the_builder@corp.example.org",
        kind="message",
        payload="I'm Dmitri, from 198.51.100.7. Thanks, sbx-bobby. Hi Alice Wonder",
    )
    return s


def _no_pii(text: str) -> None:
    for item in PII:
        assert item.lower() not in text.lower(), item


@pytest.mark.parametrize("fmt", ["jsonl", "md"])
def test_fixture_output_contains_no_pii(store: GuestStore, fmt: str) -> None:
    out = export_redacted(store, fmt=fmt)
    _no_pii(out)
    assert not re.search(r"@\w", out)
    assert "guest-" in out


def test_jsonl_shape_and_stable_pseudonyms(store: GuestStore) -> None:
    rows = [json.loads(line) for line in export_redacted(store).splitlines()]
    assert len(rows) == 4
    assert {r["guest"] for r in rows} == {"guest-1", "guest-2"}
    assert set(rows[0]) == {"guest", "kind", "date", "text"}
    # same person -> same pseudonym across rows
    assert rows[0]["guest"] == rows[1]["guest"] != rows[2]["guest"]


def test_nick_mentions_replaced_by_pseudonym(store: GuestStore) -> None:
    rows = [json.loads(line) for line in export_redacted(store).splitlines()]
    by_guest = {r["guest"]: r for r in rows}
    text = " ".join(r["text"] for r in rows)
    assert "sbx-" not in text
    pseudo_of_alice = rows[0]["guest"]
    assert f"{pseudo_of_alice} said hello" in text


def test_no_mapping_in_output_or_on_disk(store: GuestStore, tmp_path: Path) -> None:
    before = {p.name for p in tmp_path.iterdir()}
    out = export_redacted(store)
    assert {p.name for p in tmp_path.iterdir()} == before
    assert "mapping" not in out.lower()


def test_pseudonyms_differ_between_exports(store: GuestStore) -> None:
    seen = {
        tuple(json.loads(l)["guest"] for l in export_redacted(store).splitlines())
        for _ in range(30)
    }
    assert len(seen) > 1  # assignment is randomised per export


def test_scrub_text_unit() -> None:
    t = scrub_text(
        "x@y.io 192.168.1.1 ::1 +972-50-123-4567 call me Sam Smith, name's Lee",
        known_tokens=[],
    )
    for bad in ["x@y.io", "192.168.1.1", "972", "4567", "Sam", "Smith", "Lee"]:
        assert bad not in t
    assert "[email]" in t
    assert "[ip]" in t
    assert "[phone]" in t


def test_plain_text_untouched() -> None:
    assert scrub_text("hello there, 3 cats and 42 dogs", known_tokens=[]) == (
        "hello there, 3 cats and 42 dogs"
    )


def test_cli_requires_redacted(store: GuestStore, capsys) -> None:
    assert main(["guests", "export", "--store", str(store.path)]) != 0


def test_cli_export_stdout_and_file(store: GuestStore, tmp_path: Path, capsys) -> None:
    assert main(["guests", "export", "--redacted", "--store", str(store.path)]) == 0
    _no_pii(capsys.readouterr().out)
    dest = tmp_path / "out" / "t.md"
    rc = main(
        [
            "guests",
            "export",
            "--redacted",
            "--format",
            "md",
            "--out",
            str(dest),
            "--store",
            str(store.path),
        ]
    )
    assert rc == 0
    _no_pii(dest.read_text())
    assert sorted(p.name for p in dest.parent.iterdir()) == ["t.md"]


def test_export_includes_the_anonymized_corpus(tmp_path) -> None:
    """d8: corpus rows (kept after a guest's deletion) are part of the owner's
    export, with no guest pseudonym."""
    from irc_lens.guest_store import GuestStore
    from irc_lens.export import export_redacted

    s = GuestStore(tmp_path / "g.db")
    s.keep_corpus([{"question": "what is culture?", "answer": "an IRC mesh", "date": "2026-10-09"}])
    rows = [json.loads(l) for l in export_redacted(s).splitlines()]
    assert rows == [
        {
            "guest": "anonymous",
            "kind": "qa",
            "date": "2026-10-09",
            "text": "Q: what is culture?\nA: an IRC mesh",
        }
    ]


def test_scrub_ipv6_forms_and_not_times() -> None:
    from irc_lens.export import scrub_text

    assert scrub_text("ping ::1 now") == "ping [ip] now"
    assert scrub_text("from fe80::1 ok") == "from [ip] ok"
    assert scrub_text("addr 2001:db8::ff00:42:8329.") == "addr [ip]."
    assert scrub_text("mapped ::ffff:192.0.2.1 x") == "mapped [ip] x"
    assert scrub_text("at 10:30:45 today") == "at 10:30:45 today"
