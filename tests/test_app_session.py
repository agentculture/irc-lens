"""t5: app sessions -- cookie, middleware tier, revocation, CSRF coverage.

Covers spec claims c12/h8 (server-side sessions, expiry, logout, allowlist
re-check), c16/h12 (Access JWT break-glass unchanged), c6/h2 (nothing else
grants approved), c27/h18 (CSRF proof for lens_session / lens_signin) and
c29/h20 (ending an app session closes the user's live IRC session).
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import warnings
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens import metrics
from irc_lens.guest_store import SESSION_IDLE_S, SESSION_MAX_S, GuestStore
from irc_lens.session import Session
from irc_lens.web import app_session, csrf, make_app
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.render import render_index
from irc_lens.web.sessions import BACKEND_MESH
from test_session_routing import _config

ALICE = "alice@example.com"
NICK = "testsrv-alice"
SECRET = b"s" * 32
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
T0 = 1_800_000_000.0
STUB_HITS: list[str] = []


@allows_anonymous
async def _whoami(request: web.Request) -> web.Response:
    ident = request["identity"]
    return web.json_response(
        {"tier": ident.tier, "principal": ident.principal, "nick": ident.nick}
    )


@allows_anonymous
async def _stub_post(request: web.Request) -> web.Response:
    STUB_HITS.append(request.path)
    return web.Response(status=204)


class Env:
    def __init__(self, client, app, store, clock, jwks, mesh):
        self.client, self.app, self.store = client, app, store
        self.clock, self.jwks, self.mesh = clock, jwks, mesh

    @property
    def registry(self):
        return self.app["registry"]

    def cookie(self, raw: str) -> dict[str, str]:
        return {"Cookie": f"{app_session.SESSION_COOKIE_NAME}={raw}", **SAME_ORIGIN}

    def jwt(self, email: str = ALICE) -> dict[str, str]:
        tok = self.jwks.mint(aud="aud-test", claims={"email": email, "sub": "s"})
        return {"Cf-Access-Jwt-Assertion": tok, **SAME_ORIGIN}

    async def whoami(self, headers: dict[str, str]) -> dict:
        r = await self.client.get("/_whoami", headers=headers)
        assert r.status == 200
        return await r.json()

    def set_allowed(self, emails: tuple[str, ...]) -> None:
        cfg = dataclasses.replace(self.app["config"], allowed_emails=emails)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.app["config"] = cfg


async def _make_env(jwks, tmp_path, monkeypatch, *, interval=3600.0, **cfg_kw):
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", SECRET.decode())
    mesh = AgentIRCTestServer()
    await mesh.start()
    config = dataclasses.replace(_config(jwks, mesh, mesh, tmp_path), **cfg_kw)
    clock = {"t": T0}
    store = GuestStore(tmp_path / "sessions.db", clock=lambda: clock["t"])

    def mesh_factory(nick: str) -> Session:
        return Session(host=mesh.host, port=mesh.port, nick=nick)

    app = make_app(config, mesh_factory, ban_sweep_interval_s=interval)
    app["guest_store"] = store
    app.router.add_get("/_whoami", _whoami)
    app.router.add_post("/_stub", _stub_post)
    STUB_HITS.clear()
    client = TestClient(TestServer(app))
    await client.start_server()
    return Env(client, app, store, clock, jwks, mesh)


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
async def swept(jwks: FakeJWKS, tmp_path, monkeypatch) -> AsyncIterator[Env]:
    """Same env with the real sweeper running every 50 ms."""
    e = await _make_env(jwks, tmp_path, monkeypatch, interval=0.05)
    try:
        yield e
    finally:
        await _close(e)


async def _wait_for(pred, timeout=3.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def _set_cookie_header(resp: web.StreamResponse, name: str) -> str:
    morsel = resp.cookies[name]
    return morsel.OutputString()


# -- cookie primitives (c12: HttpOnly, Secure, SameSite=Lax) -----------------


def test_session_cookie_flags_exact() -> None:
    resp = web.Response()
    app_session.issue_session_cookie(resp, "raw-id")
    header = _set_cookie_header(resp, "lens_session")
    assert app_session.SESSION_COOKIE_NAME == "lens_session"
    for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=2592000"):
        assert attr in header
    assert "SameSite=Strict" not in header


def test_clear_session_cookie_expires_it() -> None:
    resp = web.Response()
    app_session.clear_session_cookie(resp)
    header = _set_cookie_header(resp, "lens_session")
    assert "Max-Age=0" in header
    assert "Path=/" in header
    assert "HttpOnly" in header
    assert "Secure" in header


def test_signin_cookie_flags_exact() -> None:
    resp = web.Response()
    app_session.set_signin_cookie(resp, "pending-value")
    header = _set_cookie_header(resp, "lens_signin")
    assert app_session.SIGNIN_COOKIE_NAME == "lens_signin"
    for attr in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/entry", "Max-Age=600"):
        assert attr in header
    resp2 = web.Response()
    app_session.clear_signin_cookie(resp2)
    header2 = _set_cookie_header(resp2, "lens_signin")
    assert "Max-Age=0" in header2
    assert "Path=/entry" in header2


async def test_read_cookies_round_trip(env: Env) -> None:
    from aiohttp.test_utils import make_mocked_request

    raw = env.store.create_session(ALICE)
    req = make_mocked_request(
        "GET", "/", headers={"Cookie": f"lens_session={raw}; lens_signin=p-1"}
    )
    assert app_session.read_session_cookie(req) == raw
    assert app_session.read_signin_cookie(req) == "p-1"
    junk = make_mocked_request("GET", "/", headers={"Cookie": "lens_session=a b;"})
    assert app_session.read_session_cookie(junk) is None


# -- criterion 1 / c12 / h8 / c6 / h2: the approved tier from lens_session ----


async def test_valid_session_yields_same_identity_as_access_jwt(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    via_cookie = await env.whoami(env.cookie(raw))
    via_jwt = await env.whoami(env.jwt())
    assert via_cookie == {"tier": "approved", "principal": ALICE, "nick": NICK}
    assert via_cookie == via_jwt


async def test_db_never_holds_raw_session_id(env: Env, tmp_path) -> None:
    raw = env.store.create_session(ALICE)
    assert (await env.whoami(env.cookie(raw)))["tier"] == "approved"
    con = sqlite3.connect(env.store.path)
    try:
        cells = [c for (c,) in con.execute("SELECT id_hash FROM sessions")]
        cells += [c for row in con.execute("SELECT * FROM sessions") for c in row]
    finally:
        con.close()
    assert all(raw not in str(c) for c in cells)


async def test_session_idle_over_7_days_is_anonymous(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    env.clock["t"] = T0 + SESSION_IDLE_S + 1
    assert (await env.whoami(env.cookie(raw)))["tier"] == "anonymous"


async def test_activity_keeps_session_alive_until_30_days(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    t = T0
    while t + 6 * 86400 < T0 + SESSION_MAX_S:
        t += 6 * 86400
        env.clock["t"] = t
        assert (await env.whoami(env.cookie(raw)))["tier"] == "approved"
    env.clock["t"] = T0 + SESSION_MAX_S + 1
    assert (await env.whoami(env.cookie(raw)))["tier"] == "anonymous"


async def test_last_seen_touched_at_most_once_a_minute(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    env.clock["t"] = T0 + 30
    await env.whoami(env.cookie(raw))
    assert env.store.get_session(raw)[2] == int(T0)  # within a minute: no write
    env.clock["t"] = T0 + 61
    await env.whoami(env.cookie(raw))
    assert env.store.get_session(raw)[2] == int(T0 + 61)


async def test_deleted_session_is_anonymous(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    env.store.delete_session(raw)
    assert (await env.whoami(env.cookie(raw)))["tier"] == "anonymous"


async def test_email_removed_from_allowlist_is_anonymous_next_request(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    assert (await env.whoami(env.cookie(raw)))["tier"] == "approved"
    env.set_allowed(("someone-else@example.com",))
    assert (await env.whoami(env.cookie(raw)))["tier"] == "anonymous"


async def test_session_for_unlisted_email_never_approved(env: Env) -> None:
    raw = env.store.create_session("mallory@example.com")
    assert (await env.whoami(env.cookie(raw)))["tier"] == "anonymous"


async def test_forged_cookie_is_anonymous_not_an_error(env: Env) -> None:
    for junk in ("x" * 43, "", "not a token", "%00" * 10, "a" * 500):
        r = await env.client.get(
            "/_whoami", headers={"Cookie": f"lens_session={junk}"}
        )
        assert r.status == 200
        assert (await r.json())["tier"] == "anonymous"


async def test_app_signin_disabled_ignores_session_cookie(
    jwks, tmp_path, monkeypatch
) -> None:
    e = await _make_env(jwks, tmp_path, monkeypatch, app_signin_enabled=False)
    try:
        raw = e.store.create_session(ALICE)
        assert (await e.whoami(e.cookie(raw)))["tier"] == "anonymous"
        r = await e.client.post("/logout", headers=e.jwt())
        assert r.status == 404
    finally:
        await _close(e)


# -- criterion 2 / c16 / h12: Access JWT break-glass unchanged ---------------


async def test_invalid_session_cookie_falls_through_to_access_jwt(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    env.store.delete_session(raw)
    headers = {**env.jwt(), "Cookie": f"lens_session={raw}"}
    assert await env.whoami(headers) == {
        "tier": "approved",
        "principal": ALICE,
        "nick": NICK,
    }


async def test_access_jwt_alone_still_approved(env: Env) -> None:
    assert (await env.whoami(env.jwt()))["tier"] == "approved"
    r = await env.client.get("/", headers=env.jwt())
    assert r.status == 200


# -- criterion 3 / c12 / h8: POST /logout ------------------------------------


async def test_logout_deletes_session_clears_cookie_and_redirects(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    before = metrics.get_metrics().snapshot()["sessions_ended"]
    r = await env.client.post(
        "/logout", headers=env.cookie(raw), allow_redirects=False
    )
    assert r.status == 303
    assert r.headers["Location"] == "/"
    header = r.cookies["lens_session"].OutputString()
    assert "Max-Age=0" in header
    assert env.store.get_session(raw) is None
    assert metrics.get_metrics().snapshot()["sessions_ended"] == before + 1
    assert (await env.whoami(env.cookie(raw)))["tier"] == "anonymous"


async def test_logout_htmx_request_gets_hx_redirect(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    r = await env.client.post(
        "/logout",
        headers={**env.cookie(raw), "HX-Request": "true"},
        allow_redirects=False,
    )
    assert r.status == 204
    assert r.headers["HX-Redirect"] == "/"
    assert env.store.get_session(raw) is None


async def test_logout_is_approved_only(env: Env) -> None:
    r = await env.client.post("/logout", headers=SAME_ORIGIN, allow_redirects=False)
    assert r.status == 401


async def test_logout_closes_irc_session_opened_via_app_session(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    assert (await env.client.get("/", headers=env.cookie(raw))).status == 200
    assert env.registry.has(ALICE, BACKEND_MESH)
    session = env.registry._sessions[(ALICE, BACKEND_MESH)]
    r = await env.client.post(
        "/logout", headers=env.cookie(raw), allow_redirects=False
    )
    assert r.status == 303
    assert not env.registry.has(ALICE, BACKEND_MESH)
    assert await _wait_for(lambda: not session.connected)


# -- criterion 3 / c29 / h20: revocation sweep -------------------------------


def _revoke(env: Env, raw: str, how: str) -> None:
    if how == "logout-elsewhere":
        env.store.delete_session(raw)
    elif how == "revoke":
        env.store.delete_sessions_for_email(ALICE)
    elif how == "expiry":
        env.clock["t"] = T0 + SESSION_IDLE_S + 1
    elif how == "allowlist-removal":
        env.set_allowed(("someone-else@example.com",))
    else:  # pragma: no cover
        raise AssertionError(how)


@pytest.mark.parametrize(
    "how", ["logout-elsewhere", "revoke", "expiry", "allowlist-removal"]
)
async def test_sweep_closes_irc_session_when_app_session_ends(
    swept: Env, how: str
) -> None:
    raw = swept.store.create_session(ALICE)
    assert (await swept.client.get("/", headers=swept.cookie(raw))).status == 200
    assert swept.registry.has(ALICE, BACKEND_MESH)
    _revoke(swept, raw, how)
    assert await _wait_for(lambda: not swept.registry.has(ALICE, BACKEND_MESH))
    # The old tab's next POST is refused (h20).
    r = await swept.client.post(
        "/input", json={"text": "hi"}, headers=swept.cookie(raw)
    )
    assert r.status == 401


async def test_sweep_once_keeps_live_app_sessions(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    await env.client.get("/", headers=env.cookie(raw))
    assert await app_session.sweep_once(env.app) == []
    assert env.registry.has(ALICE, BACKEND_MESH)


async def test_sweep_never_closes_sessions_opened_via_access_jwt(env: Env) -> None:
    assert (await env.client.get("/", headers=env.jwt())).status == 200
    raw = env.store.create_session(ALICE)
    # Same user also browses with an app session that then ends.
    await env.client.get("/", headers=env.cookie(raw))
    env.store.delete_session(raw)
    assert await app_session.sweep_once(env.app) == []
    assert env.registry.has(ALICE, BACKEND_MESH)


async def test_end_sessions_for_email_revokes_all_and_closes_now(env: Env) -> None:
    raw1 = env.store.create_session(ALICE)
    raw2 = env.store.create_session(ALICE)
    await env.client.get("/", headers=env.cookie(raw1))
    assert env.registry.has(ALICE, BACKEND_MESH)
    n = await app_session.end_sessions_for_email(env.app, ALICE)
    assert n == 2
    assert not env.registry.has(ALICE, BACKEND_MESH)
    for raw in (raw1, raw2):  # h8: password change makes old cookies fail
        assert (await env.whoami(env.cookie(raw)))["tier"] == "anonymous"


# -- criterion 4 / c27 / h18: CSRF proof for the new cookies -----------------


@pytest.mark.parametrize("cookie", ["lens_session", "lens_signin"])
@pytest.mark.parametrize("site", ["cross-site", "same-site", None])
async def test_cross_site_post_with_new_cookie_is_403(
    env: Env, cookie: str, site: str | None
) -> None:
    headers = {"Cookie": f"{cookie}=anything"}
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    for path in ("/_stub", "/logout"):
        r = await env.client.post(path, headers=headers, allow_redirects=False)
        assert r.status == 403
        assert (await r.json())["error"] == "cross-site request refused"
    assert STUB_HITS == []


@pytest.mark.parametrize("cookie", ["lens_session", "lens_signin"])
async def test_same_origin_post_with_new_cookie_passes(env: Env, cookie: str) -> None:
    r = await env.client.post(
        "/_stub", headers={"Cookie": f"{cookie}=x", "Sec-Fetch-Site": "same-origin"}
    )
    assert r.status == 204
    assert STUB_HITS == ["/_stub"]


def test_csrf_proof_cookie_set_covers_all_three() -> None:
    assert set(csrf.PROOF_COOKIE_NAMES) == {
        "lens_guest",
        "lens_session",
        "lens_signin",
    }


# -- Log out button: approved app-session users only -------------------------


async def test_logout_button_shown_for_app_session_user(env: Env) -> None:
    raw = env.store.create_session(ALICE)
    html = await (await env.client.get("/", headers=env.cookie(raw))).text()
    assert 'data-testid="logout"' in html
    assert ">Log out<" in html


async def test_logout_button_hidden_for_access_jwt_user(env: Env) -> None:
    html = await (await env.client.get("/", headers=env.jwt())).text()
    assert 'data-testid="logout"' not in html


def test_logout_button_never_rendered_for_guests() -> None:
    from irc_lens.session import Session as S

    s = S(host="127.0.0.1", port=1, nick="sbx-gus")
    s.ui_tier = "guest"
    assert 'data-testid="logout"' not in render_index(s, show_logout=True)
    s.ui_tier = "approved"
    assert 'data-testid="logout"' in render_index(s, show_logout=True)
    assert 'data-testid="logout"' not in render_index(s)
