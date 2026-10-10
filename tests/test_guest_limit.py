"""Guest limit + action-based idle sign-off (task t9 of app-native sign-in).

Coverage map:

* c24 / h16 -- while ``guest_mode.max_guests`` guests are active a new
  visitor who picks Guest mode sees the busy message before any code is
  emailed; code verification re-checks the limit under a lock so two
  guests can't slip in together; the slot frees on deletion or after the
  idle close; an approved user's Guest view never counts and is never
  refused.
* c28 / h19 -- guest idle sign-off counts actions (``POST /input``), not
  open tabs.
* c35 -- after idle sign-off the guest's cookie still works: rejoin if a
  slot is free, else the busy page; no new code is emailed.

One integration environment: two fake AgentIRC servers (mesh + sandbox),
the real entry / deletion routes, the real guest store, a recording mail
adapter and a stub legal-versions server. The registry clock is a fake
so idle times are exact.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

import aiohttp
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens import metrics
from irc_lens.config import LensConfig
from irc_lens.mail import RecordingAdapter
from irc_lens.session import Session
from irc_lens.web import csrf, entry, make_app

from test_entry import FakeVerifier, legal_server  # noqa: F401 -- fixture

BUSY = "The sandbox is busy. Try again in a few minutes."
APPROVED = "alice@example.com"
ANN = "ann@example.org"
BOB = "bob@example.org"
IDLE_S = 900
_SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _config(jwks: FakeJWKS, mesh, sandbox, tmp_path, legal_url: str) -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=(APPROVED,),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host=mesh.host,
        server_port=mesh.port,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=False,
        media_dir=str(tmp_path / "media"),
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
        guest_enabled=True,
        guest_sandbox_host=sandbox.host,
        guest_sandbox_port=sandbox.port,
        guest_store_path=str(tmp_path / "guests.db"),
        guest_legal_version_url=legal_url,
        guest_rate_entry_per_min=50,
        guest_rate_password_attempts_per_15min=50,
        guest_rate_messages_per_min=50,
        guest_max_guests=1,
        guest_idle_close_s=IDLE_S,
    )


class Env:
    def __init__(self, client, app, mesh, sandbox, jwks, clock) -> None:
        self.client, self.app, self.mesh, self.sandbox = client, app, mesh, sandbox
        self.jwks, self.clock = jwks, clock
        self.cookies: dict[str, str] = {}

    @property
    def registry(self):
        return self.app["registry"]

    @property
    def mail(self) -> RecordingAdapter:
        return self.app[entry.ENTRY_STATE].mailer

    def guest_mails(self) -> int:
        return sum(
            1 for _to, subject, _b in self.mail.sent if "deletion" not in subject
        )

    async def post(self, path: str, data: dict, headers: dict | None = None):
        return await self.client.post(
            path,
            data=data,
            headers={**_SAME_ORIGIN, **(headers or {})},
            allow_redirects=False,
        )

    def code_for(self, email: str) -> str:
        body = [b for to, _s, b in self.mail.sent if to == email][-1]
        return re.search(r"^ {4}(\S+)$", body, flags=re.M).group(1)

    async def request_code(self, email: str, nick: str):
        return await self.post(
            "/entry/guest/start", {"email": email, "nickname": nick, "consent": "on"}
        )

    async def verify(self, email: str, nick: str, code: str):
        resp = await self.post(
            "/entry/verify", {"email": email, "nickname": nick, "code": code}
        )
        if csrf.GUEST_COOKIE_NAME in resp.cookies:
            self.cookies[email] = resp.cookies[csrf.GUEST_COOKIE_NAME].value
        return resp

    def guest_headers(self, email: str) -> dict[str, str]:
        return {
            "Cookie": f"{csrf.GUEST_COOKIE_NAME}={self.cookies[email]}",
            **_SAME_ORIGIN,
        }

    def approved_headers(self) -> dict[str, str]:
        tok = self.jwks.mint(aud="aud-test", claims={"email": APPROVED, "sub": "s"})
        return {"Cf-Access-Jwt-Assertion": tok, **_SAME_ORIGIN}

    async def enter(self, email: str, nick: str) -> None:
        """Full guest entry: code mail -> verify -> GET / opens the session."""
        assert (await self.request_code(email, nick)).status == 200
        resp = await self.verify(email, nick, self.code_for(email))
        assert resp.status == 303, await resp.text()
        page = await self.client.get("/", headers=self.guest_headers(email))
        assert page.status == 200
        assert self.registry.has(f"guest:{email}", "sandbox")

    async def say(self, email: str, text: str = "hello"):
        return await self.client.post(
            "/input", json={"text": text}, headers=self.guest_headers(email)
        )

    async def reap(self) -> list:
        return await self.registry.reap_idle(now=self.clock.now, idle_s=IDLE_S)


@pytest_asyncio.fixture
async def env(
    jwks: FakeJWKS, tmp_path, legal_server, monkeypatch
) -> AsyncIterator[Env]:  # noqa: F811
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", "s" * 32)
    mesh, sandbox = AgentIRCTestServer(), AgentIRCTestServer()
    await mesh.start()
    await sandbox.start()
    config = _config(jwks, mesh, sandbox, tmp_path, legal_server)

    def mesh_factory(nick: str) -> Session:
        return Session(host=mesh.host, port=mesh.port, nick=nick)

    app = make_app(config, mesh_factory)
    state = app[entry.ENTRY_STATE]
    state.mailer = RecordingAdapter()
    state.verifier = FakeVerifier()
    clock = FakeClock()
    app["registry"]._clock = clock
    # One client, no cookie jar: every request names its guest explicitly.
    client = TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    try:
        yield Env(client, app, mesh, sandbox, jwks, clock)
    finally:
        for s in app["registry"].values():
            await s.disconnect()
        await client.close()
        await mesh.stop()
        await sandbox.stop()


# -- c24 / h16: busy before any code is mailed --------------------------------


async def test_busy_page_on_guest_mode_and_no_code_mailed(env: Env) -> None:
    await env.enter(ANN, "ann")
    mails = len(env.mail.sent)
    busy_before = metrics.get_metrics().snapshot()["guest_busy"]

    picked = await env.post("/entry/guest", {"email": BOB})
    assert BUSY in await picked.text()
    assert 'name="nickname"' not in await picked.text()
    started = await env.request_code(BOB, "bob")
    assert BUSY in await started.text()

    assert len(env.mail.sent) == mails  # h16: 0 codes emailed
    assert metrics.get_metrics().snapshot()["guest_busy"] == busy_before + 2
    assert env.registry.active_guest_count() == 1


async def test_active_guest_is_not_busy_for_themselves(env: Env) -> None:
    await env.enter(ANN, "ann")
    page = await env.post("/entry/guest", {"email": ANN})
    html = await page.text()
    assert BUSY not in html
    assert 'name="nickname"' in html


async def test_simultaneous_verifies_admit_exactly_one(env: Env) -> None:
    """h16: two guests verifying codes at the same moment -> one active."""
    await env.request_code(ANN, "ann")
    await env.request_code(BOB, "bob")
    assert env.guest_mails() == 2  # both mailed while the sandbox was empty
    a, b = await asyncio.gather(
        env.verify(ANN, "ann", env.code_for(ANN)),
        env.verify(BOB, "bob", env.code_for(BOB)),
    )
    assert sorted([a.status, b.status]) == [200, 303]
    loser = b if a.status == 303 else a
    assert BUSY in await loser.text()
    assert csrf.GUEST_COOKIE_NAME not in loser.cookies
    assert env.registry.active_guest_count() == 1
    winner = ANN if a.status == 303 else BOB
    page = await env.client.get("/", headers=env.guest_headers(winner))
    assert page.status == 200
    assert env.registry.active_guest_count() == 1
    assert env.registry.has(f"guest:{winner}", "sandbox")


async def test_verified_but_unopened_slot_expires(env: Env) -> None:
    """A reservation whose browser never comes back frees itself."""
    await env.request_code(ANN, "ann")
    assert (await env.verify(ANN, "ann", env.code_for(ANN))).status == 303
    assert env.registry.active_guest_count() == 1
    busy = await env.post("/entry/guest", {"email": BOB})
    assert BUSY in await busy.text()
    env.clock.now += 121
    assert env.registry.active_guest_count() == 0
    free = await env.post("/entry/guest", {"email": BOB})
    assert BUSY not in await free.text()


async def test_slot_frees_when_the_guest_deletes_their_data(env: Env) -> None:
    await env.enter(ANN, "ann")
    await env.post("/delete/request", {"email": ANN})
    done = await env.post("/delete/confirm", {"email": ANN, "code": env.code_for(ANN)})
    assert done.status == 200
    assert env.registry.active_guest_count() == 0
    await env.enter(BOB, "bob")  # h16: the next visitor gets in


async def test_slot_frees_after_idle_close(env: Env) -> None:
    await env.enter(ANN, "ann")
    env.clock.now += IDLE_S
    assert await env.reap() == [(f"guest:{ANN}", "sandbox")]
    assert env.registry.active_guest_count() == 0
    await env.enter(BOB, "bob")


# -- c28 / h19: idle counts actions, not open tabs ----------------------------


async def test_open_tab_with_no_input_is_signed_off(env: Env) -> None:
    await env.enter(ANN, "ann")
    session = env.registry.values()[0]
    session.event_bus.subscribe()  # the tab stays open
    env.clock.now += IDLE_S - 1
    assert await env.reap() == []
    env.clock.now += 1
    assert await env.reap() == [(f"guest:{ANN}", "sandbox")]
    assert not env.registry.has(f"guest:{ANN}", "sandbox")


async def test_chatting_guest_is_not_signed_off(env: Env) -> None:
    await env.enter(ANN, "ann")
    env.registry.values()[0].event_bus.subscribe()
    for _ in range(3):
        env.clock.now += IDLE_S - 60
        assert (await env.say(ANN)).status == 204
        assert await env.reap() == []
    assert env.registry.has(f"guest:{ANN}", "sandbox")
    env.clock.now += IDLE_S
    assert await env.reap() == [(f"guest:{ANN}", "sandbox")]


async def test_polling_does_not_count_as_action_or_reopen(env: Env) -> None:
    """Presence polls / SSE are not actions and never reopen a signed-off
    guest's session (that would hold the slot forever)."""
    await env.enter(ANN, "ann")
    env.clock.now += IDLE_S - 1
    assert (
        await env.client.get("/presence", headers=env.guest_headers(ANN))
    ).status == 200
    env.clock.now += 1
    assert await env.reap() == [(f"guest:{ANN}", "sandbox")]
    await env.client.get("/presence", headers=env.guest_headers(ANN))
    events = await env.client.get("/events", headers=env.guest_headers(ANN))
    assert events.status == 204
    assert env.registry.active_guest_count() == 0


