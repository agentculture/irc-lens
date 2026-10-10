"""Entry card (guest mode, task t9).

Acceptance criteria / obligations covered:

1. POST email always returns the same password window; sign-in success
   (approved email + correct password) 303s to ``/login``; every failure is
   the single ``Email or password is wrong`` with identical status/body and
   timing within 50 ms (o4 — probe test below).
2. Guest mode -> nickname + Terms/Privacy consent -> token emailed -> token
   entry; tokens single use, 15-minute expiry; attempts rate limited per
   email and per IP with the same generic response (429 after limit) (o6).
   One email template for every address (o5).
3. Guest nick is ``sbx-<nickname>``, sanitized, unique, never derived from
   the email.
4. Bot protection is pluggable (Turnstile when configured, scoped CSP
   allowance on the entry page only); the card carries no explanatory prose.
"""

from __future__ import annotations

import re
import secrets
import statistics
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _jwks_server import FakeJWKS
from irc_lens import legal, metrics
from irc_lens.config import LensConfig
from irc_lens.guest_store import GuestStore
from irc_lens.mail import RecordingAdapter, render_token_email
from irc_lens.web import csrf, entry, make_app

APPROVED = "alice@example.com"
APPROVED_PW = secrets.token_urlsafe(16)  # generated per run: no literal secret
UNKNOWN = "mallory@example.org"
GUEST = "guest.person@example.net"
VERSIONS = {"terms": "t-2026-10", "privacy": "p-2026-10", "effective": "2026-10-01"}
WRONG = "Email or password is wrong"
CODE_WRONG = "Wrong or expired code"


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


class FakeVerifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []
        self.site_key = None

    async def verify(self, token: str, ip: str) -> bool:
        self.calls.append((token, ip))
        return self.ok


def _config(jwks: FakeJWKS, legal_url: str, *, guest: bool = True) -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=(APPROVED,),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=False,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
        guest_enabled=guest,
        guest_legal_version_url=legal_url,
        guest_rate_entry_per_min=3,
        guest_rate_password_attempts_per_15min=4,
    )


def _boom_factory(_nick: str):
    raise AssertionError("no real-mesh session in entry tests")


@pytest_asyncio.fixture
async def legal_server() -> AsyncIterator[str]:
    async def handler(_request):
        return web.json_response(VERSIONS)

    app = web.Application()
    app.router.add_get("/version.json", handler)
    server = TestServer(app)
    await server.start_server()
    legal.clear_cache()
    try:
        yield str(server.make_url("/version.json"))
    finally:
        legal.clear_cache()
        await server.close()


class Harness:
    def __init__(self, client: TestClient, store: GuestStore, clock: Clock) -> None:
        self.client = client
        self.store = store
        self.clock = clock
        self.state: entry.EntryState = client.app[entry.ENTRY_STATE]
        self.mail: RecordingAdapter = self.state.mailer
        self.verifier: FakeVerifier = self.state.verifier

    async def post(self, path: str, data: dict, ip: str = "203.0.113.7"):
        return await self.client.post(
            path, data=data, headers={"CF-Connecting-IP": ip}, allow_redirects=False
        )


async def _harness(config: LensConfig, tmp_path) -> Harness:
    clock = Clock()
    store = GuestStore(tmp_path / "guests.db", clock=clock)
    store.set_password(APPROVED, APPROVED_PW)
    app = make_app(config, _boom_factory)
    state = app[entry.ENTRY_STATE]
    state.store = store
    state.mailer = RecordingAdapter()
    state.verifier = FakeVerifier()
    state.signin_floor_s = 0.0
    client = TestClient(TestServer(app))
    await client.start_server()
    return Harness(client, store, clock)


@pytest_asyncio.fixture
async def h(jwks, legal_server, tmp_path) -> AsyncIterator[Harness]:
    harness = await _harness(_config(jwks, legal_server), tmp_path)
    try:
        yield harness
    finally:
        await harness.client.close()


@pytest_asyncio.fixture
async def h_off(jwks, legal_server, tmp_path) -> AsyncIterator[Harness]:
    harness = await _harness(_config(jwks, legal_server, guest=False), tmp_path)
    try:
        yield harness
    finally:
        await harness.client.close()


