"""t7: signed guest cookie + CSRF guard (obligation o9)."""

from __future__ import annotations

import dataclasses
from unittest import mock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.web import csrf, make_app
from conftest import _dev_config

SECRET = b"test-secret"
STUB_HITS: list[str] = []


async def _issue(request: web.Request) -> web.Response:
    resp = web.Response(text="ok")
    csrf.issue_guest_cookie(resp, "g-123", secret=SECRET)
    return resp


async def _stub_post(request: web.Request) -> web.Response:
    STUB_HITS.append(request.path)
    return web.Response(status=204)


async def _whoami(request: web.Request) -> web.Response:
    return web.json_response({"guest": csrf.read_guest_cookie(request)})


@pytest_asyncio.fixture
async def client():
    async def factory(identity):
        raise RuntimeError("no IRC in this test")

    app = make_app(_dev_config("127.0.0.1", 1, "t-x"), factory)
    csrf.install(app, SECRET)
    # make_app freezes the router after startup only; add stubs pre-start.
    app.router.add_get("/_issue", _issue)
    app.router.add_get("/_whoami", _whoami)
    for path in ("/consent", "/guest/delete", "/_stub"):
        app.router.add_post(path, _stub_post)
        app.router.add_delete(path + "/x", _stub_post)
    STUB_HITS.clear()
    async with TestClient(TestServer(app)) as c:
        yield c


def _cookie(guest="g-1", **kw):
    return {
        "Cookie": f"{csrf.GUEST_COOKIE_NAME}={csrf.make_cookie_value(guest, SECRET, **kw)}"
    }


# --- criterion 1: cookie format ------------------------------------------


@pytest.mark.asyncio
async def test_cookie_attributes(client: TestClient) -> None:
    resp = await client.get("/_issue")
    header = resp.headers["Set-Cookie"]
    assert header.startswith(csrf.GUEST_COOKIE_NAME + "=")
    for attr in ("HttpOnly", "Secure", "SameSite=Strict", "Max-Age=3600", "Path=/"):
        assert attr in header


def test_roundtrip_expiry_and_tamper() -> None:
    v = csrf.make_cookie_value("abc", SECRET, ttl=60, now=1000)
    assert csrf.verify_cookie_value(v, SECRET, now=1010) == "abc"
    assert csrf.verify_cookie_value(v, SECRET, now=1060) is None  # expired
    assert csrf.verify_cookie_value(v, b"other", now=1010) is None  # wrong key
    payload, sig = v.split(".")
    forged = csrf.make_cookie_value("evil", b"x", ttl=60, now=1000).split(".")[0]
    assert csrf.verify_cookie_value(f"{forged}.{sig}", SECRET, now=1010) is None
    for junk in ("", "nodot", "a.b", "..", "é.é"):
        assert csrf.verify_cookie_value(junk, SECRET) is None


@pytest.mark.asyncio
async def test_read_guest_cookie(client: TestClient) -> None:
    r = await client.get("/_whoami", headers=_cookie("g-9"))
    assert (await r.json())["guest"] == "g-9"
    r = await client.get(
        "/_whoami", headers={"Cookie": f"{csrf.GUEST_COOKIE_NAME}=bad.sig"}
    )
    assert (await r.json())["guest"] is None
    r = await client.get("/_whoami")
    assert (await r.json())["guest"] is None


def test_secret_from_env_not_hardcoded() -> None:
    assert csrf.load_secret({csrf.SECRET_ENV: "s3"}) == b"s3"
    a, b = csrf.load_secret({}), csrf.load_secret({})
    assert a != b and len(a) >= 32


# --- criterion 2 / o9: cross-origin POST with valid cookie ---------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/input"),
        ("post", "/upload"),
        ("post", "/consent"),
        ("post", "/guest/delete"),
        ("delete", "/consent/x"),
    ],
)
async def test_cross_origin_with_cookie_403_no_side_effect(
    client: TestClient, method: str, path: str
) -> None:
    with mock.patch("irc_lens.web.routes._resolve_session") as get_session:
        resp = await getattr(client, method)(
            path,
            data={"text": "pwn"},
            headers={**_cookie(), "Origin": "https://evil.example"},
        )
        assert resp.status == 403
        get_session.assert_not_called()
    assert STUB_HITS == []


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["cross-site", "same-site", ""])
async def test_cookie_without_origin_needs_same_origin_fetch_metadata(
    client: TestClient, site: str
) -> None:
    headers = _cookie()
    if site:
        headers["Sec-Fetch-Site"] = site
    resp = await client.post("/consent", headers=headers)
    assert resp.status == 403
    assert STUB_HITS == []


@pytest.mark.asyncio
async def test_same_origin_with_cookie_allowed(client: TestClient) -> None:
    origin = f"http://127.0.0.1:{client.port}"
    resp = await client.post("/consent", headers={**_cookie(), "Origin": origin})
    assert resp.status == 204
    resp = await client.post(
        "/consent", headers={**_cookie(), "Sec-Fetch-Site": "same-origin"}
    )
    assert resp.status == 204
    assert len(STUB_HITS) == 2


@pytest.mark.asyncio
async def test_no_cookie_keeps_legacy_origin_floor(client: TestClient) -> None:
    assert (await client.post("/consent")).status == 204  # Origin-absent allowed
    r = await client.post("/consent", headers={"Origin": "https://evil.example"})
    assert r.status == 403


@pytest.mark.asyncio
async def test_safe_methods_not_guarded(client: TestClient) -> None:
    r = await client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert r.status == 200
