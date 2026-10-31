"""Remote MCP endpoint for the lens, via agentfront's aiohttp transport.

This is a thin mount: the MCP protocol, session handling and the 401 refusal
are agentfront's (:func:`agentfront.mcp_http.mount_aiohttp`); the lens
contributes only *where* it is mounted (``LensConfig.mcp_path``) and *who may
call* (the authenticator callback, which returns a caller principal or
``None``). The principal reaches tool handlers through
:func:`agentfront.current_caller`.

The authenticator runs before any command. A refusing (or raising)
authenticator is answered 401 and nothing executes. ``make_app`` mounts it
through :func:`irc_lens.web.agent_auth.install` (with the agent-credential
authenticator) while ``auth.email_permissions.enabled`` is on.

The routes this mount adds are recorded on the host app under
:data:`MCP_ROUTES`. The browser identity middlewares (``auth.py``, the dev
middleware in ``app.py``) skip a request only when it *resolved to one of
those routes* (:func:`is_mcp_request`) -- so a bearer request reaches the
authenticator, while no other path (not even a look-alike prefix) is
exempt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from agentfront.app import App
    from agentfront.mcp_http import Authenticator, McpHttpApp

    from irc_lens.config import LensConfig

#: The aiohttp routes :func:`mount_mcp` added (the mount path and its
#: trailing-slash form). Absent when no MCP endpoint is mounted.
MCP_ROUTES = web.AppKey("mcp_routes", frozenset)


def is_mcp_request(request: "web.Request") -> bool:
    """True iff *request* was routed to the mounted MCP endpoint."""
    routes = request.app.get(MCP_ROUTES)
    if not routes:
        return False
    return getattr(request.match_info, "route", None) in routes


def mount_mcp(
    web_app: "web.Application",
    front: "App",
    config: "LensConfig",
    *,
    authenticate: "Authenticator",
) -> "McpHttpApp":
    """Mount *front*'s remote MCP transport on *web_app* at ``config.mcp_path``.

    *authenticate* is required (there is no unauthenticated mount): it takes an
    ``agentfront.mcp_http.CallerRequest`` (sync or async) and returns the
    caller principal, or ``None`` to refuse with 401. Call before the aiohttp
    application starts serving. The added routes are recorded under
    :data:`MCP_ROUTES`.
    """
    # Imported here, not at module load: the browser identity middlewares
    # import this module for :func:`is_mcp_request` alone.
    from agentfront.mcp_http import mount_aiohttp

    before = set(web_app.router.routes())
    mounted = mount_aiohttp(
        web_app,
        front,
        path=config.mcp_path,
        authenticate=authenticate,
    )
    web_app[MCP_ROUTES] = frozenset(set(web_app.router.routes()) - before)
    return mounted
