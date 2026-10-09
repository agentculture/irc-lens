"""Tier resolution: approved vs anonymous on one host (guest mode t4).

Covers the irc-lens tier seam (obligation o1) and the guest_mode switch's
"off means exactly today" contract (obligation o10):

* ``approved`` comes ONLY from a verified Cloudflare Access JWT (header
  or ``CF_Authorization`` cookie) whose principal is on the allowlist.
* With ``guest_mode.enabled`` every other request resolves to the
  ``anonymous`` tier instead of a 401/403 — and no client-supplied
  parameter, header, or non-Access cookie can yield ``approved``.
* Real-mesh routes (anything not explicitly marked
  ``allows_anonymous``) still answer anonymous requests with 401, so an
  anonymous request can never open a real-mesh Session.
* With guest mode off, missing JWT is still 401 and a non-allowlisted
  email still 403, exactly as before.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import AsyncIterator

import jwt
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _jwks_server import FakeJWKS
from irc_lens.config import LensConfig
from irc_lens.web import make_app
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.identity import (
    ANONYMOUS_IDENTITY,
    TIER_ANONYMOUS,
    TIER_APPROVED,
    TIER_GUEST,
    TIERS,
    Identity,
)

_TIER_CANARY = "/test/tier-canary"
_APPROVED = "alice@example.com"


@allows_anonymous
async def _tier_canary(request: web.Request) -> web.Response:
    """An anonymous-capable route that echoes the resolved identity."""
    ident: Identity = request["identity"]
    return web.json_response(
        {"tier": ident.tier, "principal": ident.principal, "nick": ident.nick}
    )


def _config(jwks: FakeJWKS, *, guest: bool) -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=(_APPROVED,),
        allowed_service_tokens=("ci-bot",),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=True,
        media_dir="/tmp/irc-lens-test-media",
        media_max_file_bytes=10485760,
        media_max_store_bytes=268435456,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
        guest_enabled=guest,
    )


def _boom_factory(_nick: str):
    raise AssertionError("a real-mesh session must not open in tier tests")


async def _client(config: LensConfig) -> TestClient:
    app = make_app(config, _boom_factory)
    app.router.add_get(_TIER_CANARY, _tier_canary)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest_asyncio.fixture
async def guest_client(jwks: FakeJWKS) -> AsyncIterator[TestClient]:
    client = await _client(_config(jwks, guest=True))
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def strict_client(jwks: FakeJWKS) -> AsyncIterator[TestClient]:
    client = await _client(_config(jwks, guest=False))
    try:
        yield client
    finally:
        await client.close()


async def _tier(resp) -> str:
    assert resp.status == 200, await resp.text()
    return (await resp.json())["tier"]


# ---------------------------------------------------------------------------
# Identity model
# ---------------------------------------------------------------------------


def test_tier_constants() -> None:
    assert (TIER_APPROVED, TIER_GUEST, TIER_ANONYMOUS) == (
        "approved",
        "guest",
        "anonymous",
    )
    assert TIERS == frozenset({"approved", "guest", "anonymous"})


def test_identity_tier_defaults_fail_closed_to_anonymous() -> None:
    """An Identity built without an explicit tier is never approved."""
    ident = Identity(principal="x@example.com", nick="spark-x", raw_jwt_subject="s")
    assert ident.tier == TIER_ANONYMOUS
    assert not ident.is_approved


def test_anonymous_identity_shape() -> None:
    assert ANONYMOUS_IDENTITY.tier == TIER_ANONYMOUS
    assert ANONYMOUS_IDENTITY.principal == ""
    assert ANONYMOUS_IDENTITY.nick == ""
    assert not ANONYMOUS_IDENTITY.is_approved


# ---------------------------------------------------------------------------
# Criterion 1 — approved only from a verified Access JWT for an approved email
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("guest", [True, False])
async def test_verified_header_jwt_for_approved_email_is_approved(
    jwks: FakeJWKS, guest: bool
) -> None:
    client = await _client(_config(jwks, guest=guest))
    try:
        token = jwks.mint(aud="aud-test", claims={"email": _APPROVED, "sub": "s"})
        resp = await client.get(
            _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": token}
        )
        body = await resp.json()
        assert resp.status == 200
        assert body == {
            "tier": TIER_APPROVED,
            "principal": _APPROVED,
            "nick": "testsrv-alice",
        }
    finally:
        await client.close()


async def test_verified_cookie_jwt_for_approved_email_is_approved(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": _APPROVED, "sub": "s"})
    guest_client.session.cookie_jar.update_cookies({"CF_Authorization": token})
    assert await _tier(await guest_client.get(_TIER_CANARY)) == TIER_APPROVED


async def test_allowlisted_service_token_is_approved(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    token = jwks.mint(aud="aud-test", claims={"common_name": "ci-bot", "sub": "s"})
    resp = await guest_client.get(
        _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": token}
    )
    assert await _tier(resp) == TIER_APPROVED


async def test_guest_mode_no_jwt_resolves_anonymous(guest_client: TestClient) -> None:
    resp = await guest_client.get(_TIER_CANARY)
    body = await resp.json()
    assert resp.status == 200
    assert body == {"tier": TIER_ANONYMOUS, "principal": "", "nick": ""}


# ---------------------------------------------------------------------------
# Criterion 2 — nothing client-supplied yields approved (negative tests)
# ---------------------------------------------------------------------------


async def test_spoofed_authenticated_user_email_header_is_not_approved(
    guest_client: TestClient,
) -> None:
    resp = await guest_client.get(
        _TIER_CANARY,
        headers={
            "Cf-Access-Authenticated-User-Email": _APPROVED,
            "X-Forwarded-Email": _APPROVED,
            "X-Tier": TIER_APPROVED,
        },
    )
    assert await _tier(resp) == TIER_ANONYMOUS


@pytest.mark.parametrize(
    "query",
    [
        {"tier": "approved"},
        {"email": _APPROVED},
        {"identity": _APPROVED, "approved": "1"},
        {"CF_Authorization": "x", "Cf-Access-Jwt-Assertion": "x"},
    ],
)
async def test_query_params_are_not_approved(
    guest_client: TestClient, query: dict[str, str]
) -> None:
    resp = await guest_client.get(_TIER_CANARY, params=query)
    assert await _tier(resp) == TIER_ANONYMOUS


async def test_non_access_cookies_are_not_approved(guest_client: TestClient) -> None:
    guest_client.session.cookie_jar.update_cookies(
        {"tier": "approved", "identity": _APPROVED, "email": _APPROVED}
    )
    assert await _tier(await guest_client.get(_TIER_CANARY)) == TIER_ANONYMOUS


async def test_valid_jwt_under_non_access_cookie_name_is_not_approved(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    """Only the CF_Authorization cookie carries an Access JWT."""
    token = jwks.mint(aud="aud-test", claims={"email": _APPROVED, "sub": "s"})
    guest_client.session.cookie_jar.update_cookies({"lens_session": token})
    assert await _tier(await guest_client.get(_TIER_CANARY)) == TIER_ANONYMOUS


async def test_unsigned_alg_none_cookie_is_not_approved(
    guest_client: TestClient,
) -> None:
    forged = jwt.encode(
        {"email": _APPROVED, "aud": "aud-test", "sub": "s"},
        key=None,
        algorithm="none",
        headers={"kid": "test-kid-1"},
    )
    guest_client.session.cookie_jar.update_cookies({"CF_Authorization": forged})
    assert await _tier(await guest_client.get(_TIER_CANARY)) == TIER_ANONYMOUS


async def test_jwt_signed_by_foreign_key_with_known_kid_is_not_approved(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    impostor = FakeJWKS(kid="test-kid-1")  # same kid, different keypair
    impostor.host, impostor.port = jwks.host, jwks.port  # same issuer
    forged = impostor.mint(aud="aud-test", claims={"email": _APPROVED, "sub": "s"})
    guest_client.session.cookie_jar.update_cookies({"CF_Authorization": forged})
    assert await _tier(await guest_client.get(_TIER_CANARY)) == TIER_ANONYMOUS


async def test_garbage_cookie_is_not_approved(guest_client: TestClient) -> None:
    guest_client.session.cookie_jar.update_cookies({"CF_Authorization": "not.a.jwt"})
    assert await _tier(await guest_client.get(_TIER_CANARY)) == TIER_ANONYMOUS


async def test_valid_jwt_for_non_approved_email_is_anonymous(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    token = jwks.mint(
        aud="aud-test", claims={"email": "mallory@example.com", "sub": "s"}
    )
    resp = await guest_client.get(
        _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": token}
    )
    body = await resp.json()
    assert resp.status == 200
    # Fully anonymous: the unapproved principal is not carried forward.
    assert body == {"tier": TIER_ANONYMOUS, "principal": "", "nick": ""}


async def test_non_allowlisted_service_token_is_anonymous(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    token = jwks.mint(aud="aud-test", claims={"common_name": "rogue", "sub": "s"})
    resp = await guest_client.get(
        _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": token}
    )
    assert await _tier(resp) == TIER_ANONYMOUS


async def test_service_token_cn_matching_approved_email_is_not_approved(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    """The email/service-token lists never cross-authorize."""
    token = jwks.mint(aud="aud-test", claims={"common_name": _APPROVED, "sub": "s"})
    resp = await guest_client.get(
        _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": token}
    )
    assert await _tier(resp) == TIER_ANONYMOUS


@pytest.mark.parametrize(
    "aud,extra",
    [
        ("aud-other", {}),  # wrong audience
        ("aud-test", {"iss": "http://evil.example"}),  # wrong issuer
        ("aud-test", {"exp": int(time.time()) - 60}),  # expired
    ],
)
async def test_unverifiable_jwt_for_approved_email_is_anonymous(
    guest_client: TestClient, jwks: FakeJWKS, aud: str, extra: dict
) -> None:
    token = jwks.mint(aud=aud, claims={"email": _APPROVED, "sub": "s", **extra})
    resp = await guest_client.get(
        _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": token}
    )
    assert await _tier(resp) == TIER_ANONYMOUS


async def test_jwt_with_no_principal_is_anonymous(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    token = jwks.mint(aud="aud-test", claims={"sub": "s"})
    resp = await guest_client.get(
        _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": token}
    )
    assert await _tier(resp) == TIER_ANONYMOUS


async def test_header_jwt_still_preferred_over_cookie(
    guest_client: TestClient, jwks: FakeJWKS
) -> None:
    """A bad header token is not rescued by a good cookie (unchanged rule),
    so the request resolves anonymous rather than approved."""
    good = jwks.mint(aud="aud-test", claims={"email": _APPROVED, "sub": "s"})
    guest_client.session.cookie_jar.update_cookies({"CF_Authorization": good})
    resp = await guest_client.get(
        _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": "garbage"}
    )
    assert await _tier(resp) == TIER_ANONYMOUS


# ---------------------------------------------------------------------------
# o1 — anonymous never reaches a real-mesh route (deny by default)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/"),
        ("GET", "/events"),
        ("POST", "/input"),
        ("POST", "/upload"),
        ("GET", "/residents"),
        ("GET", "/agent"),
        ("GET", "/agent/llms.txt"),
        ("GET", "/no-such-route"),
    ],
)
async def test_guest_mode_anonymous_denied_on_real_mesh_routes(
    guest_client: TestClient, jwks: FakeJWKS, method: str, path: str
) -> None:
    """The boom factory proves no Session opens; every non-anonymous route
    answers 401 with the {error, hint} shape — no JWT and a valid JWT for a
    non-approved email get the same response (no membership signal)."""
    unapproved = jwks.mint(aud="aud-test", claims={"email": "eve@example.com"})
    bodies = []
    for headers in ({}, {"Cf-Access-Jwt-Assertion": unapproved}):
        resp = await guest_client.request(method, path, headers=headers)
        assert resp.status == 401
        bodies.append(await resp.json())
    assert bodies[0] == bodies[1]
    assert set(bodies[0]) == {"error", "hint"}


async def test_guest_mode_exempt_paths_unchanged(guest_client: TestClient) -> None:
    resp = await guest_client.get("/healthz")
    assert resp.status == 200
    assert await resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Criterion 3 / o10 — guest mode off behaves exactly as today
# ---------------------------------------------------------------------------


async def test_guest_mode_off_missing_jwt_401(strict_client: TestClient) -> None:
    for path in ("/", _TIER_CANARY):
        resp = await strict_client.get(path)
        assert resp.status == 401
        body = await resp.json()
        assert body["error"] == "missing Cloudflare Access identity"


async def test_guest_mode_off_non_allowlisted_email_403(
    strict_client: TestClient, jwks: FakeJWKS
) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": "mallory@example.com"})
    for path in ("/", _TIER_CANARY):
        resp = await strict_client.get(path, headers={"Cf-Access-Jwt-Assertion": token})
        assert resp.status == 403
        assert "allowlist" in (await resp.json())["error"].lower()


async def test_guest_mode_off_bad_jwt_401(
    strict_client: TestClient, jwks: FakeJWKS
) -> None:
    token = jwks.mint(aud="aud-other", claims={"email": _APPROVED})
    resp = await strict_client.get(
        _TIER_CANARY, headers={"Cf-Access-Jwt-Assertion": token}
    )
    assert resp.status == 401


async def test_guest_mode_off_spoofed_header_still_401(
    strict_client: TestClient,
) -> None:
    resp = await strict_client.get(
        _TIER_CANARY, headers={"Cf-Access-Authenticated-User-Email": _APPROVED}
    )
    assert resp.status == 401


def test_guest_enabled_defaults_off() -> None:
    fields = {f.name: f.default for f in dataclasses.fields(LensConfig)}
    assert fields["guest_enabled"] is False


# ---------------------------------------------------------------------------
# Dev mode — the single trusted local identity is approved
# ---------------------------------------------------------------------------


async def test_dev_mode_identity_is_approved(jwks: FakeJWKS) -> None:
    config = dataclasses.replace(
        _config(jwks, guest=False),
        auth_mode="dev",
        dev_nick="lens",
        dev_email="dev@local",
    )
    client = await _client(config)
    try:
        assert await _tier(await client.get(_TIER_CANARY)) == TIER_APPROVED
    finally:
        await client.close()
