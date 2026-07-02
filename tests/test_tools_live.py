"""t5 — e2e / integration coverage for the live-verb registry tools.

Every test here drives ``irc_lens.tools``'s public functions through the
*real* CLI surface (``agentfront.testing.run_cli`` — the same in-process
harness ``tests/test_front_agreement.py`` uses to prove the rendered CLI
works) against the in-tree fake AgentIRC server
(``tests/_agentirc_server.py``), extended for t5 with immediate
``001``/``LIST``/``WHO``/``HISTORY`` replies (see that module's docstring)
so none of these round-trips wait out ``Session.QUERY_TIMEOUT`` (10s).

Because each live-verb tool is a *sync* function that opens its own event
loop via ``asyncio.run()`` per call (t5's ephemeral-session design — see
``irc_lens/tools.py``), the fake server needs its own thread/loop rather
than the ``pytest_asyncio`` fixtures in ``conftest.py`` (which tie the
fake server to the *same* loop the test coroutine runs on — unusable
here since these are plain sync tests). ``ThreadedAgentIRCTestServer``
(added to ``_agentirc_server.py`` for this purpose) provides that.

Per-verb test level:

* ``channels`` — e2e via ``run_cli(app, ["channels", "--json", ...])``,
  the literal ask in the plan's acceptance criteria.
* ``join``, ``send``, ``who``, ``mesh``, ``switch``, ``topic``, ``me``,
  ``icon``, ``part``, ``read`` — e2e via the same ``run_cli`` surface, in
  one sequential flow (``test_full_catalog_round_trip``) so later verbs
  can observe state earlier verbs created (a channel joined, a nick
  present in it) — the fake server persists ``channel_members`` across
  separate ephemeral connections for the lifetime of one
  ``ThreadedAgentIRCTestServer`` instance. All 11 plan-named verbs get
  real socket round-trips; none are dispatch-unit-only fallbacks.
* Error-mapping (bad config, bad channel format, unreachable server,
  non-dev auth mode) — e2e via ``run_cli`` as well, since they are cheap
  single-shot checks independent of the sequential flow above.
"""

from __future__ import annotations

import json

import pytest
from agentfront.testing import run_cli

from irc_lens.cli import build_app

from _agentirc_server import ThreadedAgentIRCTestServer

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


# ---------------------------------------------------------------------------
# Fixtures — sync (no pytest_asyncio): these tests call sync entry points.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_server():
    server = ThreadedAgentIRCTestServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def dev_config_path(tmp_path, fake_server: ThreadedAgentIRCTestServer):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "auth:\n"
        "  mode: dev\n"
        "  dev:\n"
        "    nick: lens-test\n"
        "    email: dev@local\n"
        "server:\n"
        "  name: testsrv\n"
        f"  host: {fake_server.host}\n"
        f"  port: {fake_server.port}\n"
    )
    return cfg


def _run(argv: list[str], config_path):
    """Build a fresh App and run *argv* through the real CLI surface."""
    app = build_app()
    return run_cli(app, [*argv, "--config", str(config_path)])


