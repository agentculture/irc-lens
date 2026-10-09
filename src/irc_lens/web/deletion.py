"""Guest self-service data deletion (task t11, obligation o13).

``GET /delete`` -> email form; ``POST /delete/request`` mails a fresh
single-use token (``purpose="delete"``, the same template and adapter as
entry); ``POST /delete/confirm`` re-verifies that token and then erases
the guest's profile, recorded messages, consents and uploads, closes
their sandbox session and clears their cookie. Only the deletion record
(email, time, count) remains.

The request step answers identically whether or not the email belongs to
a guest, so the page cannot be used to probe who has used the service.
Every route is anonymous-allowed and 404 while guest mode is off.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from irc_lens import metrics
from irc_lens.mail import render_token_email
from irc_lens.web import csrf
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.entry import (
    ENTRY_PAGE_MARKER,
    ERR_CODE,
    TOKEN_REQUEST_WINDOW_S,
    TOKEN_VERIFY_WINDOW_S,
    _norm_email,
    _over_limit,
    _plausible_email,
    _state,
    client_ip,
)
from irc_lens.web.render import render_fragment, static_url
from irc_lens.web.sessions import BACKEND_SANDBOX, GUEST_PRINCIPAL_PREFIX

logger = logging.getLogger(__name__)

TOKEN_PURPOSE = "delete"


def install(app: web.Application) -> None:
    app.router.add_get("/delete", get_delete)
    app.router.add_post("/delete/request", post_request)
    app.router.add_post("/delete/confirm", post_confirm)


def _page(step: str, *, status: int = 200, email: str = "", error: str = ""):
    html = render_fragment(
        "delete.html.j2",
        step=step,
        email=email,
        error=error,
        css_url=static_url("entry.css"),
    )
    resp = web.Response(text=html, content_type="text/html", status=status)
    resp.headers["Cache-Control"] = "no-store"
    resp[ENTRY_PAGE_MARKER] = True
    return resp


@allows_anonymous
async def get_delete(request: web.Request) -> web.Response:  # NOSONAR S7503
    _state(request)
    return _page("email")


@allows_anonymous
async def post_request(request: web.Request) -> web.Response:
    state = _state(request)
    cfg = state.config
    form = await request.post()
    email = _norm_email(form.get("email"))
    ip = client_ip(request)
    store = state.get_store()
    limited = _over_limit(
        store,
        "delete",
        email,
        ip,
        limit=cfg.guest_rate_entry_per_min,
        window=TOKEN_REQUEST_WINDOW_S,
    )
    if limited:
        metrics.get_metrics().rate_limited()
    elif _plausible_email(email) and store.get_guest(email):
        token = store.issue_token(email, purpose=TOKEN_PURPOSE)
        subject, body = render_token_email(token)
        try:
            await asyncio.to_thread(state.get_mailer().send, email, subject, body)
        except Exception as exc:  # noqa: BLE001 -- same page either way
            logger.warning("deletion token mail not sent: %s", type(exc).__name__)
    return _page("code", status=429 if limited else 200, email=email)


@allows_anonymous
async def post_confirm(request: web.Request) -> web.Response:
    state = _state(request)
    cfg = state.config
    form = await request.post()
    email = _norm_email(form.get("email"))
    code = str(form.get("code") or "").strip()
    store = state.get_store()
    limited = _over_limit(
        store,
        "delete-verify",
        email,
        client_ip(request),
        limit=cfg.guest_rate_password_attempts_per_15min,
        window=TOKEN_VERIFY_WINDOW_S,
    )
    if limited:
        metrics.get_metrics().rate_limited()
        return _page("code", status=429, email=email, error=ERR_CODE)
    if not code or not store.verify_token(email, code, purpose=TOKEN_PURPOSE):
        return _page("code", status=401, email=email, error=ERR_CODE)

    await request.app["registry"].close(
        f"{GUEST_PRINCIPAL_PREFIX}{email}", BACKEND_SANDBOX
    )
    media = request.app.get("media_store")
    if media is not None:
        await asyncio.to_thread(media.delete_principal, email)
    await asyncio.to_thread(store.delete_guest_inputs, email)
    resp = _page("done")
    resp.del_cookie(csrf.GUEST_COOKIE_NAME, path="/")
    return resp
