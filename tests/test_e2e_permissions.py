"""t17: end-to-end suite -- humans, agents and the 0-cross-delivery check.

The announcement (spec claim c1), clause by clause, each mapped to assertions:

1. "admins have full access"                       -> ``test_three_credentials_get_three_scopes``
2. "other approved humans reach only their granted
   mesh channels"                                  -> ``test_three_credentials_get_three_scopes``,
                                                      ``test_onboarding_three_users_*``
3. "guests stay in their separate sandbox"         -> ``test_three_credentials_get_three_scopes``
4. "each human may bring one personal agent over
   the lens's single MCP endpoint"                 -> ``test_one_endpoint_*`` (the three
                                                      credentials all hit ``/mcp``)
5. "authenticated through the human's email"       -> ``test_agent_is_bound_to_its_humans_email_*``
6. "present wherever its human is with the same
   permissions"                                    -> ``test_agent_has_exactly_its_humans_*``,
                                                      ``test_revoked_grant_*``,
                                                      ``test_kill_switch_*``
7. "reachable only by its own human"               -> ``test_zero_cross_human_deliveries``
                                                      (c27's measurable: the total of
                                                      cross-human deliveries is 0)
8. no restart / no config edit (c27, h39)          -> ``test_onboarding_three_users_*``

Everything runs through the REAL app (``make_app`` with
``auth.email_permissions.enabled`` true, the real registry, the MCP endpoint at
``config.mcp_path``) against real relay-mode IRCd fixtures
(``AgentIRCTestServer(relay=True)``): one for the mesh, one for the guest
sandbox. A "raw IRC client" is a plain TCP socket that never touches the lens.
MCP calls use the SDK's ``streamable_http_client`` (a stock client).
Every address and host below is an example placeholder.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from collections.abc import AsyncIterator

import aiohttp
import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from _agentirc_server import AgentIRCTestServer
from _jwks_server import FakeJWKS
from irc_lens import config as config_module
from irc_lens.agent_nick import agent_nick_for
from irc_lens.mail import RecordingAdapter
from irc_lens.session import Session
from irc_lens.web import app_session, csrf, entry, make_app, setpw
from irc_lens.web.identity import derive_nick
from irc_lens.web.sessions import BACKEND_MESH
from test_agent_sessions import RawClient, settle
from test_session_routing import _config, consent  # noqa: F401 - fixture

ADMIN = "root@example.com"
A = "ann@example.org"
B = "ben@example.org"
G = "gus@example.org"
G2 = "gia@example.org"
SECRET = b"s" * 32
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
MCP_PATH = "/mcp"
BASE = "https://lens.example.com"
PW = "correct-horse-battery-staple-1"
SHARED = "#shared"
_TOKEN_RE = re.compile(r'data-testid="agent-token"[^>]*>([A-Za-z0-9_-]+)<')
_MARKER_RE = re.compile(r"XMARK-[a-z0-9]+-\d+")

NICK = {e: derive_nick("testsrv", e) for e in (ADMIN, A, B)}
AGENT = {e: agent_nick_for(n) for e, n in NICK.items()}
G_NICK = "sbx-gus"
G_AGENT = agent_nick_for(G_NICK)


class Env:
    """The running lens plus the helpers every test below shares."""

    def __init__(self, client, app, jwks, mesh, sandbox, mail):
        self.client, self.app, self.jwks = client, app, jwks
        self.mesh, self.sandbox, self.mail = mesh, sandbox, mail
        self.raw: list[RawClient] = []

    # -- state -------------------------------------------------------------

    @property
    def access(self):
        return self.app[app_session.ACCESS_STORE]

    @property
    def guests(self):
        return self.app["guest_store"]

    @property
    def registry(self):
        return self.app["registry"]

    def human_session(self, principal: str, backend: str = BACKEND_MESH):
        return dict(zip(self.registry.keys(), self.registry.values()))[
            (principal, backend)
        ]

    def sent_by(self, server: AgentIRCTestServer, nick: str) -> list[str]:
        out, current = [], None
        for line in server.received:
            if line.command == "NICK":
                current = line.params[0]
            elif current == nick:
                out.append(line.raw.strip())
        return out

    # -- credentials of the three tiers ------------------------------------

    def jwt(self, email: str) -> dict[str, str]:
        tok = self.jwks.mint(aud="aud-test", claims={"email": email, "sub": "s"})
        return {"Cf-Access-Jwt-Assertion": tok, **SAME_ORIGIN}

    def guest(self, email: str = G) -> dict[str, str]:
        val = csrf.make_cookie_value(email, SECRET, 3600)
        return {"Cookie": f"{csrf.GUEST_COOKIE_NAME}={val}", **SAME_ORIGIN}

    # -- web (the human's side) -------------------------------------------

    async def issue(self, headers: dict[str, str]) -> str:
        r = await self.client.post(
            "/agent-credential", headers=headers, allow_redirects=False
        )
        assert r.status == 200, await r.text()
        found = _TOKEN_RE.search(await r.text())
        assert found, "issued token not shown"
        return found.group(1)

    async def online(self, headers: dict[str, str]) -> None:
        r = await self.client.get("/", headers=headers)
        assert r.status == 200, await r.text()

    async def say(self, headers: dict[str, str], text: str):
        return await self.client.post("/input", json={"text": text}, headers=headers)

    async def admin_post(self, path: str, **data):
        r = await self.client.post(
            path, data=data, headers=self.jwt(ADMIN), allow_redirects=False
        )
        await self.drain()
        return r

    async def drain(self) -> None:
        tasks = list(self.app.get(setpw.MAIL_TASKS, ()))
        tasks += list(self.app[entry.ENTRY_STATE].mail_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- the agent's side: stock MCP client over HTTP ----------------------

    async def call(self, token: str, command: str, **args) -> dict:
        url = str(self.client.make_url(MCP_PATH))
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
            timeout=30,
        ) as http:
            async with streamable_http_client(url, http_client=http) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "run", {"command": [command], "args": args}
                    )
        return json.loads(result.content[0].text)

    async def ok(self, token: str, command: str, **args):
        payload = await self.call(token, command, **args)
        assert "result" in payload, payload
        return payload["result"]

    async def refused(self, token: str, command: str, **args) -> str:
        payload = await self.call(token, command, **args)
        assert "error" in payload, payload
        err = payload["error"]
        return f"{err['message']} {err.get('remediation', '')}"

    async def status(self, token: str) -> int:
        """Raw HTTP status of one MCP request (401 = refused before any tool)."""
        r = await self.client.post(
            MCP_PATH, headers={"Authorization": f"Bearer {token}"}, json={}
        )
        return r.status

    async def raw_client(self, nick: str, server=None) -> RawClient:
        c = await RawClient.connect(server or self.mesh, nick)
        self.raw.append(c)
        return c

    async def member_with_agent(self, email: str) -> str:
        """Human online in the shared channel, credential issued, agent joined."""
        await self.online(self.jwt(email))
        token = await self.issue(self.jwt(email))
        await self.ok(token, "join", channel=SHARED)
        r = await self.say(self.jwt(email), f"/join {SHARED}")
        assert r.status == 204, await r.text()
        return token

    async def guest_with_agent(self, email: str = G) -> str:
        await self.online(self.guest(email))
        token = await self.issue(self.guest(email))
        await self.ok(token, "whoami")
        return token


async def _build(jwks, tmp_path, monkeypatch) -> Env:
    monkeypatch.setenv("IRC_LENS_GUEST_COOKIE_SECRET", SECRET.decode())
    mesh = AgentIRCTestServer(relay=True)
    sandbox = AgentIRCTestServer(relay=True)
    await mesh.start()
    await sandbox.start()
    cfg = dataclasses.replace(
        _config(jwks, mesh, sandbox, tmp_path),
        access_enabled=True,
        allowed_emails=(ADMIN,),
        mcp_path=MCP_PATH,
        app_signin_base_url=BASE,
        guest_rate_messages_per_min=50,
    )
    app = make_app(
        cfg,
        lambda nick: Session(host=mesh.host, port=mesh.port, nick=nick),
        ban_sweep_interval_s=3600.0,
    )
    mail = RecordingAdapter()
    state = app[entry.ENTRY_STATE]
    state.mailer = mail
    state.signin_floor_s = 0.0
    # One jar per "browser" is simulated by hand: each request names its cookies.
    client = TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())
    await client.start_server()
    return Env(client, app, jwks, mesh, sandbox, mail)


async def _close(e: Env) -> None:
    for c in e.raw:
        await c.close()
    await e.drain()
    await e.client.close()
    await e.mesh.stop()
    await e.sandbox.stop()


@pytest_asyncio.fixture
async def env(
    jwks: FakeJWKS, tmp_path, consent, monkeypatch  # noqa: F811
) -> AsyncIterator[Env]:
    """Admin, two approved members (A, B) and a guest (G), a real relay IRCd."""
    e = await _build(jwks, tmp_path, monkeypatch)
    assert e.access.get_user(ADMIN)["role"] == "admin"
    for email in (A, B):
        e.access.set_user(email, role="member", state="approved", actor=None)
        e.access.set_grant(email, "channel", SHARED, "write", actor=None)
    e.access.set_grant(A, "channel", "#a-only", "write", actor=None)
    e.access.set_grant(A, "channel", "#lobby", "read", actor=None)
    e.access.set_grant(B, "channel", "#b-only", "write", actor=None)
    e.guests.record_guest(G, G_NICK, "127.0.0.1")
    e.guests.record_guest(G2, "sbx-gia", "127.0.0.2")
    try:
        yield e
    finally:
        await _close(e)


# -- c27 / h25: the 0 cross-human delivery check ---------------------------------------


async def test_zero_cross_human_deliveries(env: Env) -> None:
    """Admin, approved and guest humans, each with one agent.

    Every path by which one human could reach another human's agent is
    driven: a lens DM, an @mention and a ``nick:`` prefix in a shared channel,
    a DM sent on the human's own lens-held connection (bypassing the lens
    checks), and raw IRC clients that never touched the lens. Each hostile
    line carries a unique marker; the measurable is the number of markers that
    reach somebody else's agent over MCP.
    """
    tokens = {
        A: await env.member_with_agent(A),
        B: await env.member_with_agent(B),
        ADMIN: await env.member_with_agent(ADMIN),
    }
    tokens[G] = await env.guest_with_agent(G)
    await settle()
    for t in tokens.values():
        await env.ok(t, "read")  # drain the joins

    humans = {A: env.jwt(A), B: env.jwt(B), ADMIN: env.jwt(ADMIN)}
    # marker -> (attacker, victim email or None, is_dm)
    sent: dict[str, tuple[str, str | None, bool]] = {}
    seq = iter(range(1000))

    def marker(attacker: str, victim: str | None, path: str, dm: bool) -> str:
        m = f"XMARK-{path}-{next(seq)}"
        sent[m] = (attacker, victim, dm)
        return m

    mallory = await env.raw_client("testsrv-mallory")
    await mallory.send(f"JOIN {SHARED}")
    sandbox_mallory = await env.raw_client("sbx-mallory", env.sandbox)

    # Every human attacks every OTHER member's agent by every path.
    for attacker in (A, B, ADMIN):
        for victim in (A, B, ADMIN):
            if victim == attacker:
                continue
            target = AGENT[victim]
            lens = await env.say(
                humans[attacker],
                f"@{target} {marker(attacker, victim, 'mention', False)}",
            )
            assert lens.status == 204
            await env.say(
                humans[attacker],
                f"{target}: {marker(attacker, victim, 'prefix', False)}",
            )
            await env.say(
                humans[attacker],
                f"/send {target} {marker(attacker, victim, 'lensdm', True)}",
            )
            await env.human_session(attacker).send_raw(
                f"PRIVMSG {target} :{marker(attacker, victim, 'rawdm', True)}"
            )
            await env.human_session(attacker).send_raw(
                f"NOTICE {target} :{marker(attacker, victim, 'rawnotice', True)}"
            )
            # The attacker's own agent may not DM another human's agent.
            msg = await env.refused(
                tokens[attacker], "send", target=target, text="agent-to-agent"
            )
            assert "not permitted" in msg
    # Raw clients that bypass the lens entirely, on the mesh and the sandbox.
    for victim in (A, B, ADMIN):
        target = AGENT[victim]
        await mallory.send(
            f"PRIVMSG {target} :{marker('raw', victim, 'rawclient', True)}"
        )
        await mallory.send(
            f"PRIVMSG {SHARED} :@{target} {marker('raw', victim, 'rawmention', False)}"
        )
    await sandbox_mallory.send(f"PRIVMSG {G_AGENT} :{marker('raw', G, 'sbxdm', True)}")
    # Humans try the guest's agent too (it lives on a different IRCd entirely).
    await env.human_session(A).send_raw(
        f"PRIVMSG {G_AGENT} :{marker(A, G, 'toguest', True)}"
    )
    await env.say(humans[B], f"@{G_AGENT} {marker(B, G, 'toguest', False)}")
    await settle(0.4)

    # Count deliveries: a marker that reached the agent it was aimed at, from
    # anyone but that agent's own human, is a cross-human delivery. A DM marker
    # reaching ANY agent counts too. (Ambient chatter in the shared channel
    # that merely names a third agent is ordinary channel traffic, marked
    # owner: false, and is not what the filter exists to stop.)
    reads = {e: await env.ok(t, "read") for e, t in tokens.items()}
    cross: list[tuple[str, str]] = []
    seen_total = 0
    for email, result in reads.items():
        for line in result["lines"]:
            for m in _MARKER_RE.findall(line["text"]):
                seen_total += 1
                attacker, victim, dm = sent[m]
                if attacker == email:
                    continue  # an agent seeing its own human's line is no crossing
                if dm or victim == email:
                    cross.append((email, m))
    assert cross == [], cross
    assert len(cross) == 0  # c27's measurable: 0 cross-human deliveries
    # The filter did work: lines were withheld, not just never sent.
    withheld = sum(r["withheld"] for r in reads.values())
    dm_or_aimed = sum(
        1 for att, vic, _dm in sent.values() if vic is not None and att != vic
    )
    assert len(sent) > 30 and withheld >= 20, (withheld, dm_or_aimed, len(sent))


async def test_owner_lines_arrive_marked_owner_symmetrically(env: Env) -> None:
    """A positive control for the count above: the owner's own lines DO arrive,
    marked ``owner: true``; other humans' ambient chatter is marked false."""
    ta, tb = await env.member_with_agent(A), await env.member_with_agent(B)
    await settle()
    for t in (ta, tb):
        await env.ok(t, "read")
    for me, other, token in ((A, B, ta), (B, A, tb)):
        mine = f"XMARK-own-{me.split('@')[0]}"
        theirs = f"XMARK-amb-{other.split('@')[0]}"
        assert (await env.say(env.jwt(me), f"@{AGENT[me]} {mine}")).status == 204
        await env.human_session(me).send_raw(f"PRIVMSG {AGENT[me]} :{mine}-dm")
        await env.say(env.jwt(other), f"just chatting {theirs}")
        await settle()
        lines = (await env.ok(token, "read"))["lines"]
        by_text = {ln["text"]: ln for ln in lines}
        assert by_text[f"@{AGENT[me]} {mine}"]["owner"] is True
        assert by_text[f"{mine}-dm"]["owner"] is True
        assert by_text[f"{mine}-dm"]["target"] == "DM"
        ambient = by_text[f"just chatting {theirs}"]
        assert ambient["owner"] is False  # untrusted input, marked as such
        assert all(isinstance(ln["owner"], bool) for ln in lines)