def _visible_chunks(html: str) -> list[str]:
    """Each element's visible text, one chunk per element."""
    body = html.split("<body", 1)[1]
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "\n", body, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", body)
    return [
        re.sub(r"\s+", " ", line).strip() for line in text.split("\n") if line.strip()
    ]


def _token_from_mail(h: Harness) -> str:
    _to, _subject, body = h.mail.sent[-1]
    return re.search(r"^ {4}(\S+)$", body, flags=re.M).group(1)


async def _request_token(h: Harness, email=GUEST, nickname="maya", ip="203.0.113.7"):
    return await h.post(
        "/entry/guest/start",
        {"email": email, "nickname": nickname, "consent": "on"},
        ip=ip,
    )


# ---------------------------------------------------------------------------
# Step 1 / step 2: email -> password window
# ---------------------------------------------------------------------------


async def test_get_entry_renders_step_one(h):
    resp = await h.client.get("/entry")
    assert resp.status == 200
    html = await resp.text()
    assert 'action="/entry/email"' in html
    assert 'name="email"' in html
    assert "Ask culture." in html
    assert "/static/entry.css" in html
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]


async def test_entry_pages_send_origin_on_same_origin_posts(h):
    """no-referrer makes browsers send ``Origin: null`` on form POSTs (403)."""
    for resp in (
        await h.client.get("/entry"),
        await h.post("/entry/email", {"email": GUEST}),
    ):
        assert resp.headers["Referrer-Policy"] == "same-origin"
    other = await h.client.get("/healthz")
    assert other.headers["Referrer-Policy"] == "no-referrer"


async def test_get_entry_seam_is_callable_directly(h):
    assert callable(entry.get_entry)
    # The routing task mounts get_entry on "/" for anonymous visitors.
    assert getattr(entry.get_entry, "_irc_lens_allows_anonymous", False)


async def test_lapsed_cf_cookie_sees_entry_card(h):
    resp = await h.client.get(
        "/entry", headers={"Cookie": "CF_Authorization=expired.garbage.jwt"}
    )
    assert resp.status == 200
    assert 'action="/entry/email"' in await resp.text()


async def test_email_step_identical_for_approved_and_unknown(h):
    a = await h.post("/entry/email", {"email": APPROVED})
    u = await h.post("/entry/email", {"email": UNKNOWN})
    assert a.status == u.status == 200
    a_body = (await a.text()).replace(APPROVED, "<E>")
    u_body = (await u.text()).replace(UNKNOWN, "<E>")
    assert a_body == u_body
    assert 'name="password"' in a_body
    assert 'formaction="/entry/guest"' in a_body
    assert "Sign in" in a_body
    assert "Guest mode" in a_body


async def test_email_step_always_password_window_even_for_junk(h):
    resp = await h.post("/entry/email", {"email": "not-an-email"})
    assert resp.status == 200
    assert 'name="password"' in await resp.text()


# ---------------------------------------------------------------------------
# Sign in (o4)
# ---------------------------------------------------------------------------


async def test_sign_in_success_redirects_to_login(h):
    resp = await h.post("/entry/signin", {"email": APPROVED, "password": APPROVED_PW})
    assert resp.status == 303
    assert resp.headers["Location"] == "/login"


async def test_login_landing_approved_jwt_redirects_to_root(h, jwks):
    token = jwks.mint(aud="aud-test", claims={"email": APPROVED, "sub": "s"})
    resp = await h.client.get(
        "/login",
        headers={"Cf-Access-Jwt-Assertion": token},
        allow_redirects=False,
    )
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert resp.headers["Cache-Control"] == "no-store"


async def test_login_landing_anonymous_redirects_to_root(h):
    resp = await h.client.get("/login", allow_redirects=False)
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert resp.headers["Cache-Control"] == "no-store"


async def test_login_landing_same_for_garbage_cookie(h):
    anon = await h.client.get("/login", allow_redirects=False)
    bad = await h.client.get(
        "/login",
        headers={"Cookie": "CF_Authorization=expired.garbage.jwt"},
        allow_redirects=False,
    )
    assert (bad.status, bad.headers["Location"]) == (anon.status, anon.headers["Location"])


