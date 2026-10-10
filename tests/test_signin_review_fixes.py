"""Review fixes on the app sign-in PR (#68).

* An ``auth.allowed_emails`` entry with capital letters signs in, keeps its
  session, survives the revocation sweep and gets set-password links.
* App sign-in needs a mail provider: with ``guest_mode.mail.provider: none``
  it stays off (the 0.12.2 ``/login`` path) and one warning is logged.
* A set-password request with a non-https base URL issues no token.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path

from irc_lens.config import load_config
from irc_lens.web import app_session, make_app, setpw
from test_app_signin import ALICE, PW, SAME_ORIGIN, _make_env, pending_of

MIXED = "Alice@Example.COM"
assert MIXED.lower() == ALICE


async def _close(e) -> None:
    await e.drain()
    for s in e.app["registry"].values():
        await s.disconnect()
    await e.client.close()
    await e.mesh.stop()


async def test_mixed_case_allowlist_signs_in_and_keeps_session(
    jwks, tmp_path, monkeypatch
) -> None:
    e = await _make_env(jwks, tmp_path, monkeypatch, allowed_emails=(MIXED,))
    try:
        r = await e.signin(ALICE, PW)
        await e.drain()
        r = await e.code(ALICE, e.last_code(), pending_of(r))
        assert r.status == 303
        raw = r.cookies[app_session.SESSION_COOKIE_NAME].value
        cookie = {"Cookie": f"lens_session={raw}", **SAME_ORIGIN}
        who = await (await e.client.get("/_whoami", headers=cookie)).json()
        assert who == {"tier": "approved", "principal": ALICE}
        assert (await e.client.get("/", headers=cookie)).status == 200
        assert await app_session.sweep_once(e.app) == []
        assert e.app["registry"].has(ALICE, "mesh")
    finally:
        await _close(e)


async def test_mixed_case_allowlist_gets_and_uses_setpw_link(
    jwks, tmp_path, monkeypatch
) -> None:
    e = await _make_env(
        jwks,
        tmp_path,
        monkeypatch,
        allowed_emails=(MIXED,),
        app_signin_base_url="https://lens.example.com",
    )
    try:
        await e.client.post("/password", data={"email": ALICE}, headers=SAME_ORIGIN)
        for t in list(e.app.get(setpw.MAIL_TASKS, ())):
            await t
        m = re.search(r"/password/([A-Za-z0-9_-]+)", e.mail.sent[-1][2])
        assert m, "no set-password link was mailed"
        new_pw = PW + "-new"
        r = await e.client.post(
            f"/password/{m.group(1)}",
            data={"password": new_pw, "confirm": new_pw},
            headers=SAME_ORIGIN,
        )
        assert r.status == 200
        assert e.store.check_password(ALICE, new_pw)
    finally:
        await _close(e)


async def test_http_base_url_issues_no_setpw_token(
    jwks, tmp_path, monkeypatch, caplog
) -> None:
    e = await _make_env(
        jwks,
        tmp_path,
        monkeypatch,
        app_signin_base_url=None,
        media_public_base_url="http://media.example.com",
    )
    try:
        caplog.set_level(logging.ERROR)
        await e.client.post("/password", data={"email": ALICE}, headers=SAME_ORIGIN)
        for t in list(e.app.get(setpw.MAIL_TASKS, ())):
            await t
        assert e.mail.sent == []
        rows = e.store._all("SELECT 1 FROM tokens WHERE purpose='setpw'")
        assert rows == []
        assert any("https" in r.getMessage() for r in caplog.records)
    finally:
        await _close(e)


def _load(tmp_path: Path, guest_block: str = ""):
    p = tmp_path / "config.yaml"
    p.write_text(
        "auth:\n  mode: dev\n  dev:\n    nick: lens\n    email: dev@local\n"
        "server:\n  name: spark\n" + guest_block
    )
    return load_config(p)


def test_app_signin_off_without_mail_provider(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING)
    cfg = _load(tmp_path)
    assert cfg.app_signin_enabled is False
    # Loading the config stays quiet (CLI tools load it too)...
    assert not [r for r in caplog.records if "sign-in" in r.getMessage()]
    # ...the console says why once, at startup.
    make_app(dataclasses.replace(cfg, guest_enabled=True), lambda nick: None)
    warnings = [r for r in caplog.records if "app sign-in off" in r.getMessage()]
    assert len(warnings) == 1


def test_app_signin_on_with_mail_provider(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        "guest_mode:\n  mail:\n    provider: resend\n    from: lens@example.com\n",
    )
    assert cfg.app_signin_enabled is True
