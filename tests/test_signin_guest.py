"""Email-only guests through Sign in (frame ``email-only-guest-entry``).

Coverage map (frame claims):

* c9  -- Sign in is the only entry: an email that is not an approved user
  gets a guest code mailed on Sign in, from the background like the
  sign-in code; the Guest mode button and its routes are gone
  (``test_unknown_email_gets_a_guest_code_whatever_the_password``,
  ``test_password_page_has_one_button_and_the_hint``,
  ``test_guest_button_routes_404_with_app_signin``).
* c10 -- nothing tells a guest from a signed user before the code: a wrong
  guest code is the approved path's one error, and a busy seat is only
  shown after a right code
  (``test_wrong_guest_code_is_the_one_error``,
  ``test_busy_only_after_a_right_code_and_code_kept``).
* c11/c12 -- nickname + consent is a one-time form after the code; a
  returning guest goes straight in
  (``test_first_guest_code_opens_the_welcome_form_then_the_sandbox``,
  ``test_returning_guest_skips_the_welcome_form``,
  ``test_welcome_errors_keep_the_token``,
  ``test_welcome_token_is_single_use_and_bound_to_its_email``).
* c13/c15 -- the guest credential is a single-use code bound to the
  browser that signed in (``test_guest_code_from_another_browser_fails``).
* c14 -- guests can log out: session closed, seat freed, cookie cleared
  (``test_guest_logout_frees_the_seat``).
* s9  -- the Enter key submits the forward button, not Back
  (``test_enter_key_default_is_the_forward_button``).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import AsyncIterator

import aiohttp
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens.mail import RecordingAdapter
from irc_lens.session import Session
from irc_lens.web import app_session, csrf, entry, make_app

from test_entry import FakeVerifier, legal_server  # noqa: F401 -- fixture
from test_guest_limit import APPROVED, BUSY, FakeClock, _config

ANN = "ann+test@example.org"
BOB = "bob@example.org"
_SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
_GUEST_SUBJECT = "Your chat.culture.dev code"


class Env:
    def __init__(self, client, app, clock) -> None:
        self.client, self.app, self.clock = client, app, clock
        self.cookies: dict[str, str] = {}

    @property
    def registry(self):
        return self.app["registry"]

    @property
    def state(self):
        return self.app[entry.ENTRY_STATE]

    @property
    def store(self):
        return self.state.get_store()

    @property
    def mail(self) -> RecordingAdapter:
        return self.state.mailer

    async def drain(self) -> None:
        for task in list(self.state.mail_tasks):
            await task

    async def post(self, path: str, data: dict, cookie: str = ""):
        headers = dict(_SAME_ORIGIN)
        if cookie:
            headers["Cookie"] = cookie
        return await self.client.post(
            path, data=data, headers=headers, allow_redirects=False
        )

    async def signin(self, email: str, password: str = ""):
        resp = await self.post("/entry/signin", {"email": email, "password": password})
        await self.drain()
        return resp

    def code_for(self, email: str) -> str:
        body = [b for to, _s, b in self.mail.sent if to == email][-1]
        return re.search(r"^ {4}(\S+)$", body, flags=re.M).group(1)

    async def code(self, email: str, code: str, pending: str):
        cookie = f"{app_session.SIGNIN_COOKIE_NAME}={pending}"
        return await self.post("/entry/code", {"email": email, "code": code}, cookie)

    async def welcome(self, email: str, token: str, nick: str, consent: bool = True):
        data = {"email": email, "welcome": token, "nickname": nick}
        if consent:
            data["consent"] = "on"
        resp = await self.post("/entry/welcome", data)
        self._keep_guest_cookie(email, resp)
        return resp

    def _keep_guest_cookie(self, email: str, resp) -> None:
        if csrf.GUEST_COOKIE_NAME in resp.cookies:
            self.cookies[email] = resp.cookies[csrf.GUEST_COOKIE_NAME].value

    def guest_cookie(self, email: str) -> str:
        return f"{csrf.GUEST_COOKIE_NAME}={self.cookies[email]}"

    async def sign_in_to_code(self, email: str):
        """Sign in with no password; return (pending, the mailed code)."""
        resp = await self.signin(email)
        assert resp.status == 200
        return pending_of(resp), self.code_for(email)

    async def enter(self, email: str, nick: str) -> None:
        """First visit: Sign in -> code -> welcome form -> GET / opens it."""
        pending, code = await self.sign_in_to_code(email)
        page = await self.code(email, code, pending)
        assert page.status == 200, await page.text()
        resp = await self.welcome(email, welcome_of(await page.text()), nick)
        assert resp.status == 303, await resp.text()
        opened = await self.client.get(
            "/", headers={"Cookie": self.guest_cookie(email), **_SAME_ORIGIN}
        )
        assert opened.status == 200
        assert self.registry.has(f"guest:{email}", "sandbox")


def pending_of(resp) -> str:
    return resp.cookies[app_session.SIGNIN_COOKIE_NAME].value


def welcome_of(html: str) -> str:
    return re.search(r'name="welcome" value="([^"]+)"', html).group(1)


@pytest_asyncio.fixture
async def env(
    jwks: FakeJWKS, tmp_path, legal_server, monkeypatch
) -> AsyncIterator[Env]:  # noqa: F811
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", "s" * 32)
    mesh, sandbox = AgentIRCTestServer(), AgentIRCTestServer()
    await mesh.start()
    await sandbox.start()
    config = dataclasses.replace(
        _config(jwks, mesh, sandbox, tmp_path, legal_server), app_signin_enabled=True
    )

    def mesh_factory(nick: str) -> Session:
        return Session(host=mesh.host, port=mesh.port, nick=nick)

    app = make_app(config, mesh_factory)
    state = app[entry.ENTRY_STATE]
    state.mailer = RecordingAdapter()
    state.verifier = FakeVerifier()
    state.signin_floor_s = 0.0
    clock = FakeClock()
    app["registry"]._clock = clock
    client = TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    try:
        yield Env(client, app, clock)
    finally:
        for s in app["registry"].values():
            await s.disconnect()
        await client.close()
        await mesh.stop()
        await sandbox.stop()


# -- c9: Sign in is the only entry ---------------------------------------------


async def test_unknown_email_gets_a_guest_code_whatever_the_password(env: Env) -> None:
    for i, pw in enumerate(("", "anything")):
        email = f"guest{i}@example.org"
        resp = await env.signin(email, pw)
        assert resp.status == 200
        html = await resp.text()
        assert 'action="/entry/code"' in html
        assert 'name="nickname"' not in html
        assert [(to, s) for to, s, _ in env.mail.sent if to == email] == [
            (email, _GUEST_SUBJECT)
        ]


async def test_approved_wrong_password_and_banned_email_get_no_mail(env: Env) -> None:
    env.store.ban(email=BOB, ip=None, reason="test")
    await env.signin(APPROVED, "wrong")
    await env.signin(BOB)
    assert env.mail.sent == []


async def test_guest_signins_never_use_up_approved_attempts_on_one_ip(
    env: Env,
) -> None:
    """Found in the staging click-through: guests on a shared IP counted as
    wrong passwords and silently blocked the approved user's sign-in."""
    env.store.set_password(APPROVED, "right-password-123")
    for i in range(entry.SIGNIN_IP_ATTEMPTS_PER_15MIN + 2):
        await env.signin(f"guest{i}@example.org")
    await env.signin(APPROVED, "wrong")
    await env.signin(APPROVED, "right-password-123")
    assert [s for to, s, _ in env.mail.sent if to == APPROVED] == [
        "Your chat.culture.dev sign-in code"
    ]


