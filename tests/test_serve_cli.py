"""CLI-level tests for `irc-lens serve`.

Phase 4 only validates the argparse surface and the fail-fast contract
(no AgentIRC server needed). The end-to-end "boot, hit URLs, drive
flow" smoke is tested against the real AgentIRC fixture in Phase 9b.

`cmd_serve` runs everything inside one `asyncio.run(_serve_async(...))`
(so the IRC connection's read task survives until the web server
shuts down). To avoid actually binding ports or blocking on
``asyncio.Event().wait()`` here, the `stub_aiohttp_runtime` fixture
replaces `aiohttp.web.AppRunner`, `aiohttp.web.TCPSite`, and
`asyncio.Event` with no-op shims.
"""

from __future__ import annotations

import asyncio

import pytest

from irc_lens.cli import main
from irc_lens.session import LensConnectionLost


# ---------------------------------------------------------------------------
# Test doubles for the aiohttp runtime (no actual bind)
# ---------------------------------------------------------------------------


class _FakeRunner:
    def __init__(self, app, *_a, **_kw) -> None:
        self.app = app
        self.setup_called = False
        self.cleanup_called = False

    async def setup(self) -> None:
        self.setup_called = True

    async def cleanup(self) -> None:
        self.cleanup_called = True


class _FakeSite:
    def __init__(self, runner, host: str, port: int) -> None:
        self.runner = runner
        self.host = host
        self.port = port

    async def start(self) -> None:
        return None


class _BoomSite(_FakeSite):
    """TCPSite whose start() raises OSError (port-in-use simulation)."""

    async def start(self) -> None:
        raise OSError(98, "Address already in use")