async def test_password_step_has_hidden_username_and_current_password(h):
    resp = await h.post("/entry/email", {"email": APPROVED})
    html = await resp.text()
    m = re.search(r'<input[^>]*name="username"[^>]*>', html)
    assert m, "password form needs a username field"
    tag = m.group(0)
    assert 'autocomplete="username"' in tag
    assert f'value="{APPROVED}"' in tag
    assert "readonly" in tag
    assert 'tabindex="-1"' in tag
    assert re.search(r'<input[^>]*type="password"[^>]*autocomplete="current-password"', html)


async def test_sign_in_failures_identical(h):
    cases = [
        {"email": APPROVED, "password": "wrong"},
        {"email": UNKNOWN, "password": "wrong"},
        {"email": UNKNOWN, "password": APPROVED_PW},
        {"email": APPROVED, "password": ""},
    ]
    seen = set()
    for i, data in enumerate(cases):
        resp = await h.post("/entry/signin", data, ip=f"198.51.100.{i}")
        body = (await resp.text()).replace(data["email"], "<E>")
        assert WRONG in body
        seen.add((resp.status, body))
    assert len(seen) == 1, "every sign-in failure must look the same"


async def test_password_correct_but_not_allowlisted_fails(h):
    ex_member_pw = secrets.token_urlsafe(12)  # generated: no literal to flag
    h.store.set_password(UNKNOWN, ex_member_pw)
    resp = await h.post(
        "/entry/signin", {"email": UNKNOWN, "password": ex_member_pw}
    )
    assert resp.status != 303
    assert WRONG in await resp.text()


async def test_sign_in_bot_check_failure_is_the_generic_error(h):
    h.verifier.ok = False
    resp = await h.post("/entry/signin", {"email": APPROVED, "password": APPROVED_PW})
    assert resp.status != 303
    assert WRONG in await resp.text()


async def test_failed_sign_in_metric(h):
    before = metrics.get_metrics().snapshot()["failed_sign_ins"]
    await h.post("/entry/signin", {"email": UNKNOWN, "password": "x"})
    assert metrics.get_metrics().snapshot()["failed_sign_ins"] == before + 1


async def test_sign_in_rate_limited_per_email_same_body(h):
    limit = 4
    for i in range(limit):
        r = await h.post(
            "/entry/signin", {"email": APPROVED, "password": "x"}, ip=f"10.0.0.{i}"
        )
        assert r.status == 401
    fail_body = await r.text()
    # Over the limit even with the right password and a fresh IP.
    r = await h.post(
        "/entry/signin", {"email": APPROVED, "password": APPROVED_PW}, ip="10.9.9.9"
    )
    assert r.status == 429
    assert await r.text() == fail_body


async def test_sign_in_rate_limited_per_ip(h):
    for i in range(4):
        await h.post("/entry/signin", {"email": f"u{i}@example.com", "password": "x"})
    r = await h.post("/entry/signin", {"email": APPROVED, "password": APPROVED_PW})
    assert r.status == 429
    assert WRONG in await r.text()


async def test_sign_in_timing_probe_approved_vs_unknown(jwks, legal_server, tmp_path):
    """o4 timing probe: approved-email failure vs unknown-email failure."""
    harness = await _harness(_config(jwks, legal_server), tmp_path)
    harness.state.signin_floor_s = entry.SIGNIN_FLOOR_S  # the real floor
    try:

        async def probe(email: str, ip: str) -> float:
            t0 = time.perf_counter()
            r = await harness.post(
                "/entry/signin", {"email": email, "password": "nope"}, ip=ip
            )
            await r.read()
            assert r.status == 401
            return time.perf_counter() - t0

        approved, unknown = [], []
        for i in range(3):
            approved.append(await probe(APPROVED, f"192.0.2.{i}"))
            unknown.append(await probe(f"x{i}@example.org", f"192.0.2.{100 + i}"))
        delta = abs(statistics.median(approved) - statistics.median(unknown))
        assert delta < 0.050, (approved, unknown)
    finally:
        await harness.client.close()


# ---------------------------------------------------------------------------
# Guest mode: nickname + consent -> token -> verify
# ---------------------------------------------------------------------------


