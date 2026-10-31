"""Unit tests for ``AgentSession`` (review 2, findings 2, 3 and 10).

A real :class:`~irc_lens.session.Session` with a stubbed wire stands in for
the agent's lens-held connection, so the inbox, the per-call event
correlation and the owner lookup are exercised without an IRC server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from irc_lens.access_store import AccessStore
from irc_lens.commands import CommandType, ParsedCommand
from irc_lens.session import Session
from irc_lens.web.agent_sessions import DM_TARGET, AgentSession, HumanState
from irc_lens.web.sessions import BACKEND_MESH

MEMBER = "bob@example.org"
HUMAN_NICK = "testsrv-bob"
AGENT_NICK = "testsrv-pa_-bob"
GRANTED = "#ops"
UNGRANTED = "#secret"


class PeekOnlyRegistry:
    """A registry that answers ``peek`` only: a full scan is a test failure."""

    def __init__(self, sessions: dict[tuple[str, str], Any]) -> None:
        self.sessions = sessions
        self.peeks: list[tuple[str, str]] = []

    def peek(self, principal: str, backend: str) -> Any:
        self.peeks.append((principal, backend))
        return self.sessions.get((principal, backend))

    def keys(self) -> list:
        raise AssertionError("the owner lookup must not scan the registry")

    def values(self) -> list:
        raise AssertionError("the owner lookup must not scan the registry")


class HumanConn:
    healthy = True
    connected = True
    nick = HUMAN_NICK


@pytest.fixture
def store(tmp_path: Path) -> AccessStore:
    s = AccessStore(tmp_path / "lens.db", clock=lambda: 1000.0)
    s.set_user(MEMBER, role="member", state="approved", actor=None)
    s.set_grant(MEMBER, "channel", GRANTED, "write", actor=None)
    return s


def _agent(store: AccessStore, *, human_online: bool = True) -> AgentSession:
    session = Session(host="127.0.0.1", port=6667, nick=AGENT_NICK)
    session.set_access(store, MEMBER)
    session._transport.connected = True
    sessions = {(MEMBER, BACKEND_MESH): HumanConn()} if human_online else {}
    app = {"registry": PeekOnlyRegistry(sessions)}
    return AgentSession(
        app,  # type: ignore[arg-type]
        session,
        credential_key=MEMBER,
        email=MEMBER,
        tier="approved",
        human=HumanState(MEMBER, BACKEND_MESH, HUMAN_NICK),
    )


def _deliver(agent: AgentSession, text: str, target: str = GRANTED) -> None:
    line, _reason = agent.classify(HUMAN_NICK, target, text)
    assert line is not None
    agent._inbox.append(line)


def _texts(result: dict) -> list[str]:
    return [ln["text"] for ln in result["lines"]]


# -- finding 2: lines beyond the read limit stay unread ------------------------------


def test_read_returns_the_oldest_unread_lines_first(store) -> None:
    agent = _agent(store)
    for n in range(5):
        _deliver(agent, f"line {n}")
    assert _texts(agent.read(limit=2)) == ["line 0", "line 1"]


def test_lines_beyond_the_limit_come_back_on_the_next_read(store) -> None:
    agent = _agent(store)
    for n in range(5):
        _deliver(agent, f"line {n}")
    agent.read(limit=2)
    assert _texts(agent.read(limit=2)) == ["line 2", "line 3"]


def test_read_drains_the_rest_and_then_is_empty(store) -> None:
    agent = _agent(store)
    for n in range(5):
        _deliver(agent, f"line {n}")
    agent.read(limit=2)
    agent.read(limit=2)
    assert _texts(agent.read(limit=2)) == ["line 4"]


def test_read_after_draining_is_empty(store) -> None:
    agent = _agent(store)
    _deliver(agent, "only")
    agent.read(limit=2)
    assert agent.read(limit=2)["lines"] == []


def test_limited_read_across_targets_keeps_order(store) -> None:
    agent = _agent(store)
    _deliver(agent, "c1")
    _deliver(agent, "d1", target=AGENT_NICK)
    _deliver(agent, "c2")
    assert _texts(agent.read(limit=2)) == ["c1", "d1"]


def test_limited_read_across_targets_leaves_the_rest_unread(store) -> None:
    agent = _agent(store)
    _deliver(agent, "c1")
    _deliver(agent, "d1", target=AGENT_NICK)
    _deliver(agent, "c2")
    agent.read(limit=2)
    assert [(ln["target"], ln["text"]) for ln in agent.read()["lines"]] == [
        (GRANTED, "c2")
    ]


def test_dm_lines_are_labelled_dm(store) -> None:
    agent = _agent(store)
    _deliver(agent, "psst", target=AGENT_NICK)
    assert [ln["target"] for ln in agent.read()["lines"]] == [DM_TARGET]


# -- finding 3: concurrent tool calls each get their own result ----------------------


async def _two_concurrent_sends(agent: AgentSession) -> list:
    """A refused and an allowed send, both queued behind an in-flight command."""
    refused = ParsedCommand(type=CommandType.SEND, args=[UNGRANTED], text="no")
    allowed = ParsedCommand(type=CommandType.SEND, args=[GRANTED], text="yes")
    sent: list[str] = []

    async def send_raw(line: str) -> None:
        await asyncio.sleep(0)
        sent.append(line)

    agent.session._transport.send_raw = send_raw  # type: ignore[assignment]
    lock = agent.session._get_exec_lock()
    await lock.acquire()  # another command of this session is running
    try:
        first = asyncio.ensure_future(agent._execute(refused))
        second = asyncio.ensure_future(agent._execute(allowed))
        await asyncio.sleep(0.01)
    finally:
        lock.release()
    results = await asyncio.gather(first, second, return_exceptions=True)
    return [results, sent]


def test_concurrent_refused_call_gets_its_own_refusal(store) -> None:
    results, _sent = asyncio.run(_two_concurrent_sends(_agent(store)))
    assert "not permitted" in str(results[0])


def test_concurrent_allowed_call_is_not_charged_with_the_other_refusal(store) -> None:
    results, _sent = asyncio.run(_two_concurrent_sends(_agent(store)))
    assert results[1] is None


def test_concurrent_allowed_call_still_reaches_irc(store) -> None:
    _results, sent = asyncio.run(_two_concurrent_sends(_agent(store)))
    assert sent == [f"PRIVMSG {GRANTED} :yes"]


# -- finding 10: the owner lookup peeks, never scans ---------------------------------


def test_owner_lookup_peeks_the_humans_registry_key(store) -> None:
    agent = _agent(store)
    assert agent.is_owner(HUMAN_NICK)


def test_owner_lookup_uses_exactly_one_peek(store) -> None:
    agent = _agent(store)
    agent.is_owner(HUMAN_NICK)
    assert agent.app["registry"].peeks == [(MEMBER, BACKEND_MESH)]


def test_owner_is_nobody_while_the_human_is_offline(store) -> None:
    agent = _agent(store, human_online=False)
    assert not agent.is_owner(HUMAN_NICK)