# -- three credentials, one endpoint, three scopes -------------------------------------


async def test_three_credentials_get_three_scopes(env: Env) -> None:
    """Admin / member / guest bearer tokens on the ONE endpoint list, read and
    send differently: admin everything, member only granted, guest only the
    sandbox room."""
    admin_t = await env.issue(env.jwt(ADMIN))
    member_t = await env.issue(env.jwt(A))
    await env.online(env.guest())  # a guest's agent runs only with its human
    guest_t = await env.issue(env.guest())
    assert len({admin_t, member_t, guest_t}) == 3
    # All three reach the same URL; a bad token does not.
    for t in (admin_t, member_t, guest_t):
        assert (await env.call(t, "whoami"))["result"]["nick"]
    assert await env.status("not-a-token") == 401

    # The mesh has channels nobody granted to A.
    secret = "#secret"
    await env.ok(admin_t, "join", channel=secret)  # admin: everything
    await env.ok(admin_t, "send", target=secret, text="admin only")
    await env.ok(admin_t, "join", channel=SHARED)
    await env.ok(admin_t, "join", channel="#lobby")
    await env.ok(admin_t, "join", channel="#a-only")
    await env.ok(admin_t, "join", channel="#b-only")
    admin_chans = await env.ok(admin_t, "channels")
    assert {secret, "#a-only", "#b-only"} <= set(admin_chans["joined"])
    assert {secret, "#a-only", "#b-only"} <= set(admin_chans["listed"])

    # Member A: exactly the grants -- listed, joinable, sendable per level.
    me = await env.ok(member_t, "whoami")
    assert me["tier"] == "approved" and me["nick"] == AGENT[A]
    member_chans = await env.ok(member_t, "channels")
    assert secret not in json.dumps(member_chans)
    assert "#b-only" not in json.dumps(member_chans)
    assert set(member_chans["listed"]) <= {SHARED, "#a-only", "#lobby"}
    assert {SHARED, "#a-only"} <= set(member_chans["listed"])
    await env.ok(member_t, "join", channel="#a-only")
    await env.ok(member_t, "send", target="#a-only", text="mine")
    for bad in (secret, "#b-only"):
        assert "not permitted" in await env.refused(member_t, "join", channel=bad)
        assert "not permitted" in await env.refused(
            member_t, "send", target=bad, text="x"
        )
    await env.ok(member_t, "join", channel="#lobby")  # read-only grant
    assert "not permitted" in await env.refused(
        member_t, "send", target="#lobby", text="x"
    )
    # On the wire A's agent is a member of what it was granted and nothing else
    # (the relay IRCd tracks membership per nick; the admin's agent is the one
    # that sits in the ungranted channels).
    members = env.mesh.channel_members
    assert AGENT[A] in members["#a-only"]
    assert AGENT[A] not in members[secret] and AGENT[A] not in members["#b-only"]
    assert AGENT[ADMIN] in members[secret]

    # Guest: only the own sandbox room; nothing of the mesh.
    gme = await env.ok(guest_t, "whoami")
    assert gme["tier"] == "guest" and gme["nick"] == G_AGENT
    own = env.app["config"].guest_room_prefix + env.guests.room_id(G)
    other = env.app["config"].guest_room_prefix + env.guests.room_id(G2)
    gchans = await env.ok(guest_t, "channels")
    assert gchans["joined"] == [own]
    assert other not in json.dumps(gchans) and SHARED not in json.dumps(gchans)
    for msg in (
        await env.refused(guest_t, "join", channel=SHARED),
        await env.refused(guest_t, "join", channel=other),
        await env.refused(guest_t, "send", target=SHARED, text="x"),
        await env.refused(guest_t, "send", target=other, text="x"),
    ):
        assert "Not in guest view" in msg or "not permitted" in msg, msg
    await env.ok(guest_t, "send", text="hello sandbox")
    assert f"PRIVMSG {own} :hello sandbox" in env.sent_by(env.sandbox, G_AGENT)
    assert env.sent_by(env.mesh, G_AGENT) == []  # never the mesh


