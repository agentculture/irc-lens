"""Set or reset an approved user's password by emailed link (task t6).

``GET /password`` -> email form. ``POST /password`` always answers with the
same "check your email" page; only an address in ``auth.allowed_emails``
gets a single-use link (token purpose ``setpw``, 30 minutes) to
``/password/<token>``. The token is issued and the mail sent in a background
task, so the response time does not reveal whether the address is approved.

``GET /password/<token>`` shows the new-password form and never consumes the
token (mail scanners prefetch links). ``POST /password/<token>`` re-checks the
token without consuming it, refuses passwords under 12 characters (the token
stays usable), then consumes the token, stores an argon2id hash and ends every
app session of that email (closing their live IRC sessions).

The link's base comes only from config -- ``auth.app_signin.base_url``, else
``media.public_base_url`` -- never from the request's Host header. With
neither set the request page is unchanged but nothing is sent and one error
is logged. Tokens, links and passwords are never logged; a filter on the
loggers that write request paths (``aiohttp.access``, auth, CSRF) redacts
``/password/<token>``.

Every route is anonymous-allowed and only installed while guest mode and
app sign-in are on; POSTs pass the usual CSRF/Origin middleware.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiohttp import web

from irc_lens import metrics
from irc_lens._errors import AfiError
from irc_lens.guest_store import TOKEN_TTLS
from irc_lens.mail import render_link_email, send_with_alert
from irc_lens.web.app_session import end_sessions_for_email
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.entry import (
    ENTRY_PAGE_MARKER,
    ERR_LATER,
    TOKEN_REQUEST_WINDOW_S,
    TOKEN_VERIFY_WINDOW_S,
    _norm_email,
    _over_limit,
    _plausible_email,
    _state,
    client_ip,
)
from irc_lens.web.render import render_fragment, static_url

logger = logging.getLogger(__name__)

TOKEN_PURPOSE = "setpw"
TOKEN_TTL_S = TOKEN_TTLS[TOKEN_PURPOSE]
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 1024

ERR_SHORT = f"Use at least {MIN_PASSWORD_LEN} characters."
ERR_LONG = f"Use at most {MAX_PASSWORD_LEN} characters."
ERR_MISMATCH = "The passwords don't match."

#: Background link-mail tasks (kept referenced until done).
MAIL_TASKS = web.AppKey("setpw_mail_tasks", set)

# secrets.token_urlsafe(32) output; anything else is not one of our tokens.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_LINK_RE = re.compile(r"/password/[A-Za-z0-9_-]+")


class RedactTokenFilter(logging.Filter):
    """Replace ``/password/<token>`` in a log line with a placeholder."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/password/" in msg:
            record.msg = _LINK_RE.sub("/password/[redacted]", msg)
            record.args = None
        return True


#: Loggers that write request paths (access lines, auth and CSRF decisions).
PATH_LOGGERS = (
    "aiohttp.access",
    "irc_lens.web.auth",
    "irc_lens.web.csrf",
    "irc_lens.web.routes",
)


def _install_log_filters() -> None:
    for name in PATH_LOGGERS:
        log = logging.getLogger(name)
        if not any(isinstance(f, RedactTokenFilter) for f in log.filters):
            log.addFilter(RedactTokenFilter())


def link_base_url(cfg) -> str | None:
    """Base of set-password links: config only, never the Host header."""
    return (
        getattr(cfg, "app_signin_base_url", None)
        or getattr(cfg, "media_public_base_url", "")
        or None
    )


def install(app: web.Application) -> None:
    app[MAIL_TASKS] = set()
    _install_log_filters()
    app.router.add_get("/password", get_password)
    app.router.add_post("/password", post_password)
    app.router.add_get("/password/{token}", get_token)
    app.router.add_post("/password/{token}", post_token)


def _page(step: str, *, status: int = 200, token: str = "", error: str = ""):
    html = render_fragment(
        "setpw.html.j2",
        step=step,
        token=token,
        error=error,
        min_len=MIN_PASSWORD_LEN,
        max_len=MAX_PASSWORD_LEN,
        css_url=static_url("entry.css"),
    )
    resp = web.Response(text=html, content_type="text/html", status=status)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp[ENTRY_PAGE_MARKER] = True
    return resp