async def test_guest_button_shows_nickname_and_consent(h):
    resp = await h.post("/entry/guest", {"email": GUEST})
    assert resp.status == 200
    html = await resp.text()
    assert 'name="nickname"' in html
    assert "sbx-" in html
    assert 'name="consent"' in html
    assert "https://culture.dev/terms" in html
    assert "https://culture.dev/privacy" in html
    assert 'action="/entry/guest/start"' in html


async def test_full_guest_flow_sets_cookie_and_records(h):
    resp = await _request_token(h)
    assert resp.status == 200
    html = await resp.text()
    assert 'name="code"' in html
    assert 'action="/entry/verify"' in html
    assert len(h.mail.sent) == 1
    assert h.mail.sent[0][0] == GUEST
    token = _token_from_mail(h)
    before = metrics.get_metrics().snapshot()["entries"]
    resp = await h.post(
        "/entry/verify", {"email": GUEST, "nickname": "maya", "code": token}
    )
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert csrf.GUEST_COOKIE_NAME in resp.cookies
    secret = h.client.app[csrf.SECRET_KEY]
    assert (
        csrf.verify_cookie_value(resp.cookies[csrf.GUEST_COOKIE_NAME].value, secret)
        == GUEST
    )
    assert h.store.get_guest(GUEST) == [(GUEST, "sbx-maya", "203.0.113.7")]
    consents = h.store.get_consents(GUEST)
    assert consents[-1][2:4] == (VERSIONS["terms"], VERSIONS["privacy"])
    assert legal.consent_is_current(h.store, GUEST, VERSIONS)
    assert metrics.get_metrics().snapshot()["entries"] == before + 1


async def test_token_single_use(h):
    await _request_token(h)
    token = _token_from_mail(h)
    data = {"email": GUEST, "nickname": "maya", "code": token}
    assert (await h.post("/entry/verify", data)).status == 303
    again = await h.post("/entry/verify", data)
    assert again.status == 401
    assert CODE_WRONG in await again.text()


async def test_token_expires_after_15_minutes(h):
    await _request_token(h)
    token = _token_from_mail(h)
    h.clock.now += 15 * 60 + 1
    resp = await h.post(
        "/entry/verify", {"email": GUEST, "nickname": "maya", "code": token}
    )
    assert resp.status == 401
    assert CODE_WRONG in await resp.text()


async def test_token_bound_to_email(h):
    await _request_token(h)
    token = _token_from_mail(h)
    resp = await h.post(
        "/entry/verify", {"email": UNKNOWN, "nickname": "maya", "code": token}
    )
    assert resp.status == 401


async def test_verify_rate_limited_per_email_same_body(h):
    await _request_token(h)
    token = _token_from_mail(h)
    for i in range(4):
        r = await h.post(
            "/entry/verify",
            {"email": GUEST, "nickname": "maya", "code": "bad"},
            ip=f"10.1.0.{i}",
        )
        assert r.status == 401
    body = await r.text()
    r = await h.post(
        "/entry/verify",
        {"email": GUEST, "nickname": "maya", "code": token},
        ip="10.1.9.9",
    )
    assert r.status == 429
    assert await r.text() == body


async def test_verify_rate_limited_per_ip(h):
    for i in range(4):
        await h.post(
            "/entry/verify",
            {"email": f"v{i}@example.com", "nickname": "maya", "code": "x"},
        )
    r = await h.post("/entry/verify", {"email": GUEST, "nickname": "maya", "code": "x"})
    assert r.status == 429
    assert CODE_WRONG in await r.text()


async def test_token_request_rate_limited_per_email(h):
    for i in range(3):
        r = await _request_token(h, ip=f"10.2.0.{i}")
        assert r.status == 200
    ok_body = await r.text()
    r = await _request_token(h, ip="10.2.9.9")
    assert r.status == 429
    assert await r.text() == ok_body  # same generic response
    assert len(h.mail.sent) == 3  # nothing sent past the limit


async def test_token_request_rate_limited_per_ip(h):
    for i in range(3):
        await _request_token(h, email=f"r{i}@example.com", nickname=f"nick{i}")
    r = await _request_token(h, email="fresh@example.com", nickname="fresh")
    assert r.status == 429
    assert len(h.mail.sent) == 3