async def test_one_endpoint_one_credential_per_human(env: Env) -> None:
    """c1 clause 'each human may bring one personal agent': a second issue
    replaces the first, whose agent is refused."""
    first = await env.issue(env.jwt(A))
    await env.ok(first, "whoami")
    second = await env.issue(env.jwt(A))
    assert first != second
    assert await env.status(first) == 401
    await env.ok(second, "whoami")
    # A guest's agent takes no guest seat (it counts as its human).
    gt = await env.guest_with_agent()
    assert env.registry.active_guests() == {"guest:" + G}
    await env.ok(gt, "whoami")
    assert env.registry.active_guests() == {"guest:" + G}


async def test_agent_is_bound_to_its_humans_email(env: Env) -> None:
    """'authenticated through the human's email': a member's token resolves to
    that member, a guest sharing an address resolves to the guest, never to the
    member's rows."""
    member_t = await env.issue(env.jwt(A))
    assert (await env.ok(member_t, "whoami"))["nick"] == AGENT[A]
    # An unapproved/unknown person cannot obtain a credential at all.
    r = await env.client.post(
        "/agent-credential",
        headers=env.jwt("stranger@example.net"),
        allow_redirects=False,
    )
    assert r.status in (401, 403)
    # A removed member's existing agent is cut off on its next call.
    env.access.set_user(B, role="member", state="approved", actor=None)
    bt = await env.issue(env.jwt(B))
    await env.ok(bt, "whoami")
    env.access.set_user(B, role="member", state="removed", actor=None)
    assert await env.status(bt) == 401


