"""t8: oracle-free app sign-in -- password -> code screen -> emailed code -> session.

Covers spec claims:

* c1/h1   approved user reaches the real mesh with password + emailed code,
          without Cloudflare Access (``test_full_signin_reaches_real_mesh``).
* c8/h4   no /entry/signin response depends on the password; only the
          mailbox learns it (``test_signin_four_cases_identical_and_floored``,
          ``test_code_screen_copy_and_form``).
* c9/h5   a correct password without the mailbox never yields a session
          (``test_correct_password_without_mailbox_never_yields_session``).
* c10/h6  the four cases are byte-identical apart from the echoed email,
          each >= the floor, and only the right case sends exactly one mail,
          from a background task (``test_signin_four_cases_identical_and_floored``,
          ``test_signin_mail_is_sent_off_the_request_path``).
* c11/h7  codes: same browser only, single use, 10 minutes, one error;
          code entries are never limited or counted (c41/d7)
          (``test_code_from_another_browser_fails``, ``test_code_is_single_use``,
          ``test_code_expires_after_ten_minutes``,
          ``test_wrong_codes_never_lock_out_the_right_one``,
          ``test_code_entries_do_not_use_the_ip_limit``,
          ``test_every_code_failure_is_the_one_error``).
* c14/h10 per-IP password limit (``test_password_attempts_limited_per_ip_without_check``);
          the old per-email code cap is replaced by r6: an attack on the
          email blocks untrusted browsers silently but never a trusted one
          (``test_distributed_attack_blocks_untrusted_but_not_trusted``; the
          full r6 suite is ``test_trusted_devices.py``).
* c31/h22 fresh session id at code entry, pending cookie cleared, a prior
          session id never promoted (``test_full_signin_reaches_real_mesh``).
* c32/h23 + c7/h3 the switch off restores 0.12.2 (``test_switch_off_restores_login_redirect``;
          the 0.12.2 suite in ``test_entry.py`` runs with the switch off).
* c20     stored passwords shorter than 12 characters still sign in
          (``test_short_legacy_password_still_signs_in``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator

import aiohttp
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens import guest_store, metrics
from irc_lens.guest_store import GuestStore
from irc_lens.mail import RecordingAdapter
from irc_lens.session import Session
from irc_lens.web import app_session, entry, make_app
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.sessions import BACKEND_MESH
from test_session_routing import _config

ALICE = "alice@example.com"
UNKNOWN = "zed@example.org"
PW = secrets.token_urlsafe(16)  # generated per run: no literal secret
WRONG_PW = secrets.token_urlsafe(16)
SHORT_PW = secrets.token_urlsafe(6)[:7]  # < 12 chars
SECRET = b"s" * 32
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
T0 = 1_800_000_000.0
COPY = (
    "If your email and password are right, a code is on its way. "
    "Nothing after a minute? Go back and re-enter your password."
)
CODE_WRONG = entry.ERR_CODE


class FakeVerifier:
    site_key = None

    def __init__(self) -> None:
        self.ok = True

    async def verify(self, token: str, ip: str) -> bool:
        return self.ok


class GatedAdapter(RecordingAdapter):
    """Blocks in send() until released: proves the send is off the request."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def send(self, to: str, subject: str, body: str) -> None:
        self.entered.set()
        assert self.release.wait(10)
        super().send(to, subject, body)


@allows_anonymous
async def _whoami(request: web.Request) -> web.Response:
    ident = request["identity"]
    return web.json_response({"tier": ident.tier, "principal": ident.principal})