async def _guest_mailed_after_probing(env: Env, target: str, ip: str) -> bool:
    """3 wrong passwords for *target* from *ip*, a minute's wait, then a
    sign-in for an inbox the prober owns: was a guest code mailed?"""
    headers = {"CF-Connecting-IP": ip}
    for _ in range(entry.SIGNIN_IP_ATTEMPTS_PER_15MIN):
        await env.client.post(
            "/entry/signin",
            data={"email": target, "password": "wrong"},
            headers={**headers, **_SAME_ORIGIN},
            allow_redirects=False,
        )
    base = env.store._clock
    env.store._clock = lambda: base() + entry.TOKEN_REQUEST_WINDOW_S + 1
    own = f"prober-{ip}@example.org"
    await env.client.post(
        "/entry/signin",
        data={"email": own, "password": ""},
        headers={**headers, **_SAME_ORIGIN},
        allow_redirects=False,
    )
    await env.drain()
    return any(to == own for to, _s, _b in env.mail.sent)


async def test_guest_mail_reveals_nothing_about_approved_emails(env: Env) -> None:
    """Review finding: guest mail must not depend on the password-attempt
    counters, or an attacker's own inbox tells whether an email is
    approved (3 wrong passwords, then a guest sign-in from the same IP)."""
    env.store.set_password(APPROVED, "right-password-123")
    clock = env.store._clock
    approved = await _guest_mailed_after_probing(env, APPROVED, "198.51.100.1")
    env.store._clock = clock
    unknown = await _guest_mailed_after_probing(env, BOB, "198.51.100.2")
    assert approved is True
    assert unknown is True