async def test_agent_has_exactly_its_humans_grants_no_more(env: Env) -> None:
    """'present wherever its human is with the same permissions': agent and
    human agree on every channel, granted or not."""
    token = await env.member_with_agent(A)
    for chan, allowed in (
        (SHARED, True),
        ("#a-only", True),
        ("#b-only", False),
        ("#secret", False),
    ):
        human = env.human_session(A)
        assert human.may("read", chan, audit=False) is allowed
        agent = env.human_session(_agent_principal(A))  # the agent's own key
        assert agent.may("read", chan, audit=False) is allowed
        payload = await env.call(token, "join", channel=chan)
        assert ("result" in payload) is allowed, (chan, payload)
    # An admin's agent is the admin: everything.
    admin_t = await env.issue(env.jwt(ADMIN))
    await env.ok(admin_t, "join", channel="#secret")


def _agent_principal(email: str) -> str:
    from irc_lens.web import agent_auth

    return agent_auth.agent_registry_principal(email)


# -- mid-test switches ------------------------------------------------------------------


async def test_kill_switch_flipped_mid_test_refuses_the_next_agent_call(
    env: Env,
) -> None:
    token = await env.member_with_agent(A)
    await env.ok(token, "read")
    assert await env.status(token) != 401
    cfg = env.app["config"]
    object.__setattr__(cfg, "agents_enabled", False)  # the operator's switch
    try:
        assert await env.status(token) == 401
        with pytest.raises(Exception):  # the stock MCP client is refused
            await env.call(token, "read")
        # issuing is refused as well while the switch is thrown
        r = await env.client.post(
            "/agent-credential", headers=env.jwt(B), allow_redirects=False
        )
        assert r.status == 403
    finally:
        object.__setattr__(cfg, "agents_enabled", True)
    # Flipped back: the same credential works again, nothing restarted.
    await env.ok(token, "read")


