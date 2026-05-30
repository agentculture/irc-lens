"""Unit tests for the live agent-mesh graph.

Covers the server side of the mesh view: the snapshot builder
(``build_mesh_snapshot``), the ``agent``/``human`` classification, the
``mesh`` SSE publish path (empty-graph and populated cases), the ``/mesh``
verb, the coalescing single-flight refresh, and the JOIN/PART trigger.

The rendered graph itself is katvan's MeshIsland canvas, ported to
``static/mesh.js`` — exercised in the browser via Playwright. Here we
pin the *data contract* irc-lens emits: katvan's mesh.json shape
``{nodes: [{id, label, kind, server}], edges: [{source, target}]}``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from irc_lens.commands import CommandType, ParsedCommand, parse_command
from irc_lens.irc import Message
from irc_lens.session import Session, SessionEvent


@pytest.fixture
def session() -> Session:
    return Session(host="testsrv", port=6667, nick="lens-test")


def _who_entry(nick: str, *, server: str = "testsrv") -> dict:
    """A minimal WHO (RPL_WHOREPLY) row as ``Session.who`` returns it."""
    return {
        "nick": nick,
        "user": nick,
        "host": "x",
        "server": server,
        "flags": "H",
        "realname": "0 " + nick,
    }


def _patch_who(session: Session, members: dict[str, list[dict]]) -> None:
    """Stub ``Session.who`` so the snapshot builder doesn't hit the wire."""

    async def fake_who(target: str) -> list[dict]:
        return members.get(target, [])

    session.who = fake_who  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# /mesh command parsing
# ---------------------------------------------------------------------------


def test_parse_command_mesh() -> None:
    assert parse_command("/mesh").type is CommandType.MESH


# ---------------------------------------------------------------------------
# build_mesh_snapshot — the katvan data contract
# ---------------------------------------------------------------------------


def test_build_mesh_snapshot_shape() -> None:
    session = Session(host="testsrv", port=6667, nick="lens-test")
    session.joined_channels.update({"#ops", "#dev"})
    _patch_who(
        session,
        {
            # The empty-nick row is a malformed WHO line; the builder skips it.
            "#ops": [
                _who_entry("lens-test"),
                _who_entry("daria"),
                _who_entry("spark"),
                {"nick": "", "server": "testsrv"},
            ],
            "#dev": [_who_entry("daria"), _who_entry("nova", server="nova")],
        },
    )
    snap = asyncio.run(session.build_mesh_snapshot())

    # Room nodes for each joined channel; person nodes deduped across channels.
    rooms = {n["id"] for n in snap["nodes"] if n["kind"] == "room"}
    assert rooms == {"#ops", "#dev"}
    nick_nodes = {n["id"]: n for n in snap["nodes"] if n["kind"] != "room"}
    assert set(nick_nodes) == {"lens-test", "daria", "spark", "nova"}

    # Every node carries exactly katvan's key set.
    for node in snap["nodes"]:
        assert set(node) == {"id", "label", "kind", "server"}
        assert node["kind"] in {"room", "agent", "human"}
    for edge in snap["edges"]:
        assert set(edge) == {"source", "target"}

    # The operator nick classifies as human; the rest as agents.
    assert nick_nodes["lens-test"]["kind"] == "human"
    assert nick_nodes["daria"]["kind"] == "agent"

    # WHO's per-nick server maps onto the federated band; channel nodes
    # carry the lens host.
    assert nick_nodes["nova"]["server"] == "nova"
    assert nick_nodes["daria"]["server"] == "testsrv"
    assert next(n for n in snap["nodes"] if n["id"] == "#ops")["server"] == "testsrv"

    # Membership edges: daria is in both channels → two edges.
    daria_edges = [e for e in snap["edges"] if e["target"] == "daria"]
    assert {e["source"] for e in daria_edges} == {"#ops", "#dev"}


def test_build_mesh_snapshot_skips_failing_channel() -> None:
    """A WHO that raises (e.g. a broken pipe) drops that channel rather
    than failing the whole snapshot — a viz degrades, it doesn't crash."""
    session = Session(host="testsrv", port=6667, nick="lens-test")
    session.joined_channels.update({"#ok", "#bad"})

    async def flaky_who(target: str) -> list[dict]:
        if target == "#bad":
            raise ConnectionError("pipe gone")
        return [_who_entry("daria")]

    session.who = flaky_who  # type: ignore[assignment]
    snap = asyncio.run(session.build_mesh_snapshot())
    # #bad still appears as a room (added before the WHO), but has no
    # members/edges; #ok is fully populated.
    assert {n["id"] for n in snap["nodes"] if n["kind"] == "room"} == {"#ok", "#bad"}
    assert {e["source"] for e in snap["edges"]} == {"#ok"}


def test_classify_kind() -> None:
    session = Session(host="testsrv", port=6667, nick="me")
    assert session._classify_kind({"nick": "me"}) == "human"
    assert session._classify_kind({"nick": "someagent"}) == "agent"
    assert session._classify_kind({}) == "agent"


# ---------------------------------------------------------------------------
# Publish path: empty-graph guards + populated /mesh
# ---------------------------------------------------------------------------


def _mesh_events(events: list[SessionEvent]) -> list[dict]:
    return [json.loads(e.data) for e in events if e.name == "mesh"]


def test_recompute_publishes_empty_when_no_channels(session: Session) -> None:
    sub = session.event_bus.subscribe()
    asyncio.run(session._recompute_and_publish_mesh())
    meshes = _mesh_events(sub.drain_nowait())
    sub.close()
    assert meshes == [{"nodes": [], "edges": []}]