def _invalid() -> web.Response:
    return _page("invalid", status=400)


async def _send_link(state, email: str) -> None:
    """Issue a setpw token for *email* and mail its link (background)."""
    base = link_base_url(state.config)
    if not base:
        logger.error(
            "set-password link not sent: neither auth.app_signin.base_url "
            "nor media.public_base_url is configured"
        )
        return
    store = state.get_store()
    try:
        token = await asyncio.to_thread(store.issue_token, email, purpose=TOKEN_PURPOSE)
        subject, body = render_link_email(base, token, ttl_s=TOKEN_TTL_S)
    except AfiError:
        logger.error(
            "set-password link not sent: the configured base_url is not "
            "https (http only for 127.0.0.1/localhost)"
        )
        return
    try:
        await asyncio.to_thread(
            send_with_alert, state.get_mailer(), state.alerter, email, subject, body
        )
    except Exception as exc:  # noqa: BLE001 -- the visitor already got the page
        logger.warning("set-password mail not sent: %s", type(exc).__name__)


@allows_anonymous
async def get_password(request: web.Request) -> web.Response:  # NOSONAR S7503
    _state(request)
    return _page("request")


@allows_anonymous
async def post_password(request: web.Request) -> web.Response:
    """Same page for every address; only an approved one gets a link."""
    state = _state(request)
    cfg = state.config
    form = await request.post()
    email = _norm_email(form.get("email"))
    store = state.get_store()
    limited = _over_limit(
        store,
        "setpw",
        email,
        client_ip(request),
        limit=cfg.guest_rate_entry_per_min,
        window=TOKEN_REQUEST_WINDOW_S,
    )
    if limited:
        metrics.get_metrics().rate_limited()
    elif _plausible_email(email) and email in request.app["config"].allowed_emails:
        tasks = request.app[MAIL_TASKS]
        task = asyncio.create_task(_send_link(state, email))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return _page("sent", status=429 if limited else 200)


def _live_email(request: web.Request, store, token: str) -> str | None:
    """Email of a live setpw *token* that may still sign in; never consumes."""
    if not _TOKEN_RE.fullmatch(token):
        return None
    email = store.peek_token(token, purpose=TOKEN_PURPOSE)
    if email is None or email not in request.app["config"].allowed_emails:
        return None
    return email


@allows_anonymous
async def get_token(request: web.Request) -> web.Response:  # NOSONAR S7503
    """The new-password form; a GET never uses the token up (c30)."""
    state = _state(request)
    token = request.match_info["token"]
    if _live_email(request, state.get_store(), token) is None:
        return _invalid()
    return _page("form", token=token)


@allows_anonymous
async def post_token(request: web.Request) -> web.Response:
    state = _state(request)
    cfg = state.config
    token = request.match_info["token"]
    store = state.get_store()
    key = f"i:{client_ip(request)}"
    limited = store.rate_limited(
        "setpw-submit",
        key,
        limit=cfg.guest_rate_password_attempts_per_15min,
        window=TOKEN_VERIFY_WINDOW_S,
    )
    store.record_attempt("setpw-submit", key)
    email = _live_email(request, store, token)
    if email is None:
        return _invalid()
    if limited:
        metrics.get_metrics().rate_limited()
        return _page("form", status=429, token=token, error=ERR_LATER)

    form = await request.post()
    password = str(form.get("password") or "")
    confirm = str(form.get("confirm") or "")
    error = ""
    if len(password) < MIN_PASSWORD_LEN:
        error = ERR_SHORT
    elif len(password) > MAX_PASSWORD_LEN:
        error = ERR_LONG
    elif password != confirm:
        error = ERR_MISMATCH
    if error:
        return _page("form", status=400, token=token, error=error)

    if store.consume_token(token, purpose=TOKEN_PURPOSE) != email:
        return _invalid()
    await asyncio.to_thread(store.set_password, email, password)
    ended = await end_sessions_for_email(request.app, email)
    logger.info("password set principal=%s sessions_ended=%d", email, ended)
    return _page("done")