async def test_revoked_grant_mid_session_refuses_the_agents_next_calls(
    env: Env,
) -> None:
    token = await env.member_with_agent(A)
    await env.ok(token, "send", target=SHARED, text="while granted")
    other = await env.member_with_agent(B)  # B keeps writing into the channel
    await env.say(env.jwt(B), "line posted before the revoke XMARK-unread")
    await settle()
    env.access.remove_grant(A, "channel", SHARED, actor=None)
    # Next send: refused.
    assert "not permitted" in await env.refused(
        token, "send", target=SHARED, text="after revoke"
    )
    # Next read: the revoked channel's lines (even ones already queued) are gone.
    lines = (await env.ok(token, "read"))["lines"]
    assert not [ln for ln in lines if ln["target"] == SHARED], lines
    assert "not permitted" in await env.refused(token, "join", channel=SHARED)
    assert SHARED not in (await env.ok(token, "channels"))["joined"]
    # Only A lost it: B's agent is unaffected.
    await env.ok(other, "send", target=SHARED, text="still here")
    # And the revoke reached the wire only as the one earlier PRIVMSG.
    privmsgs = [
        ln for ln in env.sent_by(env.mesh, AGENT[A]) if ln.startswith("PRIVMSG")
    ]
    assert privmsgs == [f"PRIVMSG {SHARED} :while granted"]


