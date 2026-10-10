"""Trusted browsers and the escalating untrusted sign-in budget (r6).

Covers spec claims:

* c37/h26 a browser that completed the full sign-in with "Trust this
          browser" ticked gets ``lens_device`` (random id, only its sha256
          stored with the email; HttpOnly, Secure, SameSite=Lax, one year)
          and no sign-in attempt limit for that email only; logout keeps it;
          setting or resetting the password revokes it
          (``test_trusted_browser_signs_in_after_20_blocked_attempts``,
          ``test_trust_cookie_of_one_email_does_not_exempt_another``,
          ``test_set_password_link_revokes_trust``,
          ``test_cli_style_set_password_revokes_trust``,
          ``test_logout_keeps_trust``,
          ``test_ticked_signin_sets_device_cookie_and_stores_only_hash``).
* c39     the code screen's "Trust this browser" checkbox, unchecked by
          default; unticked = no ``lens_device``, no trust row; an already
          trusted browser that signs in unticked keeps its trust
          (``test_code_screen_has_unchecked_trust_box``,
          ``test_unticked_signin_sets_no_device_and_no_trust_row``,
          ``test_trusted_browser_signing_in_unticked_keeps_trust``).
* c38/h27 untrusted browsers have one per-email budget counting wrong
          password submissions only: 3 per 15 minutes; once exhausted the
          email is strict at 2 per 30 minutes until 24 hours pass with no
          blocked attempt; blocked attempts are silent and byte-identical; the
          per-IP limit (3 wrong password submissions per 15 minutes) still
          applies (``test_fourth_untrusted_password_attempt_blocked_silently``,
          ``test_strict_mode_two_per_thirty_minutes_after_exhaustion``,
          ``test_24_quiet_hours_restore_normal_budget``,
          ``test_blocked_attempt_restarts_the_24_hours``,
          ``test_per_ip_password_limit_still_applies_to_untrusted``,
          ``test_per_ip_limit_counts_wrong_passwords_only``).
* c42     a correct password is not counted, per IP or per email; a blocked
          one was never checked and is not refunded (owner decision, d8)
          (``test_correct_passwords_are_not_counted``,
          ``test_correct_passwords_do_not_use_the_email_budget``,
          ``test_blocked_correct_password_is_not_refunded``).
* c41     code entries count toward nothing -- no per-IP limit, no budget,
          no cap (owner decision after go-live, deviation d7)
          (``test_code_still_works_after_the_password_budget_is_used``,
          ``test_code_entries_do_not_use_the_email_budget``,
          ``test_second_browser_on_the_same_ip_signs_in``).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import AsyncIterator

import pytest_asyncio

from _jwks_server import FakeJWKS
from irc_lens.guest_store import GuestStore
from irc_lens.web import app_session, csrf, entry, setpw
from test_app_signin import (
    ALICE,
    PW,
    SAME_ORIGIN,
    T0,
    UNKNOWN,
    WRONG_PW,
    Env,
    _make_env,
    _shape,
    pending_of,
)

BOB = "bob@example.com"
BOB_PW = secrets.token_urlsafe(16)
NEW_PW = secrets.token_urlsafe(18)
DEVICE = "lens_device"
MIN = 60
HOUR = 3600


class TEnv(Env):
    """Env with a "browser" that may carry a lens_device cookie."""

    async def pw(self, email: str, password: str, ip: str, device: str = ""):
        cookie = f"{DEVICE}={device}" if device else ""
        return await self.signin(email, password, ip=ip, cookie=cookie)

    async def enter(
        self,
        email: str,
        code: str,
        pending: str | None,
        ip: str,
        *,
        device: str = "",
        trust: bool = False,
        extra_cookie: str = "",
    ):
        headers = {"CF-Connecting-IP": ip, **SAME_ORIGIN}
        cookies = []
        if pending is not None:
            cookies.append(f"{app_session.SIGNIN_COOKIE_NAME}={pending}")
        if device:
            cookies.append(f"{DEVICE}={device}")
        if extra_cookie:
            cookies.append(extra_cookie)
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        data = {"email": email, "code": code}
        if trust:
            data["trust"] = "on"
        return await self.client.post(
            "/entry/code", data=data, headers=headers, allow_redirects=False
        )

    async def full_signin(
        self, email: str, password: str, ip: str, *, device: str = "", trust=False
    ):
        """Password + code from one browser; returns the code response."""
        before = len(self.mail.sent)
        r = await self.pw(email, password, ip, device)
        assert r.status == 200
        await self.drain()
        assert len(self.mail.sent) == before + 1, "no code was mailed"
        r = await self.enter(
            email, self.last_code(), pending_of(r), ip, device=device, trust=trust
        )
        await self.drain()  # the new-browser notice (c40) goes out too
        return r

    async def trusted_device(self, email: str = ALICE, password: str = PW) -> str:
        self._trust_ip = getattr(self, "_trust_ip", 199) + 1  # own IP budget
        ip = f"192.0.2.{self._trust_ip}"
        r = await self.full_signin(email, password, ip, trust=True)
        assert r.status == 303
        return r.cookies[DEVICE].value

    async def mails_after(self, coro) -> int:
        before = len(self.mail.sent)
        await coro
        await self.drain()
        return len(self.mail.sent) - before

    def trust_rows(self, email: str | None = None) -> list[tuple]:
        if email is None:
            return self.store._all("SELECT device_hash, email FROM trusted_devices")
        return self.store._all(
            "SELECT device_hash, email FROM trusted_devices WHERE email=?", (email,)
        )


async def _tenv(jwks, tmp_path, monkeypatch, **kw) -> TEnv:
    e = await _make_env(jwks, tmp_path, monkeypatch, **kw)
    e.__class__ = TEnv
    return e


@pytest_asyncio.fixture
async def env(jwks: FakeJWKS, tmp_path, monkeypatch) -> AsyncIterator[TEnv]:
    # A second approved user (Bob) for the cross-email checks.
    e = await _tenv(
        jwks,
        tmp_path,
        monkeypatch,
        allowed_emails=(ALICE, BOB),
        app_signin_base_url="https://lens.example.test",
    )
    e.store.set_password(BOB, BOB_PW)
    try:
        yield e
    finally:
        await e.drain()
        for s in e.app["registry"].values():
            await s.disconnect()
        await e.client.close()
        await e.mesh.stop()


def _ip(i: int) -> str:
    return f"10.{50 + i // 250}.{i % 250}.1"


# ---------------------------------------------------------------------------
# c39: the checkbox
# ---------------------------------------------------------------------------


async def test_code_screen_has_unchecked_trust_box(env: TEnv) -> None:
    html = await (await env.pw(ALICE, WRONG_PW, "192.0.2.1")).text()
    m = re.search(r"<input[^>]*name=\"trust\"[^>]*>", html)
    assert m, "the trust checkbox is always shown"
    assert 'type="checkbox"' in m.group(0)
    assert "checked" not in m.group(0)
    assert "Trust this browser" in html


async def test_unticked_signin_sets_no_device_and_no_trust_row(env: TEnv) -> None:
    r = await env.full_signin(ALICE, PW, "192.0.2.2")
    assert r.status == 303
    assert app_session.SESSION_COOKIE_NAME in r.cookies
    assert DEVICE not in r.cookies
    assert env.trust_rows() == []


async def test_ticked_signin_sets_device_cookie_and_stores_only_hash(
    env: TEnv,
) -> None:
    r = await env.full_signin(ALICE, PW, "192.0.2.3", trust=True)
    assert r.status == 303
    morsel = r.cookies[DEVICE]
    raw = morsel.value
    assert len(raw) >= 40
    header = morsel.OutputString()
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=Lax" in header
    assert "Path=/" in header
    assert f"Max-Age={365 * 86400}" in header
    rows = env.trust_rows()
    assert rows == [(hashlib.sha256(raw.encode()).hexdigest(), ALICE)]
    assert raw not in repr(env.store._all("SELECT * FROM trusted_devices"))
    assert env.store.is_trusted_device(raw, ALICE)


async def test_trusted_browser_signing_in_unticked_keeps_trust(env: TEnv) -> None:
    device = await env.trusted_device()
    r = await env.full_signin(ALICE, PW, "192.0.2.4", device=device, trust=False)
    assert r.status == 303
    assert DEVICE not in r.cookies  # neither re-issued nor cleared
    assert env.store.is_trusted_device(device, ALICE)
    assert len(env.trust_rows(ALICE)) == 1


# ---------------------------------------------------------------------------
# c37/h26: trusted browsers
# ---------------------------------------------------------------------------


async def test_trusted_browser_signs_in_after_20_blocked_attempts(env: TEnv) -> None:
    device = await env.trusted_device()
    env.clock["t"] += 16 * MIN  # the trusting sign-in is out of the window
    # 3 untrusted attempts use up the budget; 20 more are blocked.
    for i in range(3):
        await env.pw(ALICE, WRONG_PW, _ip(i))
    mails = 0
    for i in range(3, 23):
        if i % 2:
            mails += await env.mails_after(env.pw(ALICE, PW, _ip(i)))
        else:
            r = await env.enter(ALICE, secrets.token_urlsafe(32), "p", _ip(i))
            assert r.status == 401
    assert mails == 0, "blocked attempts mail nothing, even with the right password"
    # Also from an IP whose per-IP budget is gone.
    for _ in range(4):
        await env.pw(UNKNOWN, WRONG_PW, "198.51.100.50")
    r = await env.full_signin(ALICE, PW, "198.51.100.50", device=device)
    assert r.status == 303
    assert app_session.SESSION_COOKIE_NAME in r.cookies


async def test_trust_cookie_of_one_email_does_not_exempt_another(env: TEnv) -> None:
    alice_device = await env.trusted_device(ALICE, PW)
    env.clock["t"] += 16 * MIN
    for i in range(4):  # exhaust Bob's untrusted budget
        await env.pw(BOB, WRONG_PW, _ip(i))
    # Alice's trust cookie gives Bob nothing: no code mailed.
    n = await env.mails_after(env.pw(BOB, BOB_PW, "192.0.2.9", alice_device))
    assert n == 0
    # ...but Alice's own sign-in from that browser still works.
    r = await env.full_signin(ALICE, PW, "192.0.2.9", device=alice_device)
    assert r.status == 303


async def test_one_browser_trusted_for_two_emails(env: TEnv) -> None:
    alice_device = await env.trusted_device(ALICE, PW)
    r = await env.full_signin(
        BOB, BOB_PW, "192.0.2.10", device=alice_device, trust=True
    )
    assert r.status == 303
    device = r.cookies[DEVICE].value
    assert device != alice_device  # a fresh id is minted, never promoted
    assert env.store.is_trusted_device(device, ALICE)
    assert env.store.is_trusted_device(device, BOB)
    assert not env.store.is_trusted_device(alice_device, ALICE)


async def test_moving_trust_to_a_new_id_keeps_its_year(env: TEnv) -> None:
    """Trusting the browser for BOB must not extend ALICE's year (c37)."""
    alice_device = await env.trusted_device(ALICE, PW)
    env.clock["t"] += 300 * 86400
    r = await env.full_signin(
        BOB, BOB_PW, "192.0.2.10", device=alice_device, trust=True
    )
    device = r.cookies[DEVICE].value
    env.clock["t"] += 66 * 86400  # 366 days after ALICE trusted it
    assert not env.store.is_trusted_device(device, ALICE)
    assert env.store.is_trusted_device(device, BOB)


