"""Local IRC PING/PONG listener for readiness integration tests."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace


@asynccontextmanager
async def listener(port=0, reply=b":spark PONG spark readyz\r\n"):
    connections = []
    closed = asyncio.Event()

    async def accept(reader, writer):
        connections.append(writer)
        try:
            assert await reader.readline() == b"PING :readyz\r\n"
            if reply is not None:
                writer.write(reply)
                await writer.drain()
            if reply != b"":
                assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(accept, "127.0.0.1", port)
    state = SimpleNamespace(
        server=server, port=server.sockets[0].getsockname()[1],
        connections=connections, closed=closed,
    )
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        for writer in connections:
            writer.close()
            await writer.wait_closed()