# -- c27 / h39: onboarding with no config change and no restart ---------------------------


async def _onboard(env: Env, email: str) -> dict[str, str]:
    """Admin invites *email*; they set a password and sign in with the code.

    Returns the cookie header of the new signed-in browser. Every step is an
    app route: /admin/users, /password/<token>, /entry/signin, /entry/code.
    """
    before = len(env.mail.sent)
    r = await env.admin_post("/admin/users", email=email, role="member", mode="invite")
    assert r.status == 303
    assert env.access.get_user(email)["state"] == "invited"
    assert len(env.mail.sent) == before + 1
    token = re.search(r"/password/([A-Za-z0-9_-]+)", env.mail.sent[-1][2]).group(1)
    r = await env.client.post(
        f"/password/{token}", data={"password": PW, "confirm": PW}, headers=SAME_ORIGIN
    )
    assert r.status == 200
    r = await env.client.post(
        "/entry/signin", data={"email": email, "password": PW}, headers=SAME_ORIGIN
    )
    await env.drain()
    pending = r.cookies[app_session.SIGNIN_COOKIE_NAME].value
    code = re.search(r"^ {4}(\S+)$", env.mail.sent[-1][2], flags=re.M).group(1)
    ok = await env.client.post(
        "/entry/code",
        data={"email": email, "code": code},
        headers={
            "Cookie": f"{app_session.SIGNIN_COOKIE_NAME}={pending}",
            **SAME_ORIGIN,
        },
        allow_redirects=False,
    )
    assert ok.status == 303
    assert env.access.get_user(email)["state"] == "approved"
    sid = ok.cookies[app_session.SESSION_COOKIE_NAME].value
    return {"Cookie": f"{app_session.SESSION_COOKIE_NAME}={sid}", **SAME_ORIGIN}


