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
