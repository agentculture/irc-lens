"""Test fixtures shared across the suite.

Phase 9b adds the e2e fixture stack: a thin AgentIRC test server
(``_agentirc_server.py``), a connected ``Session`` against that
server, and an aiohttp test client driving the lens's real
``Application``. See ``tests/README.md`` for the rationale on
choosing the in-tree server over pulling ``culture`` as a dev
dep.

Phase 2 changes the lens app signature: ``make_app(config, session_factory)``
instead of ``make_app(session)``. Tests build a dev-mode ``LensConfig``
and a session-factory closure that returns the live test ``Session``.
"""
from __future__ import annotations

import dataclasses
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.session import Session
from irc_lens.web import make_app

from _agentirc_server import AgentIRCTestServer

_BASIC_SEED = Path(__file__).parent / "fixtures" / "basic.yaml"


def _dev_config(server_host: str, server_port: int, nick: str) -> LensConfig:
    """Build a minimal dev-mode LensConfig for tests.

    The session-factory closure passed to ``make_app`` short-circuits
    Session construction by returning the already-connected fixture
    Session, so server.host/port/nick here only need to round-trip
    through validation, not actually open a network connection.
    """
    return LensConfig(
        auth_mode="dev",
        dev_nick=nick,
        dev_email="dev@local",
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host=server_host,
        server_port=server_port,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=True,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )


