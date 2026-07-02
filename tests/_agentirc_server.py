"""Thin AgentIRC test server for HTTP e2e tests.

Spec / build-plan offered two paths for the HTTP e2e fixture:
(a) import a fixture from the ``culture`` package as a pinned dev
dep, or (b) carry a thin AgentIRC test server in this repo. We took
(b) — culture's ``culture/agentirc/ircd.py`` transitively imports
``virtual_client``, telemetry, skills, history-store, and protocol
modules that would balloon the test environment for a small number
of e2e cases. This module is ~120 lines and exists only for tests.

Underscore prefix keeps pytest from collecting it as a test module.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class _ReceivedLine:
    """A single line the lens sent to the server."""

    raw: str
    command: str
    params: list[str] = field(default_factory=list)
    trailing: str | None = None


def _parse_line(raw: str) -> _ReceivedLine:
    """Minimal IRC line parser — enough to pick out command + params.

    The lens never sends tagged lines or prefixes (it's a client),
    so we don't need ``Message.parse``'s full surface. Keeping this
    in-test instead of importing keeps the test fixture independent
    of the production parser's evolution.
    """
    line = raw.rstrip("\r\n")
    trailing: str | None = None
    if " :" in line:
        line, trailing = line.split(" :", 1)
    parts = line.split(" ") if line else []
    if not parts:
        return _ReceivedLine(raw=raw, command="", params=[], trailing=trailing)
    command = parts[0].upper()
    params = parts[1:]
    if trailing is not None:
        params = [*params, trailing]
    return _ReceivedLine(raw=raw, command=command, params=params, trailing=trailing)


class AgentIRCTestServer:
    """Bind ``127.0.0.1:0`` and behave just enough like an AgentIRC
    server to keep ``Session.connect`` happy and let tests assert on
    what the lens sent.

    Public surface:
      - ``host`` / ``port`` after :meth:`start`.
      - ``received: list[_ReceivedLine]`` — every line the lens sent
        across every connection (one server per test, so this is
        per-test state).
      - :meth:`start` / :meth:`stop` lifecycle.

    t5 additions (for the live-verb registry tools' e2e coverage): the
    server now completes registration (sends ``001 RPL_WELCOME`` right
    after ``USER``, so ``Session.connected``/``wait_for_welcome()`` work
    instead of hanging) and answers ``LIST``, ``WHO <channel>``, and
    ``HISTORY RECENT <channel> <limit>`` immediately from an in-memory
    per-channel membership map (updated by ``JOIN``/``PART``) rather than
    leaving those queries to time out after ``Session.QUERY_TIMEOUT``
    (10s). ``HISTORY`` always replies with zero entries — this fixture
    tracks membership, not message history — so ``read``'s e2e coverage
    proves the query round-trips, not that it returns real backlog.
    """

    def __init__(self) -> None:
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self.received: list[_ReceivedLine] = []
        self._server: asyncio.base_events.Server | None = None
        self._client_writers: list[asyncio.StreamWriter] = []
        self._nick: str | None = None  # captured from the first NICK line
        #: channel -> member nicks, updated on JOIN/PART. Backs the LIST
        #: and WHO responses so a lens that joins a channel then queries
        #: it (in the same or a later connection to this same server
        #: instance) sees consistent, immediate results.
        self.channel_members: dict[str, set[str]] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host=self.host, port=0
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Close client writers and AWAIT wait_closed so transports
        # don't end up half-closed (which trips "unclosed transport"
        # ResourceWarnings in pytest-asyncio teardown).
        writers = list(self._client_writers)
        self._client_writers.clear()
        for w in writers:
            try:
                w.close()
            except Exception as exc:
                logger.debug("test server: writer close ignored: %s", exc)
        if writers:
            await asyncio.gather(
                *(w.wait_closed() for w in writers),
                return_exceptions=True,
            )
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._client_writers.append(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                line = _parse_line(raw.decode("utf-8", errors="replace"))
                if line.command == "":
                    continue
                self.received.append(line)
                await self._respond(line, writer)
        except ConnectionResetError:
            return
        except asyncio.CancelledError:
            # Re-raise so task cancellation semantics propagate
            # cleanly through fixture teardown — matches the
            # transport's read-loop pattern.
            raise
        except Exception as exc:
            # Per-connection error — log and exit, don't take the
            # test server down for the next test.
            logger.debug("test server: connection error: %s", exc)
        finally:
            try:
                writer.close()
            except Exception as exc:
                logger.debug("test server: writer close in finally ignored: %s", exc)

    async def _respond(
        self, line: _ReceivedLine, writer: asyncio.StreamWriter
    ) -> None:
        if line.command == "NICK" and line.params:
            self._nick = line.params[0]
            return
        if line.command == "USER":
            await self._send_welcome(writer)
            return
        if line.command == "JOIN" and line.params:
            channel = line.params[0]
            self.channel_members.setdefault(channel, set()).add(self._nick or "lens")
            await self._echo_membership(writer, "JOIN", channel)
            return
        if line.command == "PART" and line.params:
            channel = line.params[0]
            self.channel_members.get(channel, set()).discard(self._nick or "lens")
            await self._echo_membership(writer, "PART", channel)
            return
        if line.command == "LIST":
            await self._respond_list(writer)
            return
        if line.command == "WHO" and line.params:
            await self._respond_who(writer, line.params[0])
            return
        if line.command == "HISTORY" and line.params:
            # Wire shape sent by `Session.history`: "HISTORY RECENT
            # <channel> <limit>" — the channel is the second param.
            channel = line.params[1] if len(line.params) > 1 else line.params[0]
            await self._respond_historyend(writer, channel)
            return
        # PRIVMSG / TOPIC / ICON / QUIT etc. — just record, no reply
        # needed: `Session` doesn't wait on any of these (fire-and-forget
        # sends), and real IRC daemons don't echo PRIVMSGs to the sender —
        # the lens publishes its own chat event from the local-echo path.

    async def _send_welcome(self, writer: asyncio.StreamWriter) -> None:
        """Complete registration: ``001 RPL_WELCOME`` right after ``USER``.

        Without this, ``IRCTransport.connected`` (and therefore
        ``Session.connected`` / ``wait_for_welcome()``) never becomes
        true — the pre-welcome guards on ``/channels``/``/who``/``/read``
        would reject every query, and ``wait_for_welcome()`` would raise
        after its 5s timeout.
        """
        nick = self._nick or "lens"
        line = f":test-agentirc 001 {nick} :Welcome to the AgentIRC test server, {nick}\r\n"
        try:
            writer.write(line.encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.debug("test server: welcome write failed: %s", exc)

    async def _echo_membership(
        self, writer: asyncio.StreamWriter, verb: str, channel: str
    ) -> None:
        """Send a server-confirmed JOIN/PART back to the lens.

        Format: ``:<nick>!<nick>@test JOIN :#channel`` — matches what
        a real ircd sends and what `Session.dispatch`'s JOIN/PART
        listener parses.
        """
        nick = self._nick or "lens"
        line = f":{nick}!{nick}@test {verb} :{channel}\r\n"
        try:
            writer.write(line.encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.debug("test server: echo write failed: %s", exc)

    async def _respond_list(self, writer: asyncio.StreamWriter) -> None:
        """Answer ``LIST`` with one ``322`` per known channel + ``323``.

        Format matches what ``Session._on_rpl_list``/``_on_rpl_listend``
        expect: ``322``'s second param is the channel name; ``323`` just
        needs to arrive to resolve the pending future.
        """
        nick = self._nick or "lens"
        try:
            for channel, members in sorted(self.channel_members.items()):
                writer.write(
                    f":test-agentirc 322 {nick} {channel} {len(members)} :\r\n".encode("utf-8")
                )
            writer.write(f":test-agentirc 323 {nick} :End of LIST\r\n".encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.debug("test server: LIST reply write failed: %s", exc)

    async def _respond_who(self, writer: asyncio.StreamWriter, target: str) -> None:
        """Answer ``WHO <target>`` with one ``352`` per member + ``315``.

        ``target`` is treated as a channel key into ``channel_members``;
        a bare-nick WHO for the connecting nick itself also resolves (a
        client is always "in" its own WHO). Unknown targets get zero
        ``352`` rows and just the ``315`` terminator — matching a real
        ircd's empty-result shape.
        """
        nick = self._nick or "lens"
        members = self.channel_members.get(target, set())
        if not members and target == nick:
            members = {nick}
        try:
            for member in sorted(members):
                writer.write(
                    (
                        f":test-agentirc 352 {nick} {target} {member} test-host "
                        f"test-agentirc {member} H :0 {member}\r\n"
                    ).encode("utf-8")
                )
            writer.write(f":test-agentirc 315 {nick} {target} :End of WHO\r\n".encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.debug("test server: WHO reply write failed: %s", exc)

    async def _respond_historyend(self, writer: asyncio.StreamWriter, channel: str) -> None:
        """Answer ``HISTORY RECENT <channel> ...`` with an immediate,
        empty-backlog ``HISTORYEND`` — this fixture tracks channel
        membership, not message history, so there is nothing to replay.
        Answering immediately (rather than not at all) is what matters:
        it keeps ``Session.history``'s ``QUERY_TIMEOUT`` wait from ever
        firing, which would otherwise stall e2e ``/join`` and ``read``
        calls by a full 10 seconds now that ``001`` makes the pre-welcome
        guards pass.
        """
        try:
            writer.write(f":test-agentirc HISTORYEND {channel} :End of HISTORY\r\n".encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.debug("test server: HISTORYEND write failed: %s", exc)


class ThreadedAgentIRCTestServer:
    """Run an :class:`AgentIRCTestServer` on a background thread with its
    own event loop.

    ``conftest.py``'s ``agentirc_server``/``lens_session`` fixtures are
    ``pytest_asyncio`` fixtures tied to the *same* loop the test coroutine
    runs on — ideal for async tests, but useless for a test that must
    drive a **sync** entry point (``agentfront.testing.run_cli`` /
    ``irc_lens.cli.main``) whose t5 live-verb tools each open a fresh
    event loop via ``asyncio.run()`` per call (see
    ``irc_lens/tools.py``'s module docstring). ``asyncio.run()`` cannot
    be called from a thread that already has a running loop, so a sync
    CLI-surface test needs the fake server on a *different* thread/loop
    than the one the test body's own ``asyncio.run()`` calls create.
    """

    def __init__(self) -> None:
        self.server = AgentIRCTestServer()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = False

    def start(self) -> None:
        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.run_until_complete(self.server.start())
            self._ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("fake AgentIRC server thread failed to start")

    def stop(self) -> None:
        """Idempotent — safe to call directly in a test body (e.g. to make
        the server's port refuse connections) AND from fixture teardown."""
        if self._stopped or self._loop is None or self._thread is None:
            return
        self._stopped = True
        fut = asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop)
        fut.result(timeout=5.0)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)

    @property
    def host(self) -> str:
        return self.server.host

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def received(self) -> list[_ReceivedLine]:
        """A snapshot copy — safe to iterate from the main thread while
        the server thread may still be appending to the live list."""
        return list(self.server.received)

    @property
    def channel_members(self) -> dict[str, set[str]]:
        return self.server.channel_members
