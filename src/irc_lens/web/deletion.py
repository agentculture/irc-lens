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
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from irc_lens import metrics
from irc_lens.corpus import anonymized_pairs
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


def purge_flag_log(cfg, nicks: set[str]) -> None:
    """Drop a deleted guest's lines from sbx-ask's flag log (d7).

    The sandbox IRCd itself keeps no history on disk (culture server
    ``--no-persist``, d8); the guest store is the durable record and is
    erased by :meth:`GuestStore.delete_guest_inputs`.
    """
    if not cfg.guest_sandbox_flag_log:
        logger.warning("guest deletion: guest_mode.sandbox.flag_log unset; flag lines kept")
        return
    path = Path(cfg.guest_sandbox_flag_log)
    if not path.exists():
        return
    kept = []
    for line in path.read_text().splitlines(keepends=True):
        try:
            nick = json.loads(line).get("nick")
        except ValueError:
            nick = None
        if nick not in nicks:
            kept.append(line)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(kept))
    os.replace(tmp, path)


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
    nicks = {nick for _e, nick, _ip in store.get_guest(email)}
    # d8: keep only Q&A that cannot reasonably identify the guest, then erase.
    pairs = await asyncio.to_thread(anonymized_pairs, store, email)
    await asyncio.to_thread(store.keep_corpus, pairs)
    await asyncio.to_thread(purge_flag_log, cfg, nicks)
    await asyncio.to_thread(store.delete_guest_inputs, email)
    resp = _page("done")
    resp.del_cookie(csrf.GUEST_COOKIE_NAME, path="/")
    return resp