async def test_same_email_template_for_every_address(h):
    await _request_token(h, email=APPROVED, nickname="alice", ip="10.3.0.1")
    await _request_token(h, email=UNKNOWN, nickname="mal", ip="10.3.0.2")
    (_, s1, b1), (_, s2, b2) = h.mail.sent
    t1, t2 = re.search(r"^ {4}(\S+)$", b1, re.M).group(1), re.search(
        r"^ {4}(\S+)$", b2, re.M
    ).group(1)
    assert render_token_email(t1) == (s1, b1)
    assert render_token_email(t2) == (s2, b2)
    assert "code" in b1.lower()
    assert "token" not in b1.lower()
    assert "15 minutes" in b1
    assert b1.replace(t1, "T") == b2.replace(t2, "T")


async def test_token_step_identical_for_approved_and_unknown(h):
    a = await _request_token(h, email=APPROVED, nickname="nicka", ip="10.4.0.1")
    u = await _request_token(h, email=UNKNOWN, nickname="nicka", ip="10.4.0.2")
    assert a.status == u.status == 200
    assert (await a.text()).replace(APPROVED, "<E>") == (await u.text()).replace(
        UNKNOWN, "<E>"
    )


async def test_banned_email_gets_same_response_but_no_mail(h):
    h.store.ban(email=GUEST, reason="abuse")
    r = await _request_token(h)
    assert r.status == 200
    assert 'name="code"' in await r.text()
    assert h.mail.sent == []


async def test_mail_failure_still_same_response(h):
    class Broken:
        def send(self, to, subject, body):
            raise RuntimeError("provider down")

    h.state.mailer = Broken()
    r = await _request_token(h)
    assert r.status == 200
    assert 'name="code"' in await r.text()


async def test_consent_required(h):
    r = await h.post("/entry/guest/start", {"email": GUEST, "nickname": "maya"})
    assert r.status == 400
    assert h.mail.sent == []
    assert 'name="consent"' in await r.text()


async def test_guest_start_bot_check(h):
    h.verifier.ok = False
    r = await _request_token(h)
    assert r.status == 400
    assert h.mail.sent == []


# ---------------------------------------------------------------------------
# Nick rules (criterion 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Maya", "maya"),
        ("ma ya!", "maya"),
        ("dash-and_under", "dashand_under"),
        ("a", None),  # too short
        ("x" * 17, None),  # too long
        ("!!", None),  # sanitizes to empty
        ("ünï", None),  # sanitizes to "n"
    ],
)
def test_sanitize_nickname(raw, expected):
    assert entry.sanitize_nickname(raw) == expected


async def test_nick_is_sbx_prefixed_and_not_from_email(h):
    await _request_token(h, email="secretname@example.com", nickname="Zed_9")
    token = _token_from_mail(h)
    r = await h.post(
        "/entry/verify",
        {"email": "secretname@example.com", "nickname": "Zed_9", "code": token},
    )
    assert r.status == 303
    ((_, nick, _),) = h.store.get_guest("secretname@example.com")
    assert nick == "sbx-zed_9"
    assert "secretname" not in nick


async def test_nick_must_be_unique(h):
    h.store.record_guest("first@example.com", "sbx-maya", "1.1.1.1")
    r = await _request_token(h, nickname="MAYA")
    assert r.status == 400
    assert h.mail.sent == []
    assert 'name="nickname"' in await r.text()


async def test_same_guest_may_reuse_own_nick(h):
    h.store.record_guest(GUEST, "sbx-maya", "1.1.1.1")
    r = await _request_token(h, nickname="maya")
    assert r.status == 200


async def test_agent_nick_reserved(h):
    r = await _request_token(h, nickname="ask")
    assert r.status == 400
    assert h.mail.sent == []


async def test_nick_taken_between_request_and_verify(h):
    await _request_token(h)
    token = _token_from_mail(h)
    h.store.record_guest("racer@example.com", "sbx-maya", "1.1.1.1")
    r = await h.post(
        "/entry/verify", {"email": GUEST, "nickname": "maya", "code": token}
    )
    assert r.status == 400
    assert csrf.GUEST_COOKIE_NAME not in r.cookies


