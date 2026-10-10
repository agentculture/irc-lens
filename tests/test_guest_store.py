"""Unit tests for `irc_lens.guest_store` (guest mode, task t5).

Covers the acceptance criteria for the guest store:

1. A SQLite-backed :class:`GuestStore(path)` persists guests (verified
   email, nick, IP), consent records (email, IP, ToS + Privacy version,
   timestamp), single-use tokens, bans, and approved-user password
   hashes — and survives a restart (a second ``GuestStore`` on the same
   path sees everything).
2. Passwords are stored only as argon2id hashes; no plaintext or
   reversible form anywhere (the tests inspect the raw DB bytes).

Obligations:

* o6 — tokens are accepted once and rejected after their TTL (default
  900 s, configurable per issue); only a hash of the token is stored,
  never the token itself.
* o7 — passwords are hashed with argon2id via ``argon2-cffi``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from irc_lens.guest_store import GuestStore, email_hash


def _db_bytes(db_path: Path) -> bytes:
    """Every byte in the database file, for plaintext-leak inspection."""
    return db_path.read_bytes()


# ---------------------------------------------------------------------------
# Persistence / restart
# ---------------------------------------------------------------------------


def test_tables_created_on_init(tmp_path: Path) -> None:
    """Init creates the schema; reopening the file finds it intact."""
    db = tmp_path / "guests.db"
    GuestStore(db)
    con = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()
    assert {
        "guests",
        "consents",
        "tokens",
        "bans",
        "inputs",
        "deletions",
        "passwords",
    } <= tables


def test_state_survives_restart(tmp_path: Path) -> None:
    """Everything recorded before close is visible to a new GuestStore."""
    db = tmp_path / "guests.db"
    store = GuestStore(db)
    store.record_guest(email="g@example.com", nick="gg", ip="10.0.0.1")
    store.record_consent(
        "g@example.com", "10.0.0.1", tos_version="1.2", privacy_version="3.4"
    )
    token = store.issue_token("g@example.com", purpose="entry")
    store.ban(ip="10.0.0.9")
    store.set_password("approved@example.com", "hunter2-please-not-plaintext")
    del store

    again = GuestStore(db)
    guests = again.get_guest("g@example.com")
    assert guests == [("g@example.com", "gg", "10.0.0.1")]
    consents = again.get_consents("g@example.com")
    assert len(consents) == 1
    email, ip, tos, priv, ts = consents[0]
    assert (email, ip, tos, priv) == ("g@example.com", "10.0.0.1", "1.2", "3.4")
    assert ts > 0
    assert again.is_banned(None, "10.0.0.9")
    assert not again.is_banned("g@example.com", "10.0.0.1")
    assert again.check_password("approved@example.com", "hunter2-please-not-plaintext")
    # A token issued before the restart still verifies (and consumes).
    assert again.verify_token("g@example.com", token, purpose="entry")
    assert not again.verify_token("g@example.com", token, purpose="entry")


# ---------------------------------------------------------------------------
# Guests & consent
# ---------------------------------------------------------------------------


def test_record_and_get_guest(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    store.record_guest(email="a@example.com", nick="aa", ip="1.2.3.4")
    assert store.get_guest("a@example.com") == [("a@example.com", "aa", "1.2.3.4")]
    assert store.get_guest("missing@example.com") == []


def test_record_consent_stores_versions_and_timestamp(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    before = int(time.time())
    store.record_consent(
        "a@example.com", "1.2.3.4", tos_version="9", privacy_version="8"
    )
    after = int(time.time())
    rows = store.get_consents("a@example.com")
    assert len(rows) == 1
    email, ip, tos, priv, ts = rows[0]
    assert (email, ip, tos, priv) == ("a@example.com", "1.2.3.4", "9", "8")
    assert before <= ts <= after


# ---------------------------------------------------------------------------
# Tokens (o6): single-use, hashed at rest, TTL
# ---------------------------------------------------------------------------


def test_issue_token_returns_plaintext_once_and_stores_hash_only(
    tmp_path: Path,
) -> None:
    store = GuestStore(tmp_path / "guests.db")
    token = store.issue_token("a@example.com", purpose="entry")
    assert isinstance(token, str)
    assert len(token) >= 16
    digest = hashlib.sha256(token.encode()).hexdigest()
    con = sqlite3.connect(store.path)
    try:
        rows = list(con.execute("SELECT * FROM tokens"))
    finally:
        con.close()
    assert len(rows) == 1
    stored = [col for col in rows[0]]
    assert digest in stored, "token must be stored as its sha256 hexdigest"
    assert token not in stored, "plaintext token must never be stored"
    assert _db_bytes(store.path).find(token.encode()) == -1


def test_verify_token_is_single_use(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    token = store.issue_token("a@example.com", purpose="entry")
    assert store.verify_token("a@example.com", token, purpose="entry")
    assert not store.verify_token("a@example.com", token, purpose="entry")


def test_verify_token_rejects_wrong_purpose_or_email(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    entry = store.issue_token("a@example.com", purpose="entry")
    reset = store.issue_token("a@example.com", purpose="password-reset")
    assert not store.verify_token("a@example.com", entry, purpose="password-reset")
    assert not store.verify_token("b@example.com", entry, purpose="entry")
    # The wrong-purpose attempt must not have consumed the tokens.
    assert store.verify_token("a@example.com", entry, purpose="entry")
    assert store.verify_token("a@example.com", reset, purpose="password-reset")


def test_default_ttl_is_900_seconds(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    store.issue_token("a@example.com", purpose="entry")
    con = sqlite3.connect(store.path)
    try:
        ttl = con.execute("SELECT ttl FROM tokens LIMIT 1").fetchone()[0]
    finally:
        con.close()
    assert ttl == 900


def test_expired_token_is_rejected(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    token = store.issue_token("a@example.com", purpose="entry", ttl=0)
    assert not store.verify_token("a@example.com", token, purpose="entry")
    # Rejection must not leave the row consumable later either.
    assert not store.verify_token("a@example.com", token, purpose="entry")


def test_custom_ttl_honoured(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    token = store.issue_token("a@example.com", purpose="entry", ttl=60)
    assert store.verify_token("a@example.com", token, purpose="entry")


# ---------------------------------------------------------------------------
# Bans
# ---------------------------------------------------------------------------


def test_ban_by_email_and_ip(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    store.ban(email="bad@example.com")
    store.ban(ip="9.9.9.9")
    assert store.is_banned("bad@example.com", "1.1.1.1")
    assert store.is_banned("good@example.com", "9.9.9.9")
    assert not store.is_banned("good@example.com", "1.1.1.1")


def test_ban_with_no_selector_raises(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    with pytest.raises(ValueError):
        store.ban()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_delete_guest_inputs_removes_row_and_records_deletion(
    tmp_path: Path,
) -> None:
    store = GuestStore(tmp_path / "guests.db")
    store.record_guest(email="a@example.com", nick="aa", ip="1.2.3.4")
    store.record_input("a@example.com", kind="message", payload="hello there")
    removed = store.delete_guest_inputs("a@example.com")
    assert removed >= 2  # the guest row plus the input
    assert store.get_guest("a@example.com") == []
    con = sqlite3.connect(store.path)
    try:
        inputs = list(con.execute("SELECT * FROM inputs"))
        deletions = list(con.execute("SELECT * FROM deletions"))
    finally:
        con.close()
    assert inputs == []
    assert len(deletions) == 1
    assert deletions[0][0] == email_hash("a@example.com")  # d9: hash only
    # Nothing sensitive lingers in the file.
    assert b"hello there" not in _db_bytes(store.path)


# ---------------------------------------------------------------------------
# Passwords (o7 / criterion 2): argon2id only
# ---------------------------------------------------------------------------


def test_password_stored_as_argon2id_hash_not_plaintext(tmp_path: Path) -> None:
    db = tmp_path / "guests.db"
    store = GuestStore(db)
    secret = "correct horse battery staple"
    store.set_password("approved@example.com", secret)

    con = sqlite3.connect(db)
    try:
        rows = list(con.execute("SELECT * FROM passwords"))
    finally:
        con.close()
    assert len(rows) == 1
    blob = " ".join(str(col) for col in rows[0]).encode()
    assert rows[0][-1].startswith("$argon2id$"), "hash column must be argon2id"
    assert secret.encode() not in blob
    assert hashlib.sha256(secret.encode()).hexdigest().encode() not in blob
    # And nowhere else in the file either.
    whole = _db_bytes(db)
    assert secret.encode() not in whole
    assert hashlib.sha256(secret.encode()).hexdigest().encode() not in whole


def test_check_password_accepts_correct_rejects_wrong(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    store.set_password("approved@example.com", "s3cret!")
    assert store.check_password("approved@example.com", "s3cret!")
    assert not store.check_password("approved@example.com", "wrong")
    assert not store.check_password("unknown@example.com", "s3cret!")


def test_set_password_overwrites_previous_hash(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "guests.db")
    store.set_password("approved@example.com", "first")
    store.set_password("approved@example.com", "second")
    assert store.check_password("approved@example.com", "second")
    assert not store.check_password("approved@example.com", "first")
    con = sqlite3.connect(store.path)
    try:
        count = con.execute("SELECT COUNT(*) FROM passwords").fetchone()[0]
    finally:
        con.close()
    assert count == 1


# ---------------------------------------------------------------------------
# Injectable clock (o6), attempt counting, flags, listings
# ---------------------------------------------------------------------------


def test_token_expires_after_900s_with_injected_clock(tmp_path: Path) -> None:
    now = [1000.0]
    store = GuestStore(tmp_path / "g.db", clock=lambda: now[0])
    ok = store.issue_token("a@example.com", purpose="entry")
    late = store.issue_token("a@example.com", purpose="entry")
    now[0] += 899
    assert store.verify_token("a@example.com", ok, purpose="entry")
    now[0] += 1  # 900 s after issue
    assert not store.verify_token("a@example.com", late, purpose="entry")


def test_attempt_counting_and_rate_limit_window(tmp_path: Path) -> None:
    now = [1000.0]
    store = GuestStore(tmp_path / "g.db", clock=lambda: now[0])
    for _ in range(3):
        store.record_attempt("email", "a@example.com")
    store.record_attempt("ip", "1.2.3.4")
    assert store.count_attempts("email", "a@example.com", 60) == 3
    assert store.rate_limited("email", "a@example.com", limit=3, window=60)
    assert not store.rate_limited("ip", "1.2.3.4", limit=3, window=60)
    now[0] += 61
    assert store.count_attempts("email", "a@example.com", 60) == 0


def test_flags_and_listings(tmp_path: Path) -> None:
    store = GuestStore(tmp_path / "g.db")
    store.record_guest("a@example.com", "aa", "1.1.1.1")
    store.record_flag("a@example.com", kind="nsfw", detail="msg 4")
    store.ban(email="a@example.com", reason="nsfw")
    assert store.list_guests() == [("a@example.com", "aa", "1.1.1.1")]
    flags = store.list_flags("a@example.com")
    assert [f[:3] for f in flags] == [("a@example.com", "nsfw", "msg 4")]
    assert store.list_flags() == flags
    bans = store.list_bans()
    assert bans[0][:3] == ("a@example.com", None, "nsfw")


def test_password_hash_survives_restart_and_is_salted(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    store = GuestStore(db)
    store.set_password("a@example.com", "same")
    store.set_password("b@example.com", "same")
    con = sqlite3.connect(db)
    try:
        hashes = [r[0] for r in con.execute("SELECT hash FROM passwords")]
    finally:
        con.close()
    assert len(set(hashes)) == 2, "per-user salt: equal passwords differ"
    assert GuestStore(db).check_password("a@example.com", "same")


def test_room_id_is_random_stable_and_forgotten_on_deletion(tmp_path) -> None:
    """d7: rooms are #g-<random id> per guest, not derived from the nickname,
    so nickname reuse never inherits a deleted guest's history."""
    s = GuestStore(tmp_path / "g.db")
    a = s.room_id("a@example.org")
    assert a == s.room_id("a@example.org")
    assert a != s.room_id("b@example.org")
    assert len(a) >= 8
    assert a.isalnum()
    assert sorted(s.list_rooms()) == sorted(
        [("a@example.org", a), ("b@example.org", s.room_id("b@example.org"))]
    )
    s.delete_guest_inputs("a@example.org")
    assert [e for e, _ in s.list_rooms()] == ["b@example.org"]
    assert s.room_id("a@example.org") != a
