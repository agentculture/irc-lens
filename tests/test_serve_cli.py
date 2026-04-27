"""CLI-level tests for `irc-lens serve`.

Phase 4 only validates the argparse surface and the fail-fast contract
(no AgentIRC server needed). The end-to-end "boot, hit URLs, drive
flow" smoke is tested against the real AgentIRC fixture in Phase 9b.
"""

from __future__ import annotations

import pytest

from irc_lens.cli import main
from irc_lens.session import LensConnectionLost


def test_serve_requires_host_port_nick(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing required flags must error via the AfiError + hint contract,
    not an argparse traceback."""
    with pytest.raises(SystemExit) as exc:
        main(["serve"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_serve_help_lists_all_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """Every flag from the spec's CLI shape is registered."""
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in (
        "--host",
        "--port",
        "--nick",
        "--web-port",
        "--bind",
        "--icon",
        "--open",
        "--seed",
        "--log-json",
    ):
        assert flag in out, f"--help missing flag {flag!r}"


def test_serve_fails_fast_on_unreachable_agentirc(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentIRC unreachable → exit 1, error + hint on stderr, aiohttp
    never binds (we monkeypatch run_app to a tripwire that explodes
    if reached)."""

    async def boom_connect(self) -> None:
        raise LensConnectionLost("Cannot connect to IRC server at 127.0.0.1:1")

    monkeypatch.setattr("irc_lens.session.Session.connect", boom_connect)

    def tripwire(*_a, **_kw) -> None:  # pragma: no cover - must NOT run
        raise AssertionError("aiohttp.run_app must not bind on connect failure")

    monkeypatch.setattr("aiohttp.web.run_app", tripwire)

    rc = main(["serve", "--host", "127.0.0.1", "--port", "1", "--nick", "lens"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "AgentIRC" in err
    assert "Traceback" not in err


def test_serve_translates_port_in_use_to_exit_2(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web port in use → exit 2 (env error per the policy)."""

    async def ok_connect(self) -> None:
        return None

    monkeypatch.setattr("irc_lens.session.Session.connect", ok_connect)

    def boom_run_app(*_a, **_kw) -> None:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr("aiohttp.web.run_app", boom_run_app)

    rc = main(["serve", "--host", "x", "--port", "1", "--nick", "lens"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "web port" in err.lower()


def test_serve_warns_on_bind_zero(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--bind 0.0.0.0` prints the loud no-auth warning to stderr."""

    async def ok_connect(self) -> None:
        return None

    monkeypatch.setattr("irc_lens.session.Session.connect", ok_connect)

    runs: list[dict] = []

    def fake_run_app(app, **kw) -> None:
        runs.append(kw)

    monkeypatch.setattr("aiohttp.web.run_app", fake_run_app)

    rc = main(
        [
            "serve",
            "--host", "x", "--port", "1", "--nick", "lens",
            "--bind", "0.0.0.0",
            "--web-port", "65000",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "0.0.0.0" in err
    assert "no auth" in err.lower() or "no authentication" in err.lower()
    # And the bind value reached run_app.
    assert runs and runs[0]["host"] == "0.0.0.0"


def test_serve_seed_logs_deferred_diagnostic(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--seed <path>` is accepted now, loader lands in Phase 8."""

    async def ok_connect(self) -> None:
        return None

    monkeypatch.setattr("irc_lens.session.Session.connect", ok_connect)
    monkeypatch.setattr("aiohttp.web.run_app", lambda *a, **kw: None)

    rc = main(
        [
            "serve",
            "--host", "x", "--port", "1", "--nick", "lens",
            "--seed", "tests/fixtures/example.yaml",
            "--web-port", "65001",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "seed" in err.lower()
    assert "deferred" in err.lower()