class Env:
    def __init__(self, client, app, store, clock, mesh) -> None:
        self.client, self.app, self.store = client, app, store
        self.clock, self.mesh = clock, mesh
        self.state: entry.EntryState = app[entry.ENTRY_STATE]

    @property
    def mail(self) -> RecordingAdapter:
        return self.state.mailer

    async def drain(self) -> None:
        tasks = list(self.state.mail_tasks)
        if tasks:
            await asyncio.gather(*tasks)

    async def signin(
        self, email: str, password: str, ip: str = "203.0.113.7", cookie: str = ""
    ):
        headers = {"CF-Connecting-IP": ip, **SAME_ORIGIN}
        if cookie:
            headers["Cookie"] = cookie
        return await self.client.post(
            "/entry/signin",
            data={"email": email, "password": password},
            headers=headers,
            allow_redirects=False,
        )

    async def code(
        self,
        email: str,
        code: str,
        pending: str | None,
        ip: str = "203.0.113.7",
        extra_cookie: str = "",
    ):
        headers = {"CF-Connecting-IP": ip, **SAME_ORIGIN}
        cookies = []
        if pending is not None:
            cookies.append(f"{app_session.SIGNIN_COOKIE_NAME}={pending}")
        if extra_cookie:
            cookies.append(extra_cookie)
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        return await self.client.post(
            "/entry/code",
            data={"email": email, "code": code},
            headers=headers,
            allow_redirects=False,
        )

    def last_code(self) -> str:
        _to, _subject, body = self.mail.sent[-1]
        return re.search(r"^ {4}(\S+)$", body, flags=re.M).group(1)

    async def password_and_code(self, ip: str = "203.0.113.7") -> tuple[str, str]:
        r = await self.signin(ALICE, PW, ip=ip)
        assert r.status == 200
        await self.drain()
        return pending_of(r), self.last_code()


def roomy_budget(monkeypatch) -> None:
    """Lift the per-email untrusted budget (r6) for tests about other limits."""
    monkeypatch.setattr(guest_store, "SIGNIN_BUDGET", (100, 900))


def pending_of(resp) -> str:
    return resp.cookies[app_session.SIGNIN_COOKIE_NAME].value


async def _make_env(jwks, tmp_path, monkeypatch, **cfg_kw) -> Env:
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", SECRET.decode())
    mesh = AgentIRCTestServer()
    await mesh.start()
    cfg_kw.setdefault("allowed_emails", (ALICE,))
    config = dataclasses.replace(_config(jwks, mesh, mesh, tmp_path), **cfg_kw)
    clock = {"t": T0}
    store = GuestStore(tmp_path / "signin.db", clock=lambda: clock["t"])
    store.set_password(ALICE, PW)

    def mesh_factory(nick: str) -> Session:
        return Session(host=mesh.host, port=mesh.port, nick=nick)

    app = make_app(config, mesh_factory)
    app["guest_store"] = store
    state = app[entry.ENTRY_STATE]
    state.store = store
    state.mailer = RecordingAdapter()
    state.verifier = FakeVerifier()
    state.signin_floor_s = 0.0
    app.router.add_get("/_whoami", _whoami)
    # Each request carries exactly the cookies the test names (one jar per
    # "browser" is simulated by hand).
    client = TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    return Env(client, app, store, clock, mesh)


@pytest_asyncio.fixture
async def env(jwks: FakeJWKS, tmp_path, monkeypatch) -> AsyncIterator[Env]:
    e = await _make_env(jwks, tmp_path, monkeypatch)
    try:
        yield e
    finally:
        await e.drain()
        for s in e.app["registry"].values():
            await s.disconnect()
        await e.client.close()
        await e.mesh.stop()


# ---------------------------------------------------------------------------
# POST /entry/signin: one answer for every case (c8/h4, c10/h6)
# ---------------------------------------------------------------------------

_VOLATILE = {"Date", "Content-Length", "Set-Cookie"}


def _shape(resp, body: str, email: str) -> tuple:
    headers = tuple(
        sorted((k, v) for k, v in resp.headers.items() if k not in _VOLATILE)
    )
    cookies = tuple(
        re.sub(r"lens_signin=[^;]*", "lens_signin=<P>", v)
        for v in resp.headers.getall("Set-Cookie", [])
    )
    return resp.status, headers, cookies, body.replace(email, "<E>")


