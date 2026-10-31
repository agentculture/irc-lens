"""t10: admin-only routes and backend selection (email-keyed permissions).

Sandbox enter/leave, owner metrics and the residents page are admin-only when
``access_enabled``; ``_backend_for`` sends guests to the sandbox and approved
non-admins to the mesh, never to the all-rooms guest view.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from irc_lens.web import routes
from irc_lens.web.identity import ROLE_ADMIN, TIER_APPROVED, TIER_GUEST, Identity
from test_access_approval import ALICE, BOB, Env, _build

# Every approved user has chosen a nickname (q14); see conftest.
pytestmark = pytest.mark.usefixtures("chosen_nicknames")

ADMIN_ONLY = [
    ("POST", "/sandbox/enter"),
    ("POST", "/sandbox/leave"),
    ("GET", "/owner/metrics"),
    ("GET", "/residents"),
]


@pytest_asyncio.fixture
async def env(jwks, tmp_path, monkeypatch):
    e = await _build(jwks, tmp_path, monkeypatch)
    e.access.set_user(BOB, role="member", state="approved", actor=None)
    try:
        yield e
    finally:
        await e.client.close()
        await e.mesh.stop()


async def _call(env: Env, method: str, path: str, headers):
    return await env.client.request(method, path, headers=headers)


@pytest.mark.parametrize("method,path", ADMIN_ONLY)
async def test_non_admin_approved_gets_403(env: Env, method, path) -> None:
    r = await _call(env, method, path, env.jwt(BOB))
    assert r.status == 403


@pytest.mark.parametrize("method,path", ADMIN_ONLY)
async def test_admin_is_allowed(env: Env, method, path) -> None:
    r = await _call(env, method, path, env.jwt(ALICE))
    assert r.status == 200


async def test_denial_names_a_hint(env: Env) -> None:
    r = await _call(env, "GET", "/owner/metrics", env.jwt(BOB))
    body = await r.json()
    assert body["error"]
    assert body["hint"]


async def test_admin_toggle_selects_sandbox(env: Env) -> None:
    await _call(env, "POST", "/sandbox/enter", env.jwt(ALICE))
    assert ALICE in env.app["sandbox_toggle"]


def test_require_admin_helper_refuses_non_admin_and_agents() -> None:
    admin = Identity("a", "n", "s", tier=TIER_APPROVED, role=ROLE_ADMIN)
    member = Identity("b", "n", "s", tier=TIER_APPROVED, role="member")
    guest = Identity("g", "n", "s", tier=TIER_GUEST)
    assert routes.admin_only_denial(admin, access_enabled=True) is None
    assert routes.admin_only_denial(member, access_enabled=True).status == 403
    assert routes.admin_only_denial(guest, access_enabled=True).status == 401
    # An agent-shaped identity (role "agent") is refused by the same helper.
    agent = Identity("x", "n", "s", tier=TIER_APPROVED, role="agent")
    assert routes.admin_only_denial(agent, access_enabled=True).status == 403


def test_legacy_behaviour_unchanged_when_disabled() -> None:
    member = Identity("b", "n", "s", tier=TIER_APPROVED)
    guest = Identity("g", "n", "s", tier=TIER_GUEST)
    assert routes.admin_only_denial(member, access_enabled=False) is None
    assert routes.admin_only_denial(guest, access_enabled=False).status == 401


class _Req(dict):
    def __init__(self, identity, config, toggled=()):
        super().__init__(identity=identity)
        self.app = {"config": config, "sandbox_toggle": set(toggled)}


def test_backend_for_member_never_sandbox_even_if_stale_toggle(env: Env) -> None:
    cfg = env.app["config"]
    member = Identity("b@example.org", "spark-b", "s", tier=TIER_APPROVED, role="member")
    _, backend = routes._backend_for(_Req(member, cfg, toggled=[member.principal]))
    assert backend == routes.BACKEND_MESH


def test_backend_for_guest_is_sandbox_and_admin_toggle_honoured(env: Env) -> None:
    cfg = env.app["config"]
    guest = Identity("g1", "g1", "s", tier=TIER_GUEST)
    _, backend = routes._backend_for(_Req(guest, cfg))
    assert backend == routes.BACKEND_SANDBOX
    admin = Identity("a@example.com", "spark-a", "s", tier=TIER_APPROVED, role=ROLE_ADMIN)
    _, backend = routes._backend_for(_Req(admin, cfg, toggled=[admin.principal]))
    assert backend == routes.BACKEND_SANDBOX
    _, backend = routes._backend_for(_Req(admin, cfg))
    assert backend == routes.BACKEND_MESH