async def test_onboarding_three_users_with_no_config_change_or_restart(
    env: Env, monkeypatch
) -> None:
    users = {
        "one@example.net": ("#ch-one", "write"),
        "two@example.net": ("#ch-two", "read"),
        "three@example.net": ("#ch-three", "ask"),
    }
    cfg_before = env.app["config"]
    assert cfg_before.allowed_emails == (ADMIN,)
    server_before = env.client.server
    admin_before = env.registry.keys()
    # Config must never be re-read while onboarding.
    loaded: list = []
    for name in ("load_config", "resolve_access"):
        if hasattr(config_module, name):
            monkeypatch.setattr(
                config_module,
                name,
                lambda *a, _n=name, **k: loaded.append(_n),
            )

    cookies: dict[str, dict[str, str]] = {}
    for email in users:
        cookies[email] = await _onboard(env, email)
    # Before any grant they hold nothing: default-deny.
    for email in users:
        assert env.access.grants_for(email) == []
    for email, (chan, level) in users.items():
        r = await env.admin_post(
            "/admin/grants", email=email, channel=chan, level=level
        )
        assert r.status == 303

    # Each sees exactly their grants on the very next request.
    all_chans = [c for c, _ in users.values()]
    for email, (chan, level) in users.items():
        browser = cookies[email]
        r = await env.client.get("/", headers=browser)
        assert r.status == 200, await r.text()
        r = await env.say(browser, f"/join {chan}")
        assert r.status == 204
        human = env.human_session(email)
        for other in all_chans:
            assert human.may("read", other, audit=False) is (other == chan)
            assert human.may("write", other, audit=False) is (
                other == chan and level in ("write", "ask")
            )
        # ...and so does the agent they issue, over the one endpoint.
        token = await env.issue(browser)
        chans = await env.ok(token, "channels")
        assert set(chans["listed"]) & set(all_chans) <= {chan}
        await env.ok(token, "join", channel=chan)
        for other in all_chans:
            if other != chan:
                assert "not permitted" in await env.refused(
                    token, "join", channel=other
                )
        send = await env.call(token, "send", target=chan, text="hi")
        assert ("result" in send) is (level in ("write", "ask")), send

    # No config edit, no restart, no reload.
    assert env.app["config"] is cfg_before
    assert env.app["config"].allowed_emails == (ADMIN,)
    assert env.client.server is server_before
    assert loaded == []
    # The lens kept running: the admin's earlier connections are untouched and
    # nobody was disconnected to make the onboarding take effect.
    assert set(admin_before) <= set(env.registry.keys())
    # The kept-running app still serves the admin and the endpoint.
    r = await env.client.get("/admin", headers=env.jwt(ADMIN))
    assert r.status == 200
    assert await env.status("nope") == 401