async def test_signin_four_cases_identical_and_floored(env: Env) -> None:
    env.state.signin_floor_s = entry.SIGNIN_FLOOR_S  # the real floor
    # Use up the per-IP sign-in budget of 198.51.100.99 (3 per 15 minutes).
    for _ in range(entry.SIGNIN_IP_ATTEMPTS_PER_15MIN):
        await env.signin(UNKNOWN, "x", ip="198.51.100.99")
    env.mail.sent.clear()
    cases = [
        ("unknown", UNKNOWN, PW, "198.51.100.1"),
        ("wrong", ALICE, WRONG_PW, "198.51.100.2"),
        ("right", ALICE, PW, "198.51.100.3"),
        ("rate-limited", ALICE, PW, "198.51.100.99"),
    ]
    shapes, pendings = {}, set()
    for name, email, pw, ip in cases:
        t0 = time.perf_counter()
        r = await env.signin(email, pw, ip=ip)
        body = await r.text()
        elapsed = time.perf_counter() - t0
        assert elapsed >= entry.SIGNIN_FLOOR_S, (name, elapsed)
        assert r.status == 200, name
        assert len(r.headers.getall("Set-Cookie")) == 1, name
        pendings.add(pending_of(r))
        shapes[name] = _shape(r, body, email)
    assert len(set(shapes.values())) == 1, "every sign-in answer must be identical"
    assert len(pendings) == 4, "a fresh pending value every time"
    await env.drain()
    # Only the right (non-limited) case mailed, exactly once.
    assert [(to, subj) for to, subj, _ in env.mail.sent] == [
        (ALICE, "Your chat.culture.dev sign-in code")
    ]


async def test_signin_mail_is_sent_off_the_request_path(env: Env) -> None:
    gated = GatedAdapter()
    env.state.mailer = gated
    r = await env.signin(ALICE, PW)
    # The response is complete while the send is still blocked.
    assert r.status == 200
    await r.text()
    assert gated.sent == []
    assert len(env.state.mail_tasks) == 1
    gated.release.set()
    await env.drain()
    assert len(gated.sent) == 1
    assert gated.sent[0][0] == ALICE


async def test_code_screen_copy_and_form(env: Env) -> None:
    for i, (email, pw) in enumerate(((ALICE, PW), (ALICE, WRONG_PW), (UNKNOWN, WRONG_PW))):
        r = await env.signin(email, pw, ip=f"192.0.2.{20 + i}")
        html = await r.text()
        assert COPY in html
        assert 'action="/entry/code"' in html
        assert 'autocomplete="one-time-code"' in html
        assert 'formaction="/entry/email"' in html  # Back to the password
        assert entry.ERR_SIGNIN not in html
        assert 'name="nickname"' not in html


def test_signin_cookie_is_strict_pending_cookie() -> None:
    resp = web.Response()
    app_session.set_signin_cookie(resp, "v")
    header = resp.cookies["lens_signin"].OutputString()
    assert "SameSite=Strict" in header
    assert "Path=/entry" in header


# ---------------------------------------------------------------------------
# POST /entry/code: same browser, single use, 10 minutes, 5 tries (c11/h7)
# ---------------------------------------------------------------------------


async def test_full_signin_reaches_real_mesh(env: Env) -> None:
    started = metrics.get_metrics().snapshot()["sessions_started"]
    fixation = secrets.token_urlsafe(32)  # a session id the browser held before
    pending, code = await env.password_and_code()
    r = await env.code(ALICE, code, pending, extra_cookie=f"lens_session={fixation}")
    assert r.status == 303
    assert r.headers["Location"] == "/"
    raw = r.cookies[app_session.SESSION_COOKIE_NAME].value
    assert raw not in (fixation, pending, code)
    assert env.store.get_session(raw)[0] == ALICE
    assert env.store.get_session(fixation) is None  # never promoted
    cleared = r.cookies[app_session.SIGNIN_COOKIE_NAME]
    assert cleared.value == ""
    assert cleared["max-age"] == "0"
    assert metrics.get_metrics().snapshot()["sessions_started"] == started + 1
    # The session is the approved tier, with no Cloudflare Access JWT at all...
    cookie = {"Cookie": f"lens_session={raw}", **SAME_ORIGIN}
    who = await (await env.client.get("/_whoami", headers=cookie)).json()
    assert who == {"tier": "approved", "principal": ALICE}
    # ...and the console opens the real mesh.
    assert (await env.client.get("/", headers=cookie)).status == 200
    assert env.app["registry"].has(ALICE, BACKEND_MESH)