# ---------------------------------------------------------------------------
# Guest mode off -> 404; no prose; CSP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/entry"),
        ("POST", "/entry/email"),
        ("POST", "/entry/signin"),
        ("POST", "/entry/guest"),
        ("POST", "/entry/guest/start"),
        ("POST", "/entry/verify"),
    ],
)
async def test_routes_404_when_guest_mode_off(h_off, jwks, method, path):
    token = jwks.mint(aud="aud-test", claims={"email": APPROVED})
    resp = await h_off.client.request(
        method, path, headers={"Cf-Access-Jwt-Assertion": token}, data={}
    )
    assert resp.status == 404


async def test_no_explanatory_prose(h):
    pages = [
        await (await h.client.get("/entry")).text(),
        await (await h.post("/entry/email", {"email": GUEST})).text(),
        await (await h.post("/entry/signin", {"email": GUEST, "password": "x"})).text(),
        await (await h.post("/entry/guest", {"email": GUEST})).text(),
        await (await _request_token(h, ip="10.5.0.1")).text(),
    ]
    transcript = {
        "Ask culture.",
        "sbx-maya",
        "sbx-ask",
        "what is the all-backends rule?",
        "A feature added to one enforced backend (claude, codex, colleague) "
        "must reach all of them. A feature in only one backend is a bug.",
        "can you run ls / for me?",
        "I can't. I have no tools here, only answers.",
        "I agree to the Terms and Privacy Policy",
        # d9: owner-mandated wording of the optional training consent.
        "Use my conversations to improve culture.dev's models (optional)",
        WRONG,  # the contract-mandated single sign-in error (o4)
    }
    for html in pages:
        for chunk in _visible_chunks(html):
            if chunk in transcript or chunk == GUEST:
                continue
            # UI chrome: every label/button/error <= 4 words (owner rule).
            assert len(chunk.split()) <= 4, chunk


async def test_turnstile_csp_scoped_to_entry_page(
    jwks, legal_server, tmp_path, monkeypatch
):
    monkeypatch.setenv(entry.TURNSTILE_SITE_KEY_ENV, "site-key-123")
    monkeypatch.setenv(entry.TURNSTILE_SECRET_ENV, "secret-xyz")
    harness = await _harness(_config(jwks, legal_server), tmp_path)
    try:
        assert isinstance(harness.state.verifier, FakeVerifier)  # overridden
        harness.state.verifier = entry.make_bot_verifier()
        assert isinstance(harness.state.verifier, entry.TurnstileVerifier)
        r = await harness.post("/entry/email", {"email": GUEST})
        html = await r.text()
        csp = r.headers["Content-Security-Policy"]
        assert "https://challenges.cloudflare.com" in csp
        assert 'data-sitekey="site-key-123"' in html
        assert "secret-xyz" not in html
        assert "script-src 'self' https://challenges.cloudflare.com;" in csp
        # Other HTML pages keep the strict policy.
        r = await harness.client.get("/entry")
        assert "challenges.cloudflare.com" not in r.headers["Content-Security-Policy"]
    finally:
        await harness.client.close()


def test_noop_verifier_when_unconfigured(monkeypatch):
    monkeypatch.delenv(entry.TURNSTILE_SITE_KEY_ENV, raising=False)
    monkeypatch.delenv(entry.TURNSTILE_SECRET_ENV, raising=False)
    v = entry.make_bot_verifier()
    assert isinstance(v, entry.NoopVerifier)
    assert v.site_key is None


async def test_turnstile_verifier_posts_to_siteverify(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(dict(await request.post()))
        return web.json_response({"success": calls[-1]["response"] == "good"})

    app = web.Application()
    app.router.add_post("/siteverify", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        v = entry.TurnstileVerifier(
            "sk", "sec", verify_url=str(server.make_url("/siteverify"))
        )
        assert await v.verify("good", "1.2.3.4") is True
        assert await v.verify("bad", "1.2.3.4") is False
        assert await v.verify("", "1.2.3.4") is False  # no network call
        assert calls[0] == {"secret": "sec", "response": "good", "remoteip": "1.2.3.4"}
        assert len(calls) == 2
    finally:
        await server.close()


def test_guest_nicknames_cannot_contain_a_dash() -> None:
    """d7: approved users' sandbox nicks are sbx-op-<x>; a dash-free guest
    nickname can never collide with one (or impersonate it)."""
    from irc_lens.web.entry import sanitize_nickname

    assert sanitize_nickname("op-ori") == "opori"
    assert sanitize_nickname("a_b") == "a_b"
