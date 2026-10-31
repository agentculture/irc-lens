"""Remote MCP transport mounted into the lens's aiohttp stack (task t2).

Uses the official ``mcp`` SDK client against a real aiohttp ``TestServer``,
so initialize / list_tools / call_tool cross a real socket and the whole
agentfront ASGI-over-aiohttp bridge. All host names are example placeholders.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from agentfront import current_caller
from agentfront.app import App
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from irc_lens.config import load_config
from irc_lens.web.mcp_http import mount_mcp

MCP_PATH = "/agents/mcp-test"


def _front() -> App:
    app = App(name="mcp-test", version="0.0.0", description="mcp http test app")
    calls: list[str] = []

    @app.tool
    def whoami() -> dict:
        """Report the authenticated caller principal."""
        calls.append("whoami")
        return {"caller": current_caller()}

    app.calls = calls  # type: ignore[attr-defined]
    return app


def _config(path: str | None = None):
    cfg = load_config(_write_cfg(path))
    return cfg


def _write_cfg(mcp_path: str | None) -> Path:
    import tempfile

    web_block = "web:\n  port: 8765\n"
    if mcp_path is not None:
        web_block += f"  mcp_path: {mcp_path}\n"
    text = (
        "auth:\n  mode: dev\n  dev:\n    nick: tester\n    email: tester@example.com\n"
        "server:\n  name: spark\n  host: irc.example.org\n  port: 6667\n" + web_block
    )
    p = Path(tempfile.mkdtemp()) / "config.yaml"
    p.write_text(text)
    return p


async def _serve(front: App, authenticate, path: str = MCP_PATH):
    cfg = dataclasses.replace(_config(), mcp_path=path)
    web_app = web.Application()
    mount_mcp(web_app, front, cfg, authenticate=authenticate)
    server = TestServer(web_app)
    await server.start_server()
    return server


async def test_stock_client_initialize_list_and_call():
    front = _front()
    server = await _serve(front, lambda req: "principal-a")
    try:
        url = str(server.make_url(MCP_PATH))
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [t.name for t in tools.tools] == ["run"]
                result = await session.call_tool(
                    "run", {"command": ["whoami"], "args": {}}
                )
                payload = json.loads(result.content[0].text)
        assert payload == {"result": {"caller": "principal-a"}}
    finally:
        await server.close()


async def test_authenticator_receives_request_and_principal_reaches_tool():
    seen = []

    async def authenticate(req):
        seen.append((req.method, req.path, req.headers.get("authorization")))
        return {"email": "agent@example.com"}

    front = _front()
    server = await _serve(front, authenticate)
    try:
        url = str(server.make_url(MCP_PATH))
        async with streamablehttp_client(
            url, headers={"Authorization": "Bearer t"}
        ) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(
                    "run", {"command": ["whoami"], "args": {}}
                )
        assert json.loads(result.content[0].text) == {
            "result": {"caller": {"email": "agent@example.com"}}
        }
        assert seen
        assert all(p == MCP_PATH and a == "Bearer t" for _, p, a in seen)
    finally:
        await server.close()


@pytest.mark.parametrize("mode", ["none", "raise"])
async def test_rejecting_authenticator_gives_401_and_runs_no_tool(mode):
    def authenticate(req):
        if mode == "raise":
            raise RuntimeError("boom")
        return None

    front = _front()
    server = await _serve(front, authenticate)
    try:
        import aiohttp

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run", "arguments": {"command": ["whoami"], "args": {}}},
        }
        async with aiohttp.ClientSession() as http:
            async with http.post(
                server.make_url(MCP_PATH),
                json=body,
                headers={"Accept": "application/json, text/event-stream"},
            ) as resp:
                assert resp.status == 401
                data = await resp.json()
        assert data["error"]["code"] == 401
        assert front.calls == []
    finally:
        await server.close()


async def test_mount_path_comes_from_config():
    front = _front()
    server = await _serve(front, lambda req: "p", path="/elsewhere/rpc")
    try:
        import aiohttp

        async with aiohttp.ClientSession() as http:
            async with http.post(server.make_url(MCP_PATH), json={}) as resp:
                assert resp.status == 404
            async with http.post(server.make_url("/elsewhere/rpc"), json={}) as resp:
                assert resp.status != 404
    finally:
        await server.close()


def test_config_mcp_path_default_and_override():
    assert _config().mcp_path == "/mcp"
    assert _config("/custom/mcp").mcp_path == "/custom/mcp"


@pytest.mark.parametrize("path", ["/mcp/{x}", "/{tail:.*}", "/m{cp", "/mc}p"])
def test_config_mcp_path_refuses_dynamic_segments(path: str):
    """Low finding: aiohttp reads ``{...}`` in a route as a dynamic segment,
    which would widen the MCP mount; web.mcp_path must refuse braces."""
    from irc_lens._errors import AfiError

    with pytest.raises(AfiError):
        _config(f"'{path}'")


def test_module_has_no_host_or_address_literal():
    import irc_lens.web.mcp_http as mod

    src = Path(mod.__file__).read_text()
    for needle in (
        "127.0.0.1",
        "0.0.0.0",
        "localhost",
        "http://",
        "https://",
        ".com",
        ".org",
        ".dev",
    ):
        assert needle not in src, needle
