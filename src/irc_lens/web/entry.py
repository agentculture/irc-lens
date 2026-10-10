"""Entry card for chat.culture.dev guest mode (task t9).

The page an anonymous visitor lands on: email -> one password window with
two ways forward, **Sign in** (approved users) or **Guest mode** (the
sandbox). Steps, each a plain HTML form POST (no JavaScript of ours, so the
CSP keeps ``script-src 'self'``):

1. ``GET /entry`` (:func:`get_entry`; the routing task also serves it on
   ``/`` for anonymous visitors) -- Email + Continue.
2. ``POST /entry/email`` -- the password window. Identical for every
   address (approved, unknown, malformed): the email is only echoed back.
3. ``POST /entry/signin`` -- approved email + correct password -> 303
   ``/login`` (Cloudflare Access takes over). Every failure -- unknown
   email, wrong password, failed bot check, rate limit -- renders the one
   error ``Email or password is wrong`` with the same status and body, and
   every attempt is padded to a fixed time floor after a real or dummy
   argon2 verify, so approved and unknown emails are indistinguishable
   (obligation o4). Over the per-email / per-IP limit the same body comes
   back as 429.
4. ``POST /entry/guest`` -- nickname (fixed ``sbx-`` prefix) + consent to
   the Terms and Privacy Policy, plus a separate, optional, unchecked
   training opt-in (d9) carried through the code step as a hidden field.
5. ``POST /entry/guest/start`` -- issues a single-use 15-minute token and
   mails it with the one fixed template (o5); the code-entry step renders
   the same way whether or not mail went out (banned / malformed address,
   provider failure). Rate limited per email and per IP (429, same body).
6. ``POST /entry/verify`` -- token check (single use, 15 min, rate limited
   per email and per IP, o6); on success records the guest
   (``sbx-<nickname>``, never derived from the email) and its consent
   against the current legal versions (with the training opt-in, default
   off), sets the signed ``lens_guest``
   cookie and 303s to ``/``.

Every route is :func:`~irc_lens.web.auth.allows_anonymous` and answers 404
while ``guest_mode.enabled`` is false. ``csrf_middleware`` already guards
the POSTs.

Bot protection is pluggable (:class:`BotVerifier`): Cloudflare Turnstile
when ``IRC_LENS_TURNSTILE_SITE_KEY`` and ``IRC_LENS_TURNSTILE_SECRET`` are
both set in the environment, a no-op otherwise. Turnstile needs its
loader script and iframe from ``challenges.cloudflare.com``; only the
responses that render the widget get a CSP widened to that one origin
(marked with :data:`CSP_TURNSTILE_MARKER`, applied in ``web.app``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol

import aiohttp
from aiohttp import web
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from irc_lens import legal, metrics
from irc_lens.config import LensConfig, _default_guest_store_path
from irc_lens.guest_store import GuestStore
from irc_lens.alerts import Alerter, make_alerter
from irc_lens.mail import (
    PURPOSE_SIGNIN,
    MailAdapter,
    make_adapter,
    render_signin_notice,
    render_token_email,
    send_with_alert,
)
from irc_lens.web import app_session, csrf
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.render import render_fragment, static_url
from irc_lens.web.sessions import GUEST_PRINCIPAL_PREFIX

logger = logging.getLogger(__name__)

ENTRY_STATE = web.AppKey("entry_state", object)

#: Minimum wall time of every sign-in response (o4). An argon2id verify is
#: ~50 ms here; the floor absorbs it and any allowlist/DB branch.
SIGNIN_FLOOR_S = 0.5
SIGNIN_WINDOW_S = 900
TOKEN_REQUEST_WINDOW_S = 60
TOKEN_VERIFY_WINDOW_S = 900
TOKEN_PURPOSE = "guest"
SIGNIN_CODE_PURPOSE = PURPOSE_SIGNIN
#: App sign-in, untrusted browsers: password submissions and code entries
#: from one IP, counted together, per 15 minutes (r6/c38). Separate from
#: ``rate_limits.password_attempts_per_15min``, which guests and the 0.12.2
#: rollback path keep using.
SIGNIN_IP_ATTEMPTS_PER_15MIN = 3
SIGNIN_IP_KIND = "signin-ip"
NICK_PREFIX = "sbx-"
NICK_MIN, NICK_MAX = 2, 16
#: Nicknames a guest may never take (``sbx-ask`` is the sandbox agent).
RESERVED_NICKNAMES = frozenset({"ask"})
MAX_EMAIL_LEN = 254

ERR_SIGNIN = "Email or password is wrong"
ERR_CODE = "Wrong or expired code"
ERR_NICK_TAKEN = "Nickname taken"
ERR_NICK_INVALID = "Invalid nickname"
ERR_CONSENT = "Consent required"
ERR_BOT = "Verification failed"
ERR_LATER = "Try again later"
#: Shown while guest_mode.max_guests guests are active (c24); exact wording.
MSG_BUSY = "The sandbox is busy. Try again in a few minutes."

TERMS_URL = "https://culture.dev/terms"
PRIVACY_URL = "https://culture.dev/privacy"

TURNSTILE_SITE_KEY_ENV = "IRC_LENS_TURNSTILE_SITE_KEY"
TURNSTILE_SECRET_ENV = "IRC_LENS_TURNSTILE_SECRET"
TURNSTILE_ORIGIN = "https://challenges.cloudflare.com"
TURNSTILE_SCRIPT_URL = f"{TURNSTILE_ORIGIN}/turnstile/v0/api.js"
TURNSTILE_VERIFY_URL = f"{TURNSTILE_ORIGIN}/turnstile/v0/siteverify"
TURNSTILE_FIELD = "cf-turnstile-response"
#: Response-local flag (``response[CSP_TURNSTILE_MARKER] = True``) asking the
#: security-headers middleware for the Turnstile-widened CSP.
CSP_TURNSTILE_MARKER = "irc_lens_csp_turnstile"
#: Response-local flag marking an entry-card page. Its forms POST same-origin
#: as plain navigations, and under the global ``Referrer-Policy: no-referrer``
#: browsers send ``Origin: null`` on those, which the CSRF Origin floor (rightly)
#: refuses. Entry pages therefore get ``Referrer-Policy: same-origin``: the real
#: Origin on same-origin POSTs, still nothing to any other site.
ENTRY_PAGE_MARKER = "irc_lens_entry_page"


# ---------------------------------------------------------------------------
# Bot protection
# ---------------------------------------------------------------------------


class BotVerifier(Protocol):
    #: Public widget key to render, or None when no widget is needed.
    site_key: str | None

    async def verify(self, token: str, ip: str) -> bool: ...


class NoopVerifier:
    """No bot check configured: every request passes."""

    site_key = None

    async def verify(self, token: str, ip: str) -> bool:  # NOSONAR S7503
        return True


class TurnstileVerifier:
    """Cloudflare Turnstile server-side validation (siteverify)."""

    def __init__(
        self,
        site_key: str,
        secret: str,
        *,
        verify_url: str = TURNSTILE_VERIFY_URL,
        timeout_s: float = 5.0,
    ) -> None:
        self.site_key = site_key
        self._secret = secret
        self._url = verify_url
        self._timeout = timeout_s

    async def verify(self, token: str, ip: str) -> bool:
        if not token:
            return False
        data = {"secret": self._secret, "response": token, "remoteip": ip}
        try:
            async with aiohttp.ClientSession() as session, session.post(
                self._url,
                data=data,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                result = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("turnstile verify failed: %s", type(exc).__name__)
            return False  # fail closed
        return isinstance(result, dict) and result.get("success") is True


def make_bot_verifier(env: dict[str, str] | None = None) -> BotVerifier:
    """Turnstile when both env keys are set, otherwise a no-op."""
    env = os.environ if env is None else env
    site_key = env.get(TURNSTILE_SITE_KEY_ENV, "")
    secret = env.get(TURNSTILE_SECRET_ENV, "")
    if site_key and secret:
        return TurnstileVerifier(site_key, secret)
    return NoopVerifier()


# ---------------------------------------------------------------------------
# Per-app state
# ---------------------------------------------------------------------------


@dataclass
class EntryState:
    """Collaborators of the entry routes; tests swap any of them."""

    config: LensConfig
    store: GuestStore | None = None
    mailer: MailAdapter | None = None
    alerter: Alerter | None = None
    verifier: BotVerifier = field(default_factory=NoopVerifier)
    signin_floor_s: float = SIGNIN_FLOOR_S
    #: Background sign-in mail sends (kept referenced until done).
    mail_tasks: set = field(default_factory=set)

    def get_store(self) -> GuestStore:
        if self.store is None:
            path = self.config.guest_store_path or _default_guest_store_path()
            self.store = GuestStore(path)
        return self.store

    def get_mailer(self) -> MailAdapter:
        if self.mailer is None:
            self.mailer = make_adapter(self.config)
        return self.mailer


def install(app: web.Application, config: LensConfig) -> None:
    """Register state and the entry routes on *app*."""
    # Share the app-wide store (created by make_app in guest mode) so the
    # entry routes, the guest tier in auth and the routing consent gate all
    # read and write one store.
    app[ENTRY_STATE] = EntryState(
        config=config,
        store=app.get("guest_store"),
        alerter=make_alerter(config),
        verifier=make_bot_verifier(),
    )
    app.router.add_get("/entry", get_entry)
    # Post-SSO landing: Cloudflare Access forwards the user back here.
    app.router.add_get("/login", get_login)
    # Consent gate target (routes.CONSENT_PATH): a guest whose consent is
    # outdated re-runs the entry flow, which records consent for the
    # current Terms/Privacy versions on verification.
    app.router.add_get("/consent", get_entry)
    app.router.add_post("/entry/email", post_email)
    app.router.add_post("/entry/signin", post_signin)
    app.router.add_post("/entry/code", post_code)
    app.router.add_post("/entry/guest", post_guest)
    app.router.add_post("/entry/guest/start", post_guest_start)
    app.router.add_post("/entry/verify", post_verify)


def get_guest_store(app: web.Application) -> GuestStore:
    """The app's (lazily opened) guest store -- shared with other routes."""
    return app[ENTRY_STATE].get_store()


