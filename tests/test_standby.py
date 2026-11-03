"""Standby contract, exercised through the real middleware without listeners."""
import asyncio
import re
import socket
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from multidict import MultiDict

from conftest import _dev_config
from irc_lens._errors import AfiError
from irc_lens.access_store import AccessStore
from irc_lens.config import load_config
from irc_lens.mail import RecordingAdapter
from irc_lens.web import app_session, bans, entry, make_app, routes, standby
from test_registry_access import _factory

ADMIN = "admin@example.org"
GUEST = "guest@example.org"
USER = "alice@example.org"
INPUT_PATH = "/input"
SIGNIN = "/entry/signin"
CODE = "/entry/code"
FUTURE_PATH = "/future-account-change"
BASE_CONFIG = "auth: {mode: dev, dev: {nick: local, email: dev@local}}\nserver: {name: spark2}\n"
# Inventory traced from AccessStore/GuestStore mutators, including token
# issuance for password/deletion flows and guest account/consent creation.
ACCOUNT_ROUTES = {
    "/admin/users", "/admin/users/remove", "/admin/users/nickname",
    "/admin/grants", "/admin/extension", "/nickname", "/agent-credential",
    "/entry/welcome", "/entry/guest/start", "/entry/verify",
    "/delete/request", "/delete/confirm", "/password", "/password/{token}",
}
OTHER_BLOCKED = {"/entry/guest", "/sandbox/enter", "/sandbox/leave", "/upload"}
SESSION_ROUTES = {"/entry/email", SIGNIN, CODE, "/logout", "/guest/logout", INPUT_PATH}


def _require_worker_sockets():
    left, right = socket.socketpair()
    try:
        left.send(b"x")
    except PermissionError:
        pytest.skip("sandbox blocks socketpair send used by asyncio worker wakeups")
    finally:
        left.close()
        right.close()


async def _inline_worker(func, *args, **kwargs):
    """Exercise real store/mail functions without the blocked worker wakeup socket."""
    await asyncio.sleep(0)
    return func(*args, **kwargs)


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "accounts.db"
    store = AccessStore(path)
    store.set_user(ADMIN, role="admin", state="approved", actor=None)
    store.set_nickname(ADMIN, "root", actor=None)
    store.set_user(USER, role="member", state="approved", actor=None)
    store.set_nickname(USER, "alice", actor=None)
    store.set_grant(USER, "channel", "#chat", "write", actor=None)
    store.mark_initialised()
    cfg = replace(
        _dev_config("127.0.0.1", 1, "local"),
        auth_mode="cloudflare-access", cf_aud="test", cf_team_domain="example.invalid",
        server_name="spark2", guest_enabled=True, guest_store_path=str(path),
        access_enabled=True, standby_enabled=True, media_dir=str(tmp_path / "media"),
    )
    lines = []
    result = make_app(cfg, _factory(lines))
    result["test_lines"] = lines
    state = result[entry.ENTRY_STATE]
    state.mailer = RecordingAdapter()
    state.signin_floor_s = 0
    return result