async def _serve_lens(session: Session, host: str, port: int) -> AsyncIterator[TestClient]:
    """Spin up an aiohttp ``TestClient`` against ``session``.

    Helper that ``lens_client`` and ``seeded_lens_client`` share so
    the start/teardown shape lives in one place — no drift if either
    fixture grows new behaviour.

    The factory closure is wired through ``make_app`` as the spec
    requires, but the registry is immediately pre-seeded with the
    already-connected fixture session so ``get_or_open`` returns it
    on first access without calling ``Session.connect()`` a second
    time (which would open a duplicate TCP connection).
    """
    config = _dev_config(host, port, nick=session.nick)
    factory = lambda _nick: session  # noqa: E731 — single-line closure is the point
    app: web.Application = make_app(config, factory)
    # Pre-seed the registry so the test session is returned immediately
    # on the first request without re-connecting. dev_email is the
    # principal the dev-mode middleware stamps on every request.
    app["registry"].register(config.dev_email, session)
    test_server = TestServer(app)
    client = TestClient(test_server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def agentirc_server() -> AsyncIterator[AgentIRCTestServer]:
    """Function-scoped: each test gets a fresh server bound to a
    random port. Teardown closes the listening socket and any open
    client connections."""
    server = AgentIRCTestServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest_asyncio.fixture
async def lens_session(agentirc_server: AgentIRCTestServer) -> AsyncIterator[Session]:
    """A fully-connected ``Session`` against the test server.

    Connection happens inside the fixture so individual tests don't
    repeat the boilerplate; teardown calls ``disconnect()`` which
    sends QUIT (the test server silently consumes it).
    """
    session = Session(host=agentirc_server.host, port=agentirc_server.port, nick="lens-test")
    await session.connect()
    try:
        yield session
    finally:
        await session.disconnect()


@pytest_asyncio.fixture
async def lens_client(
    lens_session: Session, agentirc_server: AgentIRCTestServer
) -> AsyncIterator[TestClient]:
    """An aiohttp ``TestClient`` driving the lens's real
    ``Application`` against ``lens_session``."""
    async for client in _serve_lens(lens_session, agentirc_server.host, agentirc_server.port):
        yield client


@pytest_asyncio.fixture
async def seeded_lens_client(
    lens_session: Session, agentirc_server: AgentIRCTestServer
) -> AsyncIterator[TestClient]:
    """Like ``lens_client`` but applies ``tests/fixtures/basic.yaml``
    after connect."""
    from irc_lens.seed import apply_seed  # see module top comment in original

    apply_seed(lens_session, _BASIC_SEED)
    async for client in _serve_lens(lens_session, agentirc_server.host, agentirc_server.port):
        yield client


@pytest_asyncio.fixture
async def media_hosted_lens_client(
    lens_session: Session, agentirc_server: AgentIRCTestServer
) -> AsyncIterator[TestClient]:
    """A seeded lens client whose media state renders uploaded URLs as
    *direct* lens-hosted embeds that actually load over loopback.

    ``lens_client`` / ``seeded_lens_client`` build the fixture ``Session``
    with the plain ``Session(host, port, nick)`` constructor, which leaves
    ``media_embed_prefixes`` empty and ``media.public_base_url`` unset — so
    ``POST /upload`` returns a ``http://127.0.0.1:0/media/...`` URL (the
    bogus port-0 config default) that ``media_items`` classifies as
    *remote*, rendering a click-to-load placeholder. That's the right shape
    for t8's send-path tests, but task t10's proof chain needs the
    lens-hosted *direct-embed* branch exercised end to end: an uploaded
    file that renders as a real ``<img>`` / ``<audio>`` and whose ``src``
    the browser can actually fetch.

    Both facts are only knowable after the ``TestServer`` binds its random
    port, so this fixture starts the server first, then patches
    ``app["media_base"]`` (what ``POST /upload`` stamps into the returned
    URL) and the session's ``media_embed_prefixes`` (what ``media_items``
    matches against) to the server's real reachable origin. An uploaded URL
    is then both origin-matched (→ direct embed) and loopback-reachable
    (→ the browser can load it / the ``/media/`` route serves it).

    ``media_embed_prefixes`` holds ``(scheme, hostname, port)`` origin
    tuples, not URL-string prefixes (fixed after a Qodo PR #50 finding —
    see ``tests/test_render_media.py``'s "Trusted-host matching is
    origin-exact, not prefix-based" section and
    ``web/render.py::MediaOrigin``), so this fixture derives one exact
    origin from the same ``host``/``port`` used to build ``base`` below
    rather than passing ``f"{base}/media/"`` as a string.

    Seeds ``tests/fixtures/basic.yaml`` so ``#general`` is the active
    channel with two historical chat lines, matching ``seeded_lens_client``.
    """
    from irc_lens.seed import apply_seed

    # Bind the TestServer to a known port *before* building the app, so the
    # upload base can be baked into `LensConfig` (→ `app["media_base"]`) at
    # construction time. Mutating `app[...]` after the server has started is
    # deprecated by aiohttp, and the port is the one thing not otherwise
    # knowable until the socket binds — pre-reserving it sidesteps both.
    host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    base = f"http://{host}:{port}"

    config = dataclasses.replace(
        _dev_config(agentirc_server.host, agentirc_server.port, nick=lens_session.nick),
        web_bind=host,
        web_port=port,
        media_public_base_url=base,
    )
    factory = lambda _nick: lens_session  # noqa: E731 — single-line closure is the point
    app: web.Application = make_app(config, factory)
    app["registry"].register(config.dev_email, lens_session)
    apply_seed(lens_session, _BASIC_SEED)
    # The session (not the app) is the embed-prefix source and is safe to
    # mutate any time — point it at the same origin uploads will advertise,
    # so a lens-hosted URL renders as a direct embed rather than a remote
    # placeholder.
    lens_session.media_embed_prefixes = (("http", host, port),)
    test_server = TestServer(app, host=host, port=port)
    client = TestClient(test_server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# CF Access fixtures (shared across test_auth_middleware + test_audit_log)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def jwks() -> AsyncIterator["FakeJWKS"]:
    """An in-tree FakeJWKS server bound to a random port. See
    `tests/_jwks_server.py` for the full surface."""
    from _jwks_server import FakeJWKS

    j = FakeJWKS()
    await j.start()
    try:
        yield j
    finally:
        await j.stop()
