"""q14 (1): approved users' chosen nicknames in the access store.

A nickname is stored per approved user, unique across users (case-
insensitive), follows the guest nickname character rules
(:func:`irc_lens.nickname.sanitize_nickname`, shared with the guest entry
form) and never enters the personal-agent namespace or a reserved name.
Nothing is ever derived from the email address.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from irc_lens import nickname
from irc_lens.access_store import REFUSAL_CHOSEN, AccessStore, NicknameError
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


# -- review of PR #86 -----------------------------------------------------------

CAROL = "carol@example.net"


def test_finding1_renamed_away_nickname_stays_reserved(store: AccessStore) -> None:
    store.set_user(CAROL, role="member", state="approved", actor=None)
    store.set_nickname(ALICE, "alice", actor=ALICE)
    store.set_nickname(ALICE, "bob", actor=ADMIN)
    assert _reason(store, CAROL, "alice") == "taken"
    assert store.get_user(CAROL)["nickname"] is None


def test_finding1_the_same_user_may_take_an_old_nickname_back(
    store: AccessStore,
) -> None:
    store.set_nickname(ALICE, "alice", actor=ALICE)
    store.set_nickname(ALICE, "bob", actor=ADMIN)
    assert store.set_nickname(ALICE, "Alice", actor=ADMIN) == "alice"


def test_finding1_rename_revokes_the_agent_credential(store: AccessStore) -> None:
    store.set_nickname(ALICE, "alice", actor=ALICE)
    token = store.issue_agent_credential(ALICE, "srv-pa_-alice", ttl=3600, actor=ALICE)
    store.set_nickname(ALICE, "bob", actor=ADMIN)
    assert store.verify_agent_credential(token) is None


def test_finding1_first_choice_keeps_the_agent_credential(
    store: AccessStore,
) -> None:
    token = store.issue_agent_credential(ALICE, "srv-pa_-x", ttl=3600, actor=ALICE)
    store.set_nickname(ALICE, "alice", actor=ALICE)
    assert store.verify_agent_credential(token) is not None


def test_finding1_reservation_survives_a_store_reopen(store: AccessStore) -> None:
    store.set_user(CAROL, role="member", state="approved", actor=None)
    store.set_nickname(ALICE, "alice", actor=ALICE)
    store.set_nickname(ALICE, "bob", actor=ADMIN)
    reopened = AccessStore(store.path)
    with pytest.raises(NicknameError) as info:
        reopened.set_nickname(CAROL, "alice", actor=ADMIN)
    assert info.value.reason == "taken"


def test_finding1_migration_reserves_nicknames_already_held(tmp_path: Path) -> None:
    db = tmp_path / "pre.db"
    with closing(sqlite3.connect(db)) as con, con:
        con.execute(
            "CREATE TABLE users (email TEXT PRIMARY KEY, role TEXT NOT NULL, "
            "state TEXT NOT NULL, nickname TEXT)"
        )
        con.execute(
            "INSERT INTO users VALUES (?, 'member', 'approved', 'comet')", (ALICE,)
        )
        con.execute("INSERT INTO users VALUES (?, 'member', 'approved', NULL)", (BOB,))
    store = AccessStore(db)
    store.set_nickname(ALICE, "nova", actor=ADMIN)
    assert _reason(store, BOB, "comet") == "taken"


def test_finding2_concurrent_claims_store_exactly_one(store: AccessStore) -> None:
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def claim(raw: str) -> None:
        barrier.wait()
        try:
            outcomes[raw] = store.claim_nickname(ALICE, raw, actor=ALICE)
        except NicknameError as exc:
            outcomes[raw] = exc.reason

    threads = [threading.Thread(target=claim, args=(n,)) for n in ("comet", "nova")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes.values()).count(REFUSAL_CHOSEN) == 1
    winner = next(n for n, why in outcomes.items() if why != REFUSAL_CHOSEN)
    assert store.get_user(ALICE)["nickname"] == winner
    actions = [r[2] for r in store.audit_rows() if r[2].startswith("nickname.")]
    assert actions == ["nickname.set"]


def test_finding2_claim_never_renames(store: AccessStore) -> None:
    store.set_nickname(ALICE, "comet", actor=ADMIN)
    with pytest.raises(NicknameError) as info:
        store.claim_nickname(ALICE, "nova", actor=ALICE)
    assert info.value.reason == REFUSAL_CHOSEN
    assert store.get_user(ALICE)["nickname"] == "comet"


def test_finding2_claim_applies_the_same_rules(store: AccessStore) -> None:
    store.set_nickname(BOB, "comet", actor=ADMIN)
    with pytest.raises(NicknameError) as info:
        store.claim_nickname(ALICE, "Comet", actor=ALICE)
    assert info.value.reason == "taken"
    assert store.claim_nickname(ALICE, "Nova", actor=ALICE) == "nova"


def test_finding5_add_with_a_taken_nickname_adds_nobody(store: AccessStore) -> None:
    store.set_nickname(ALICE, "comet", actor=ADMIN)
    with pytest.raises(NicknameError) as info:
        store.set_user_keeping_admin(
            CAROL, role="member", state="approved", actor=None, nickname="comet"
        )
    assert info.value.reason == "taken"
    assert store.get_user(CAROL) is None


def test_finding5_add_with_a_nickname_is_one_write(store: AccessStore) -> None:
    store.set_user_keeping_admin(
        CAROL, role="admin", state="approved", actor=None, nickname="Nova"
    )
    assert store.get_user(CAROL) == {
        "email": CAROL,
        "role": "admin",
        "state": "approved",
        "nickname": "nova",
    }


def test_finding5_add_with_a_reserved_nickname_adds_nobody(
    store: AccessStore,
) -> None:
    with pytest.raises(NicknameError) as info:
        store.set_user_keeping_admin(
            CAROL, role="member", state="approved", actor=None, nickname="pa_x"
        )
    assert info.value.reason == "reserved"
    assert store.get_user(CAROL) is None


class _Interleave:
    """A connection proxy: right after its first ``PRAGMA table_info``, a
    second store migrates the same file (another process starting up)."""

    def __init__(self, con: sqlite3.Connection, other) -> None:
        self._con = con
        self._other = other

    def execute(self, sql: str, *args):
        result = self._con.execute(sql, *args)
        if sql.startswith("PRAGMA table_info") and self._other is not None:
            rows = list(result)
            other, self._other = self._other, None
            other()
            return iter(rows)
        return result

    def __getattr__(self, name: str):
        return getattr(self._con, name)


def test_finding7_two_stores_migrate_one_old_file_concurrently(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db)) as con, con:
        con.execute(
            "CREATE TABLE users (email TEXT PRIMARY KEY, role TEXT NOT NULL, "
            "state TEXT NOT NULL)"
        )
    real_connect = AccessStore._connect
    errors: list[BaseException] = []
    second: list[threading.Thread] = []

    def other_process() -> None:
        def run() -> None:
            try:
                AccessStore(db)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        second.append(thread)
        thread.join(timeout=1.0)  # finishes now unless the first holds a lock

    hooks = [other_process]

    def connect(self, timeout: float = 30.0):
        con = real_connect(self, timeout)
        return _Interleave(con, hooks.pop() if hooks else None)

    monkeypatch.setattr(AccessStore, "_connect", connect)
    AccessStore(db)
    for thread in second:
        thread.join()
    assert errors == []
    with closing(sqlite3.connect(db)) as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    assert "nickname" in cols