def _received_eventually(fake_server, predicate, timeout: float = 2.0) -> bool:
    """Poll ``fake_server.received`` until *predicate* matches a line.

    ``run_cli`` returns once the *client* side of the ephemeral session
    has disconnected, but the fake server appends to ``received`` on its
    own thread/loop — a fire-and-forget write (TOPIC/PRIVMSG/ICON) can
    land in ``received`` a beat after the tool exits. Polling instead of
    asserting instantly removes that cross-thread race.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(predicate(line) for line in fake_server.received):
            return True
        time.sleep(0.01)
    return any(predicate(line) for line in fake_server.received)


def _run_json(argv: list[str], config_path):
    """Like :func:`_run` but appends ``--json`` and returns the parsed
    payload, asserting a clean (exit 0, empty stderr) success first."""
    result = _run([*argv, "--json"], config_path)
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# The literal ask: `irc-lens channels --json` round-trips against the fake
# server.
# ---------------------------------------------------------------------------


def test_channels_e2e_json_round_trip(dev_config_path) -> None:
    result = _run(["channels", "--json"], dev_config_path)
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload == []  # nothing joined yet on a fresh fake server


# ---------------------------------------------------------------------------
# Full-catalog sequential round trip: all 11 plan-named verbs, one flow,
# each building on state the previous verb created via the SAME fake
# server instance (channel_members persists across separate ephemeral
# connections for the lifetime of one ThreadedAgentIRCTestServer).
# ---------------------------------------------------------------------------


def test_full_catalog_round_trip(dev_config_path, fake_server: ThreadedAgentIRCTestServer) -> None:
    # join — real JOIN over the wire + local joined_channels bookkeeping.
    payload = _run_json(["join", "#agora"], dev_config_path)
    assert payload == {"channel": "#agora", "joined_channels": ["#agora"]}
    assert _received_eventually(
        fake_server, lambda line: line.command == "JOIN" and line.params[:1] == ["#agora"]
    )

    # send — PRIVMSG to the joined channel.
    result = _run(["send", "#agora", "hello mesh"], dev_config_path)
    assert result.exit_code == 0, result.stderr
    assert _received_eventually(
        fake_server,
        lambda line: line.command == "PRIVMSG" and line.params == ["#agora", "hello mesh"],
    )

    # who — the fake server tracks #agora's membership across connections;
    # lens-test joined it above, so a fresh ephemeral WHO sees itself.
    who_entries = _run_json(["who", "#agora"], dev_config_path)
    assert any(e["nick"] == "lens-test" for e in who_entries)

    # channels — LIST now reports #agora.
    channels_payload = _run_json(["channels"], dev_config_path)
    assert "#agora" in channels_payload

    # mesh — builds katvan's graph shape from #agora's membership.
    mesh_payload = _run_json(["mesh", "#agora"], dev_config_path)
    node_ids = {n["id"] for n in mesh_payload["nodes"]}
    assert {"#agora", "lens-test"} <= node_ids
    assert {"source": "#agora", "target": "lens-test"} in mesh_payload["edges"]

    # switch — auto-joins then flips current_channel.
    switch_payload = _run_json(["switch", "#agora"], dev_config_path)
    assert switch_payload == {"current_channel": "#agora"}

    # topic — sets and is observable on the wire.
    result = _run(["topic", "#agora", "--text", "today's agenda"], dev_config_path)
    assert result.exit_code == 0, result.stderr
    assert _received_eventually(
        fake_server,
        lambda line: line.command == "TOPIC" and line.params == ["#agora", "today's agenda"],
    )

    # me — CTCP ACTION on the wire.
    result = _run(["me", "#agora", "waves hello"], dev_config_path)
    assert result.exit_code == 0, result.stderr
    assert _received_eventually(
        fake_server,
        lambda line: line.command == "PRIVMSG"
        and line.params[0] == "#agora"
        and "ACTION waves hello" in line.params[1],
    )

    # icon — raw ICON line on the wire.
    result = _run(["icon", "🎉"], dev_config_path)
    assert result.exit_code == 0, result.stderr
    assert _received_eventually(
        fake_server, lambda line: line.command == "ICON" and line.params == ["🎉"]
    )

    # read — round-trips HISTORY/HISTORYEND without stalling; the fake
    # server has no real backlog, so an empty list is the honest result.
    read_payload = _run_json(["read", "#agora", "--limit", "5"], dev_config_path)
    assert read_payload == []

    # part — leaves the channel.
    result = _run(["part", "#agora"], dev_config_path)
    assert result.exit_code == 0, result.stderr
    assert any(
        line.command == "PART" and line.params[:1] == ["#agora"] for line in fake_server.received
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_missing_config_exits_user_error_with_hint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config-dir"))
    app = build_app()
    result = run_cli(app, ["channels"])
    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "hint:" in result.stderr
    assert "config init" in result.stderr
    assert "Traceback" not in result.stderr


def test_bad_channel_format_exits_user_error(dev_config_path) -> None:
    result = _run(["join", "not-a-channel"], dev_config_path)
    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "hint:" in result.stderr
    assert "invalid channel" in result.stderr
    assert "Traceback" not in result.stderr


def test_unreachable_server_exits_env_error(tmp_path, fake_server: ThreadedAgentIRCTestServer) -> None:
    """A server that refuses the connection maps to EXIT_ENV_ERROR (2) —
    an environment failing to deliver an existing resource, per this
    task's exit-code mapping (documented in irc_lens/tools.py)."""
    host, port = fake_server.host, fake_server.port
    fake_server.stop()  # now nothing is listening on (host, port)

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "auth:\n"
        "  mode: dev\n"
        "  dev:\n"
        "    nick: lens-test\n"
        "    email: dev@local\n"
        "server:\n"
        "  name: testsrv\n"
        f"  host: {host}\n"
        f"  port: {port}\n"
    )
    app = build_app()
    result = run_cli(app, ["channels", "--config", str(cfg)])
    assert result.exit_code == 2, result.stderr
    assert "error:" in result.stderr
    assert "hint:" in result.stderr
    assert "Traceback" not in result.stderr


def test_cloudflare_access_mode_rejected_as_user_error(tmp_path) -> None:
    """Live-verb tools have no per-request JWT to derive an identity
    from, so a cloudflare-access config is a clear user-input error, not
    an attempt to guess an identity."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "auth:\n"
        "  mode: cloudflare-access\n"
        "  cloudflare:\n"
        "    aud: aud-test\n"
        "    team_domain: example.cloudflareaccess.com\n"
        "  allowed_emails: [alice@example.com]\n"
        "server:\n"
        "  name: testsrv\n"
        "  host: 127.0.0.1\n"
        "  port: 6667\n"
    )
    app = build_app()
    result = run_cli(app, ["channels", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "auth.mode: dev" in result.stderr
    assert "hint:" in result.stderr
    assert "Traceback" not in result.stderr