async def _exhaust(env: TEnv, email: str = ALICE, start: int = 0) -> None:
    for i in range(start, start + 4):
        await env.pw(email, WRONG_PW, _ip(i))


async def test_set_password_link_revokes_trust(env: TEnv) -> None:
    device = await env.trusted_device()
    other = await env.trusted_device(BOB, BOB_PW)
    # Set a new password through the emailed link.
    r = await env.client.post("/password", data={"email": ALICE}, headers=SAME_ORIGIN)
    assert r.status == 200
    for t in list(env.app.get(setpw.MAIL_TASKS, ())):
        await t
    token = re.search(r"/password/([A-Za-z0-9_-]+)", env.mail.sent[-1][2]).group(1)
    r = await env.client.post(
        f"/password/{token}",
        data={"password": NEW_PW, "confirm": NEW_PW},
        headers=SAME_ORIGIN,
    )
    assert r.status == 200
    assert env.trust_rows(ALICE) == []
    assert env.store.is_trusted_device(other, BOB)  # other emails untouched
    # The old cookie gives no exemption any more.
    env.clock["t"] += 16 * MIN
    await _exhaust(env)
    assert await env.mails_after(env.pw(ALICE, NEW_PW, "192.0.2.11", device)) == 0


async def test_cli_style_set_password_revokes_trust(env: TEnv) -> None:
    device = await env.trusted_device()
    env.store.set_password(ALICE, NEW_PW)  # what `irc-lens guests passwd` does
    assert not env.store.is_trusted_device(device, ALICE)