async def test_correct_password_without_mailbox_never_yields_session(env: Env) -> None:
    r = await env.signin(ALICE, PW)
    pending = pending_of(r)
    for _ in range(6):
        guess = secrets.token_urlsafe(32)
        resp = await env.code(ALICE, guess, pending)
        assert resp.status == 401
        assert app_session.SESSION_COOKIE_NAME not in resp.cookies
    assert env.store._all("SELECT COUNT(*) FROM sessions")[0][0] == 0


async def test_code_from_another_browser_fails(env: Env, monkeypatch) -> None:
    roomy_budget(monkeypatch)  # 5 tries on one email; the budget is not under test
    pending_a, code_a = await env.password_and_code(ip="192.0.2.10")
    # Browser B ran the password step itself and holds its own pending value.
    r_b = await env.signin(ALICE, PW, ip="192.0.2.11")
    pending_b = pending_of(r_b)
    await env.drain()
    assert (await env.code(ALICE, code_a, pending_b, ip="192.0.2.11")).status == 401
    assert (await env.code(ALICE, code_a, None, ip="192.0.2.11")).status == 401
    # Still good in browser A: the failures were the binding, not the code.
    assert (await env.code(ALICE, code_a, pending_a, ip="192.0.2.10")).status == 303


async def test_code_bound_to_its_email(env: Env) -> None:
    pending, code = await env.password_and_code()
    assert (await env.code(UNKNOWN, code, pending)).status == 401


async def test_code_is_single_use(env: Env) -> None:
    pending, code = await env.password_and_code()
    assert (await env.code(ALICE, code, pending)).status == 303
    r = await env.code(ALICE, code, pending)
    assert r.status == 401
    assert CODE_WRONG in await r.text()


async def test_code_expires_after_ten_minutes(env: Env) -> None:
    pending, code = await env.password_and_code()
    env.clock["t"] = T0 + 11 * 60
    assert (await env.code(ALICE, code, pending)).status == 401


async def test_code_still_good_just_under_ten_minutes(env: Env) -> None:
    pending, code = await env.password_and_code()
    env.clock["t"] = T0 + 9 * 60
    assert (await env.code(ALICE, code, pending)).status == 303


async def test_wrong_codes_never_lock_out_the_right_one(env: Env) -> None:
    # c41/d7: code entries count toward nothing. A code is 256 random bits
    # bound to the browser that entered the right password, so guessing is
    # infeasible; any number of wrong entries leaves the right code working.
    pending, code = await env.password_and_code(ip="10.1.0.1")
    for _ in range(20):
        assert (await env.code(ALICE, "nope", pending, ip="10.1.0.1")).status == 401
    assert (await env.code(ALICE, code, pending, ip="10.1.0.1")).status == 303


async def test_code_entries_do_not_use_the_ip_limit(env: Env) -> None:
    # c41/d7: the per-IP limit (3 per 15 minutes) counts password
    # submissions only; code entries from the IP leave it untouched.
    ip = "10.2.0.1"
    pending, _code = await env.password_and_code(ip=ip)  # password 1
    for i in range(10):
        await env.code(f"u{i}@example.com", "nope", pending, ip=ip)
    for _ in range(2):  # passwords 2 and 3 are still checked and mailed
        before = len(env.mail.sent)
        await env.signin(ALICE, PW, ip=ip)
        await env.drain()
        assert len(env.mail.sent) == before + 1


async def test_every_code_failure_is_the_one_error(env: Env, monkeypatch) -> None:
    roomy_budget(monkeypatch)  # several sign-ins; the budget is not under test
    bodies = set()

    async def record(resp) -> None:
        assert resp.status == 401
        bodies.add(await resp.text())

    pending, code = await env.password_and_code(ip="10.3.0.1")
    await record(await env.code(ALICE, "wrong", pending, ip="10.3.0.2"))  # wrong
    await record(await env.code(ALICE, code, None, ip="10.3.0.3"))  # no cookie
    assert (await env.code(ALICE, code, pending, ip="10.3.0.4")).status == 303
    await record(await env.code(ALICE, code, pending, ip="10.3.0.5"))  # reused
    pending, code = await env.password_and_code(ip="10.3.0.6")
    env.clock["t"] += 11 * 60
    await record(await env.code(ALICE, code, pending, ip="10.3.0.7"))  # expired
    assert len(bodies) == 1
    assert CODE_WRONG in bodies.pop()


