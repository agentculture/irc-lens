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
from irc_lens.mail import MailAdapter, make_adapter, render_token_email, send_with_alert
from irc_lens.web import csrf
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.render import render_fragment, static_url

logger = logging.getLogger(__name__)

ENTRY_STATE = web.AppKey("entry_state", object)

#: Minimum wall time of every sign-in response (o4). An argon2id verify is
#: ~50 ms here; the floor absorbs it and any allowlist/DB branch.
SIGNIN_FLOOR_S = 0.5
SIGNIN_WINDOW_S = 900
TOKEN_REQUEST_WINDOW_S = 60
TOKEN_VERIFY_WINDOW_S = 900
TOKEN_PURPOSE = "guest"
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
    """Approved email + correct password -> /login; else the one error."""
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


@allows_anonymous
async def post_guest(request: web.Request) -> web.Response:
    """Guest mode button: nickname + consent."""
    state = _state(request)
    form = await request.post()
    return _page(state, "guest", email=_norm_email(form.get("email")))


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
    if not store.verify_token(email, code, purpose=TOKEN_PURPOSE):
        return wrong(401)

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