async def test_guest_mail_capped_per_minute(env: Env) -> None:
    limit = env.app["config"].guest_rate_entry_per_min
    for _ in range(limit + 3):
        resp = await env.signin(ANN)
        assert resp.status == 200  # the same page, capped or not
    assert sum(1 for to, _s, _b in env.mail.sent if to == ANN) == limit


async def test_password_page_has_one_button_and_the_hint(env: Env) -> None:
    html = await (await env.post("/entry/email", {"email": ANN})).text()
    assert "Guest mode" not in html
    assert 'formaction="/entry/guest"' not in html
    assert "No password? Leave it empty. We'll email you a code." in html
    assert html.count('type="submit"') == 1


async def test_guest_button_routes_404_with_app_signin(env: Env) -> None:
    for path in ("/entry/guest", "/entry/guest/start", "/entry/verify"):
        resp = await env.post(path, {"email": ANN, "nickname": "ann", "consent": "on"})
        assert resp.status == 404, path
    assert env.mail.sent == []


# -- c10: nothing tells a guest apart before the code ---------------------------


async def test_wrong_guest_code_is_the_one_error(env: Env) -> None:
    pending, _code = await env.sign_in_to_code(ANN)
    resp = await env.code(ANN, "not-the-code", pending)
    html = await resp.text()
    assert resp.status == 401
    assert entry.ERR_CODE in html
    assert 'action="/entry/code"' in html
    assert BUSY not in html
    assert 'name="nickname"' not in html


async def test_busy_only_after_a_right_code_and_code_kept(env: Env) -> None:
    await env.enter(ANN, "ann")
    pending, code = await env.sign_in_to_code(BOB)  # mailed even while busy
    wrong = await env.code(BOB, "not-the-code", pending)
    assert BUSY not in await wrong.text()  # a wrong code never sees "busy"
    busy = await env.code(BOB, code, pending)
    assert BUSY in await busy.text()
    # The seat frees; the same code still works (it was not used up).
    await env.post("/guest/logout", {}, env.guest_cookie(ANN))
    page = await env.code(BOB, code, pending)
    assert page.status == 200
    assert 'action="/entry/welcome"' in await page.text()


# -- c11/c12: one-time welcome form after the code ------------------------------


