"""Test fixtures shared across the suite.

Phase 9b adds the e2e fixture stack: a thin AgentIRC test server
(``_agentirc_server.py``), a connected ``Session`` against that
server, and an aiohttp test client driving the lens's real
``Application``. See ``tests/README.md`` for the rationale on
choosing the in-tree server over pulling ``culture`` as a dev
dep.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.session import Session
from irc_lens.web import make_app

from _agentirc_server import AgentIRCTestServer

# `irc_lens.seed` is imported function-locally inside
# `seeded_lens_client` to avoid eagerly loading the cli package
# (seed.py -> cli._errors -> cli/__init__.py) for every test.
# Tests that don't use the seeded fixture skip the cost.

_BASIC_SEED = Path(__file__).parent / "fixtures" / "basic.yaml"


async def _serve_lens(session: Session) -> AsyncIterator[TestClient]:
    """Spin up an aiohttp ``TestClient`` against ``session``.

    Helper that ``lens_client`` and ``seeded_lens_client`` share so
    the start/teardown shape lives in one place — no drift if either
    fixture grows new behaviour.
    """
    app: web.Application = make_app(session)
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
async def lens_client(lens_session: Session) -> AsyncIterator[TestClient]:
    """An aiohttp ``TestClient`` driving the lens's real
    ``Application`` against ``lens_session``.

    Tests get ``client.get('/')``, ``client.post('/input', ...)``,
    streaming via ``client.get('/events')`` — the same handlers
    production traffic hits, just without binding a real port.
    """
    async for client in _serve_lens(lens_session):
        yield client


@pytest_asyncio.fixture
async def seeded_lens_client(lens_session: Session) -> AsyncIterator[TestClient]:
    """Like ``lens_client`` but applies ``tests/fixtures/basic.yaml``
    after connect — Phase 9c (Playwright) starts every test from a
    deterministic DOM. ``apply_seed`` is pure state mutation, so the
    SSE bus has nothing to publish; the initial ``GET /`` render
    reads the seeded state directly."""
    from irc_lens.seed import apply_seed  # see module top comment

    apply_seed(lens_session, _BASIC_SEED)
    async for client in _serve_lens(lens_session):
        yield client
