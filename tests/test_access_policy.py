"""Tests for `irc_lens.access` (default-deny policy, task t6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from irc_lens import access
from irc_lens.access_store import AccessStore
from irc_lens.guest_store import email_hash

ADMIN = "root@example.com"
USER = "alice@example.com"
PENDING = "pending@example.org"
STRANGER = "nobody@example.org"
CHAN = "#room"
OTHER = "#other"

ACTIONS = {
    "list": access.can_list,
    "read": access.can_read,
    "write": access.can_write,
}
# (role, grant level) -> expected {list, read, write}
MATRIX = [
    ("admin", None, (True, True, True)),
    ("admin", "read", (True, True, True)),
    ("member", None, (False, False, False)),
    ("member", "read", (True, True, False)),
    ("member", "write", (True, True, True)),
    ("member", "ask", (True, True, True)),
    ("member", "bogus", (False, False, False)),
]


@pytest.fixture
def store(tmp_path: Path) -> AccessStore:
    s = AccessStore(tmp_path / "lens.db", clock=lambda: 1000.0)
    s.set_user(ADMIN, role="admin", state="approved", actor=None)
    s.set_user(USER, role="member", state="approved", actor=None)
    s.set_user(PENDING, role="member", state="pending", actor=None)
    return s


@pytest.mark.parametrize("role,level,expected", MATRIX)
def test_role_level_matrix(store, role, level, expected):
    human = ADMIN if role == "admin" else USER
    if level:
        store.set_grant(human, "channel", CHAN, level, actor=None)
    got = tuple(fn(human, CHAN, store=store) for fn in ACTIONS.values())
    assert got == expected


def test_grant_is_per_channel(store):
    store.set_grant(USER, "channel", CHAN, "write", actor=None)
    assert access.can_write(USER, OTHER, store=store) is False
    assert access.can_list(USER, OTHER, store=store) is False


def test_channel_exact_email_case_insensitive(store):
    """AgentIRC keys channels by their exact name: ``#Room`` and ``#ROOM``
    are different rooms, so a grant on one never opens the other. The
    email half of the key stays case-insensitive (it is normalised)."""
    store.set_grant(USER, "channel", "#Room", "read", actor=None)
    assert access.can_read(USER.upper(), "#Room", store=store) is True
    for other in ("#ROOM", "#room", "#rOOM"):
        for fn in ACTIONS.values():
            assert fn(USER, other, store=store) is False


def test_non_channel_grant_kind_ignored(store):
    store.set_grant(USER, "library_tag", CHAN, "write", actor=None)
    assert access.can_read(USER, CHAN, store=store) is False


@pytest.mark.parametrize("who", [STRANGER, PENDING, "", None])
def test_unknown_or_unapproved_denied(store, who):
    store.set_grant(PENDING, "channel", CHAN, "write", actor=None)
    for fn in ACTIONS.values():
        assert fn(who, CHAN, store=store) is False


def test_unapproved_admin_denied(store):
    store.set_user(ADMIN, role="admin", state="suspended", actor=None)
    assert access.can_read(ADMIN, CHAN, store=store) is False


class Broken:
    def get_user(self, email):
        raise RuntimeError("db unreadable")

    grants_for = get_user


def test_unreadable_store_fails_closed():
    for fn in ACTIONS.values():
        assert fn(ADMIN, CHAN, store=Broken()) is False


def test_unreadable_grants_fail_closed(store, monkeypatch):
    store.set_grant(USER, "channel", CHAN, "write", actor=None)

    def boom(*a, **k):
        raise OSError("gone")

    monkeypatch.setattr(store, "grants_for", boom)
    assert access.can_write(USER, CHAN, store=store) is False


def test_audit_failure_still_denies(store, monkeypatch):
    def boom(*a, **k):
        raise OSError("audit down")

    monkeypatch.setattr(store, "_tx", boom)
    assert access.can_read(USER, CHAN, store=store) is False


def test_missing_database_file_fails_closed(tmp_path):
    s = AccessStore(tmp_path / "lens.db")
    s.set_user(ADMIN, role="admin", state="approved", actor=None)
    s.path.unlink()
    assert access.can_read(ADMIN, CHAN, store=s) is False


def test_revocation_refuses_agent_next_check(store):
    store.set_grant(USER, "channel", CHAN, "write", actor=None)
    token = store.issue_agent_credential(USER, "alice-agent", ttl=60, actor=None)
    assert access.agent_can_write(token, CHAN, store=store) is True
    store.remove_grant(USER, "channel", CHAN, actor=None)
    assert access.agent_can_write(token, CHAN, store=store) is False
    assert access.agent_can_read(token, CHAN, store=store) is False


def test_agent_follows_downgrade_and_bad_token(store):
    store.set_grant(USER, "channel", CHAN, "write", actor=None)
    token = store.issue_agent_credential(USER, "alice-agent", ttl=60, actor=None)
    store.set_grant(USER, "channel", CHAN, "read", actor=None)
    assert access.agent_can_read(token, CHAN, store=store) is True
    assert access.agent_can_write(token, CHAN, store=store) is False
    assert access.agent_can_list("not-a-token", CHAN, store=store) is False
    store.revoke_agent_credential(USER, actor=None)
    assert access.agent_can_read(token, CHAN, store=store) is False


def test_refusal_appends_audit_without_plain_email(store):
    before = len(store.audit_rows())
    assert access.can_write(USER, CHAN, store=store) is False
    rows = store.audit_rows()[before:]
    assert len(rows) == 1
    ts, actor_hash, action, target = rows[0]
    assert actor_hash == email_hash(USER)
    assert action == "access.deny"
    assert "write" in target
    assert CHAN in target
    assert USER not in " ".join(map(str, rows[0]))


def test_allow_appends_no_audit(store):
    store.set_grant(USER, "channel", CHAN, "read", actor=None)
    before = len(store.audit_rows())
    assert access.can_read(USER, CHAN, store=store) is True
    assert len(store.audit_rows()) == before


def test_refusal_of_unknown_human_audited_by_hash(store):
    before = len(store.audit_rows())
    access.can_read(STRANGER, CHAN, store=store)
    rows = store.audit_rows()[before:]
    assert rows
    assert rows[0][1] == email_hash(STRANGER)
    assert STRANGER not in " ".join(map(str, rows[0]))
