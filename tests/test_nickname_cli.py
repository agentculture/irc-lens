"""q14 (4): ``irc-lens users nick`` and ``users add --nick``.

The CLI sets or renames an approved user's nickname in the access store
with the same rules as the app (:meth:`AccessStore.set_nickname`). A
running lens notices on its next request or sweep and reconnects the user
under the new nick (``tests/test_nickname_rename.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from irc_lens.access_store import AccessStore
from test_access_cli import run

EMAIL = "pat@example.com"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "guests.db"
    AccessStore(path)
    return path


def test_cli_users_nick_sets_and_renames(db: Path, capsys) -> None:
    assert run(db, "users", "add", EMAIL) == 0
    assert run(db, "users", "nick", EMAIL, "Comet") == 0
    assert AccessStore(db).get_user(EMAIL)["nickname"] == "comet"
    assert run(db, "users", "nick", EMAIL, "nova") == 0
    out = capsys.readouterr().out
    assert "nova" in out
    rows = [r[2] for r in AccessStore(db).audit_rows() if r[2].startswith("nickname")]
    assert rows == ["nickname.set", "nickname.rename"]


def test_cli_users_add_with_nick(db: Path) -> None:
    assert run(db, "users", "add", EMAIL, "--nick", "comet") == 0
    assert AccessStore(db).get_user(EMAIL)["nickname"] == "comet"


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (("users", "nick", EMAIL, "x"), "invalid"),
        (("users", "nick", "nobody@example.org", "comet"), "no such user"),
        (("users", "nick", EMAIL, "ask"), "reserved"),
        (("users", "nick", EMAIL, "taken1"), "taken"),
    ],
)
def test_cli_users_nick_refusals(db: Path, capsys, argv, needle) -> None:
    assert run(db, "users", "add", "sam@example.org", "--nick", "taken1") == 0
    assert run(db, "users", "add", EMAIL) == 0
    capsys.readouterr()
    assert run(db, *argv) == 1
    assert needle in capsys.readouterr().err


def test_cli_add_with_a_bad_nick_adds_nobody(db: Path, capsys) -> None:
    assert run(db, "users", "add", EMAIL, "--nick", "x") == 1
    assert AccessStore(db).get_user(EMAIL) is None
