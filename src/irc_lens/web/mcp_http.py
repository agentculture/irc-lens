"""Remote MCP endpoint for the lens, via agentfront's aiohttp transport.

This is a thin mount: the MCP protocol, session handling and the 401 refusal
are agentfront's (:func:`agentfront.mcp_http.mount_aiohttp`); the lens
contributes only *where* it is mounted (``LensConfig.mcp_path``) and *who may
call* (the authenticator callback, which returns a caller principal or
``None``). The principal reaches tool handlers through
:func:`agentfront.current_caller`.

The authenticator runs before any command. A refusing (or raising)
authenticator is answered 401 and nothing executes. Wiring this into
``make_app`` is deliberately not done here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentfront.mcp_http import Authenticator, McpHttpApp, mount_aiohttp

if TYPE_CHECKING:
    from agentfront.app import App
    from aiohttp import web

    from irc_lens.config import LensConfig


def mount_mcp(
    web_app: "web.Application",
    front: "App",
    config: "LensConfig",
    *,
    authenticate: Authenticator,
) -> McpHttpApp:
    """Mount *front*'s remote MCP transport on *web_app* at ``config.mcp_path``.

    *authenticate* is required (there is no unauthenticated mount): it takes an
    ``agentfront.mcp_http.CallerRequest`` (sync or async) and returns the
    caller principal, or ``None`` to refuse with 401. Call before the aiohttp
    application starts serving.
    """
    return mount_aiohttp(
        web_app,
        front,
        path=config.mcp_path,
        authenticate=authenticate,
    )
