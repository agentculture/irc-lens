"""Guest privacy hardening (owner deviation d9).

1. Deletion keeps nothing: no anonymized corpus, no question or answer text
   of the guest anywhere in the store after deletion.
2. The deletion log stores only sha256(lower(email)), never the email.
3. Training use is a separate, optional, unchecked-by-default consent; the
   export includes only guests whose latest consent opted in.
4. A retention sweep erases inactive guests (same path as user deletion)
   and expires tokens, attempts and old bans; it runs at startup and hourly.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import sqlite3
from pathlib import Path

import pytest

from irc_lens.config import load_config
from irc_lens.export import export_redacted
from irc_lens.guest_store import GuestStore, email_hash
from irc_lens.web import entry, retention

# Fixtures (env / legal_server) and helpers reused from the admin suite.
from test_guest_admin import EMAIL, IP, NICK, _env, env, legal_server  # noqa: F401
from test_entry import GUEST, _token_from_mail, h, h_off  # noqa: F401

DAY = 86400
QUESTION = "what is the zebra-quartz protocol?"
ANSWER = "zebra-quartz is a made-up answer"
TRAIN_LABEL = "Use my conversations to improve culture.dev's models (optional)"


def _every_cell(path: Path) -> list[str]:
    """Every value of every row of every table, as text."""
    out: list[str] = []
    with sqlite3.connect(path) as con:
        tables = [
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for t in tables:
            for row in con.execute(f'SELECT * FROM "{t}"'):  # noqa: S608 - test
                out.extend(str(v) for v in row if v is not None)
    return out


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as con:
        return {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


# -- 1. deletion keeps nothing -------------------------------------------------


def test_store_has_no_corpus_api_or_table(tmp_path) -> None:
    s = GuestStore(tmp_path / "g.db")
    assert not hasattr(s, "keep_corpus")
    assert not hasattr(s, "list_corpus")
    assert "corpus" not in _tables(s.path)
    import irc_lens.corpus as corpus

    assert not hasattr(corpus, "anonymized_pairs")
    assert hasattr(corpus, "answer_recorder")  # answers are still recorded


def test_existing_corpus_table_is_dropped_on_init(tmp_path) -> None:
    db = tmp_path / "g.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE corpus (id TEXT PRIMARY KEY, day TEXT NOT NULL, "
            "question TEXT NOT NULL, answer TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO corpus VALUES ('x', '2026-10-01', ?, ?)", (QUESTION, ANSWER)
        )
    GuestStore(db)
    assert "corpus" not in _tables(db)
    assert QUESTION.encode() not in db.read_bytes()


async def test_web_deletion_leaves_no_question_or_answer_text(env, tmp_path):
    env.store.room_id(EMAIL)
    env.store.record_input(EMAIL, kind="message", payload=QUESTION)
    env.store.record_input(EMAIL, kind="answer", payload=ANSWER)
    tok = env.store.issue_token(EMAIL, purpose="delete")
    r = await env.client.post("/delete/confirm", data={"email": EMAIL, "code": tok})
    assert r.status == 200
    cells = "\n".join(_every_cell(env.store.path))
    assert "zebra-quartz" not in cells
    assert "zebra-quartz" not in "\n".join(
        sqlite3.connect(env.store.path).iterdump()
    )
    assert b"zebra-quartz" not in env.store.path.read_bytes()


# -- 2. deletion log stores only a hash ----------------------------------------


def test_email_hash_is_sha256_of_lowercased_email() -> None:
    assert email_hash("Gus@Example.NET") == hashlib.sha256(
        b"gus@example.net"
    ).hexdigest()


def test_deletion_log_holds_hash_not_email(tmp_path) -> None:
    s = GuestStore(tmp_path / "g.db")
    s.record_guest(EMAIL, NICK, IP)
    s.record_consent(EMAIL, IP, tos_version="t", privacy_version="p")
    s.record_input(EMAIL, kind="message", payload="hi")
    s.record_flag(EMAIL, detail="x")
    s.issue_token(EMAIL, purpose="guest")
    s.room_id(EMAIL)
    s.delete_guest_inputs(EMAIL)
    with sqlite3.connect(s.path) as con:
        rows = con.execute("SELECT email FROM deletions").fetchall()
    assert rows == [(email_hash(EMAIL),)]
    assert EMAIL not in "\n".join(_every_cell(s.path))
    assert EMAIL not in "\n".join(sqlite3.connect(s.path).iterdump())


def test_banned_guest_email_survives_only_in_bans(tmp_path) -> None:
    s = GuestStore(tmp_path / "g.db")
    s.record_guest(EMAIL, NICK, IP)
    s.ban(email=EMAIL, reason="abuse")
    s.delete_guest_inputs(EMAIL)
    with sqlite3.connect(s.path) as con:
        for table in _tables(s.path) - {"bans"}:
            cells = [
                str(v)
                for row in con.execute(f'SELECT * FROM "{table}"')  # noqa: S608
                for v in row
            ]
            assert EMAIL not in cells, table
    assert s.is_banned(EMAIL, None)


def test_existing_plaintext_deletion_rows_are_hashed_on_init(tmp_path) -> None:
    db = tmp_path / "g.db"
    GuestStore(db)
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO deletions VALUES (?, 1, 2)", ("Old@Example.com",))
    GuestStore(db)
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT email FROM deletions").fetchall()
    assert rows == [(email_hash("old@example.com"),)]


# -- 3. separate, optional training consent ------------------------------------


async def test_training_checkbox_unchecked_and_optional(h):
    page = await (await h.post("/entry/guest", {"email": GUEST})).text()
    tag = next(
        line for line in page.splitlines() if 'name="train"' in line
    )
    assert 'type="checkbox"' in tag
    assert "checked" not in tag
    assert "required" not in tag
    assert TRAIN_LABEL in html_lib.unescape(page)


async def _sign_up(h, *, train: bool):
    data = {"email": GUEST, "nickname": "maya", "consent": "on"}
    if train:
        data["train"] = "on"
    resp = await h.post("/entry/guest/start", data)
    assert resp.status == 200
    page = await resp.text()
    token = _token_from_mail(h)
    verify = {"email": GUEST, "nickname": "maya", "code": token}
    if 'name="train" value="on"' in page:
        verify["train"] = "on"
    return await h.post("/entry/verify", verify)


async def test_signup_without_training_box_records_train_0(h):
    resp = await _sign_up(h, train=False)
    assert resp.status == 303
    assert h.store.training_opt_in(GUEST) is False
    with sqlite3.connect(h.store.path) as con:
        assert con.execute("SELECT train FROM consents").fetchall() == [(0,)]


async def test_signup_with_training_box_records_train_1(h):
    resp = await _sign_up(h, train=True)
    assert resp.status == 303
    assert h.store.training_opt_in(GUEST) is True
    with sqlite3.connect(h.store.path) as con:
        assert con.execute("SELECT train FROM consents").fetchall() == [(1,)]


async def test_code_page_identical_for_any_email_with_training(h):
    a = await h.post(
        "/entry/guest/start",
        {"email": GUEST, "nickname": "maya", "consent": "on", "train": "on"},
        ip="10.9.0.1",
    )
    b = await h.post(
        "/entry/guest/start",
        {"email": "z@example.org", "nickname": "maya", "consent": "on", "train": "on"},
        ip="10.9.0.2",
    )
    assert a.status == b.status == 200
    ta = (await a.text()).replace(GUEST, "<E>")
    tb = (await b.text()).replace("z@example.org", "<E>")
    assert ta == tb


def test_consents_train_column_migrated_on_existing_db(tmp_path) -> None:
    db = tmp_path / "g.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE consents (email TEXT NOT NULL, ip TEXT NOT NULL, "
            "tos_version TEXT NOT NULL, privacy_version TEXT NOT NULL, ts INTEGER NOT NULL)"
        )
        con.execute("INSERT INTO consents VALUES ('a@x.io', '1.1.1.1', 't', 'p', 5)")
    s = GuestStore(db)
    GuestStore(db)  # idempotent
    with sqlite3.connect(db) as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(consents)")]
        assert cols.count("train") == 1
        assert con.execute("SELECT train FROM consents").fetchall() == [(0,)]
    assert s.training_opt_in("a@x.io") is False


def test_set_training_updates_latest_consent(tmp_path) -> None:
    clock = [1000]
    s = GuestStore(tmp_path / "g.db", clock=lambda: clock[0])
    s.record_consent(EMAIL, IP, tos_version="t", privacy_version="p", train=True)
    clock[0] = 2000
    s.record_consent(EMAIL, IP, tos_version="t2", privacy_version="p2")
    assert s.training_opt_in(EMAIL) is False
    assert s.set_training(EMAIL, True) == 1
    assert s.training_opt_in(EMAIL) is True
    with sqlite3.connect(s.path) as con:
        assert con.execute("SELECT train FROM consents ORDER BY ts").fetchall() == [
            (1,),
            (1,),
        ]
    s.set_training(EMAIL, False)
    assert s.training_opt_in(EMAIL) is False
    assert s.set_training("nobody@example.org", True) == 0


def test_export_only_includes_opted_in_guests(tmp_path) -> None:
    s = GuestStore(tmp_path / "g.db")
    s.record_guest("in@example.com", "sbx-inny", "10.0.0.1")
    s.record_guest("out@example.com", "sbx-outy", "10.0.0.2")
    s.record_consent("in@example.com", "10.0.0.1", tos_version="t", privacy_version="p", train=True)
    s.record_consent("out@example.com", "10.0.0.2", tos_version="t", privacy_version="p")
    s.record_input("in@example.com", kind="message", payload="opted-in question")
    s.record_input("out@example.com", kind="message", payload="private question")
    s.record_input("nobody@example.com", kind="message", payload="no consent at all")
    rows = [json.loads(line) for line in export_redacted(s).splitlines()]
    assert [r["text"] for r in rows] == ["opted-in question"]
    assert {r["guest"] for r in rows} == {"guest-1"}
    assert "qa" not in {r["kind"] for r in rows}
    md = export_redacted(s, fmt="md")
    assert "private question" not in md
    assert "no consent at all" not in md


def test_export_follows_latest_consent_row(tmp_path) -> None:
    clock = [1000]
    s = GuestStore(tmp_path / "g.db", clock=lambda: clock[0])
    s.record_guest("a@example.com", "sbx-aaa", "10.0.0.1")
    s.record_consent("a@example.com", "10.0.0.1", tos_version="t", privacy_version="p", train=True)
    s.record_input("a@example.com", kind="message", payload="hello")
    clock[0] = 2000
    s.record_consent("a@example.com", "10.0.0.1", tos_version="t2", privacy_version="p2")
    assert export_redacted(s) == ""
    s.set_training("a@example.com", True)
    assert "hello" in export_redacted(s)


# -- 4. retention sweep ----------------------------------------------------------


def _clocked(tmp_path, start: int = 1_000_000_000):
    clock = [start]
    return GuestStore(tmp_path / "g.db", clock=lambda: clock[0]), clock


def test_sweep_deletes_inactive_guests_with_hashed_log(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.record_guest("old@example.com", "sbx-old", "10.0.0.1")
    s.record_consent("old@example.com", "10.0.0.1", tos_version="t", privacy_version="p")
    s.record_input("old@example.com", kind="message", payload="ancient question")
    s.room_id("old@example.com")
    s.record_flag("old@example.com", detail="nsfw")
    now = clock[0] + 91 * DAY
    counts = s.sweep(now=now)
    assert counts["guests"] == 1
    assert counts["flags"] == 1
    assert s.get_guest("old@example.com") == []
    assert s.list_flags("old@example.com") == []
    assert s.peek_room("old@example.com") is None
    cells = "\n".join(_every_cell(s.path))
    assert "old@example.com" not in cells
    assert "ancient question" not in cells
    with sqlite3.connect(s.path) as con:
        assert con.execute("SELECT email FROM deletions").fetchall() == [
            (email_hash("old@example.com"),)
        ]


def test_sweep_keeps_recently_active_guest(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.record_guest("busy@example.com", "sbx-busy", "10.0.0.1")
    s.record_flag("busy@example.com", detail="x")
    clock[0] += 80 * DAY
    s.record_input("busy@example.com", kind="message", payload="still here")
    counts = s.sweep(now=clock[0] + 30 * DAY)  # created 110 days ago, input 30
    assert counts["guests"] == 0
    assert counts["flags"] == 0
    assert s.get_guest("busy@example.com")
    assert s.list_flags("busy@example.com")


def test_sweep_activity_counts_consents(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.record_guest("c@example.com", "sbx-ccc", "10.0.0.1")
    clock[0] += 85 * DAY
    s.record_consent("c@example.com", "10.0.0.1", tos_version="t", privacy_version="p")
    assert s.sweep(now=clock[0] + 10 * DAY)["guests"] == 0
    assert s.sweep(now=clock[0] + 91 * DAY)["guests"] == 1


def test_sweep_inactive_days_is_configurable(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.record_guest("g@example.com", "sbx-ggg", "10.0.0.1")
    assert s.sweep(now=clock[0] + 10 * DAY, inactive_days=30)["guests"] == 0
    assert s.sweep(now=clock[0] + 31 * DAY, inactive_days=30)["guests"] == 1


def test_sweep_expires_tokens_older_than_a_day(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.record_guest("t@example.com", "sbx-ttt", "10.0.0.1")
    s.issue_token("t@example.com", purpose="guest")
    clock[0] += DAY - 10
    s.issue_token("t@example.com", purpose="guest")
    counts = s.sweep(now=clock[0] + 20)
    assert counts["tokens"] == 1
    with sqlite3.connect(s.path) as con:
        assert con.execute("SELECT COUNT(*) FROM tokens").fetchone() == (1,)


def test_sweep_expires_attempts_older_than_a_day(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.record_attempt("token", "i:10.0.0.1")
    clock[0] += DAY - 10
    s.record_attempt("token", "i:10.0.0.1")
    counts = s.sweep(now=clock[0] + 20)
    assert counts["attempts"] == 1
    with sqlite3.connect(s.path) as con:
        assert con.execute("SELECT COUNT(*) FROM attempts").fetchone() == (1,)


def test_sweep_expires_bans_older_than_a_year(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.ban(email="old-ban@example.com", reason="x")
    clock[0] += 300 * DAY
    s.ban(ip="10.6.6.6", reason="y")
    counts = s.sweep(now=clock[0] + 66 * DAY)  # 366 and 66 days old
    assert counts["bans"] == 1
    assert not s.is_banned("old-ban@example.com", None)
    assert s.is_banned(None, "10.6.6.6")


def test_sweep_uses_erase_callback_for_each_inactive_guest(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.record_guest("a@example.com", "sbx-aaa", "10.0.0.1")
    s.record_guest("b@example.com", "sbx-bbb", "10.0.0.2")
    seen = []

    def erase(email: str) -> int:
        seen.append(email)
        return s.delete_guest_inputs(email)

    counts = s.sweep(now=clock[0] + 91 * DAY, erase=erase)
    assert sorted(seen) == ["a@example.com", "b@example.com"]
    assert counts["guests"] == 2
    assert s.list_guests() == []


def test_sweep_defaults_now_to_store_clock(tmp_path) -> None:
    s, clock = _clocked(tmp_path)
    s.record_guest("g@example.com", "sbx-ggg", "10.0.0.1")
    assert s.sweep()["guests"] == 0
    clock[0] += 91 * DAY
    assert s.sweep()["guests"] == 1


# -- 4. config + background task -------------------------------------------------


def _cfg_yaml(tmp_path: Path, guest: str = "") -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        "auth:\n  mode: dev\n  dev:\n    nick: lens\n    email: dev@local\n"
        f"server:\n  name: spark\n{guest}"
    )
    return p


def test_retention_days_config_default_and_override(tmp_path) -> None:
    assert load_config(_cfg_yaml(tmp_path)).guest_retention_days == 90
    cfg = load_config(_cfg_yaml(tmp_path, "guest_mode:\n  retention_days: 30\n"))
    assert cfg.guest_retention_days == 30


@pytest.mark.parametrize("bad", ["0", "-5", "nope"])
def test_retention_days_must_be_positive_int(tmp_path, bad) -> None:
    from irc_lens.cli._errors import AfiError

    with pytest.raises(AfiError) as exc:
        load_config(_cfg_yaml(tmp_path, f"guest_mode:\n  retention_days: {bad}\n"))
    assert "guest_mode.retention_days" in exc.value.message


async def test_retention_task_sweeps_at_startup_and_is_cancelled(
    jwks, legal_server, tmp_path  # noqa: F811
):
    # A guest last active 100 days ago, already in the store at startup.
    db = tmp_path / "guests.db"
    old = GuestStore(db, clock=lambda: 1_000_000_000)
    old.record_guest("stale@example.com", "sbx-stale", "10.0.0.3")
    old.record_input("stale@example.com", kind="message", payload="stale question")

    e = await _env(jwks, legal_server, tmp_path)
    try:
        task = e.app[retention.RETENTION_TASK]
        assert isinstance(task, asyncio.Task)
        assert not task.done()
        loop = asyncio.get_running_loop()
        end = loop.time() + 3
        while loop.time() < end and e.store.get_guest("stale@example.com"):
            await asyncio.sleep(0.02)
        assert e.store.get_guest("stale@example.com") == []
        # The fixture's own guest was created just now and survives.
        assert e.store.get_guest(EMAIL)
        assert not task.done()  # keeps running for the hourly sweep
    finally:
        await e.client.close()
    assert task.cancelled()
    assert "stale question" not in "\n".join(_every_cell(db))


async def test_retention_sweep_once_purges_flag_log_and_media(
    jwks, legal_server, tmp_path  # noqa: F811
):
    import dataclasses

    flags = tmp_path / "flags.jsonl"
    flags.write_text(
        json.dumps({"nick": "sbx-stale", "excerpt": "x"})
        + "\n"
        + json.dumps({"nick": "sbx-kim", "excerpt": "y"})
        + "\n"
    )
    e = await _env(jwks, legal_server, tmp_path)
    try:
        state = e.app[entry.ENTRY_STATE]
        state.config = dataclasses.replace(
            state.config, guest_sandbox_flag_log=str(flags)
        )
        clock = [1_000_000_000]
        e.store._clock = lambda: clock[0]
        e.store.record_guest("stale@example.com", "sbx-stale", "10.0.0.3")
        clock[0] += 91 * DAY
        counts = await retention.sweep_once(e.app, now=clock[0])
        assert counts["guests"] >= 1
        assert e.store.get_guest("stale@example.com") == []
        assert "sbx-stale" not in flags.read_text()
        assert "sbx-kim" in flags.read_text()
    finally:
        await e.client.close()


async def test_retention_not_installed_when_guest_mode_off(h_off):
    assert retention.RETENTION_TASK not in h_off.client.app