class _ImmediateEvent:
    """`asyncio.Event` replacement whose `wait()` returns immediately.

    Python's default constructor is sufficient — no instance state needed.
    """

    async def wait(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_aiohttp_runtime(monkeypatch: pytest.MonkeyPatch):
    """Replace aiohttp's runner / site / wait-forever so cmd_serve exits."""
    monkeypatch.setattr("aiohttp.web.AppRunner", _FakeRunner)
    monkeypatch.setattr("aiohttp.web.TCPSite", _FakeSite)
    monkeypatch.setattr("asyncio.Event", _ImmediateEvent)


@pytest.fixture
def successful_connect(monkeypatch: pytest.MonkeyPatch):
    async def ok(self) -> None:
        return None

    async def welcomed(self) -> None:
        # Mocked alongside connect() — without it, the serve command's
        # `wait_for_welcome` would block on the real welcome Event that
        # nothing sets (no actual transport here), making every test
        # that monkeypatches connect() time out. Yields once so the
        # function isn't a sync-disguised-as-async (sonarcloud S7503).
        await asyncio.sleep(0)

    monkeypatch.setattr("irc_lens.session.Session.connect", ok)
    monkeypatch.setattr("irc_lens.session.Session.wait_for_welcome", welcomed)


# ---------------------------------------------------------------------------
# Argparse surface
# ---------------------------------------------------------------------------


def test_serve_requires_only_nick(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--host`` / ``--port`` default to a local AgentIRC, so only ``--nick``
    is required. The bare ``irc-lens serve`` invocation still must error via
    the AfiError + hint contract (no argparse traceback) and the hint must
    point at the concrete fix — supplying ``--nick``."""
    with pytest.raises(SystemExit) as exc:
        main(["serve"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "--nick" in err
    assert "Traceback" not in err
    # The argparse "required" complaint must mention nick and ONLY nick now
    # that host/port have defaults — guards against silent regression of the
    # defaults.
    assert "--host" not in err
    assert "--port" not in err
    # The hint must be the serve-specific copy-pasteable form, NOT the
    # generic `run '<prog> --help'` fallback. This is the regression
    # Copilot flagged on PR #16: a weaker assertion would silently pass
    # if `_hint_for` regressed to the generic branch.
    hint_line = next(line for line in err.splitlines() if "hint:" in line)
    assert "try 'irc-lens serve --nick" in hint_line


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


def test_serve_help_renders_defaults_from_argparse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Help text for each defaulted flag must show the argparse-stored
    default value verbatim. This is the drift-catcher Copilot flagged on
    PR #16: hard-coding the default in the `help=` string makes it
    possible to change `default=` without updating the help — using
    `%(default)s` (or any equivalent) keeps them in lockstep, and this
    test asserts the rendered output proves it.

    Driving the assertion from `parser._actions` rather than the literal
    values means a future bump (e.g. default --port → 6668) only needs
    the `default=` change; the test re-derives the expected help string.
    """
    from irc_lens.cli import _build_parser  # noqa: PLC0415 - local import keeps the test self-contained

    parser = _build_parser()
    serve_parser = parser._subparsers._group_actions[0].choices["serve"]
    expected: dict[str, object] = {
        action.option_strings[0]: action.default
        for action in serve_parser._actions
        if action.option_strings
        and action.default is not None
        and action.default is not False  # store_true flags
        and action.dest != "help"
    }

    with pytest.raises(SystemExit):
        main(["serve", "--help"])
    out = capsys.readouterr().out

    # Every defaulted flag's stored default must surface in the rendered help.
    for flag, default_value in expected.items():
        token = f"(default: {default_value})"
        assert token in out, (
            f"--help text for {flag} is out of sync with argparse default "
            f"{default_value!r}: expected to find {token!r} in output"
        )


# ---------------------------------------------------------------------------
# Lifecycle contracts
# ---------------------------------------------------------------------------


def test_serve_fails_fast_on_unreachable_agentirc(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentIRC unreachable → exit 1, error + hint on stderr; the aiohttp
    runner is never even constructed (tripwire below)."""

    async def boom(self) -> None:
        raise LensConnectionLost("Cannot connect to IRC server at 127.0.0.1:1")

    monkeypatch.setattr("irc_lens.session.Session.connect", boom)

    def tripwire(*_a, **_kw):  # pragma: no cover - must NOT run
        raise AssertionError("AppRunner must not be constructed on connect failure")

    monkeypatch.setattr("aiohttp.web.AppRunner", tripwire)

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
    successful_connect,
) -> None:
    """Web port in use → exit 2 (env error per the policy)."""
    monkeypatch.setattr("aiohttp.web.AppRunner", _FakeRunner)
    monkeypatch.setattr("aiohttp.web.TCPSite", _BoomSite)
    monkeypatch.setattr("asyncio.Event", _ImmediateEvent)

    rc = main(["serve", "--host", "x", "--port", "1", "--nick", "lens"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "web port" in err.lower()


def test_serve_warns_on_bind_zero(
    capsys: pytest.CaptureFixture[str],
    stub_aiohttp_runtime,
    successful_connect,
) -> None:
    """`--bind 0.0.0.0` prints the loud no-auth warning before binding."""
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


def test_serve_displays_routable_url_when_binding_to_zero(
    capsys: pytest.CaptureFixture[str],
    stub_aiohttp_runtime,
    successful_connect,
) -> None:
    """When binding to 0.0.0.0, the printed URL must use 127.0.0.1 — most
    browsers won't navigate to http://0.0.0.0:port/."""
    rc = main(
        [
            "serve",
            "--host", "x", "--port", "1", "--nick", "lens",
            "--bind", "0.0.0.0",
            "--web-port", "65010",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "http://127.0.0.1:65010/" in err
    assert "http://0.0.0.0:" not in err


def test_serve_displays_bind_url_for_localhost(
    capsys: pytest.CaptureFixture[str],
    stub_aiohttp_runtime,
    successful_connect,
) -> None:
    """For non-wildcard binds, the URL uses the bind value as-is."""
    rc = main(
        [
            "serve",
            "--host", "x", "--port", "1", "--nick", "lens",
            "--web-port", "65011",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "http://127.0.0.1:65011/" in err


def test_serve_seed_loads_yaml_fixture(
    capsys: pytest.CaptureFixture[str],
    stub_aiohttp_runtime,
    successful_connect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 8: `--seed <path>` overlays YAML state onto Session before
    `make_app` runs. Verify by spying on the loader."""
    captured: dict[str, object] = {}

    def fake_apply(session, path):
        captured["session"] = session
        captured["path"] = path

    # Patch at the definition site since serve.py imports
    # `apply_seed` function-locally (see serve.py module top comment
    # — there's a real production-code import cycle to avoid).
    monkeypatch.setattr("irc_lens.seed.apply_seed", fake_apply)
    rc = main(
        [
            "serve",
            "--host", "x", "--port", "1", "--nick", "lens",
            "--seed", "tests/fixtures/basic.yaml",
            "--web-port", "65001",
        ]
    )
    assert rc == 0
    assert str(captured["path"]).endswith("tests/fixtures/basic.yaml")


def test_serve_seed_missing_file_exits_user_error(
    capsys: pytest.CaptureFixture[str],
    stub_aiohttp_runtime,
    successful_connect,
) -> None:
    """A bad --seed path surfaces as `error:`/`hint:` per the rubric;
    no aiohttp bind, no traceback."""
    rc = main(
        [
            "serve",
            "--host", "x", "--port", "1", "--nick", "lens",
            "--seed", "tests/fixtures/does_not_exist.yaml",
            "--web-port", "65001",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err