async def test_logout_keeps_trust(env: TEnv) -> None:
    r = await env.full_signin(ALICE, PW, "192.0.2.12", trust=True)
    device = r.cookies[DEVICE].value
    session = r.cookies[app_session.SESSION_COOKIE_NAME].value
    out = await env.client.post(
        "/logout",
        headers={
            "Cookie": f"lens_session={session}; {DEVICE}={device}",
            **SAME_ORIGIN,
        },
        allow_redirects=False,
    )
    assert out.status == 303
    assert DEVICE not in out.cookies
    assert env.store.is_trusted_device(device, ALICE)
    env.clock["t"] += 16 * MIN
    await _exhaust(env)
    r = await env.full_signin(ALICE, PW, "192.0.2.13", device=device)
    assert r.status == 303


async def test_device_cookie_needs_same_origin_proof(env: TEnv) -> None:
    assert app_session.DEVICE_COOKIE_NAME == DEVICE
    assert DEVICE in csrf.PROOF_COOKIE_NAMES
    r = await env.client.post(
        "/entry/code",
        data={"email": ALICE, "code": "x"},
        headers={"Cookie": f"{DEVICE}=anything", "Sec-Fetch-Site": "cross-site"},
        allow_redirects=False,
    )
    assert r.status == 403


async def test_garbage_device_cookie_is_untrusted(env: TEnv) -> None:
    await _exhaust(env)
    junk = secrets.token_urlsafe(32)
    assert await env.mails_after(env.pw(ALICE, PW, "192.0.2.14", junk)) == 0


