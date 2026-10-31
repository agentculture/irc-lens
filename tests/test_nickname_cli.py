"""q14 (4): ``irc-lens users nick`` and ``users add --nick``.

The CLI sets or renames an approved user's nickname in the access store
with the same rules as the app (:meth:`AccessStore.set_nickname`). A
running lens notices on its next request or sweep and reconnects the user
under the new nick (``tests/test_nickname_rename.py``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from irc_lens.access_store import AccessStore
from test_access_cli import run

EMAIL = "pat@example.com"


@pytest.fixture(autouse=True)
def _no_real_config(tmp_path_factory, monkeypatch) -> None:
    """Setting a nickname reads the config (residents list): never the real one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))


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


# -- review of PR #86 ------------------------------------------------------------


def test_finding5_add_with_a_reserved_nickname_adds_nobody(db: Path, capsys) -> None:
    assert run(db, "users", "add", EMAIL, "--nick", "alice") == 0
    assert run(db, "users", "nick", EMAIL, "bob") == 0
    capsys.readouterr()
    assert run(db, "users", "add", "carol@example.net", "--nick", "alice") == 1
    assert "taken" in capsys.readouterr().err
    assert AccessStore(db).get_user("carol@example.net") is None


def _residents(monkeypatch, *nicks: str) -> None:
    from irc_lens.web import nickname as nickname_web
    from irc_lens.web.residents import ResidentsResult

    cfg = SimpleNamespace(
        server_name="testsrv",
        culture_residents_url="http://residents.invalid/residents.json",
        culture_overview_name=None,
    )
    monkeypatch.setattr("irc_lens.config.resolve_config", lambda _path: cfg)

    async def fetch(_url: str) -> ResidentsResult:
        payload = {"supported": True, "residents": [{"nick": n} for n in nicks]}
        return ResidentsResult("supported", payload)

    monkeypatch.setattr(nickname_web, "fetch_residents", fetch)


@pytest.mark.parametrize(
    "argv",
    [
        ("users", "nick", EMAIL, "Culture"),
        ("users", "add", "new@example.org", "--nick", "culture"),
    ],
)
def test_finding3_cli_refuses_a_resident_agent_nick(
    db: Path, capsys, monkeypatch, argv
) -> None:
    assert run(db, "users", "add", EMAIL) == 0
    _residents(monkeypatch, "testsrv-culture")
    capsys.readouterr()
    assert run(db, *argv) == 1
    assert "taken" in capsys.readouterr().err
    assert AccessStore(db).get_user(EMAIL)["nickname"] is None
    assert AccessStore(db).get_user("new@example.org") is None


def test_finding3_cli_allows_a_nick_no_resident_holds(db: Path, monkeypatch) -> None:
    assert run(db, "users", "add", EMAIL) == 0
    _residents(monkeypatch, "testsrv-culture")
    assert run(db, "users", "nick", EMAIL, "comet") == 0
    assert AccessStore(db).get_user(EMAIL)["nickname"] == "comet"


def test_finding3_without_a_config_the_cli_says_residents_were_not_checked(
    db: Path, capsys
) -> None:
    assert run(db, "users", "add", EMAIL, "--nick", "comet") == 0
    assert "resident" in capsys.readouterr().err


async def test_finding3_resident_check_runs_inside_a_running_loop(  # NOSONAR S7503 - needs a running loop, awaits nothing
    db: Path, monkeypatch
) -> None:
    """A verb dispatched from an async surface (MCP) still checks residents."""
    assert run(db, "users", "add", EMAIL) == 0
    _residents(monkeypatch, "testsrv-culture")
    assert run(db, "users", "nick", EMAIL, "culture") == 1
    assert AccessStore(db).get_user(EMAIL)["nickname"] is None
