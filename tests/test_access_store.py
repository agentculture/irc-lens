"""Tests for `irc_lens.access_store` (email-keyed permissions, task t1)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from irc_lens.access_store import AccessStore
from irc_lens.guest_store import GuestStore, email_hash

ALICE = "alice@example.com"
BOB = "bob@example.org"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "lens.db"


@pytest.fixture
def store(db: Path) -> AccessStore:
    return AccessStore(db, clock=Clock())


def _tables(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()


def _dump(path: Path, tables: list[str]) -> dict[str, list[tuple]]:
    con = sqlite3.connect(path)
    try:
        return {
            t: con.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
            for t in tables
        }
    finally:
        con.close()


# -- schema / migration ----------------------------------------------------
def test_tables_created(store: AccessStore, db: Path) -> None:
    assert {
        "users",
        "grants",
        "agent_credentials",
        "agent_extensions",
        "audit",
    } <= _tables(db)


def test_preexisting_guest_db_unchanged(db: Path) -> None:
    gs = GuestStore(db)
    gs.record_guest(ALICE, "guest-1", "203.0.113.5")
    gs.room_id(ALICE)
    old_tables = sorted(_tables(db))
    before = _dump(db, old_tables)
    AccessStore(db)
    AccessStore(db)  # idempotent
    assert _dump(db, old_tables) == before
    assert gs.get_guest(ALICE) == [(ALICE, "guest-1", "203.0.113.5")]


def test_guest_store_still_opens_after_access_migration(db: Path) -> None:
    AccessStore(db)
    GuestStore(db)


# -- users / roles ----------------------------------------------------------
def test_user_role_state(store: AccessStore) -> None:
    store.set_user(ALICE, role="member", state="approved", actor=BOB)
    assert store.get_user(ALICE) == {
        "email": ALICE,
        "role": "member",
        "state": "approved",
    }
    store.set_user("ALICE@Example.com", role="admin", state="approved", actor=BOB)
    assert store.get_user(ALICE)["role"] == "admin"
    assert store.get_user(BOB) is None


# -- generic grants ---------------------------------------------------------
def test_grants_generic_kind(store: AccessStore) -> None:
    store.set_grant(ALICE, "channel", "#room", "write", actor=BOB)
    store.set_grant(ALICE, "library_tag", "physics", "read", actor=BOB)
    assert store.grants_for(ALICE) == [
        ("channel", "#room", "write"),
        ("library_tag", "physics", "read"),
    ]
    store.set_grant(ALICE, "channel", "#room", "read", actor=BOB)  # update in place
    assert store.grants_for(ALICE, resource_kind="channel") == [
        ("channel", "#room", "read")
    ]
    store.remove_grant(ALICE, "channel", "#room", actor=BOB)
    assert store.grants_for(ALICE, resource_kind="channel") == []


def test_remove_grant_reports_whether_a_row_went(store: AccessStore) -> None:
    """Finding 2: revoking a grant that does not exist (for example a case
    variant of the stored channel) removes nothing and says so; one row per
    (email, kind, resource) however often it is set."""
    store.set_grant(ALICE, "channel", "#Ops", "write", actor=BOB)
    store.set_grant(ALICE.upper(), "channel", "#Ops", "read", actor=BOB)
    assert store.grants_for(ALICE) == [("channel", "#Ops", "read")]
    before = len(store.audit_rows())
    assert store.remove_grant(ALICE, "channel", "#ops", actor=BOB) == 0
    assert store.grants_for(ALICE) == [("channel", "#Ops", "read")]
    assert len(store.audit_rows()) == before  # nothing changed, nothing audited
    assert store.remove_grant(ALICE, "channel", "#Ops", actor=BOB) == 1
    assert store.grants_for(ALICE) == []


# -- agent credentials (o1) -------------------------------------------------
def test_issue_credential_revokes_prior(store: AccessStore, db: Path) -> None:
    t1 = store.issue_agent_credential(ALICE, "alice-agent", ttl=3600, actor=BOB)
    t2 = store.issue_agent_credential(ALICE, "alice-agent", ttl=3600, actor=BOB)
    assert t1 != t2
    assert store.verify_agent_credential(t1) is None
    assert store.verify_agent_credential(t2) == (ALICE, "alice-agent")
    assert store.active_credential_count(ALICE) == 1
    raw = db.read_bytes()
    assert t1.encode() not in raw and t2.encode() not in raw


def test_credential_expiry_and_revoke(db: Path) -> None:
    clock = Clock()
    s = AccessStore(db, clock=clock)
    tok = s.issue_agent_credential(ALICE, "n", ttl=100, actor=BOB)
    clock.now += 99
    assert s.verify_agent_credential(tok) == (ALICE, "n")
    clock.now += 2
    assert s.verify_agent_credential(tok) is None
    tok2 = s.issue_agent_credential(ALICE, "n", ttl=100, actor=BOB)
    s.revoke_agent_credential(ALICE, actor=BOB)
    assert s.verify_agent_credential(tok2) is None
    assert s.active_credential_count(ALICE) == 0


def test_concurrent_issue_leaves_one_active(db: Path) -> None:
    s = AccessStore(db)
    errors: list[BaseException] = []

    def go() -> None:
        try:
            s.issue_agent_credential(ALICE, "n", ttl=3600, actor=BOB)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert s.active_credential_count(ALICE) == 1


def test_credentials_independent_per_human(store: AccessStore) -> None:
    store.issue_agent_credential(ALICE, "a", ttl=60, actor=BOB)
    store.issue_agent_credential(BOB, "b", ttl=60, actor=BOB)
    assert store.active_credential_count(ALICE) == 1
    assert store.active_credential_count(BOB) == 1


# -- extensions -------------------------------------------------------------
def test_extension(db: Path) -> None:
    clock = Clock()
    s = AccessStore(db, clock=clock)
    assert s.extension_until(ALICE) is None
    s.set_extension(ALICE, until=5000, set_by=BOB)
    assert s.extension_until(ALICE) == 5000
    s.set_extension(ALICE, until=6000, set_by=BOB)
    assert s.extension_until(ALICE) == 6000
    s.clear_extension(ALICE, actor=BOB)
    assert s.extension_until(ALICE) is None


# -- one-time admin import (o21) --------------------------------------------
def test_import_admins_once(store: AccessStore) -> None:
    assert store.import_admins([ALICE, "Bob@Example.org"]) == 2
    for e in (ALICE, BOB):
        assert store.get_user(e) == {"email": e, "role": "admin", "state": "approved"}
    assert store.import_admins([ALICE, BOB]) == 0
    # a later demotion survives a re-run
    store.set_user(ALICE, role="member", state="approved", actor=BOB)
    assert store.import_admins([ALICE, BOB]) == 0
    assert store.get_user(ALICE)["role"] == "member"
    # a new address is still imported
    assert store.import_admins([ALICE, "carol@example.com"]) == 1


def test_import_survives_restart(db: Path) -> None:
    AccessStore(db).import_admins([ALICE])
    assert AccessStore(db).import_admins([ALICE]) == 0


# -- audit ------------------------------------------------------------------
def test_mutations_audited_by_hash_only(store: AccessStore, db: Path) -> None:
    store.set_user(ALICE, role="member", state="approved", actor=BOB)
    store.set_grant(ALICE, "channel", "#room", "write", actor=BOB)
    store.remove_grant(ALICE, "channel", "#room", actor=BOB)
    store.issue_agent_credential(ALICE, "n", ttl=60, actor=BOB)
    store.revoke_agent_credential(ALICE, actor=BOB)
    store.set_extension(ALICE, until=9, set_by=BOB)
    store.import_admins(["carol@example.com"])
    rows = store.audit_rows()
    actions = [r[2] for r in rows]
    for a in (
        "user.set",
        "grant.set",
        "grant.remove",
        "credential.issue",
        "credential.revoke",
        "extension.set",
        "admin.import",
    ):
        assert a in actions
    assert all(r[1] == email_hash(BOB) or r[1] == "system" for r in rows)
    assert any(email_hash(ALICE) in r[3] for r in rows)
    con = sqlite3.connect(db)
    try:
        text = " ".join(str(c) for r in con.execute("SELECT * FROM audit") for c in r)
    finally:
        con.close()
    for e in (ALICE, BOB, "carol@example.com"):
        assert e not in text
