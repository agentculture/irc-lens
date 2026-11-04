"""Worker-to-origin trust contract (t6, deviations d1 and d2)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from irc_lens.config import LensConfig, load_config
from irc_lens.web import auth, entry, routes

from helpers import DEV_CONFIG

EDGE_TOKEN_HEADER = "X-Lens-Edge-Token"
EDGE_CLIENT_IP_HEADER = "X-Lens-Edge-Client-IP"
EDGE_SECRET_ENV = "IRC_LENS_TEST_EDGE_TOKEN"
PUBLIC_ORIGIN = "https://chat.example.test"


def _cf_config(**changes: object) -> LensConfig:
    config = replace(
        DEV_CONFIG,
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="test-aud",
        cf_team_domain="team.cloudflareaccess.com",
        allowed_emails=("alice@example.com",),
    )
    return replace(config, **changes)


def _request(
    config: LensConfig,
    *,
    method: str = "GET",
    path: str = "/healthz",
    headers: dict[str, str] | None = None,
) -> web.Request:
    app = web.Application()
    app["config"] = config
    return make_mocked_request(
        method,
        path,
        headers=headers,
        app=app,
        client_max_size=1024,
    )


def test_worker_trust_defaults_off() -> None:
    assert DEV_CONFIG.public_origin is None
    assert DEV_CONFIG.require_edge_credential is False
    assert DEV_CONFIG.edge_credential_env is None


def test_worker_trust_config_loads_env_name_not_secret(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
auth:
  mode: cloudflare-access
  cloudflare:
    aud: test-aud
    team_domain: team.cloudflareaccess.com
  allowed_emails: [alice@example.com]
  public_origin: {PUBLIC_ORIGIN}
  require_edge_credential: true
  edge_credential_env: {EDGE_SECRET_ENV}
server:
  name: spark
"""
    )

    config = load_config(path)

    assert config.public_origin == PUBLIC_ORIGIN
    assert config.require_edge_credential is True
    assert config.edge_credential_env == EDGE_SECRET_ENV


def test_configured_public_origin_passes_for_internal_worker_host() -> None:
    config = _cf_config(public_origin=PUBLIC_ORIGIN)
    request = _request(
        config,
        method="POST",
        path="/input",
        headers={
            "Host": "spark-origin.example.test",
            "Origin": PUBLIC_ORIGIN,
        },
    )

    assert routes._origin_ok(request) is True


def test_foreign_origin_is_still_refused_with_public_origin_configured() -> None:
    config = _cf_config(public_origin=PUBLIC_ORIGIN)
    request = _request(
        config,
        method="POST",
        path="/input",
        headers={
            "Host": "spark-origin.example.test",
            "Origin": "https://evil.example.test",
        },
    )

    assert routes._origin_ok(request) is False


async def _ok_handler(_request: web.Request) -> web.Response:
    return web.Response(status=204)


async def test_worker_post_with_public_origin_reaches_handler(monkeypatch) -> None:
    token = "expected-token"
    monkeypatch.setenv(EDGE_SECRET_ENV, token)
    monkeypatch.setattr(auth, "_adopt_app_session", lambda _request: True)
    config = _cf_config(
        public_origin=PUBLIC_ORIGIN,
        require_edge_credential=True,
        edge_credential_env=EDGE_SECRET_ENV,
    )
    middleware = auth.build_cloudflare_middleware(config)
    request = _request(
        config,
        method="POST",
        path="/input",
        headers={
            "Host": "spark-origin.example.test",
            "Origin": PUBLIC_ORIGIN,
            EDGE_TOKEN_HEADER: token,
        },
    )

    async def origin_checked_handler(incoming: web.Request) -> web.Response:
        return web.Response(status=204 if routes._origin_ok(incoming) else 403)

    response = await middleware(request, origin_checked_handler)

    assert response.status == 204


async def test_edge_credential_required_refuses_missing_header(
    monkeypatch,
) -> None:
    monkeypatch.setenv(EDGE_SECRET_ENV, "expected-token")
    config = _cf_config(
        require_edge_credential=True,
        edge_credential_env=EDGE_SECRET_ENV,
    )
    middleware = auth.build_cloudflare_middleware(config)

    response = await middleware(_request(config), _ok_handler)

    assert response.status == 403


async def test_edge_credential_off_preserves_public_path_behavior() -> None:
    config = _cf_config(require_edge_credential=False)
    middleware = auth.build_cloudflare_middleware(config)

    response = await middleware(_request(config), _ok_handler)

    assert response.status == 204


async def test_valid_edge_credential_passes_and_uses_constant_time_compare(
    monkeypatch,
) -> None:
    token = "expected-token"
    compared: list[tuple[bytes, bytes]] = []

    def compare_digest(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return left == right

    monkeypatch.setenv(EDGE_SECRET_ENV, token)
    monkeypatch.setattr(auth.hmac, "compare_digest", compare_digest)
    config = _cf_config(
        require_edge_credential=True,
        edge_credential_env=EDGE_SECRET_ENV,
    )
    middleware = auth.build_cloudflare_middleware(config)
    request = _request(config, headers={EDGE_TOKEN_HEADER: token})

    response = await middleware(request, _ok_handler)

    assert response.status == 204
    assert compared == [(token.encode(), token.encode())]


def test_valid_worker_token_allows_edge_client_ip(monkeypatch) -> None:
    monkeypatch.setenv(EDGE_SECRET_ENV, "expected-token")
    config = _cf_config(edge_credential_env=EDGE_SECRET_ENV)
    request = _request(
        config,
        headers={
            EDGE_TOKEN_HEADER: "expected-token",
            EDGE_CLIENT_IP_HEADER: "203.0.113.8",
            "CF-Connecting-IP": "198.51.100.9",
        },
    )

    assert entry.client_ip(request) == "203.0.113.8"


def test_forged_edge_client_ip_without_valid_token_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv(EDGE_SECRET_ENV, "expected-token")
    config = _cf_config(edge_credential_env=EDGE_SECRET_ENV)
    request = _request(
        config,
        headers={
            EDGE_TOKEN_HEADER: "forged-token",
            EDGE_CLIENT_IP_HEADER: "203.0.113.66",
            "CF-Connecting-IP": "198.51.100.9",
        },
    )

    assert entry.client_ip(request) == "198.51.100.9"


def test_non_ascii_edge_token_is_refused_without_error(monkeypatch) -> None:
    monkeypatch.setenv(EDGE_SECRET_ENV, "secret")
    config = _cf_config(edge_credential_env=EDGE_SECRET_ENV)
    request = _request(headers={EDGE_TOKEN_HEADER: "sécret"}, config=config)
    assert auth.edge_credential_valid(request, config) is False