def _state(request: web.Request) -> EntryState:
    state = request.app.get(ENTRY_STATE)
    if state is None or not state.config.guest_enabled:
        raise web.HTTPNotFound()
    return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# No "-": approved users' sandbox nicks are sbx-op-<x> (d7).
_NICK_DROP = re.compile(r"[^a-z0-9_]")


def sanitize_nickname(raw: str) -> str | None:
    """Lowercase, keep ``[a-z0-9_]``; None unless 2..16 chars remain."""
    nick = _NICK_DROP.sub("", (raw or "").lower())
    if not NICK_MIN <= len(nick) <= NICK_MAX:
        return None
    return nick


def _norm_email(raw: object) -> str:
    return str(raw or "").strip().lower()[:MAX_EMAIL_LEN]


def _plausible_email(email: str) -> bool:
    local, sep, domain = email.partition("@")
    return bool(sep and local and "." in domain and " " not in email)


def client_ip(request: web.Request) -> str:
    """Visitor IP: ``CF-Connecting-IP`` (cloudflared), else the peer.

    The lens binds loopback behind cloudflared, so every peer address is
    the tunnel's; Cloudflare's header is the only per-visitor signal.
    """
    return (
        request.headers.get("CF-Connecting-IP", "").strip()
        or request.remote
        or "unknown"
    )