def test_recompute_publishes_empty_when_disconnected(session: Session) -> None:
    """Channels joined but the transport never welcomed → empty graph
    (we never WHO an unregistered connection)."""
    session.joined_channels.add("#ops")
    assert not session.connected
    sub = session.event_bus.subscribe()
    asyncio.run(session._recompute_and_publish_mesh())
    meshes = _mesh_events(sub.drain_nowait())
    sub.close()
    assert meshes == [{"nodes": [], "edges": []}]


def test_exec_mesh_switches_view_and_publishes_snapshot() -> None:
    session = Session(host="testsrv", port=6667, nick="lens-test")
    session.joined_channels.add("#ops")
    session._transport.connected = True  # bypass the pre-welcome guard
    _patch_who(session, {"#ops": [_who_entry("lens-test"), _who_entry("daria")]})

    sub = session.event_bus.subscribe()
    asyncio.run(session.execute(ParsedCommand(type=CommandType.MESH)))
    events = sub.drain_nowait()
    sub.close()

    assert session.view == "mesh"
    names = [e.name for e in events]
    # _switch_view emits view + info; the recompute emits one mesh event.
    view = next(e for e in events if e.name == "view")
    assert json.loads(view.data) == {"view": "mesh"}
    assert "info" in names
    meshes = _mesh_events(events)
    assert len(meshes) == 1
    snap = meshes[0]
    assert {n["id"] for n in snap["nodes"]} == {"#ops", "lens-test", "daria"}
    assert {"source": "#ops", "target": "daria"} in snap["edges"]


# ---------------------------------------------------------------------------
# Coalescing single-flight refresh + JOIN/PART trigger
# ---------------------------------------------------------------------------


async def test_request_mesh_refresh_coalesces(session: Session) -> None:
    """A burst of refresh requests collapses into a single rebuild — the
    drain task is reused, not stacked, so JOIN/PART floods don't fan out
    into a WHO storm."""
    calls = 0

    async def fake_recompute() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)

    session._recompute_and_publish_mesh = fake_recompute  # type: ignore[assignment]
    for _ in range(5):
        session._request_mesh_refresh()
    assert session._mesh_task is not None
    await session._mesh_task
    assert calls == 1


async def test_request_mesh_refresh_reruns_when_dirtied_midflight(session: Session) -> None:
    """A request that lands while a build is in flight triggers exactly
    one more rebuild (the dirty flag), not zero and not many."""
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_recompute() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()

    session._recompute_and_publish_mesh = fake_recompute  # type: ignore[assignment]
    session._request_mesh_refresh()  # starts the drain; build #1 parks
    await started.wait()
    session._request_mesh_refresh()  # dirties mid-flight
    release.set()
    await session._mesh_task
    assert calls == 2


async def test_public_request_mesh_refresh_schedules(session: Session) -> None:
    """The public wrapper (used by the SSE handler on subscribe) routes
    through the same coalescing drain as the internal path."""
    called = 0

    async def fake_recompute() -> None:
        nonlocal called
        called += 1

    session._recompute_and_publish_mesh = fake_recompute  # type: ignore[assignment]
    session.request_mesh_refresh()
    assert session._mesh_task is not None
    await session._mesh_task
    assert called == 1


async def test_drain_survives_recompute_error(session: Session) -> None:
    """A refresh that raises is logged, not propagated — the drain task
    must finish cleanly so a transient failure doesn't wedge the loop."""
    calls = 0

    async def boom() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("nope")

    session._recompute_and_publish_mesh = boom  # type: ignore[assignment]
    session._request_mesh_refresh()
    await session._mesh_task  # must not raise
    assert calls == 1


async def test_mesh_refresh_loop_ticks(session: Session, monkeypatch) -> None:
    """The periodic refresher fires a refresh when someone is watching,
    we're connected, and there are channels — and stays idle otherwise."""
    monkeypatch.setattr("irc_lens.session.MESH_REFRESH_INTERVAL", 0.01)
    session._transport.connected = True
    session.joined_channels.add("#ops")
    sub = session.event_bus.subscribe()  # subscriber_count > 0
    ticks = 0

    def fake_request() -> None:
        nonlocal ticks
        ticks += 1

    session._request_mesh_refresh = fake_request  # type: ignore[assignment]
    task = asyncio.create_task(session._mesh_refresh_loop())
    try:
        async with asyncio.timeout(1.0):
            while ticks < 1:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        sub.close()
    assert ticks >= 1


def test_join_dispatch_triggers_mesh_refresh(session: Session) -> None:
    """Inbound JOIN/PART must request a mesh refresh (topology changed)."""
    refreshed = 0

    def fake_request() -> None:
        nonlocal refreshed
        refreshed += 1

    session._request_mesh_refresh = fake_request  # type: ignore[assignment]
    asyncio.run(
        session.dispatch(Message(prefix="a!a@h", command="JOIN", params=["#ops"]))
    )
    asyncio.run(
        session.dispatch(Message(prefix="a!a@h", command="PART", params=["#ops"]))
    )
    assert refreshed == 2


# ---------------------------------------------------------------------------
# Lifecycle: the periodic refresher starts on connect, stops on disconnect
# ---------------------------------------------------------------------------


async def test_refresher_task_lifecycle(lens_session: Session) -> None:
    """connect() (run by the fixture) starts the per-session refresher;
    the cancel path (invoked by disconnect) tears it down cleanly. We
    call ``_cancel_mesh_tasks`` directly rather than ``disconnect`` so the
    fixture's own teardown disconnect doesn't double-close the transport.
    """
    assert lens_session._mesh_refresher is not None
    assert not lens_session._mesh_refresher.done()
    await lens_session._cancel_mesh_tasks()
    assert lens_session._mesh_refresher is None
    assert lens_session._mesh_task is None
