"""New-browser sign-in notice (owner decision c40, deviation d6).

A completed app sign-in from a browser that is not trusted for that email
mails the user a notice (time, IP, browser); a browser already trusted for
that email gets none, even from another IP. The notice carries no code,
session id or device id, and a failed notice never breaks the sign-in.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest_asyncio

from irc_lens.mail import render_signin_notice
from test_app_signin import ALICE, PW
from test_trusted_devices import DEVICE, TEnv, _tenv

NOTICE_SUBJECT = "New sign-in to chat.culture.dev"


async def _close(e: TEnv) -> None:
    await e.drain()
    for s in e.app["registry"].values():
        await s.disconnect()
    await e.client.close()
    await e.mesh.stop()


@pytest_asyncio.fixture
async def env(jwks, tmp_path, monkeypatch) -> AsyncIterator[TEnv]:
    e = await _tenv(jwks, tmp_path, monkeypatch)
    try:
        yield e
    finally:
        await _close(e)


def _notices(env: TEnv) -> list[tuple[str, str, str]]:
    return [m for m in env.mail.sent if m[1] == NOTICE_SUBJECT]


async def _signin(env: TEnv, ip: str, **kw):
    r = await env.full_signin(ALICE, PW, ip, **kw)
    assert r.status == 303
    await env.drain()
    return r


async def test_untrusted_signin_mails_a_notice(env: TEnv) -> None:
    r = await _signin(env, "198.51.100.23")
    [(to, _subject, body)] = _notices(env)
    assert to == ALICE
    assert "198.51.100.23" in body
    assert "Set or reset password" in body
    session = r.cookies["lens_session"].value
    code = re.search(r"^ {4}(\S+)$", env.mail.sent[-2][2], flags=re.M).group(1)
    assert session not in body
    assert code not in body


async def test_ticked_first_signin_is_still_a_new_browser(env: TEnv) -> None:
    r = await _signin(env, "198.51.100.24", trust=True)
    assert len(_notices(env)) == 1
    assert r.cookies[DEVICE].value not in _notices(env)[0][2]


async def test_trusted_browser_from_a_new_ip_gets_no_notice(env: TEnv) -> None:
    device = await env.trusted_device()
    await env.drain()
    before = len(_notices(env))
    await _signin(env, "203.0.113.99", device=device)
    assert len(_notices(env)) == before


async def test_trusted_for_another_email_is_new_for_this_one(
    jwks, tmp_path, monkeypatch
) -> None:
    bob = "bob@example.com"
    e = await _tenv(jwks, tmp_path, monkeypatch, allowed_emails=(ALICE, bob))
    try:
        e.store.set_password(bob, PW)
        device = await e.trusted_device(bob, PW)
        await _signin(e, "203.0.113.98", device=device)
        assert [m[0] for m in _notices(e)].count(ALICE) == 1
    finally:
        await _close(e)


async def test_failed_notice_does_not_break_signin(env: TEnv, monkeypatch) -> None:
    sent = env.mail.send

    def send(to, subject, body):
        if subject == NOTICE_SUBJECT:
            raise RuntimeError("provider down")
        return sent(to, subject, body)

    monkeypatch.setattr(env.mail, "send", send)
    r = await _signin(env, "198.51.100.25")
    assert r.status == 303


def test_notice_browser_text_is_cleaned() -> None:
    ua = "Evil\r\nBcc: x@example.com\x00" + "A" * 500
    _subject, body = render_signin_notice(
        ip="192.0.2.1", user_agent=ua, when=1_800_000_000
    )
    assert "\r" not in body
    assert "\x00" not in body
    assert "Bcc: x@example.com\n" not in body
    line = next(ln for ln in body.splitlines() if "Browser:" in ln)
    assert len(line) < 200
    assert "2027-01-15 08:00 UTC" in body