# ---------------------------------------------------------------------------
# Rate limits (c14/h10)
# ---------------------------------------------------------------------------


async def test_password_attempts_limited_per_ip_without_check(
    env: Env, monkeypatch
) -> None:
    roomy_budget(monkeypatch)  # 5 tries on one email: only the IP limit is tested
    calls: list[str] = []
    real = env.store.check_password

    def counting(email: str, password: str) -> bool:
        calls.append(email)
        return real(email, password)

    monkeypatch.setattr(env.store, "check_password", counting)
    # r6/c38: 3 per IP per 15 minutes on the app path (was 5).
    for _ in range(3):
        await env.signin(ALICE, WRONG_PW, ip="10.4.0.1")
    assert len(calls) == 3
    before = await (await env.signin(ALICE, WRONG_PW, ip="10.4.0.2")).text()
    r = await env.signin(ALICE, PW, ip="10.4.0.1")  # 4th from this IP
    assert r.status == 200
    assert await r.text() == before  # same screen
    assert len(calls) == 4  # only the other IP's attempt was checked
    await env.drain()
    assert env.mail.sent == []


async def test_distributed_attack_blocks_untrusted_but_not_trusted(env: Env) -> None:
    # r6 replaces "no per-email lockout, 5 codes per 15 minutes": a trusted
    # browser is never limited, untrusted ones share one per-email budget.
    device = env.store.add_trusted_device(ALICE)
    # A distributed attack: 30 wrong passwords for the owner from 30 IPs.
    for i in range(30):
        await env.signin(ALICE, "guess", ip=f"10.5.{i}.1")
    # Untrusted browsers: the same screen, but no code is mailed.
    bodies = set()
    for i in range(3):
        r = await env.signin(ALICE, PW, ip=f"10.6.{i}.1")
        assert r.status == 200
        bodies.add(await r.text())
    await env.drain()
    assert env.mail.sent == []
    assert len(bodies) == 1
    # The owner's trusted browser still gets a code every time.
    for i in range(5):
        r = await env.signin(
            ALICE, PW, ip=f"10.7.{i}.1", cookie=f"{app_session.DEVICE_COOKIE_NAME}={device}"
        )
        assert await r.text() in bodies
    await env.drain()
    assert len(env.mail.sent) == 5


# ---------------------------------------------------------------------------
# Legacy passwords (c20), the switch (c32/h23, c7/h3), hygiene
# ---------------------------------------------------------------------------


async def test_short_legacy_password_still_signs_in(env: Env) -> None:
    env.store.set_password(ALICE, SHORT_PW)  # < 12 chars, set before the rule
    r = await env.signin(ALICE, SHORT_PW)
    await env.drain()
    assert len(env.mail.sent) == 1
    assert (await env.code(ALICE, env.last_code(), pending_of(r))).status == 303


@pytest_asyncio.fixture
async def env_off(jwks: FakeJWKS, tmp_path, monkeypatch) -> AsyncIterator[Env]:
    e = await _make_env(jwks, tmp_path, monkeypatch, app_signin_enabled=False)
    try:
        yield e
    finally:
        await e.client.close()
        await e.mesh.stop()


async def test_switch_off_restores_login_redirect(env_off: Env) -> None:
    r = await env_off.signin(ALICE, PW)
    assert r.status == 303
    assert r.headers["Location"] == "/login"
    assert app_session.SIGNIN_COOKIE_NAME not in r.cookies
    r = await env_off.signin(ALICE, WRONG_PW, ip="10.8.0.2")
    assert r.status == 401
    assert entry.ERR_SIGNIN in await r.text()
    assert env_off.mail.sent == []
    # No code step exists with the switch off.
    assert (await env_off.code(ALICE, "x", "p")).status == 404


async def test_no_secret_is_logged(env: Env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    r = await env.signin(ALICE, PW)
    pending = pending_of(r)
    await env.drain()
    code = env.last_code()
    ok = await env.code(ALICE, code, pending)
    raw = ok.cookies[app_session.SESSION_COOKIE_NAME].value
    for secret in (PW, pending, code, raw):
        assert secret not in caplog.text
