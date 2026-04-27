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

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pathlib import Path

from irc_lens.seed import apply_seed
from irc_lens.session import Session
from irc_lens.web import make_app

from _agentirc_server import AgentIRCTestServer

_BASIC_SEED = Path(__file__).parent / "fixtures" / "basic.yaml"


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
    app: web.Application = make_app(lens_session)
    test_server = TestServer(app)
    client = TestClient(test_server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def seeded_lens_client(lens_session: Session) -> AsyncIterator[TestClient]:
    """Like ``lens_client`` but applies ``tests/fixtures/basic.yaml``
    after connect — Phase 9c (Playwright) starts every test from a
    deterministic DOM. ``apply_seed`` is pure state mutation, so the
    SSE bus has nothing to publish; the initial ``GET /`` render
    reads the seeded state directly."""
    apply_seed(lens_session, _BASIC_SEED)
    app: web.Application = make_app(lens_session)
    test_server = TestServer(app)
    client = TestClient(test_server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