def _over_limit(
    store: GuestStore, kind: str, email: str, ip: str, *, limit: int, window: int
) -> bool:
    """Check per-email and per-IP limits, then count this attempt."""
    keys = (f"e:{email}", f"i:{ip}")
    limited = any(
        store.rate_limited(kind, key, limit=limit, window=window) for key in keys
    )
    for key in keys:
        store.record_attempt(kind, key)
    return limited


_dummy_hasher = PasswordHasher()
_dummy_hash: str | None = None


def _dummy_verify(password: str) -> None:
    """Burn one argon2id verify so unknown emails cost what known ones do."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _dummy_hasher.hash(secrets.token_hex(16))
    try:
        _dummy_hasher.verify(_dummy_hash, password)
    except (VerificationError, InvalidHashError):
        pass


def _nick_taken(store: GuestStore, email: str, nick: str) -> bool:
    if nick in RESERVED_NICKNAMES:
        return True
    full = NICK_PREFIX + nick
    return any(
        other_nick.lower() == full and other_email != email
        for other_email, other_nick, _ip in store.list_guests()
    )


def _page(
    state: EntryState,
    step: str,
    *,
    status: int = 200,
    email: str = "",
    nickname: str = "",
    error: str = "",
    train: bool = False,
) -> web.Response:
    site_key = state.verifier.site_key if step in ("password", "guest") else None
    html = render_fragment(
        "entry.html.j2",
        step=step,
        email=email,
        nickname=nickname,
        error=error,
        train=train,
        site_key=site_key,
        turnstile_script=TURNSTILE_SCRIPT_URL,
        turnstile_field=TURNSTILE_FIELD,
        busy=MSG_BUSY,
        nick_prefix=NICK_PREFIX,
        nick_max=NICK_MAX,
        terms_url=TERMS_URL,
        privacy_url=PRIVACY_URL,
        css_url=static_url("entry.css"),
    )
    resp = web.Response(text=html, content_type="text/html", status=status)
    resp.headers["Cache-Control"] = "no-store"
    resp[ENTRY_PAGE_MARKER] = True
    if site_key:
        resp[CSP_TURNSTILE_MARKER] = True
    return resp


def _train(form) -> bool:
    """The optional training opt-in (d9): only an explicit ``on`` counts."""
    return form.get("train") == "on"


def _see_other(location: str) -> web.Response:
    return web.Response(
        status=303, headers={"Location": location, "Cache-Control": "no-store"}
    )


def busy_page(state: EntryState) -> web.Response:
    """The guest-limit page (c24): one fixed message, nothing else.

    Also served on ``/`` to a returning guest whose slot was taken (c35).
    """
    metrics.get_metrics().guest_busy()
    return _page(state, "busy")


def _guests_full(request: web.Request, email: str) -> bool:
    """True iff max_guests guests are active and *email* is not one of them.

    Only real guests count; an approved user's Guest view never does.
    """
    registry = request.app["registry"]
    limit = request.app["config"].guest_max_guests
    return not registry.guest_slot_free(GUEST_PRINCIPAL_PREFIX + email, limit)


def _signin_blocked(store: GuestStore, request: web.Request, email: str) -> bool:
    """App sign-in limits for one password submission or code entry (r6).

    A trusted browser (a valid ``lens_device`` for *this* email, c37) skips
    every limit and is not counted. Any other browser is limited per IP
    (:data:`SIGNIN_IP_ATTEMPTS_PER_15MIN`, password and code attempts
    together), then -- if the IP still has room -- by the email's shared
    untrusted budget (c38: 3 per 15 minutes, strict 2 per 30 minutes after
    exhaustion until 24 quiet hours). The caller answers a blocked attempt
    exactly like an unblocked one.
    """
    if store.is_trusted_device(app_session.read_device_cookie(request), email):
        return False
    ip_key = f"i:{client_ip(request)}"
    ip_limited = store.rate_limited(
        SIGNIN_IP_KIND,
        ip_key,
        limit=SIGNIN_IP_ATTEMPTS_PER_15MIN,
        window=SIGNIN_WINDOW_S,
    )
    store.record_attempt(SIGNIN_IP_KIND, ip_key)
    return ip_limited or not store.signin_budget_take(email)


async def _pad(started: float, floor: float) -> None:
    remaining = floor - (time.perf_counter() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@allows_anonymous
async def get_entry(request: web.Request) -> web.Response:  # NOSONAR S7503
    """Step 1: Email + Continue. Also the anonymous landing for ``/``."""
    return _page(_state(request), "email")


@allows_anonymous
async def get_login(request: web.Request) -> web.Response:  # NOSONAR S7503
    """Return target after Cloudflare Access SSO (path-scoped on ``/login``).

    Always 303 ``/``, whatever the tier: ``/`` routes approved users to the
    console and everyone else to the entry card, so this answer can never
    reveal whether an address is approved. Never 404 (also with guest mode
    off) and never cached.
    """
    return _see_other("/")


@allows_anonymous
async def post_email(request: web.Request) -> web.Response:
    """Step 2: the one password window, whatever the address."""
    state = _state(request)
    form = await request.post()
    return _page(state, "password", email=_norm_email(form.get("email")))


@allows_anonymous
async def post_signin(request: web.Request) -> web.Response:
    """Password step. App sign-in: always the code screen; else 0.12.2."""
    if _state(request).config.app_signin_enabled:
        return await _post_signin_app(request)
    return await _post_signin_access(request)


async def _post_signin_access(request: web.Request) -> web.Response:
    """0.12.2 (``auth.app_signin.enabled: false``): correct -> /login."""
    started = time.perf_counter()
    state = _state(request)
    cfg = state.config
    form = await request.post()
    email = _norm_email(form.get("email"))
    password = str(form.get("password") or "")
    ip = client_ip(request)
    store = state.get_store()
    limited = _over_limit(
        store,
        "signin",
        email,
        ip,
        limit=cfg.guest_rate_password_attempts_per_15min,
        window=SIGNIN_WINDOW_S,
    )
    ok = False
    if limited:
        await asyncio.to_thread(_dummy_verify, password)
    else:
        human = await state.verifier.verify(str(form.get(TURNSTILE_FIELD) or ""), ip)
        allowed = {e.lower() for e in cfg.allowed_emails}
        if email in allowed:
            pw_ok = await asyncio.to_thread(store.check_password, email, password)
        else:
            await asyncio.to_thread(_dummy_verify, password)
            pw_ok = False
        ok = human and pw_ok
    await _pad(started, state.signin_floor_s)
    if ok:
        return _see_other("/login")
    metrics.get_metrics().failed_sign_in()
    if limited:
        metrics.get_metrics().rate_limited()
    return _page(
        state, "password", status=429 if limited else 401, email=email, error=ERR_SIGNIN
    )


async def _send_signin_code(state: EntryState, email: str, code: str) -> None:
    """Mail one sign-in code (runs as a background task, off the request)."""
    subject, body = render_token_email(code, purpose=PURPOSE_SIGNIN)
    try:
        await asyncio.to_thread(
            send_with_alert, state.get_mailer(), state.alerter, email, subject, body
        )
    except Exception as exc:  # noqa: BLE001 -- the page was already the same
        logger.warning("signin code mail not sent: %s", type(exc).__name__)
        return
    metrics.get_metrics().signin_code_sent()


def _schedule_signin_code(state: EntryState, email: str, code: str) -> None:
    task = asyncio.create_task(_send_signin_code(state, email, code))
    state.mail_tasks.add(task)
    task.add_done_callback(state.mail_tasks.discard)


async def _send_signin_notice(
    state: EntryState, email: str, ip: str, user_agent: str, when: float
) -> None:
    """Mail the new-browser sign-in notice (c40; background, off the request)."""
    subject, body = render_signin_notice(ip=ip, user_agent=user_agent, when=when)
    try:
        await asyncio.to_thread(
            send_with_alert, state.get_mailer(), state.alerter, email, subject, body
        )
    except Exception as exc:  # noqa: BLE001 -- the sign-in already succeeded
        logger.warning("sign-in notice mail not sent: %s", type(exc).__name__)


def _schedule_signin_notice(state: EntryState, request: web.Request, email: str) -> None:
    task = asyncio.create_task(
        _send_signin_notice(
            state,
            email,
            client_ip(request),
            request.headers.get("User-Agent", ""),
            state.get_store().now(),
        )
    )
    state.mail_tasks.add(task)
    task.add_done_callback(state.mail_tasks.discard)


async def _post_signin_app(request: web.Request) -> web.Response:
    """Oracle-free sign-in: every case answers with the same code screen.

    Unknown email, wrong password, right password and a blocked attempt
    (over the per-IP limit or the email's untrusted budget, r6) all get
    status 200, the same page (only the echoed email differs), one fresh
    ``lens_signin`` pending cookie and the same time floor. Only for an
    approved email with the right password, not blocked, is a code issued,
    bound to this pending value, and mailed from a background task -- so
    only the mailbox learns the password was right.
    """
    started = time.perf_counter()
    state = _state(request)
    cfg = state.config
    form = await request.post()
    email = _norm_email(form.get("email"))
    password = str(form.get("password") or "")
    ip = client_ip(request)
    store = state.get_store()
    # A trusted browser is never limited, so an attack on the email can't
    # lock its owner out (r6); untrusted browsers are limited per IP and by
    # the email's shared budget.
    limited = _signin_blocked(store, request, email)
    correct = False
    if limited:
        await asyncio.to_thread(_dummy_verify, password)
    else:
        human = await state.verifier.verify(str(form.get(TURNSTILE_FIELD) or ""), ip)
        allowed = {e.lower() for e in cfg.allowed_emails}
        if email in allowed:
            pw_ok = await asyncio.to_thread(store.check_password, email, password)
        else:
            await asyncio.to_thread(_dummy_verify, password)
            pw_ok = False
        correct = human and pw_ok
    pending = secrets.token_urlsafe(32)
    if correct:
        code = store.issue_token(email, purpose=SIGNIN_CODE_PURPOSE)
        store.bind_signin(pending, code)
        _schedule_signin_code(state, email, code)
    else:
        metrics.get_metrics().failed_sign_in()
    if limited:
        metrics.get_metrics().rate_limited()
    await _pad(started, state.signin_floor_s)
    resp = _page(state, "signin_code", email=email)
    app_session.set_signin_cookie(resp, pending)
    return resp


@allows_anonymous
async def post_code(request: web.Request) -> web.Response:
    """Sign-in code + this browser's pending cookie -> a new app session.

    A wrong, expired, reused, other-browser or blocked (per-IP limit or the
    email's untrusted budget, r6) code all get the one error, padded to the
    sign-in floor; a blocked try never checks (or uses up) the code. On
    success a fresh session id is minted (nothing the browser held before
    is promoted) and the pending cookie is cleared. With "Trust this
    browser" ticked (c39) the browser also gets a fresh ``lens_device``;
    unticked leaves any existing trust as it is.
    """
    started = time.perf_counter()
    state = _state(request)
    cfg = state.config
    if not cfg.app_signin_enabled:
        raise web.HTTPNotFound()
    form = await request.post()
    email = _norm_email(form.get("email"))
    code = str(form.get("code") or "").strip()
    pending = app_session.read_signin_cookie(request)
    store = state.get_store()
    limited = _signin_blocked(store, request, email)
    allowed = {e.lower() for e in cfg.allowed_emails}
    ok = (
        not limited
        and bool(code)
        and bool(pending)
        and email in allowed
        and store.verify_signin_token(email, code, pending)
    )
    if not ok:
        metrics.get_metrics().failed_sign_in()
        if limited:
            metrics.get_metrics().rate_limited()
        await _pad(started, state.signin_floor_s)
        return _page(state, "signin_code", status=401, email=email, error=ERR_CODE)
    raw = store.create_session(email)
    device = app_session.read_device_cookie(request)
    was_trusted = store.is_trusted_device(device, email)
    new_device = None
    if form.get("trust") == "on":
        new_device = store.add_trusted_device(email, previous_raw=device)
        logger.info("app sign-in browser trusted")
    elif was_trusted:
        store.touch_trusted_device(device, email)
    if not was_trusted:
        # A browser not trusted for this email is new to it (c40): tell the
        # user, whatever the IP. A trusted one is known, even from a new IP.
        _schedule_signin_notice(state, request, email)
    await _pad(started, state.signin_floor_s)
    resp = _see_other("/")
    app_session.issue_session_cookie(resp, raw)
    app_session.clear_signin_cookie(resp)
    if new_device is not None:
        app_session.issue_device_cookie(resp, new_device)
    metrics.get_metrics().session_started()
    logger.info("app sign-in session started")
    return resp


@allows_anonymous
async def post_guest(request: web.Request) -> web.Response:
    """Guest mode button: nickname + consent (or the busy page, c24)."""
    state = _state(request)
    form = await request.post()
    email = _norm_email(form.get("email"))
    if _guests_full(request, email):
        return busy_page(state)
    return _page(state, "guest", email=email)


@allows_anonymous
async def post_guest_start(request: web.Request) -> web.Response:
    """Validate nickname + consent, then email a single-use token."""
    state = _state(request)
    cfg = state.config
    form = await request.post()
    email = _norm_email(form.get("email"))
    raw_nick = str(form.get("nickname") or "")
    ip = client_ip(request)
    store = state.get_store()
    # c24: never email a code while the sandbox is full.
    if _guests_full(request, email):
        return busy_page(state)

    def again(error: str) -> web.Response:
        return _page(
            state, "guest", status=400, email=email, nickname=raw_nick, error=error
        )

    if form.get("consent") != "on":
        return again(ERR_CONSENT)
    nick = sanitize_nickname(raw_nick)
    if nick is None:
        return again(ERR_NICK_INVALID)
    if _nick_taken(store, email, nick):
        return again(ERR_NICK_TAKEN)
    if not await state.verifier.verify(str(form.get(TURNSTILE_FIELD) or ""), ip):
        return again(ERR_BOT)

    limited = _over_limit(
        store,
        "token",
        email,
        ip,
        limit=cfg.guest_rate_entry_per_min,
        window=TOKEN_REQUEST_WINDOW_S,
    )
    if limited:
        metrics.get_metrics().rate_limited()
    elif _plausible_email(email) and not store.is_banned(email, ip):
        token = store.issue_token(email, purpose=TOKEN_PURPOSE)
        subject, body = render_token_email(token)
        try:
            await asyncio.to_thread(send_with_alert, state.get_mailer(), state.alerter, email, subject, body)
        except Exception as exc:  # noqa: BLE001 -- same page either way (o4/o5)
            logger.warning("guest token mail not sent: %s", type(exc).__name__)
    return _page(
        state,
        "code",
        status=429 if limited else 200,
        email=email,
        nickname=nick,
        train=_train(form),
    )


@allows_anonymous
async def post_verify(request: web.Request) -> web.Response:
    """Check the emailed code; on success enter the sandbox as a guest."""
    state = _state(request)
    cfg = state.config
    form = await request.post()
    email = _norm_email(form.get("email"))
    raw_nick = str(form.get("nickname") or "")
    code = str(form.get("code") or "").strip()
    ip = client_ip(request)
    store = state.get_store()

    limited = _over_limit(
        store,
        "verify",
        email,
        ip,
        limit=cfg.guest_rate_password_attempts_per_15min,
        window=TOKEN_VERIFY_WINDOW_S,
    )
    nick = sanitize_nickname(raw_nick)

    def wrong(status: int) -> web.Response:
        return _page(
            state,
            "code",
            status=status,
            email=email,
            nickname=nick or "",
            error=ERR_CODE,
            train=_train(form),
        )

    if limited:
        metrics.get_metrics().rate_limited()
        return wrong(429)
    if nick is None or not code or store.is_banned(email, ip):
        return wrong(401)
    if _nick_taken(store, email, nick):
        return _page(
            state, "guest", status=400, email=email, nickname=nick, error=ERR_NICK_TAKEN
        )
    try:
        versions = await legal.current_legal_versions(cfg)
    except legal.LegalVersionsUnavailable as exc:
        logger.warning("guest entry blocked: %s", exc)
        return _page(
            state,
            "code",
            status=503,
            email=email,
            nickname=nick,
            error=ERR_LATER,
            train=_train(form),
        )
    # c24/h16: re-check the guest limit and claim the slot atomically, so
    # two simultaneous verifies can never both get in. The code is checked
    # inside the lock *after* the limit, so a refused guest's code is not
    # consumed (they may retry it while it is still valid). The slot is
    # held as a short reservation until this browser's GET / opens the
    # session.
    registry = request.app["registry"]
    principal = GUEST_PRINCIPAL_PREFIX + email
    async with registry.guest_lock:
        if not registry.guest_slot_free(principal, cfg.guest_max_guests):
            return busy_page(state)
        if not store.verify_token(email, code, purpose=TOKEN_PURPOSE):
            return wrong(401)
        registry.try_reserve_guest(principal, cfg.guest_max_guests)

    store.record_guest(email, NICK_PREFIX + nick, ip)
    store.record_consent(
        email,
        ip,
        tos_version=versions["terms"],
        privacy_version=versions["privacy"],
        train=_train(form),
    )
    metrics.get_metrics().entry()
    resp = _see_other("/")
    csrf.issue_guest_cookie(
        resp, guest_id=email, secret=request.app.get(csrf.SECRET_KEY)
    )
    return resp