async def request(app, method, path, data=None, cookie=""):
    if not app.frozen:
        app.freeze()
    payload = Mock(read=AsyncMock(side_effect=[urlencode(data or {}).encode(), b""]))
    req = make_mocked_request(method, path, app=app, payload=payload, headers={
        "Sec-Fetch-Site": "same-origin", "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    req._post = MultiDict(data or {})
    try:
        return await asyncio.wait_for(app._handle(req), timeout=5)
    except web.HTTPException as response:
        return response


@pytest.mark.parametrize("section, enabled", [("", False), ("standby: {enabled: true}\n", True)])
def test_config_default_and_enabled(tmp_path, section, enabled):
    path = tmp_path / "config.yaml"
    path.write_text(BASE_CONFIG + section)
    assert load_config(path).standby_enabled is enabled


@pytest.mark.parametrize("section", ["standby: nope", "standby: {enabled: yesplease}", "standby: {enabld: true}"])
def test_config_rejects_invalid_standby(tmp_path, section):
    path = tmp_path / "config.yaml"
    path.write_text(BASE_CONFIG + section)
    with pytest.raises(AfiError):
        load_config(path)


def test_all_write_routes_are_reviewed(app):
    actual = {r.resource.canonical for r in app.router.routes() if r.method == "POST"}
    assert actual == ACCOUNT_ROUTES | OTHER_BLOCKED | SESSION_ROUTES
    assert standby.ACCOUNT_CHANGE_PATHS == ACCOUNT_ROUTES


@pytest.mark.parametrize("path", sorted(ACCOUNT_ROUTES | OTHER_BLOCKED))
async def test_account_changes_refused(app, path):
    before = app["guest_store"].path.read_bytes()
    response = await request(app, "POST", path.replace("{token}", "test-token"))
    assert response.status == 503
    assert standby.OUTAGE_MESSAGE in response.text
    assert app["guest_store"].path.read_bytes() == before


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_new_write_route_fails_closed(app, method):
    handler = AsyncMock(return_value=web.Response())
    app.router.add_route(method, FUTURE_PATH, handler)
    response = await request(app, method, FUTURE_PATH)
    assert response.status == 503
    handler.assert_not_awaited()


@pytest.mark.parametrize("path", [SIGNIN, CODE])
@pytest.mark.parametrize("state", [None, "invited", "removed"])
async def test_guest_or_unapproved_signin_has_no_writes(app, path, state):
    store = app[app_session.ACCESS_STORE]
    if state:
        store.set_user(GUEST, role="member", state=state, actor=None)
    before = store.path.read_bytes()
    response = await request(app, "POST", path, {"email": GUEST})
    assert response.status == 503
    assert standby.OUTAGE_MESSAGE in response.text
    assert store.path.read_bytes() == before
    assert app["guest_store"].get_guest(GUEST) == []


@pytest.mark.parametrize("workers", ["inline", "thread"])
async def test_approved_signin_session_and_message(app, monkeypatch, workers):
    if workers == "thread":
        _require_worker_sockets()
    else:
        monkeypatch.setattr(asyncio, "to_thread", _inline_worker)
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", "s" * 32)
    state = app[entry.ENTRY_STATE]
    state.verifier = type("Verifier", (), {"verify": AsyncMock(return_value=True)})()
    store = app["guest_store"]
    store.set_password(USER, "correct-password")
    response = await request(app, "POST", SIGNIN, {"email": USER, "password": "correct-password"})
    assert response.status == 200
    for task in list(state.mail_tasks):
        await asyncio.wait_for(task, timeout=5)
    code = re.search(r"^ {4}(\S+)$", state.mailer.sent[0][2], re.M).group(1)
    pending = response.cookies[app_session.SIGNIN_COOKIE_NAME].value
    response = await request(app, "POST", CODE, {"email": USER, "code": code},
                             f"{app_session.SIGNIN_COOKIE_NAME}={pending}")
    assert response.status == 303
    raw = response.cookies[app_session.SESSION_COOKIE_NAME].value
    assert store.get_session(raw)[0] == USER
    cookie = f"{app_session.SESSION_COOKIE_NAME}={raw}"
    response = await request(app, "POST", INPUT_PATH, {"text": "/send #chat hello"}, cookie=cookie)
    assert response.status == 204
    session = list(app["registry"].values())[0]
    assert session.nick == "spark2-alice"
    assert "PRIVMSG #chat :hello" in app["test_lines"]
    response = await request(app, "GET", "/", cookie=cookie)
    assert standby.BANNER_MESSAGE in response.text
    for task in list(state.mail_tasks):
        await asyncio.wait_for(task, timeout=5)


def test_invited_access_signin_does_not_promote(app):
    store = app[app_session.ACCESS_STORE]
    store.set_user(GUEST, role="member", state="invited", actor=None)
    assert app_session.approved_on_signin(app, GUEST) is None
    app_session.promote_on_signin(app, GUEST)
    assert store.get_user(GUEST)["state"] == "invited"


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/static/app.css", "/media/test.png"])
async def test_public_paths_pass_through(app, path):
    handler = AsyncMock(return_value=web.Response(text="available"))
    req = make_mocked_request("GET", path, app=app)
    response = await standby.standby_middleware(req, handler)
    assert response.text == "available"
    handler.assert_awaited_once()


async def test_off_is_unchanged(app):
    app["config"] = replace(app["config"], standby_enabled=False)
    req = make_mocked_request("POST", FUTURE_PATH, app=app)
    handler = AsyncMock(return_value=web.Response(text="unchanged"))
    response = await standby.standby_middleware(req, handler)
    assert response.text == "unchanged"
    handler.assert_awaited_once()


@pytest.mark.parametrize("state, expected", [("approved", 303), ("invited", 303)])
async def test_access_signin_does_not_change_accounts(app, monkeypatch, state, expected):
    from irc_lens.web import auth

    store = app[app_session.ACCESS_STORE]
    store.set_user(ADMIN, role="admin", state=state, actor=None)
    monkeypatch.setattr(auth, "_decode_and_verify_jwt", AsyncMock(return_value={"email": ADMIN}))
    before = store.path.read_bytes()
    response = await request(app, "GET", "/login", cookie="CF_Authorization=test")
    assert response.status == expected
    assert store.path.read_bytes() == before


async def test_existing_guest_cookie_cannot_chat(app):
    from irc_lens.web import csrf

    store = app["guest_store"]
    store.record_guest(GUEST, "sbx-visitor", "")
    cookie = csrf.make_cookie_value(GUEST, app[csrf.SECRET_KEY])
    response = await request(app, "POST", INPUT_PATH, {"text": "hello"},
                             f"{csrf.GUEST_COOKIE_NAME}={cookie}")
    assert response.status == 401
    assert not list(app["registry"].values())


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_mcp_unavailable(app, method):
    response = await request(app, method, app["config"].mcp_path)
    assert response.status == 503


async def test_banner_absent_when_off(app):
    app["config"] = replace(app["config"], standby_enabled=False)
    response = await request(app, "GET", "/entry")
    assert response.status == 200
    assert standby.BANNER_MESSAGE not in response.text


async def test_service_token_is_not_an_approved_user(app, monkeypatch):
    from irc_lens.web import auth

    cfg = replace(app["config"], allowed_service_tokens=("service-client",))
    middleware = auth.build_cloudflare_middleware(cfg)
    monkeypatch.setattr(auth, "_decode_and_verify_jwt", AsyncMock(return_value={"common_name": "service-client"}))
    req = make_mocked_request("GET", "/owner/metrics", app=app, headers={"Cf-Access-Jwt-Assertion": "test"})
    req._match_info = await app.router.resolve(req)
    req._match_info.add_app(app)
    handler = AsyncMock(return_value=web.Response())
    response = await middleware(req, handler)
    assert response.status == 401
    handler.assert_not_awaited()


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/static/lens.css", "/media/local.png"])
async def test_public_handlers_still_work(app, monkeypatch, path):
    if path.startswith("/static/"):
        _require_worker_sockets()
    monkeypatch.setattr(asyncio, "to_thread", _inline_worker)
    monkeypatch.setattr(app[routes.READINESS_PROBE], "check", AsyncMock(return_value=True))
    media = app["media_store"].root / "owner"
    media.mkdir(parents=True)
    (media / "local.png").write_bytes(b"local media")
    response = await request(app, "GET", path)
    assert response.status == 200


def test_standby_does_not_import_admins(tmp_path):
    cfg = replace(_dev_config("127.0.0.1", 1, "local"),
                  access_enabled=True, standby_enabled=True,
                  allowed_emails=(ADMIN,), guest_store_path=str(tmp_path / "empty.db"))
    app = web.Application()
    app["config"] = cfg
    with pytest.raises(AfiError):
        app_session.install_access(app)
    assert AccessStore(cfg.guest_store_path).list_users() == []


def test_ban_sweep_gets_no_access_store_on_standby():
    """The sweep purges a banned guest's credentials: that's an account change."""
    store = object()
    for enabled, expected in ((True, None), (False, store)):
        app = web.Application()
        app["config"] = SimpleNamespace(standby_enabled=enabled)
        app[app_session.ACCESS_STORE] = store
        assert bans._writable_access(app) is expected