# ---------------------------------------------------------------------------
# c38/h27: the untrusted per-email budget
# ---------------------------------------------------------------------------


async def test_fourth_untrusted_password_attempt_blocked_silently(env: TEnv) -> None:
    env.state.signin_floor_s = entry.SIGNIN_FLOOR_S  # the real floor
    shapes = set()
    for i in range(3):  # wrong passwords: these count (c42)
        r = await env.pw(ALICE, WRONG_PW, _ip(i))
        shapes.add(_shape(r, await r.text(), ALICE))
    t0 = time.perf_counter()
    r = await env.pw(ALICE, PW, _ip(3))  # 4th within 15 minutes, right password
    body = await r.text()
    assert time.perf_counter() - t0 >= entry.SIGNIN_FLOOR_S
    shapes.add(_shape(r, body, ALICE))
    assert len(shapes) == 1, "a blocked attempt looks exactly like an unblocked one"
    await env.drain()
    assert env.mail.sent == [], "no code mail when blocked"


async def test_correct_passwords_are_not_counted(env: TEnv) -> None:
    """c42/d8: only wrong passwords count, per IP and per email."""
    ip = "203.0.113.60"
    for _ in range(6):  # six new browsers on one IP, right password each
        assert await env.mails_after(env.pw(ALICE, PW, ip)) == 1
    for _ in range(3):  # three wrong ones use the IP's room
        await env.pw(ALICE, WRONG_PW, ip)
    assert await env.mails_after(env.pw(ALICE, PW, ip)) == 0  # now blocked


async def test_correct_passwords_do_not_use_the_email_budget(env: TEnv) -> None:
    await _wrong(env, 2, 0)  # 2 of 3 counted
    for i in range(5):  # right passwords from fresh IPs: not counted
        assert await _right_mails(env, 10 + i) is True
    await _wrong(env, 1, 20)  # 3rd counted: full
    assert await _right_mails(env, 21) is False


async def test_blocked_correct_password_is_not_refunded(env: TEnv) -> None:
    """A blocked attempt was never checked, so nothing is taken back."""
    await _exhaust(env)  # 3 wrong + 1 blocked: strict mode
    for i in range(5):
        assert await env.mails_after(env.pw(ALICE, PW, _ip(40 + i))) == 0


