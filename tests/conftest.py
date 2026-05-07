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
    app["registry"]._sessions[config.dev_email] = session
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
