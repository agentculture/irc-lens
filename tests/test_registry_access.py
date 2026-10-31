"""The session registry switches on grant enforcement (deviation d2).

t9 built enforcement into ``Session`` behind ``set_access``; these tests
pin that the registry — the one place mesh sessions are opened for a
verified identity — actually calls it, and fails closed when the feature
is on but no access store is installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from irc_lens.access_store import AccessStore
from irc_lens.commands import CommandType, ParsedCommand
from irc_lens.session import Session
from irc_lens.web.identity import Identity
from irc_lens.web.sessions import BACKEND_MESH, BACKEND_SANDBOX, SessionRegistry

ADMIN = "root@example.com"
USER = "alice@example.com"


@pytest.fixture(autouse=True)
def _fast_queries(monkeypatch):
    # The stub wire never answers the WHO/HISTORY a JOIN triggers; these
    # tests only assert which JOIN lines are sent, so don't wait 10 s.
    monkeypatch.setattr("irc_lens.session.QUERY_TIMEOUT", 0.05)


@pytest.fixture
def store(tmp_path: Path) -> AccessStore:
    s = AccessStore(tmp_path / "lens.db", clock=lambda: 1000.0)
    s.set_user(ADMIN, role="admin", state="approved", actor=None)
    s.set_user(USER, role="member", state="approved", actor=None)
    s.set_grant(USER, "channel", "#x", "write", actor=None)
    return s


def _identity(email: str) -> Identity:
    return Identity(
        principal=email, nick="lens-alice", raw_jwt_subject="t", tier="approved"
    )


def _factory(lines: list[str]):
    """A real Session whose handshake and wire are stubbed."""

    def make(nick: str) -> Session:
        session = Session(host="irc.example.org", port=1, nick=nick)

        async def connect() -> None:
            session._transport.connected = True

        async def welcome() -> None:
            return None

        async def send_raw(line: str) -> None:
            lines.append(line)

        session.connect = connect  # type: ignore[assignment]
        session.wait_for_welcome = welcome  # type: ignore[assignment]
        session._transport.send_raw = send_raw  # type: ignore[assignment]
        return session

    return make


def _registry(lines, *, enabled: bool, store) -> SessionRegistry:
    return SessionRegistry(
        factory=_factory(lines),
        sandbox_factory=_factory(lines),
        access_enabled=enabled,
        access_store=lambda: store,
    )


def _join(session: Session, channel: str) -> None:
    asyncio.run(session.execute(ParsedCommand(type=CommandType.JOIN, args=[channel])))


def test_mesh_session_for_non_admin_refuses_ungranted_join(store):
    lines: list[str] = []
    reg = _registry(lines, enabled=True, store=store)
    session = asyncio.run(reg.get_or_open(_identity(USER), BACKEND_MESH))
    assert session.access_enforced
    _join(session, "#y")
    assert not any(line.startswith("JOIN") for line in lines)
    _join(session, "#x")
    assert any(line.startswith("JOIN #x") for line in lines)


def test_mesh_session_for_admin_is_enforced_but_unrestricted(store):
    lines: list[str] = []
    reg = _registry(lines, enabled=True, store=store)
    session = asyncio.run(reg.get_or_open(_identity(ADMIN), BACKEND_MESH))
    assert session.access_enforced
    _join(session, "#y")
    assert any(line.startswith("JOIN #y") for line in lines)


def test_feature_off_leaves_sessions_unrestricted(store):
    lines: list[str] = []
    reg = _registry(lines, enabled=False, store=store)
    session = asyncio.run(reg.get_or_open(_identity(USER), BACKEND_MESH))
    assert not session.access_enforced


def test_enabled_without_store_refuses_to_open(store):
    reg = _registry([], enabled=True, store=None)
    with pytest.raises(PermissionError):
        asyncio.run(reg.get_or_open(_identity(USER), BACKEND_MESH))


def test_sandbox_sessions_are_not_given_mesh_grants(store):
    lines: list[str] = []
    reg = _registry(lines, enabled=True, store=store)
    session = asyncio.run(reg.get_or_open(_identity(ADMIN), BACKEND_SANDBOX))
    assert not session.access_enforced