# -- c35: returning guest after idle sign-off ---------------------------------


async def test_returning_guest_rejoins_when_slot_free(env: Env) -> None:
    await env.enter(ANN, "ann")
    mails = len(env.mail.sent)
    env.clock.now += IDLE_S
    await env.reap()
    page = await env.client.get("/", headers=env.guest_headers(ANN))
    assert page.status == 200
    assert BUSY not in await page.text()
    assert env.registry.has(f"guest:{ANN}", "sandbox")
    assert (await env.say(ANN)).status == 204
    assert len(env.mail.sent) == mails  # no new code


async def test_returning_guest_by_input_rejoins_when_slot_free(env: Env) -> None:
    """The tab stayed open: the next message reopens the session."""
    await env.enter(ANN, "ann")
    env.clock.now += IDLE_S
    await env.reap()
    assert (await env.say(ANN)).status == 204
    assert env.registry.has(f"guest:{ANN}", "sandbox")


async def test_returning_guest_sees_busy_when_slot_taken(env: Env) -> None:
    await env.enter(ANN, "ann")
    env.clock.now += IDLE_S
    await env.reap()
    await env.enter(BOB, "bob")
    mails = len(env.mail.sent)
    page = await env.client.get("/", headers=env.guest_headers(ANN))
    assert BUSY in await page.text()
    said = await env.say(ANN)
    assert said.status == 503
    assert said.headers.get("HX-Redirect") == "/"
    assert not env.registry.has(f"guest:{ANN}", "sandbox")
    assert env.registry.active_guest_count() == 1
    assert len(env.mail.sent) == mails  # no new code