async def test_code_still_works_after_the_password_budget_is_used(env: TEnv) -> None:
    # c41/d7: code entries are never blocked; only passwords are budgeted.
    r = await env.pw(ALICE, PW, _ip(0))  # password 1, mails a code
    await env.drain()
    pending, code = pending_of(r), env.last_code()
    for i in (1, 2, 3):  # passwords 2, 3 and a blocked 4th
        await env.pw(ALICE, WRONG_PW, _ip(i))
    ok = await env.enter(ALICE, code, pending, _ip(4))
    assert ok.status == 303
    assert app_session.SESSION_COOKIE_NAME in ok.cookies


async def test_code_entries_do_not_use_the_email_budget(env: TEnv) -> None:
    r = await env.pw(ALICE, WRONG_PW, _ip(0))  # wrong password: counted (1)
    for i in range(5):  # wrong codes: not counted
        await env.enter(ALICE, "nope", pending_of(r), _ip(10 + i))
    r = await env.pw(ALICE, PW, _ip(1))  # right password: not counted (c42)
    await env.drain()
    ok = await env.enter(ALICE, env.last_code(), pending_of(r), _ip(2))
    assert ok.status == 303
    await env.drain()  # the new-browser notice (c40)
    for i in (3, 4):  # wrong passwords 2 and 3
        await env.pw(ALICE, WRONG_PW, _ip(i))
    assert await env.mails_after(env.pw(ALICE, PW, _ip(5))) == 0  # budget full


async def test_second_browser_on_the_same_ip_signs_in(env: TEnv) -> None:
    """Go-live regression (d7): a trusted first browser's own sign-in left a
    new second browser on the same IP no room to enter its code."""
    ip = "203.0.113.50"
    first = await env.full_signin(ALICE, PW, ip, trust=True)
    assert first.status == 303
    r = await env.pw(ALICE, PW, ip)  # second browser, untrusted
    await env.drain()
    pending, code = pending_of(r), env.last_code()
    for _ in range(3):  # typos on the code screen
        assert (await env.enter(ALICE, "typo", pending, ip)).status == 401
    ok = await env.enter(ALICE, code, pending, ip)
    assert ok.status == 303
    await env.drain()  # the new-browser notice (c40)


async def _wrong(env: TEnv, n: int, start: int) -> None:
    """*n* wrong-password attempts from fresh IPs (these count, c42)."""
    for i in range(start, start + n):
        await env.pw(ALICE, WRONG_PW, _ip(i))


async def _right_mails(env: TEnv, i: int) -> bool:
    """One right-password attempt: True if it passed (a code was mailed).

    A passing right password is not counted (c42); a blocked one restarts
    the strict period like any blocked attempt.
    """
    return await env.mails_after(env.pw(ALICE, PW, _ip(i))) == 1


async def test_strict_mode_two_per_thirty_minutes_after_exhaustion(
    env: TEnv,
) -> None:
    await _wrong(env, 3, 0)
    assert await _right_mails(env, 3) is False  # 4th: blocked -> strict
    # 16 minutes on, the normal rule would have room; strict has none (3
    # counted within the last 30 minutes).
    env.clock["t"] = T0 + 16 * MIN
    assert await _right_mails(env, 10) is False
    # 31 minutes after the last block: strict allows 2 per 30 minutes.
    env.clock["t"] = T0 + 16 * MIN + 31 * MIN
    await _wrong(env, 1, 20)
    assert await _right_mails(env, 21) is True  # 1 counted, room for one more
    await _wrong(env, 1, 22)
    assert await _right_mails(env, 23) is False  # 2 counted: strict is full


async def test_24_quiet_hours_restore_normal_budget(env: TEnv) -> None:
    await _exhaust(env)
    env.clock["t"] = T0 + 24 * HOUR + 1
    await _wrong(env, 2, 10)
    assert await _right_mails(env, 12) is True  # normal 3: room left
    await _wrong(env, 1, 13)
    assert await _right_mails(env, 14) is False  # 3 counted: full