async def test_first_guest_code_opens_the_welcome_form_then_the_sandbox(
    env: Env,
) -> None:
    pending, code = await env.sign_in_to_code(ANN)
    page = await env.code(ANN, code, pending)
    html = await page.text()
    assert page.status == 200
    assert 'action="/entry/welcome"' in html
    assert 'name="nickname"' in html
    assert 'name="consent"' in html
    assert csrf.GUEST_COOKIE_NAME not in page.cookies  # not in yet
    resp = await env.welcome(ANN, welcome_of(html), "ann")
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert env.store.get_guest(ANN)[-1][1] == "sbx-ann"
    assert env.store.get_consents(ANN)


async def test_returning_guest_skips_the_welcome_form(env: Env) -> None:
    await env.enter(ANN, "ann")
    await env.post("/guest/logout", {}, env.guest_cookie(ANN))
    pending, code = await env.sign_in_to_code(ANN)
    resp = await env.code(ANN, code, pending)
    assert resp.status == 303
    assert csrf.GUEST_COOKIE_NAME in resp.cookies
    assert env.store.get_guest(ANN)[-1][1] == "sbx-ann"


async def test_welcome_errors_keep_the_token(env: Env) -> None:
    pending, code = await env.sign_in_to_code(ANN)
    token = welcome_of(await (await env.code(ANN, code, pending)).text())
    no_consent = await env.welcome(ANN, token, "ann", consent=False)
    assert no_consent.status == 400
    assert entry.ERR_CONSENT in await no_consent.text()
    bad_nick = await env.welcome(ANN, token, "!")
    assert entry.ERR_NICK_INVALID in await bad_nick.text()
    assert (await env.welcome(ANN, token, "ann")).status == 303


async def test_welcome_token_is_single_use_and_bound_to_its_email(env: Env) -> None:
    pending, code = await env.sign_in_to_code(ANN)
    token = welcome_of(await (await env.code(ANN, code, pending)).text())
    other = await env.welcome(BOB, token, "bob")
    assert other.status == 401
    assert entry.ERR_EXPIRED in await other.text()
    assert (await env.welcome(ANN, token, "ann")).status == 303
    again = await env.welcome(ANN, token, "ann")
    assert again.status == 401


# -- c13/c15: single-use code, bound to the browser -----------------------------


async def test_guest_code_from_another_browser_fails(env: Env) -> None:
    _pending, code = await env.sign_in_to_code(ANN)
    other = pending_of(await env.signin(BOB))
    resp = await env.code(ANN, code, other)
    assert resp.status == 401
    assert entry.ERR_CODE in await resp.text()


# -- c14: guest logout ----------------------------------------------------------


async def test_guest_logout_frees_the_seat(env: Env) -> None:
    await env.enter(ANN, "ann")
    page = await env.client.get(
        "/", headers={"Cookie": env.guest_cookie(ANN), **_SAME_ORIGIN}
    )
    assert 'action="/guest/logout"' in await page.text()
    resp = await env.post("/guest/logout", {}, env.guest_cookie(ANN))
    assert resp.status == 303
    assert resp.cookies[csrf.GUEST_COOKIE_NAME].value == ""
    assert not env.registry.has(f"guest:{ANN}", "sandbox")
    assert env.registry.active_guest_count() == 0
    await env.enter(BOB, "bob")  # the next guest gets the seat at once


# -- s9: the Enter key's default button is the forward one ----------------------


def _first_submit(html: str, action: str) -> str:
    form = html[html.index(f'action="{action}"') :]
    form = form[: form.index("</form>")]
    button = re.search(r'<button type="submit"[^>]*>([^<]+)</button>', form)
    return button.group(1)


async def test_enter_key_default_is_the_forward_button(env: Env) -> None:
    pending, code = await env.sign_in_to_code(ANN)
    code_page = await (await env.signin(ANN)).text()
    assert _first_submit(code_page, "/entry/code") == "Verify"
    welcome = await (await env.code(ANN, code, pending)).text()
    assert _first_submit(welcome, "/entry/welcome") == "Enter"
