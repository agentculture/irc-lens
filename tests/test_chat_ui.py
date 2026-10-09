"""Chat UI uplift (t13): tier-aware palette, header, sandbox marker, guest
command allowlist (deviation d4a) and per-guest message rate limit (d4b).

Reuses the two-server guest-mode environment from ``test_session_routing``.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from irc_lens import metrics
from irc_lens.commands import (
    PALETTE,
    CommandType,
    ParsedCommand,
    allowed_in_sandbox,
    help_for,
    palette_for,
    parse_command,
)
from irc_lens.web.render import render_fragment

# Fixtures + helpers from the routing suite (pytest picks the fixtures up
# by name from this module's namespace).
from test_session_routing import (  # noqa: F401
    Env,
    _APPROVED,
    _GUEST,
    consent,
    entry_seam,
    env,
)

#: Mesh/agent-control commands a guest must never run (derived from
#: ``irc_lens.commands`` — every parsed command not in SANDBOX_ALLOWED).
_REFUSED_FOR_GUESTS = [
    "/kick sbx-ask",
    "/start sbx-ask",
    "/stop sbx-ask",
    "/restart sbx-ask",
    "/invite sbx-x",
    "/server x",
    "/icon x",
    "/topic #general new topic",
    "/join #other",
    "/part #general",
    "/send #general hi",
    "/switch #other",
    "/channels",
    "/agents",
    "/mesh",
    "/overview",
    "/status",
    "/quit",
    "/sandbox",
    "/residents",
    "/nonsense",
]
_ALLOWED_FOR_GUESTS = ["/help", "/me waves", "hello there"]
_SANDBOX_ONLY_HIDDEN = [
    "/join",
    "/channels",
    "/agents",
    "/mesh",
    "/residents",
    "/sandbox",
]


class _Text(HTMLParser):
    """Visible text nodes outside script/style/chat-log."""

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[str] = []
        self._skip = 0
        self._stack: list[bool] = []

    def handle_starttag(self, tag, attrs):
        void = tag in {"meta", "link", "img", "input", "br"}
        skip = (
            tag in {"script", "style", "title"} or dict(attrs).get("id") == "chat-log"
        )
        if not void:
            self._stack.append(skip)
            self._skip += skip

    def handle_endtag(self, tag):
        if self._stack:
            self._skip -= self._stack.pop()

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.nodes.append(" ".join(data.split()))


def _page_text(html: str) -> list[str]:
    p = _Text()
    p.feed(html)
    return p.nodes


# -- criterion 1: tier-aware palette ------------------------------------------


def test_palette_guest_shows_only_sandbox_commands() -> None:
    cmds = [e.command for e in palette_for("guest")]
    assert cmds == ["/help", "/who", "/me", "/read"]
    assert [e.command for e in palette_for("sandbox_preview")] == cmds


def test_palette_approved_gets_mesh_commands_and_sandbox_toggle() -> None:
    with_toggle = [e.command for e in palette_for("approved", sandbox_toggle=True)]
    without = [e.command for e in palette_for("approved", sandbox_toggle=False)]
    for c in (
        "/help",
        "/join",
        "/channels",
        "/agents",
        "/mesh",
        "/residents",
        "/sandbox",
    ):
        assert c in with_toggle
    assert "/sandbox" not in without


def test_palette_descriptions_are_one_to_three_words() -> None:
    for e in PALETTE:
        assert 1 <= len(e.label.split()) <= 3, e
    for e in help_for("approved", sandbox_toggle=True):
        assert 1 <= len(e.label.split()) <= 3, e


def test_every_palette_sandbox_command_is_allowed_server_side() -> None:
    """The palette never offers a guest a command the allowlist refuses."""
    for e in palette_for("guest"):
        assert allowed_in_sandbox(parse_command(f"{e.command} x")), e.command


def test_allowlist_is_exactly_the_sandbox_commands() -> None:
    assert {
        t.name for t in CommandType if allowed_in_sandbox(ParsedCommand(type=t))
    } == {"CHAT", "HELP", "WHO", "ME", "READ"}


async def test_guest_page_palette_lists_only_allowed(env: Env) -> None:
    env.add_guest()
    html = await (await env.client.get("/", headers=env.guest_headers())).text()
    rows = re.findall(r'data-cmd="(/[a-z]+)"', html)
    assert rows == ["/help", "/who", "/me", "/read"]
    for hidden in _SANDBOX_ONLY_HIDDEN:
        assert f'data-cmd="{hidden}"' not in html


async def test_approved_page_palette_has_mesh_commands_and_sandbox(env: Env) -> None:
    html = await (await env.client.get("/", headers=env.approved_headers())).text()
    rows = re.findall(r'data-cmd="(/[a-z]+)"', html)
    assert rows == [
        "/help",
        "/who",
        "/me",
        "/read",
        "/join",
        "/channels",
        "/agents",
        "/mesh",
        "/residents",
        "/sandbox",
    ]


async def test_guest_help_pane_lists_no_mesh_commands(env: Env) -> None:
    env.add_guest()
    await env.client.get("/", headers=env.guest_headers())
    session = env.app["registry"].values()[0]
    session.set_view("help")
    out = render_fragment("_info.html.j2", session=session)
    for cmd in ("/help", "/who", "/me", "/read"):
        assert cmd in out
    for cmd in ("/join", "/channels", "/kick", "/mesh", "/topic", "/status"):
        assert cmd not in out


# -- criterion 2: header, tier badge, sandbox marker, agent offline -----------


async def test_guest_header_nick_badge_room_no_host_port(env: Env) -> None:
    env.add_guest()
    html = await (await env.client.get("/", headers=env.guest_headers())).text()
    assert 'data-testid="tier-badge">Guest<' in html
    assert 'data-testid="identity">sbx-gus<' in html
    assert 'data-testid="header-room"' in html
    assert f"{env.sandbox.host}:{env.sandbox.port}" not in html
    assert "@" + env.sandbox.host not in html
    assert not re.search(r"sbx-gus@\S+:\d+", html)
    assert 'data-tier="guest"' in html
    assert "lens-sandbox" in html  # amber rule
    assert 'data-testid="sandbox-banner"' not in html  # banner is approved-only


async def test_approved_header_real_mesh_and_way_into_sandbox(env: Env) -> None:
    html = await (await env.client.get("/", headers=env.approved_headers())).text()
    assert 'data-testid="tier-badge">Real mesh<' in html
    assert 'data-testid="identity">testsrv-alice<' in html
    assert "lens-sandbox" not in html
    assert 'hx-post="/sandbox/enter"' in html
    assert ">Guest view<" in html
    assert not re.search(r"testsrv-alice@\S+:\d+", html)


async def test_sandbox_preview_has_amber_rule_banner_and_back(env: Env) -> None:
    h = env.approved_headers()
    r = await env.client.post("/sandbox/enter", headers=h)
    assert r.status == 200
    assert r.headers["HX-Refresh"] == "true"
    html = await (await env.client.get("/", headers=h)).text()
    assert 'data-testid="tier-badge">Sandbox preview<' in html
    assert 'data-testid="identity">sbx-op-alice<' in html
    assert "lens-sandbox" in html
    assert 'data-testid="sandbox-banner"' in html
    assert "<strong>Guest view</strong>" in html
    assert 'hx-post="/sandbox/leave"' in html
    assert ">Back to mesh<" in html
    assert 'hx-post="/sandbox/enter"' not in html
    # sandbox preview offers the sandbox palette only
    assert re.findall(r'data-cmd="(/[a-z]+)"', html) == [
        "/help",
        "/who",
        "/me",
        "/read",
    ]
    r = await env.client.post("/sandbox/leave", headers=h)
    assert r.headers["HX-Refresh"] == "true"


def _guest_session(env: Env):
    return next(
        sess
        for (principal, backend), sess in zip(
            env.app["registry"].keys(), env.app["registry"].values()
        )
        if backend == "sandbox" and principal.startswith("guest:")
    )


async def test_agent_state_follows_room_membership(env: Env) -> None:
    """Online iff the agent is in this guest's room (d6), re-read from the
    server on every poll: AgentIRC sends no QUIT for a bot-capability client
    or an abrupt disconnect, so a stopped agent is only seen via WHO."""
    env.add_guest()
    h = env.guest_headers()
    html = await (await env.client.get("/", headers=h)).text()
    assert 'data-testid="agent-state" data-state="offline"' in html
    room = "#g-" + env.store.room_id("gus@example.org")
    env.sandbox.channel_members[room].add("sbx-ask")  # the agent follows in
    frag = await (await env.client.get("/presence", headers=h)).text()
    assert 'data-state="online"' in frag
    assert "agent online" in frag
    assert 'hx-get="/presence"' in frag
    env.sandbox.channel_members[room].discard("sbx-ask")  # stopped, no QUIT
    frag = await (await env.client.get("/presence", headers=h)).text()
    assert 'data-state="offline"' in frag
    assert "agent offline" in frag


async def test_mesh_view_has_no_agent_state(env: Env) -> None:
    mesh_html = await (await env.client.get("/", headers=env.approved_headers())).text()
    assert "agent-state" not in mesh_html
    assert (
        await (await env.client.get("/presence", headers=env.approved_headers())).text()
    ).strip() == ""


async def test_presence_requires_identity(env: Env) -> None:
    assert (await env.client.get("/presence")).status == 401


# -- criterion 3: little-to-no prose ------------------------------------------


@pytest.mark.parametrize("who", ["guest", "approved", "preview"])
async def test_ui_strings_are_at_most_four_words(env: Env, who: str) -> None:
    if who == "guest":
        env.add_guest()
        headers = env.guest_headers()
    else:
        headers = env.approved_headers()
        if who == "preview":
            await env.client.post("/sandbox/enter", headers=headers)
    html = await (await env.client.get("/", headers=headers)).text()
    long = [t for t in _page_text(html) if len(t.split()) > 4]
    assert long == []


def test_meta_viewport_and_phone_css_present() -> None:
    from importlib.resources import files

    css = files("irc_lens").joinpath("static/lens.css").read_text()
    assert "@media (max-width: 760px)" in css
    assert ":focus-visible" in css
    assert "min-height: 44px" in css  # phone touch targets


# -- d4a: server-side guest command allowlist ----------------------------------


@pytest.mark.parametrize("text", _REFUSED_FOR_GUESTS)
async def test_guest_refused_commands_never_reach_irc(env: Env, text: str) -> None:
    env.add_guest()
    h = env.guest_headers()
    await env.client.get("/", headers=h)  # open the sandbox session
    before = len(env.sandbox.received)
    r = await env.client.post("/input", json={"text": text}, headers=h)
    assert r.status == 403
    body = await r.json()
    assert body["error"] == "Not in guest view"
    assert len(env.sandbox.received) == before
    assert env.mesh.received == []


@pytest.mark.parametrize("text", _ALLOWED_FOR_GUESTS)
async def test_guest_allowed_commands_run(env: Env, text: str) -> None:
    env.add_guest()
    r = await env.client.post(
        "/input", json={"text": text}, headers=env.guest_headers()
    )
    assert r.status == 204


async def test_sandbox_preview_also_enforces_allowlist(env: Env) -> None:
    h = env.approved_headers()
    await env.client.post("/sandbox/enter", headers=h)
    r = await env.client.post("/input", json={"text": "/join #x"}, headers=h)
    assert r.status == 403
    # The automatic private-room JOINs are expected; /join #x is not.
    assert not any(l.command == "JOIN" and l.params[:1] == ["#x"] for l in env.sandbox.received)


async def test_guest_view_may_switch_between_guest_rooms(env: Env) -> None:
    """The owner watches every guest room from Guest view (d6), so /switch is
    allowed there; guests have one room and still may not /switch."""
    env.add_guest()
    await env.client.get("/", headers=env.guest_headers())  # opens #g-gus
    h = env.approved_headers()
    await env.client.post("/sandbox/enter", headers=h)
    await env.client.get("/", headers=h)  # opens #g-alice + joins #g-gus
    r = await env.client.post("/input", json={"text": "/switch #g-gus"}, headers=h)
    assert r.status == 204
    r = await env.client.post(
        "/input", json={"text": "/switch #g-alice"}, headers=env.guest_headers()
    )
    assert r.status == 403


async def test_approved_on_mesh_keeps_full_command_set(env: Env) -> None:
    h = env.approved_headers()
    r = await env.client.post("/input", json={"text": "/join #ops"}, headers=h)
    assert r.status == 204
    assert any(l.command == "JOIN" for l in env.mesh.received)


async def test_sandbox_command_enters_guest_view(env: Env) -> None:
    h = env.approved_headers()
    r = await env.client.post("/input", json={"text": "/sandbox"}, headers=h)
    assert r.status == 204
    assert r.headers["HX-Refresh"] == "true"
    assert env.app["sandbox_toggle"] == {_APPROVED}
    html = await (await env.client.get("/", headers=h)).text()
    assert "Sandbox preview" in html


# -- d4b: per-guest message rate limit -----------------------------------------


@pytest.fixture
def limit3(env: Env):
    cfg = env.app["config"]
    old = cfg.guest_rate_messages_per_min
    object.__setattr__(cfg, "guest_rate_messages_per_min", 3)
    yield 3
    object.__setattr__(cfg, "guest_rate_messages_per_min", old)


async def test_guest_message_rate_limit_429(env: Env, limit3) -> None:
    env.add_guest()
    h = {**env.guest_headers(), "CF-Connecting-IP": "203.0.113.5"}
    before = metrics.get_metrics().snapshot()["rate_limited_429"]
    for _ in range(limit3):
        assert (
            await env.client.post("/input", json={"text": "hi"}, headers=h)
        ).status == 204
    r = await env.client.post("/input", json={"text": "hi"}, headers=h)
    assert r.status == 429
    body = await r.json()
    assert body == {"error": "Slow down", "hint": "Too many messages"}
    assert r.headers["Retry-After"] == "60"
    assert metrics.get_metrics().snapshot()["rate_limited_429"] == before + 1


async def test_refused_commands_count_toward_the_limit(env: Env, limit3) -> None:
    env.add_guest()
    h = {**env.guest_headers(), "CF-Connecting-IP": "203.0.113.6"}
    for _ in range(limit3):
        assert (
            await env.client.post("/input", json={"text": "/kick x"}, headers=h)
        ).status == 403
    assert (
        await env.client.post("/input", json={"text": "hi"}, headers=h)
    ).status == 429


async def test_rate_limit_is_per_guest_and_per_ip(env: Env, limit3) -> None:
    env.add_guest()
    env.add_guest("other@example.org", "sbx-other")
    a = {**env.guest_headers(), "CF-Connecting-IP": "203.0.113.7"}
    for _ in range(limit3):
        await env.client.post("/input", json={"text": "hi"}, headers=a)
    assert (
        await env.client.post("/input", json={"text": "hi"}, headers=a)
    ).status == 429
    # different guest + different IP: unaffected
    b = {**env.guest_headers("other@example.org"), "CF-Connecting-IP": "203.0.113.8"}
    assert (
        await env.client.post("/input", json={"text": "hi"}, headers=b)
    ).status == 204
    # different guest, SAME ip: the IP bucket is exhausted
    c = {**env.guest_headers("other@example.org"), "CF-Connecting-IP": "203.0.113.7"}
    assert (
        await env.client.post("/input", json={"text": "hi"}, headers=c)
    ).status == 429
    # same guest, new IP: the per-email bucket still limits
    d = {**env.guest_headers(), "CF-Connecting-IP": "203.0.113.9"}
    assert (
        await env.client.post("/input", json={"text": "hi"}, headers=d)
    ).status == 429


async def test_rate_limit_does_not_apply_to_approved(env: Env, limit3) -> None:
    h = env.approved_headers()
    for _ in range(limit3 + 3):
        assert (
            await env.client.post("/input", json={"text": "hi"}, headers=h)
        ).status == 204


async def test_rate_limited_message_never_reaches_irc(env: Env, limit3) -> None:
    env.add_guest()
    h = {**env.guest_headers(), "CF-Connecting-IP": "203.0.113.10"}
    for _ in range(limit3):
        await env.client.post("/input", json={"text": "hi"}, headers=h)
    before = len(env.sandbox.received)
    await env.client.post("/input", json={"text": "hi"}, headers=h)
    assert len(env.sandbox.received) == before


async def test_mesh_empty_state_has_a_next_step(env: Env) -> None:
    """h58: an approved user on the real mesh with no room gets a hint."""
    html = await (await env.client.get("/", headers=env.approved_headers())).text()
    assert 'data-testid="empty-hint"' in html
    assert "/join" in html


async def test_owner_metrics_agent_state_uses_room_membership(env: Env) -> None:
    env.add_guest()
    await env.client.get("/", headers=env.guest_headers())
    room = "#g-" + env.store.room_id("gus@example.org")
    body = await (await env.client.get("/owner/metrics", headers=env.approved_headers())).json()
    assert body["agent"]["state"] == "offline"
    env.sandbox.channel_members[room].add("sbx-ask")
    await env.client.get("/presence", headers=env.guest_headers())  # a poll re-reads WHO
    body = await (await env.client.get("/owner/metrics", headers=env.approved_headers())).json()
    assert body["agent"]["state"] == "online"
