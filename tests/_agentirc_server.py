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
    """

    def __init__(self) -> None:
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self.received: list[_ReceivedLine] = []
        self._server: asyncio.base_events.Server | None = None
        self._client_writers: list[asyncio.StreamWriter] = []
        self._nick: str | None = None  # captured from the first NICK line

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host=self.host, port=0
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Close client writers first so per-connection coroutines
        # exit cleanly; then close the listening socket.
        for w in self._client_writers:
            try:
                w.close()
            except Exception as exc:
                logger.debug("test server: writer close ignored: %s", exc)
        self._client_writers.clear()
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
        except (ConnectionResetError, asyncio.CancelledError):
            return
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
            return
        if line.command == "JOIN" and line.params:
            await self._echo_membership(writer, "JOIN", line.params[0])
            return
        if line.command == "PART" and line.params:
            await self._echo_membership(writer, "PART", line.params[0])
            return
        # PRIVMSG / TOPIC / QUIT etc. — just record, no echo. Real
        # IRC daemons don't echo PRIVMSGs to the sender; the lens
        # publishes its own chat event from the local-echo path.
        return

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
