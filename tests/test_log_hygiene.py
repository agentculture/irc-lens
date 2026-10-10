"""t10: log hygiene -- no secret value appears in any log record.

Covers spec claims c33/h24 (the log half): one app drives the approved
sign-in, set-password, guest and guest-deletion flows end to end with log
capture at DEBUG on the root logger (and ``aiohttp.access``), then asserts
that no password, sign-in/guest/deletion code, set-password token, session
id or cookie value appears in any record's message, args or formatted text.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import secrets
from collections.abc import AsyncIterator

import aiohttp
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens.guest_store import GuestStore
from irc_lens.mail import RecordingAdapter
from irc_lens.session import Session
from irc_lens.web import app_session, csrf, entry, make_app, setpw

from test_entry import FakeVerifier, legal_server  # noqa: F401 -- fixture
from test_guest_limit import APPROVED, _config

ANN = "ann@example.org"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
IP = {"CF-Connecting-IP": "203.0.113.7"}
PW = secrets.token_urlsafe(16)
NEW_PW = secrets.token_urlsafe(16)
CODE_RE = re.compile(r"^ {4}(\S+)$", flags=re.M)
LINK_RE = re.compile(r"/password/([A-Za-z0-9_-]+)")


@pytest_asyncio.fixture
async def app_env(
    jwks: FakeJWKS, tmp_path, legal_server, monkeypatch  # noqa: F811
) -> AsyncIterator[tuple[TestClient, object, RecordingAdapter]]:
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", "s" * 32)
    mesh, sandbox = AgentIRCTestServer(), AgentIRCTestServer()
    await mesh.start()
    await sandbox.start()
    config = dataclasses.replace(
        _config(jwks, mesh, sandbox, tmp_path, legal_server),
        app_signin_base_url="https://lens.example.com",
    )

    def mesh_factory(nick: str) -> Session:
        return Session(host=mesh.host, port=mesh.port, nick=nick)

    app = make_app(config, mesh_factory)
    store = GuestStore(tmp_path / "hygiene.db")
    app["guest_store"] = store
    state = app[entry.ENTRY_STATE]
    state.store = store
    state.mailer = mail = RecordingAdapter()
    state.verifier = FakeVerifier()
    state.signin_floor_s = 0.0
    store.set_password(APPROVED, PW)
    client = TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    try:
        yield client, app, mail
    finally:
        for s in app["registry"].values():
            await s.disconnect()
        await client.close()
        await mesh.stop()
        await sandbox.stop()


async def _drain(app) -> None:
    tasks = list(app[entry.ENTRY_STATE].mail_tasks) + list(app.get(setpw.MAIL_TASKS, ()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _code(mail: RecordingAdapter) -> str:
    return CODE_RE.search(mail.sent[-1][2]).group(1)


async def test_no_secret_value_in_any_log_record(app_env, caplog) -> None:
    client, app, mail = app_env
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="aiohttp.access")
    secrets_seen: dict[str, str] = {"password": PW, "new password": NEW_PW}

    # -- approved app sign-in, session, logout -------------------------------
    r = await client.post(
        "/entry/signin",
        data={"email": APPROVED, "password": PW},
        headers={**IP, **SAME_ORIGIN},
        allow_redirects=False,
    )
    assert r.status == 200
    pending = r.cookies[app_session.SIGNIN_COOKIE_NAME].value
    secrets_seen["lens_signin"] = pending
    await _drain(app)
    code = _code(mail)
    secrets_seen["sign-in code"] = code
    r = await client.post(
        "/entry/code",
        data={"email": APPROVED, "code": code},
        headers={
            **IP,
            **SAME_ORIGIN,
            "Cookie": f"{app_session.SIGNIN_COOKIE_NAME}={pending}",
        },
        allow_redirects=False,
    )
    assert r.status == 303
    raw = r.cookies[app_session.SESSION_COOKIE_NAME].value
    secrets_seen["lens_session"] = raw
    cookie = {"Cookie": f"{app_session.SESSION_COOKIE_NAME}={raw}", **SAME_ORIGIN}
    assert (await client.get("/", headers=cookie)).status == 200
    assert (await client.post("/logout", headers=cookie, allow_redirects=False)).status == 303

    # -- set-password: request, link GET, POST -------------------------------
    r = await client.post("/password", data={"email": APPROVED}, headers=SAME_ORIGIN)
    assert r.status == 200
    await _drain(app)
    token = LINK_RE.search(mail.sent[-1][2]).group(1)
    secrets_seen["setpw token"] = token
    assert (await client.get(f"/password/{token}")).status == 200
    r = await client.post(
        f"/password/{token}",
        data={"password": NEW_PW, "confirm": NEW_PW},
        headers=SAME_ORIGIN,
    )
    assert r.status == 200

    # -- guest flow: code -> verify -> page -> message -----------------------
    r = await client.post(
        "/entry/guest/start",
        data={"email": ANN, "nickname": "ann", "consent": "on"},
        headers=SAME_ORIGIN,
        allow_redirects=False,
    )
    assert r.status == 200
    await _drain(app)
    gcode = _code(mail)
    secrets_seen["guest code"] = gcode
    r = await client.post(
        "/entry/verify",
        data={"email": ANN, "nickname": "ann", "code": gcode},
        headers=SAME_ORIGIN,
        allow_redirects=False,
    )
    assert r.status == 303
    gcookie = r.cookies[csrf.GUEST_COOKIE_NAME].value
    secrets_seen["guest cookie"] = gcookie
    gh = {"Cookie": f"{csrf.GUEST_COOKIE_NAME}={gcookie}", **SAME_ORIGIN}
    assert (await client.get("/", headers=gh)).status == 200
    assert (await client.post("/input", json={"text": "hello"}, headers=gh)).status == 204

    # -- guest deletion: request + confirm -----------------------------------
    r = await client.post("/delete/request", data={"email": ANN}, headers=SAME_ORIGIN)
    assert r.status == 200
    await _drain(app)
    dcode = _code(mail)
    secrets_seen["deletion code"] = dcode
    r = await client.post(
        "/delete/confirm",
        data={"email": ANN, "code": dcode},
        headers=SAME_ORIGIN,
    )
    assert r.status == 200

    # -- nothing may leak ----------------------------------------------------
    assert len(set(secrets_seen.values())) == len(secrets_seen), "secrets must be distinct"
    assert caplog.records, "log capture saw nothing; the test would be vacuous"
    for rec in caplog.records:
        haystacks = [rec.getMessage(), rec.msg if isinstance(rec.msg, str) else "", str(rec.args)]
        haystacks.append(logging.Formatter().format(rec))
        for name, value in secrets_seen.items():
            for h in haystacks:
                assert value not in h, f"{name} leaked in log record {rec.name}: {h!r}"
