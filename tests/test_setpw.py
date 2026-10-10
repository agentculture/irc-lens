"""t6: set or reset password by emailed link.

Maps to spec claims:

* c15 / h11 -- ``/password`` asks for an email and always shows the same
  "check your email" page; only an approved email gets a single-use
  ``setpw`` link (30 minutes) to a page that sets an argon2id password of
  at least 12 characters and ends every session of that email.
* c30 / h21 -- a GET of the link never consumes the token; only the POST
  that sets the password does.
* d2 -- the link's base comes from ``auth.app_signin.base_url`` (falling
  back to ``media.public_base_url``), never from the request Host header;
  with neither set nothing is sent and one error is logged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import sqlite3
import warnings
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens.cli._errors import AfiError
from irc_lens.config import load_config
from irc_lens.guest_store import GuestStore
from irc_lens.mail import RecordingAdapter
from irc_lens.session import Session
from irc_lens.web import app_session, entry, make_app, setpw
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.render import render_fragment
from irc_lens.web.sessions import BACKEND_MESH
from test_session_routing import _config

ALICE = "alice@example.com"
UNKNOWN = "nobody@example.org"
SECRET = b"s" * 32
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
T0 = 1_800_000_000.0
BASE = "https://lens.example.com"
GOOD_PW = "correct horse battery"
SENT_TEXT = "If this address can sign in here, we've emailed a link"
EXPIRED_TEXT = "This link has expired or was already used."
DONE_TEXT = "Password set. Sign in with your new password."
LINK_RE = re.compile(r"(\S+)/password/([A-Za-z0-9_-]+)")


@allows_anonymous
async def _whoami(request: web.Request) -> web.Response:
    ident = request["identity"]
    return web.json_response({"tier": ident.tier, "principal": ident.principal})


class Env:
    def __init__(self, client, app, store, clock, mail, mesh):
        self.client, self.app, self.store = client, app, store
        self.clock, self.mail, self.mesh = clock, mail, mesh

    @property
    def registry(self):
        return self.app["registry"]

    def cookie(self, raw: str) -> dict[str, str]:
        return {"Cookie": f"{app_session.SESSION_COOKIE_NAME}={raw}", **SAME_ORIGIN}

    async def request_link(self, email: str = ALICE, **headers) -> web.Response:
        r = await self.client.post(
            "/password", data={"email": email}, headers={**SAME_ORIGIN, **headers}
        )
        await drain(self.app)
        return r

    def link(self) -> tuple[str, str]:
        m = LINK_RE.search(self.mail.sent[-1][2])
        assert m, self.mail.sent[-1][2]
        return m.group(1), m.group(2)

    async def submit(self, token: str, pw: str, confirm: str | None = None):
        return await self.client.post(
            f"/password/{token}",
            data={"password": pw, "confirm": pw if confirm is None else confirm},
            headers=SAME_ORIGIN,
        )


async def drain(app: web.Application) -> None:
    """Wait for the background link-mail tasks to finish."""
    tasks = list(app.get(setpw.MAIL_TASKS, ()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _make_env(jwks, tmp_path, monkeypatch, **cfg_kw) -> Env:
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", SECRET.decode())
    mesh = AgentIRCTestServer()
    await mesh.start()
    cfg_kw.setdefault("app_signin_base_url", BASE)
    config = dataclasses.replace(_config(jwks, mesh, mesh, tmp_path), **cfg_kw)
    clock = {"t": T0}
    store = GuestStore(tmp_path / "setpw.db", clock=lambda: clock["t"])

    def mesh_factory(nick: str) -> Session:
        return Session(host=mesh.host, port=mesh.port, nick=nick)

    app = make_app(config, mesh_factory)
    app["guest_store"] = store
    mail = RecordingAdapter()
    if entry.ENTRY_STATE in app:
        state = app[entry.ENTRY_STATE]
        state.store = store
        state.mailer = mail
    app.router.add_get("/_whoami", _whoami)
    client = TestClient(TestServer(app))
    await client.start_server()
    return Env(client, app, store, clock, mail, mesh)


async def _close(env: Env) -> None:
    for s in env.registry.values():
        await s.disconnect()
    await env.client.close()
    await env.mesh.stop()


@pytest_asyncio.fixture
async def env(jwks: FakeJWKS, tmp_path, monkeypatch) -> AsyncIterator[Env]:
    e = await _make_env(jwks, tmp_path, monkeypatch)
    try:
        yield e
    finally:
        await _close(e)


@pytest_asyncio.fixture
async def envs(jwks: FakeJWKS, tmp_path, monkeypatch):
    made: list[Env] = []

    async def factory(**cfg_kw) -> Env:
        e = await _make_env(jwks, tmp_path, monkeypatch, **cfg_kw)
        made.append(e)
        return e

    try:
        yield factory
    finally:
        for e in made:
            await _close(e)


# -- c15 / h11: the request step ------------------------------------------------


async def test_get_password_shows_email_form(env: Env) -> None:
    r = await env.client.get("/password")
    assert r.status == 200
    html = await r.text()
    assert 'action="/password"' in html
    assert 'name="email"' in html
    assert r.headers["Cache-Control"] == "no-store"


async def test_request_page_identical_for_allowed_and_unknown_email(env: Env) -> None:
    a = await env.request_link(ALICE)
    b = await env.request_link(UNKNOWN)
    assert a.status == b.status == 200
    text_a, text_b = await a.text(), await b.text()
    assert SENT_TEXT in text_a
    assert "works once and expires in 30 minutes" in text_a
    assert text_a.replace(ALICE, "E") == text_b.replace(UNKNOWN, "E")


async def test_only_allowed_email_gets_setpw_link(env: Env) -> None:
    await env.request_link(UNKNOWN)
    assert env.mail.sent == []
    await env.request_link(ALICE)
    assert len(env.mail.sent) == 1
    to, _subject, _body = env.mail.sent[0]
    assert to == ALICE
    base, token = env.link()
    assert base == BASE
    assert env.store.peek_token(token, purpose="setpw") == ALICE
    # Purpose-bound: the link is no sign-in or deletion token.
    assert env.store.peek_token(token, purpose="signin") is None


async def test_request_rate_limited_per_email(env: Env) -> None:
    limit = env.app["config"].guest_rate_entry_per_min
    for i in range(limit):
        r = await env.request_link(ALICE, **{"CF-Connecting-IP": f"10.0.0.{i}"})
        assert r.status == 200
    r = await env.request_link(ALICE, **{"CF-Connecting-IP": "10.0.1.1"})
    assert r.status == 429
    assert SENT_TEXT in await r.text()
    assert len(env.mail.sent) == limit


async def test_request_rate_limited_per_ip(env: Env) -> None:
    limit = env.app["config"].guest_rate_entry_per_min
    ip = {"CF-Connecting-IP": "10.9.9.9"}
    for i in range(limit):
        await env.request_link(f"u{i}@example.org", **ip)
    r = await env.request_link(ALICE, **ip)
    assert r.status == 429
    assert env.mail.sent == []


# -- d2: link base from config, never from Host ------------------------------


async def test_link_host_comes_from_config_not_host_header(env: Env) -> None:
    await env.request_link(ALICE, Host="evil.example")
    base, _token = env.link()
    assert base == BASE
    assert "evil.example" not in env.mail.sent[-1][2]


async def test_link_base_falls_back_to_media_public_base_url(envs) -> None:
    e = await envs(app_signin_base_url=None, media_public_base_url="https://m.example")
    await e.request_link(ALICE, Host="evil.example")
    base, _token = e.link()
    assert base == "https://m.example"


async def test_no_base_url_sends_nothing_logs_one_error(envs, caplog) -> None:
    e = await envs(app_signin_base_url=None, media_public_base_url="")
    caplog.set_level(logging.INFO)
    r = await e.request_link(ALICE)
    assert r.status == 200
    assert SENT_TEXT in await r.text()
    assert e.mail.sent == []
    errors = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.ERROR and rec.name.startswith("irc_lens")
    ]
    assert len(errors) == 1
    assert "base_url" in errors[0].getMessage()


# -- c30 / h21: GET never consumes ------------------------------------------------


async def test_get_link_twice_leaves_token_usable(env: Env) -> None:
    await env.request_link(ALICE)
    _base, token = env.link()
    for _ in range(2):
        r = await env.client.get(f"/password/{token}")
        assert r.status == 200
        html = await r.text()
        assert 'name="password"' in html and 'name="confirm"' in html
        assert 'minlength="12"' in html
        # Never no-referrer: browsers then send Origin: null on the form
        # POST and the CSRF floor refuses it (found in the browser pass).
        assert 'content="same-origin"' in html
        assert 'no-referrer' not in html
        assert r.headers["Referrer-Policy"] == "same-origin"
    assert env.store.peek_token(token, purpose="setpw") == ALICE
    r = await env.submit(token, GOOD_PW)
    assert r.status == 200
    assert DONE_TEXT in await r.text()


async def test_unknown_token_shows_neutral_page(env: Env) -> None:
    r = await env.client.get("/password/" + "x" * 43)
    assert r.status == 400
    html = await r.text()
    assert EXPIRED_TEXT in html
    assert 'href="/password"' in html
    r = await env.submit("y" * 43, GOOD_PW)
    assert r.status == 400
    assert EXPIRED_TEXT in await r.text()


# -- c15 / h11: the set step -------------------------------------------------------


async def test_short_password_refused_and_token_stays_valid(env: Env) -> None:
    await env.request_link(ALICE)
    _base, token = env.link()
    r = await env.submit(token, "elevenchars")  # 11 characters
    assert r.status == 400
    html = await r.text()
    assert "at least 12 characters" in html
    assert 'name="password"' in html
    assert env.store.peek_token(token, purpose="setpw") == ALICE
    assert not env.store.check_password(ALICE, "elevenchars")
    r = await env.submit(token, "twelve chars")  # exactly 12
    assert r.status == 200
    assert env.store.check_password(ALICE, "twelve chars")


async def test_mismatched_confirmation_refused_and_token_stays_valid(env: Env) -> None:
    await env.request_link(ALICE)
    _base, token = env.link()
    r = await env.submit(token, GOOD_PW, confirm=GOOD_PW + "!")
    assert r.status == 400
    assert "The passwords don" in await r.text()
    assert env.store.peek_token(token, purpose="setpw") == ALICE


async def test_set_password_argon2id_consumes_token_and_ends_sessions(
    env: Env, tmp_path: Path
) -> None:
    env.store.set_password(ALICE, "old password value")
    raw1 = env.store.create_session(ALICE)
    raw2 = env.store.create_session(ALICE)
    # A live IRC session opened through an app session.
    assert (await env.client.get("/", headers=env.cookie(raw1))).status == 200
    assert env.registry.has(ALICE, BACKEND_MESH)

    await env.request_link(ALICE)
    _base, token = env.link()
    r = await env.submit(token, GOOD_PW)
    assert r.status == 200
    html = await r.text()
    assert DONE_TEXT in html
    assert 'href="/"' in html

    assert env.store.check_password(ALICE, GOOD_PW)
    assert not env.store.check_password(ALICE, "old password value")
    with sqlite3.connect(tmp_path / "setpw.db") as con:
        (hashed,) = con.execute(
            "SELECT hash FROM passwords WHERE email=?", (ALICE,)
        ).fetchone()
        assert hashed.startswith("$argon2id$")
        assert con.execute(
            "SELECT COUNT(*) FROM sessions WHERE email=?", (ALICE,)
        ).fetchone() == (0,)
    assert not env.registry.has(ALICE, BACKEND_MESH)
    for raw in (raw1, raw2):
        r = await env.client.get("/_whoami", headers=env.cookie(raw))
        assert (await r.json())["tier"] == "anonymous"

    # Single use: the second submit (and a GET) now fail.
    r = await env.submit(token, "another good password")
    assert r.status == 400
    assert EXPIRED_TEXT in await r.text()
    assert env.store.check_password(ALICE, GOOD_PW)
    assert (await env.client.get(f"/password/{token}")).status == 400


async def test_expired_link_refused_after_30_minutes(env: Env) -> None:
    await env.request_link(ALICE)
    _base, token = env.link()
    env.clock["t"] = T0 + 1799
    assert (await env.client.get(f"/password/{token}")).status == 200
    env.clock["t"] = T0 + 1800
    assert (await env.client.get(f"/password/{token}")).status == 400
    r = await env.submit(token, GOOD_PW)
    assert r.status == 400
    assert EXPIRED_TEXT in await r.text()
    assert not env.store.check_password(ALICE, GOOD_PW)


async def test_link_for_email_no_longer_allowed_is_refused(env: Env) -> None:
    await env.request_link(ALICE)
    _base, token = env.link()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        env.app["config"] = dataclasses.replace(env.app["config"], allowed_emails=())
    r = await env.submit(token, GOOD_PW)
    assert r.status == 400
    assert not env.store.check_password(ALICE, GOOD_PW)


async def test_submit_rate_limited_per_ip(env: Env) -> None:
    await env.request_link(ALICE)
    _base, token = env.link()
    limit = env.app["config"].guest_rate_password_attempts_per_15min
    for _ in range(limit):
        assert (await env.submit(token, "short")).status == 400
    r = await env.submit(token, GOOD_PW)
    assert r.status == 429
    assert not env.store.check_password(ALICE, GOOD_PW)
    assert env.store.peek_token(token, purpose="setpw") == ALICE


async def test_cross_site_post_refused(env: Env) -> None:
    await env.request_link(ALICE)
    _base, token = env.link()
    r = await env.client.post(
        f"/password/{token}",
        data={"password": GOOD_PW, "confirm": GOOD_PW},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status == 403
    assert env.store.peek_token(token, purpose="setpw") == ALICE


async def test_token_link_and_password_never_logged(env: Env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    await env.request_link(ALICE)
    _base, token = env.link()
    await env.client.get(f"/password/{token}")
    await env.submit(token, "short")
    await env.client.post(
        f"/password/{token}", data={}, headers={"Origin": "https://evil.example"}
    )
    await env.submit(token, GOOD_PW)
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert token not in text
    assert GOOD_PW not in text


def test_access_log_filter_redacts_token() -> None:
    rec = logging.LogRecord(
        "aiohttp.access",
        logging.INFO,
        __file__,
        1,
        '1.2.3.4 "GET /password/abcDEF_-0123456789abcdef HTTP/1.1" 200 '
        '"https://lens.example.com/password/abcDEF_-0123456789abcdef"',
        None,
        None,
    )
    assert setpw.RedactTokenFilter().filter(rec) is True
    msg = rec.getMessage()
    assert "abcDEF_-0123456789abcdef" not in msg
    assert "/password/[redacted]" in msg


async def test_redact_filter_installed_on_path_loggers(env: Env) -> None:
    for name in ("aiohttp.access", "irc_lens.web.auth", "irc_lens.web.csrf"):
        assert any(
            isinstance(f, setpw.RedactTokenFilter)
            for f in logging.getLogger(name).filters
        ), name


# -- routes off when guest mode or app sign-in is off ---------------------------


@pytest.mark.parametrize(
    "cfg_kw", [{"guest_enabled": False}, {"app_signin_enabled": False}]
)
async def test_routes_absent_when_disabled(envs, cfg_kw) -> None:
    e = await envs(**cfg_kw)
    # Unregistered: anonymous visitors get the usual 401/404, never the page.
    for path in ("/password", "/password/" + "x" * 43):
        r = await e.client.get(path)
        assert r.status in (401, 404)
        assert "Set or reset password" not in await r.text()
    r = await e.request_link(ALICE)
    assert r.status in (401, 403, 404, 405)
    assert e.mail.sent == []


# -- criterion 3: the entry card links to /password -------------------------------


def test_entry_password_step_links_to_password() -> None:
    html = render_fragment(
        "entry.html.j2",
        step="password",
        email=ALICE,
        nickname="",
        error="",
        train=False,
        site_key=None,
        turnstile_script="",
        turnstile_field="",
        nick_prefix="sbx-",
        nick_max=16,
        terms_url="",
        privacy_url="",
        css_url="/static/entry.css",
    )
    assert 'href="/password"' in html
    assert "Set or reset password" in html


# -- store helpers --------------------------------------------------------------


def test_store_peek_does_not_consume_and_consume_is_single_use(tmp_path) -> None:
    clock = {"t": T0}
    store = GuestStore(tmp_path / "s.db", clock=lambda: clock["t"])
    tok = store.issue_token(ALICE, purpose="setpw")
    assert store.peek_token(tok, purpose="setpw") == ALICE
    assert store.peek_token(tok, purpose="setpw") == ALICE
    assert store.consume_token(tok, purpose="delete") is None
    assert store.consume_token(tok, purpose="setpw") == ALICE
    assert store.consume_token(tok, purpose="setpw") is None
    assert store.peek_token(tok, purpose="setpw") is None
    tok2 = store.issue_token(ALICE, purpose="setpw")
    clock["t"] = T0 + 1800
    assert store.peek_token(tok2, purpose="setpw") is None
    assert store.consume_token(tok2, purpose="setpw") is None


# -- d2: config key -----------------------------------------------------------------


def _load(tmp_path: Path, signin_block: str):
    p = tmp_path / "config.yaml"
    p.write_text(
        "auth:\n  mode: dev\n  dev:\n    nick: lens\n    email: dev@local\n"
        f"  app_signin:\n{signin_block}\nserver:\n  name: spark\n"
    )
    return load_config(p)


def test_config_base_url_default_none(tmp_path) -> None:
    assert _load(tmp_path, "    enabled: true").app_signin_base_url is None


@pytest.mark.parametrize(
    "url",
    ["https://chat.culture.dev", "http://127.0.0.1:8080", "http://localhost"],
)
def test_config_base_url_accepted(tmp_path, url) -> None:
    assert _load(tmp_path, f"    base_url: {url}").app_signin_base_url == url


@pytest.mark.parametrize(
    "url",
    ["http://chat.culture.dev", "https://", "ftp://x.example", "chat.culture.dev"],
)
def test_config_base_url_rejected(tmp_path, url) -> None:
    with pytest.raises(AfiError, match="auth.app_signin.base_url"):
        _load(tmp_path, f"    base_url: {url}")


async def test_setpw_pages_never_send_no_referrer(env):
    """Regression: the request form must POST with a real Origin in browsers."""
    for path in ("/password",):
        r = await env.client.get(path)
        html = await r.text()
        assert r.headers["Referrer-Policy"] == "same-origin"
        assert "no-referrer" not in html
