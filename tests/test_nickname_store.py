"""q14 (1): approved users' chosen nicknames in the access store.

A nickname is stored per approved user, unique across users (case-
insensitive), follows the guest nickname character rules
(:func:`irc_lens.nickname.sanitize_nickname`, shared with the guest entry
form) and never enters the personal-agent namespace or a reserved name.
Nothing is ever derived from the email address.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from irc_lens import nickname
from irc_lens.access_store import AccessStore, NicknameError
from irc_lens.guest_store import email_hash
from irc_lens.web import entry

ALICE = "alice@example.com"
BOB = "bob@example.org"
ADMIN = "root@example.net"


@pytest.fixture
def store(tmp_path: Path) -> AccessStore:
    s = AccessStore(tmp_path / "lens.db")
    for email in (ALICE, BOB):
        s.set_user(email, role="member", state="approved", actor=None)
    return s


def _reason(store: AccessStore, email: str, raw: str) -> str:
    with pytest.raises(NicknameError) as info:
        store.set_nickname(email, raw, actor=ADMIN)
    return info.value.reason


# -- shared character rules ---------------------------------------------------


def test_guest_form_reuses_the_shared_sanitizer() -> None:
    assert entry.sanitize_nickname is nickname.sanitize_nickname
    assert entry.RESERVED_NICKNAMES is nickname.RESERVED_NICKNAMES


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("x", "invalid"),
        ("a" * 17, "invalid"),
        ("ann@example.org", "invalid"),  # an address is never a nickname
        ("ask", "reserved"),
        ("pa_", "reserved"),
        ("pa_bot", "reserved"),
    ],
)
def test_refused_nicknames(store: AccessStore, raw: str, why: str) -> None:
    assert nickname.approved_refusal(raw) == why
    assert _reason(store, ALICE, raw) == why


def test_valid_nickname_is_sanitized_like_a_guest_one() -> None:
    assert nickname.approved_refusal("Sky_Walker") is None
    assert nickname.normalize_approved("Sky_Walker") == "sky_walker"


# -- storage ------------------------------------------------------------------


def test_new_user_has_no_nickname(store: AccessStore) -> None:
    assert store.get_user(ALICE)["nickname"] is None


def test_set_and_read_nickname(store: AccessStore) -> None:
    assert store.set_nickname(ALICE, "Comet", actor=ALICE) == "comet"
    assert store.get_user(ALICE)["nickname"] == "comet"
    listed = {u["email"]: u["nickname"] for u in store.list_users()}
    assert listed == {ALICE: "comet", BOB: None}


def test_role_change_keeps_the_nickname(store: AccessStore) -> None:
    store.set_nickname(ALICE, "comet", actor=ALICE)
    store.set_user(ALICE, role="admin", state="approved", actor=ADMIN)
    assert store.get_user(ALICE)["nickname"] == "comet"


def test_nickname_unique_case_insensitive(store: AccessStore) -> None:
    store.set_nickname(ALICE, "comet", actor=ALICE)
    assert _reason(store, BOB, "COMET") == "taken"
    assert store.get_user(BOB)["nickname"] is None


def test_unique_index_backs_the_check(store: AccessStore) -> None:
    store.set_nickname(ALICE, "comet", actor=ALICE)
    with closing(sqlite3.connect(store.path)) as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE users SET nickname='Comet' WHERE email=?", (BOB,))


def test_own_nickname_may_be_set_again(store: AccessStore) -> None:
    store.set_nickname(ALICE, "comet", actor=ALICE)
    assert store.set_nickname(ALICE, "Comet", actor=ALICE) == "comet"


def test_unknown_user_cannot_get_a_nickname(store: AccessStore) -> None:
    assert _reason(store, "nobody@example.com", "comet") == "no-user"


def test_migration_adds_the_column_to_an_old_store(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db)) as con, con:
        con.execute(
            "CREATE TABLE users (email TEXT PRIMARY KEY, role TEXT NOT NULL, "
            "state TEXT NOT NULL)"
        )
        con.execute("INSERT INTO users VALUES (?, 'admin', 'approved')", (ADMIN,))
    store = AccessStore(db)
    AccessStore(db)  # idempotent
    assert store.get_user(ADMIN) == {
        "email": ADMIN,
        "role": "admin",
        "state": "approved",
        "nickname": None,
    }


def test_nickname_never_derived_from_the_email(store: AccessStore) -> None:
    store.import_admins([ADMIN])
    assert store.get_user(ADMIN)["nickname"] is None


def test_nickname_change_is_audited_without_the_email(store: AccessStore) -> None:
    store.set_nickname(ALICE, "comet", actor=ALICE)
    store.set_nickname(ALICE, "nova", actor=ADMIN)
    rows = [r for r in store.audit_rows() if r[2].startswith("nickname.")]
    assert [(r[1], r[2], r[3]) for r in rows] == [
        (email_hash(ALICE), "nickname.set", "comet"),
        (email_hash(ADMIN), "nickname.rename", "nova"),
    ]
    text = " ".join(str(c) for r in store.audit_rows() for c in r)
    assert ALICE not in text
    assert "alice" not in " ".join(r[3] for r in rows)


def test_setting_the_same_nickname_is_not_audited_again(store: AccessStore) -> None:
    store.set_nickname(ALICE, "comet", actor=ALICE)
    store.set_nickname(ALICE, "comet", actor=ALICE)
    assert [r[2] for r in store.audit_rows()].count("nickname.set") == 1
