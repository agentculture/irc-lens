"""Per-principal Session registry: lazy open, double-check, shutdown-all."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from irc_lens.web.identity import Identity
from irc_lens.web.sessions import (
    SessionRegistry,
    disconnect_all,
)


def _fake_session_factory() -> tuple[Callable[[str], MagicMock], list[MagicMock]]:
    created: list[MagicMock] = []

    def factory(nick: str) -> MagicMock:
        s = MagicMock()
        s.connect = AsyncMock()
        s.wait_for_welcome = AsyncMock()
        s.disconnect = AsyncMock()
        s.nick = nick
        created.append(s)
        return s

    return factory, created


@pytest.mark.asyncio
async def test_first_request_opens_session() -> None:
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    s = await reg.get_or_open(ident)

    assert s is created[0]
    s.connect.assert_awaited_once()
    s.wait_for_welcome.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_request_reuses_session() -> None:
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    a = await reg.get_or_open(ident)
    b = await reg.get_or_open(ident)

    assert a is b
    assert len(created) == 1


@pytest.mark.asyncio
async def test_concurrent_first_requests_share_one_session() -> None:
    """Two coroutines reaching get_or_open at once must not both build a Session."""
    factory, created = _fake_session_factory()

    async def slow_connect() -> None:
        await asyncio.sleep(0.01)

    base_factory = factory

    def slow_factory(nick: str) -> MagicMock:
        s = base_factory(nick)
        s.connect = AsyncMock(side_effect=slow_connect)
        return s

    reg = SessionRegistry(factory=slow_factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    a, b = await asyncio.gather(reg.get_or_open(ident), reg.get_or_open(ident))

    assert a is b
    assert len(created) == 1


@pytest.mark.asyncio
async def test_failed_open_does_not_register() -> None:
    factory, created = _fake_session_factory()

    class Boom(Exception):
        pass

    def bad_factory(nick: str) -> MagicMock:
        s = factory(nick)
        s.connect = AsyncMock(side_effect=Boom("nope"))
        return s

    reg = SessionRegistry(factory=bad_factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    with pytest.raises(Boom):
        await reg.get_or_open(ident)

    # Second attempt must build a fresh Session, not reuse a half-open one.
    with pytest.raises(Boom):
        await reg.get_or_open(ident)
    assert len(created) == 2


@pytest.mark.asyncio
async def test_failed_welcome_disconnects_session() -> None:
    """If connect() succeeds but wait_for_welcome() fails, the partially-open
    session must be disconnected — otherwise the TCP socket and read task
    leak (Qodo PR #31 review)."""
    factory, created = _fake_session_factory()

    class Boom(Exception):
        pass

    def half_open_factory(nick: str) -> MagicMock:
        s = factory(nick)
        s.connect = AsyncMock()                                 # succeeds
        s.wait_for_welcome = AsyncMock(side_effect=Boom("nope"))  # fails
        return s

    reg = SessionRegistry(factory=half_open_factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    with pytest.raises(Boom):
        await reg.get_or_open(ident)

    # The session that was partially opened must have been cleaned up.
    assert len(created) == 1
    created[0].connect.assert_awaited_once()
    created[0].wait_for_welcome.assert_awaited_once()
    created[0].disconnect.assert_awaited_once()
    # And it must NOT be registered (so disconnect_all won't touch it twice
    # at shutdown, and a retry builds a fresh session).
    assert "alice@example.com" not in reg


@pytest.mark.asyncio
async def test_disconnect_failure_during_cleanup_does_not_mask_original() -> None:
    """If disconnect() itself fails during cleanup, the original error must
    still propagate — disconnect failures don't get to silently swallow the
    real diagnostic."""
    factory, created = _fake_session_factory()

    class WelcomeBoom(Exception):
        pass

    def factory_with_failing_disconnect(nick: str) -> MagicMock:
        s = factory(nick)
        s.connect = AsyncMock()
        s.wait_for_welcome = AsyncMock(side_effect=WelcomeBoom("nick rejected"))
        s.disconnect = AsyncMock(side_effect=RuntimeError("disconnect also fails"))
        return s

    reg = SessionRegistry(factory=factory_with_failing_disconnect)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    # Original WelcomeBoom must surface, NOT the disconnect's RuntimeError.
    with pytest.raises(WelcomeBoom):
        await reg.get_or_open(ident)
    created[0].disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_short_circuits_get_or_open() -> None:
    """register() pre-seeds; subsequent get_or_open MUST NOT call factory/connect."""
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    pre_session = MagicMock()
    pre_session.connect = AsyncMock()
    pre_session.wait_for_welcome = AsyncMock()

    reg.register("alice@example.com", pre_session)

    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")
    s = await reg.get_or_open(ident)

    assert s is pre_session
    pre_session.connect.assert_not_awaited()
    pre_session.wait_for_welcome.assert_not_awaited()
    assert created == []                     # factory never invoked
    assert "alice@example.com" in reg
    assert pre_session in reg.values()


@pytest.mark.asyncio
async def test_disconnect_all_calls_each_session() -> None:
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    a = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")
    b = Identity(principal="bob@example.com", nick="spark-bob", raw_jwt_subject="s")
    await reg.get_or_open(a)
    await reg.get_or_open(b)

    await disconnect_all(reg)

    for s in created:
        s.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_all_swallows_individual_failures() -> None:
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    a = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")
    b = Identity(principal="bob@example.com", nick="spark-bob", raw_jwt_subject="s")
    await reg.get_or_open(a)
    await reg.get_or_open(b)
    created[0].disconnect = AsyncMock(side_effect=RuntimeError("first fails"))

    # Must not raise — both should be attempted.
    await disconnect_all(reg)
    created[1].disconnect.assert_awaited_once()