# -- approved users' Guest view never counts and is never refused -------------


async def test_approved_guest_view_never_counts_or_refused(env: Env) -> None:
    await env.enter(ANN, "ann")
    r = await env.client.post("/sandbox/enter", headers=env.approved_headers())
    assert r.status in (200, 204)
    page = await env.client.get("/", headers=env.approved_headers())
    assert page.status == 200
    assert BUSY not in await page.text()
    assert env.registry.has(APPROVED, "sandbox") or any(
        b == "sandbox" and not p.startswith("guest:") for p, b in env.registry.keys()
    )
    assert env.registry.active_guest_count() == 1


async def test_approved_guest_view_does_not_block_a_guest(env: Env) -> None:
    r = await env.client.post("/sandbox/enter", headers=env.approved_headers())
    assert r.status in (200, 204)
    assert (await env.client.get("/", headers=env.approved_headers())).status == 200
    assert env.registry.active_guest_count() == 0
    await env.enter(ANN, "ann")


async def test_approved_guest_view_keeps_the_open_tab_rule(env: Env) -> None:
    """The sandbox_preview session is not action-reaped while its tab is open."""
    await env.client.post("/sandbox/enter", headers=env.approved_headers())
    await env.client.get("/", headers=env.approved_headers())
    preview = env.registry.values()[0]
    preview.event_bus.subscribe()
    await env.reap()
    env.clock.now += IDLE_S * 3
    assert await env.reap() == []