async def test_blocked_attempt_restarts_the_24_hours(env: TEnv) -> None:
    await _exhaust(env)  # strict from T0
    env.clock["t"] = T0 + 23 * HOUR
    await _wrong(env, 3, 10)  # 2 counted, the 3rd blocked: restarts at 23 h
    env.clock["t"] = T0 + 24 * HOUR + 1  # only 1 h since the last block
    await _wrong(env, 2, 20)
    assert await _right_mails(env, 22) is False  # still strict (2 per 30 min)
    env.clock["t"] = T0 + 48 * HOUR + 2  # 24 h after the last block
    await _wrong(env, 2, 30)
    assert await _right_mails(env, 32) is True  # normal again


async def test_budget_is_per_email(env: TEnv) -> None:
    await _exhaust(env, ALICE)
    n = await env.mails_after(env.pw(BOB, BOB_PW, "192.0.2.20"))
    assert n == 1


async def test_per_ip_password_limit_still_applies_to_untrusted(env: TEnv) -> None:
    ip = "198.51.100.60"
    for i in range(3):  # different unknown emails: no per-email budget used
        await env.pw(f"x{i}@example.org", WRONG_PW, ip)
    assert await env.mails_after(env.pw(ALICE, PW, ip)) == 0  # 4th from the IP
    assert await env.mails_after(env.pw(ALICE, PW, "198.51.100.59")) == 1
    # A trusted browser at the same IP is not limited.
    device = await env.trusted_device()
    assert await env.mails_after(env.pw(ALICE, PW, ip, device)) == 1


async def test_per_ip_limit_counts_wrong_passwords_only(env: TEnv) -> None:
    ip = "198.51.100.62"
    r = await env.pw(ALICE, PW, ip)  # right password: not counted (c42)
    await env.drain()
    pending, code = pending_of(r), env.last_code()
    for i in range(5):  # code entries: not counted (c41)
        await env.enter(f"z{i}@example.org", "nope", pending, ip)
    assert (await env.enter(ALICE, code, pending, ip)).status == 303
    await env.drain()  # the new-browser notice (c40)
    await env.pw(UNKNOWN, WRONG_PW, ip)  # 1
    await env.pw(BOB, WRONG_PW, ip)  # 2
    await env.pw(UNKNOWN, WRONG_PW, ip)  # 3
    assert await env.mails_after(env.pw(BOB, BOB_PW, ip)) == 0  # 4th: blocked
    # 15 minutes on, the IP has room again.
    env.clock["t"] = T0 + 15 * MIN + 1
    assert await env.mails_after(env.pw(BOB, BOB_PW, ip)) == 1


# ---------------------------------------------------------------------------
# store: retention
# ---------------------------------------------------------------------------


def test_sweep_drops_year_old_trust_and_quiet_budget_rows(tmp_path) -> None:
    clock = {"t": T0}
    s = GuestStore(tmp_path / "t.db", clock=lambda: clock["t"])
    raw = s.add_trusted_device(ALICE)
    assert s.is_trusted_device(raw, ALICE)
    assert not s.is_trusted_device(raw, BOB)
    for _ in range(4):
        s.signin_budget_take(ALICE)
    assert s._all("SELECT COUNT(*) FROM signin_budget")[0][0] == 1
    clock["t"] = T0 + 2 * 86400
    counts = s.sweep()
    assert counts["signin_budget"] == 1
    assert counts["trusted_devices"] == 0
    clock["t"] = T0 + 366 * 86400
    assert not s.is_trusted_device(raw, ALICE)
    counts = s.sweep()
    assert counts["trusted_devices"] == 1
    assert s._all("SELECT COUNT(*) FROM trusted_devices")[0][0] == 0


async def test_no_device_id_or_code_is_logged(env: TEnv, caplog) -> None:
    caplog.set_level("DEBUG")
    r = await env.pw(ALICE, PW, "192.0.2.30")
    await env.drain()
    code, pending = env.last_code(), pending_of(r)
    ok = await env.enter(ALICE, code, pending, "192.0.2.30", trust=True)
    device = ok.cookies[DEVICE].value
    await _exhaust(env)
    await env.pw(ALICE, PW, "192.0.2.31", device)
    for secret in (PW, code, pending, device):
        assert secret not in caplog.text
