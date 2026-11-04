"""One fail-closed HTTP gate for the standby replica.

Keep authentication bookkeeping local: approved sign-in codes, rate limits,
trusted browsers and session rows may change. Account data may not. New unsafe
routes are refused until explicitly reviewed here; tests inventory the router.
"""
from __future__ import annotations

import re

from aiohttp import web

from irc_lens.web import app_session
from irc_lens.web.mcp_http import is_mcp_request

BANNER_MESSAGE = "Standby mode: agents and spark media are unavailable."
OUTAGE_MESSAGE = (
    "Standby mode: guest access and account changes are temporarily unavailable. "
    "Approved users can still sign in and chat."
)

# Traced from access_store.py and guest_store.py: users/roles, grants,
# nickname reservations, extensions/credentials, guests/consents, password
# changes and deletion (including the tokens authorizing those changes).
ACCOUNT_CHANGE_PATHS = frozenset({
    "/admin/users", "/admin/users/remove", "/admin/users/nickname",
    "/admin/grants", "/admin/extension", "/nickname", "/agent-credential",
    "/entry/welcome", "/entry/guest/start", "/entry/verify",
    "/delete/request", "/delete/confirm", "/password", "/password/{token}",
})
_SIGNIN_PATHS = frozenset({"/entry/signin", "/entry/code"})
_LOCAL_POST_PATHS = _SIGNIN_PATHS | {
    "/entry/email", "/input", "/logout", "/guest/logout",
}
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _unavailable() -> web.Response:
    return web.Response(
        text=OUTAGE_MESSAGE, status=503,
        headers={"Cache-Control": "no-store", "Retry-After": "300"},
    )


async def _refused(request: web.Request) -> bool:
    if is_mcp_request(request) or request.path == "/agent-credential":
        return True
    if request.method in _SAFE_METHODS:
        return False
    if request.method != "POST" or request.path not in _LOCAL_POST_PATHS:
        return True
    if request.path in _SIGNIN_PATHS:
        form = await request.post()
        email = str(form.get("email") or "").strip().lower()[:254]
        return app_session.approved_user(request.app, email) is None
    return False


def _banner(response: web.StreamResponse) -> None:
    if not isinstance(response, web.Response) or response.prepared:
        return
    if response.content_type != "text/html" or not response.text:
        return
    banner = f'<div role="status" class="lens-banner" data-testid="standby-banner">{BANNER_MESSAGE}</div>'
    response.text = re.sub(r"(<body\b[^>]*>)", lambda m: m[0] + banner,
                           response.text, count=1, flags=re.IGNORECASE)
    response.headers["Cache-Control"] = "no-store"


@web.middleware
async def standby_middleware(request: web.Request, handler):
    """Wrap authentication too, before a blocked route can write anything."""
    if not request.app["config"].standby_enabled:
        return await handler(request)
    if await _refused(request):
        return _unavailable()
    response = await handler(request)
    _banner(response)
    return response
