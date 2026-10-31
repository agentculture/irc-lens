"""CLI access verbs (t8): users, grants, agents, extend on a temp store."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from irc_lens.access_store import AccessStore
from irc_lens.cli import main

EMAIL = "pat@example.com"
OTHER = "sam@example.org"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "guests.db"
    AccessStore(path)
    return path


def run(db: Path, *argv: str) -> int:
    return main([argv[0], *argv[1:], "--store", str(db)])


def test_users_add_role_remove(db, capsys):
    assert run(db, "users", "add", EMAIL) == 0
    store = AccessStore(db)
    assert store.get_user(EMAIL) == {
        "email": EMAIL,
        "role": "member",
        "state": "approved",
    }
    assert run(db, "users", "role", EMAIL, "admin") == 0
    assert AccessStore(db).get_user(EMAIL)["role"] == "admin"
    assert run(db, "users", "add", OTHER, "--role", "admin") == 0
    assert AccessStore(db).get_user(OTHER)["role"] == "admin"
    assert run(db, "users", "remove", EMAIL) == 0
    user = AccessStore(db).get_user(EMAIL)
    assert user["state"] != "approved"


def test_users_role_rejects_unknown_role_and_user(db):
    assert run(db, "users", "add", EMAIL) == 0
    assert run(db, "users", "role", EMAIL, "root") != 0
    assert run(db, "users", "role", OTHER, "admin") != 0
    assert AccessStore(db).get_user(OTHER) is None


def test_users_remove_revokes_agent(db):
    store = AccessStore(db)
    run(db, "users", "add", EMAIL)
    token = store.issue_agent_credential(EMAIL, "bot", ttl=3600, actor=None)
    assert run(db, "users", "remove", EMAIL) == 0
    assert AccessStore(db).verify_agent_credential(token) is None


def test_grants_add_list_revoke_applies_live(db, capsys):
    from irc_lens import access

    run(db, "users", "add", EMAIL)
    live = AccessStore(db)  # an already-open store sees later CLI changes
    assert not access.can_read(EMAIL, "#room", store=live)
    assert run(db, "grants", "add", EMAIL, "#room", "--level", "write") == 0
    assert access.can_write(EMAIL, "#room", store=live)
    capsys.readouterr()
    assert run(db, "grants", "list", EMAIL) == 0
    out = capsys.readouterr().out
    assert "#room" in out
    assert "write" in out
    assert run(db, "grants", "revoke", EMAIL, "#room") == 0
    assert not access.can_read(EMAIL, "#room", store=live)


def test_grants_revoke_of_a_missing_grant_is_an_error(db, capsys):
    """Finding 2: ``grants revoke`` of a channel that was never granted (here
    a case variant) must not claim success; the real grant stays."""
    run(db, "users", "add", EMAIL)
    assert run(db, "grants", "add", EMAIL, "#Ops", "--level", "write") == 0
    capsys.readouterr()
    assert run(db, "grants", "revoke", EMAIL, "#ops") != 0
    captured = capsys.readouterr()
    assert "revoked" not in captured.out
    assert "no such grant" in captured.err
    assert AccessStore(db).grants_for(EMAIL) == [("channel", "#Ops", "write")]
    assert run(db, "grants", "revoke", EMAIL, "#Ops") == 0
    assert AccessStore(db).grants_for(EMAIL) == []


def test_grants_rejects_bad_level(db):
    assert run(db, "grants", "add", EMAIL, "#room", "--level", "god") != 0
    assert AccessStore(db).grants_for(EMAIL) == []


def test_agents_list_and_revoke(db, capsys):
    store = AccessStore(db)
    token = store.issue_agent_credential(EMAIL, "bot", ttl=3600, actor=None)
    capsys.readouterr()
    assert run(db, "agents", "list", EMAIL) == 0
    out = capsys.readouterr().out
    assert "bot" in out or "1" in out
    assert token not in out
    assert run(db, "agents", "revoke", EMAIL) == 0
    assert AccessStore(db).verify_agent_credential(token) is None
    assert AccessStore(db).active_credential_count(EMAIL) == 0


def test_agents_revoke_reaches_a_guests_agent(db, capsys):
    """Finding 8: a guest's credential is keyed ``guest:<email>``; ``agents
    revoke <email>`` must revoke it too, not only the approved key."""
    store = AccessStore(db)
    guest_token = store.issue_agent_credential(
        "guest:" + EMAIL, "sbx-pa_-pat", ttl=3600, actor=None
    )
    member_token = store.issue_agent_credential(
        EMAIL, "x-pa_-pat", ttl=3600, actor=None
    )
    capsys.readouterr()
    assert run(db, "agents", "revoke", EMAIL) == 0
    assert store.verify_agent_credential(guest_token) is None
    assert store.verify_agent_credential(member_token) is None
    out = capsys.readouterr().out
    assert "revoked 2 agent credential(s)" in out


def test_users_role_and_remove_keep_the_last_admin(db, capsys):
    """Finding 8: the CLI, like /admin, refuses to demote or remove the last
    approved admin."""
    assert run(db, "users", "add", EMAIL, "--role", "admin") == 0
    capsys.readouterr()
    assert run(db, "users", "role", EMAIL, "member") != 0
    assert "admin must remain" in capsys.readouterr().err
    assert run(db, "users", "remove", EMAIL) != 0
    assert AccessStore(db).get_user(EMAIL) == {
        "email": EMAIL,
        "role": "admin",
        "state": "approved",
    }
    # With a second admin, either change is allowed.
    assert run(db, "users", "add", OTHER, "--role", "admin") == 0
    assert run(db, "users", "role", EMAIL, "member") == 0
    assert run(db, "users", "remove", OTHER) != 0  # now OTHER is the last one
    assert run(db, "users", "add", OTHER, "--role", "member") != 0  # add demotes too


def test_extend_sets_until(db):
    before = int(time.time())
    assert run(db, "extend", EMAIL, "--days", "3") == 0
    until = AccessStore(db).extension_until(EMAIL)
    assert before + 3 * 86400 <= until <= int(time.time()) + 3 * 86400


def test_extend_audits_the_plain_system_marker(db):
    from irc_lens.access_store import SYSTEM_ACTOR

    assert run(db, "extend", EMAIL, "--days", "1") == 0
    rows = [r for r in AccessStore(db).audit_rows() if r[2] == "extension.set"]
    assert [r[1] for r in rows] == [SYSTEM_ACTOR]


def test_extend_clear(db):
    run(db, "extend", EMAIL, "--days", "1")
    assert run(db, "extend", EMAIL, "--clear") == 0
    assert AccessStore(db).extension_until(EMAIL) is None


def test_extend_needs_days_or_clear(db):
    assert run(db, "extend", EMAIL) != 0


def test_no_command_prints_an_email_it_was_not_given(db, capsys):
    store = AccessStore(db)
    store.set_user("hidden@example.net", role="admin", state="approved", actor=None)
    store.issue_agent_credential("hidden@example.net", "bot", ttl=60, actor=None)
    store.set_grant("hidden@example.net", "channel", "#x", "read", actor=None)
    capsys.readouterr()
    cmds = [
        ("users", "add", EMAIL),
        ("users", "role", EMAIL, "admin"),
        ("users", "remove", EMAIL),
        ("grants", "add", EMAIL, "#room"),
        ("grants", "list", EMAIL),
        ("grants", "revoke", EMAIL, "#room"),
        ("agents", "list", EMAIL),
        ("agents", "list"),
        ("agents", "revoke", EMAIL),
        ("extend", EMAIL, "--days", "2"),
    ]
    for argv in cmds:
        run(db, *argv)
    captured = capsys.readouterr()
    assert "hidden@example.net" not in captured.out + captured.err
    assert "example.net" not in captured.out + captured.err


def test_missing_store_is_an_error(tmp_path):
    assert main(["users", "add", EMAIL, "--store", str(tmp_path / "nope.db")]) != 0
