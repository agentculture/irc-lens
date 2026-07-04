"""Route tests for task t6 — ``POST /upload`` + ``GET /media/{name}``.

Covers the acceptance criteria from
`docs/plans/2026-07-02-media-support.md` (task t6):

1. ``POST /upload`` is identity-gated and origin-checked like
   ``/input``; multipart field ``file``; over-cap 413 and bad-type 400
   both return ``{error, hint}``; success returns 201 with ``url`` and
   ``kind``.
2. ``GET /media/token.ext`` is exempt from the auth middleware (no
   auth headers required — the agent-fetch path) and serves via
   ``web.FileResponse`` with nosniff, immutable private cache, inline
   disposition; an unknown/malformed token 404s.
3. ``client_max_size`` is raised app-wide to the media cap; the
   companion assertion that ``POST /input`` still enforces its own
   4 KiB bound (including for chunked bodies) lives in the pinned test
   in ``tests/test_web_events.py`` (updated by this same task).
4. This task owns ``web/routes.py``, the ``web/app.py`` /
   ``web/auth.py`` exemption lists, and this file.

Two auth-exemption variants for ``GET /media/``:

* ``test_get_media_no_auth_headers_succeeds_dev_mode`` — the baseline
  the plan explicitly allows ("dev-mode fixture is fine").
* ``test_get_media_no_auth_headers_succeeds_cf_mode`` — the stronger
  proof: under ``cloudflare-access`` auth (where every *other* route
  401s without a JWT), ``/media/`` still serves with zero auth headers,
  demonstrating the exemption list actually does something rather than
  riding on dev mode's blanket auto-identity.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.session import Session
from irc_lens.web import make_app
from irc_lens.web.store import MediaStore

from _jwks_server import FakeJWKS

# ---------------------------------------------------------------------------
# Sample payload — a minimal but valid PNG (magic bytes + padding).
# ---------------------------------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SVG_BYTES = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
)


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def _dev_config(tmp_path: Path, **overrides: object) -> LensConfig:
    base: dict[str, object] = dict(
        auth_mode="dev",
        dev_nick="lens-test",
        dev_email="dev@local",
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=True,
        media_dir=str(tmp_path / "media"),
        media_max_file_bytes=4096,
        media_max_store_bytes=1_048_576,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )
    base.update(overrides)
    return LensConfig(**base)


def _cf_config(jwks: FakeJWKS, tmp_path: Path, **overrides: object) -> LensConfig:
    base: dict[str, object] = dict(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=("alice@example.com",),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
        media_enabled=True,
        media_dir=str(tmp_path / "media"),
        media_max_file_bytes=4096,
        media_max_store_bytes=1_048_576,
        media_public_base_url="",
        media_remote_embeds="click",
        media_trusted_hosts=(),
    )
    base.update(overrides)
    return LensConfig(**base)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    return Session(host="127.0.0.1", port=6667, nick="lens-test")


@pytest.fixture
def media_config(tmp_path: Path) -> LensConfig:
    return _dev_config(tmp_path)


@pytest_asyncio.fixture
async def client(media_config: LensConfig, session: Session) -> AsyncIterator[TestClient]:
    app = make_app(media_config, lambda _nick: session)
    app["registry"].register(media_config.dev_email, session)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


@pytest.fixture
def small_cap_config(tmp_path: Path) -> LensConfig:
    # A tiny per-file cap so an oversize upload is cheap to construct
    # and clearly exceeds it, while `client_max_size` (cap + 64 KiB
    # headroom, see `make_app`) stays comfortably above the actual
    # payload size — the rejection we're testing is MediaStore's own
    # streaming cap, not the framework-level multipart size guard.
    return _dev_config(tmp_path, media_max_file_bytes=16)


@pytest_asyncio.fixture
async def small_cap_client(
    small_cap_config: LensConfig, session: Session
) -> AsyncIterator[TestClient]:
    app = make_app(small_cap_config, lambda _nick: session)
    app["registry"].register(small_cap_config.dev_email, session)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


@pytest.fixture
def disabled_config(tmp_path: Path) -> LensConfig:
    return _dev_config(tmp_path, media_enabled=False)


@pytest_asyncio.fixture
async def disabled_client(
    disabled_config: LensConfig, session: Session
) -> AsyncIterator[TestClient]:
    app = make_app(disabled_config, lambda _nick: session)
    app["registry"].register(disabled_config.dev_email, session)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


def _png_form(filename: str = "pic.png", content_type: str = "image/png") -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("file", PNG_BYTES, filename=filename, content_type=content_type)
    return form


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------


async def test_post_upload_happy_path_returns_201(
    client: TestClient, media_config: LensConfig
) -> None:
    resp = await client.post("/upload", data=_png_form())
    assert resp.status == 201
    body = await resp.json()
    assert set(body) == {"url", "kind"}
    assert body["kind"] == "image"
    assert body["url"].startswith(
        f"http://{media_config.web_bind}:{media_config.web_port}/media/"
    )
    assert body["url"].endswith(".png")

    token_ext = body["url"].rsplit("/media/", 1)[1]
    stored = list(Path(media_config.media_dir).rglob(token_ext))
    assert len(stored) == 1, f"expected exactly one stored file named {token_ext!r}"
    assert stored[0].read_bytes() == PNG_BYTES


async def test_post_upload_missing_file_field_returns_400(client: TestClient) -> None:
    form = aiohttp.FormData()
    form.add_field("notfile", b"whatever", filename="pic.png", content_type="image/png")
    resp = await client.post("/upload", data=form)
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body and "hint" in body


async def test_post_upload_svg_returns_400(client: TestClient) -> None:
    form = aiohttp.FormData()
    form.add_field("file", SVG_BYTES, filename="evil.svg", content_type="image/svg+xml")
    resp = await client.post("/upload", data=form)
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body and "hint" in body


async def test_post_upload_oversize_returns_413(small_cap_client: TestClient) -> None:
    """A payload comfortably over the 16-byte per-file cap (but well
    under the framework-level `client_max_size`) must 413 with our
    `{error, hint}` shape — `MediaStore.save` enforcing its cap while
    streaming, not a framework-level rejection."""
    payload = PNG_BYTES + b"\x00" * 200
    form = aiohttp.FormData()
    form.add_field("file", payload, filename="big.png", content_type="image/png")
    resp = await small_cap_client.post("/upload", data=form)
    assert resp.status == 413
    body = await resp.json()
    assert "error" in body and "hint" in body


async def test_post_upload_foreign_origin_returns_403(client: TestClient) -> None:
    resp = await client.post(
        "/upload", data=_png_form(), headers={"Origin": "http://evil.example.com"}
    )
    assert resp.status == 403
    body = await resp.json()
    assert "error" in body and "hint" in body


async def test_post_upload_disabled_returns_404(disabled_client: TestClient) -> None:
    resp = await disabled_client.post("/upload", data=_png_form())
    assert resp.status == 404
    body = await resp.json()
    assert "error" in body and "hint" in body
    assert "disabled" in (body["error"] + body["hint"]).lower()


# ---------------------------------------------------------------------------
# GET /media/{name}
# ---------------------------------------------------------------------------


async def _upload(client: TestClient) -> str:
    """Upload a PNG and return its ``token.ext`` path segment."""
    resp = await client.post("/upload", data=_png_form())
    assert resp.status == 201
    body = await resp.json()
    return body["url"].rsplit("/media/", 1)[1]


async def test_get_media_disabled_returns_404(disabled_client: TestClient) -> None:
    resp = await disabled_client.get("/media/whatever.png")
    assert resp.status == 404
    body = await resp.json()
    assert "error" in body and "hint" in body
    assert "disabled" in (body["error"] + body["hint"]).lower()


async def test_get_media_happy_path_headers_exact(client: TestClient) -> None:
    token_ext = await _upload(client)
    resp = await client.get(f"/media/{token_ext}")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert resp.headers["Content-Disposition"] == "inline"
    assert await resp.read() == PNG_BYTES


async def test_get_media_range_request_returns_206(client: TestClient) -> None:
    token_ext = await _upload(client)
    resp = await client.get(f"/media/{token_ext}", headers={"Range": "bytes=0-3"})
    assert resp.status == 206
    assert await resp.read() == PNG_BYTES[:4]


async def test_get_media_unknown_token_returns_404(client: TestClient) -> None:
    resp = await client.get("/media/abcdefghijklmnopqrstuv12.png")
    assert resp.status == 404
    body = await resp.json()
    assert "error" in body and "hint" in body


async def test_get_media_traversal_shaped_name_returns_404(client: TestClient) -> None:
    """Two literal dots (no slash — the `{name}` route variable never
    matches `/`, so a slash-bearing traversal attempt can't even reach
    this handler) must still 404: `MediaStore.resolve`'s strict
    `^[A-Za-z0-9_-]+\\.[a-z0-9]+$` shape rejects a second dot, so this
    exercises the no-traversal contract through the actual route."""
    resp = await client.get("/media/foo..png")
    assert resp.status == 404
    body = await resp.json()
    assert "error" in body and "hint" in body


async def test_get_media_encoded_slash_traversal_returns_404(client: TestClient) -> None:
    """Belt-and-suspenders: an encoded-slash traversal attempt must
    never succeed, regardless of which layer (router or store) ends up
    rejecting it."""
    resp = await client.get("/media/..%2f..%2fetc%2fpasswd")
    assert resp.status == 404


async def test_get_media_no_auth_headers_succeeds_dev_mode(client: TestClient) -> None:
    """The agent-fetch path: GET /media/<token>.<ext> succeeds with no
    auth headers at all. Dev-mode variant — explicitly acceptable per
    the plan even though dev mode auto-authenticates every route; see
    `test_get_media_no_auth_headers_succeeds_cf_mode` below for the
    variant that actually proves the exemption does something."""
    token_ext = await _upload(client)
    resp = await client.get(f"/media/{token_ext}")
    assert resp.status == 200


async def test_get_media_no_auth_headers_succeeds_cf_mode(
    jwks: FakeJWKS, tmp_path: Path
) -> None:
    """CF-mode proof of the exemption. Under `cloudflare-access` auth,
    every *other* route 401s without a JWT (see
    `tests/test_auth_middleware.py::test_missing_jwt_returns_401`).
    `/media/` must not — this seeds a file directly into the store
    (no HTTP round-trip needed for that) and fetches it with zero auth
    headers, contrasting against `GET /` under the identical config to
    prove the exemption list is actually doing something rather than
    riding on dev mode's blanket auto-identity."""
    config = _cf_config(jwks, tmp_path)

    def boom_factory(_nick: str):
        raise AssertionError("session factory must not run for /media/ requests")

    app = make_app(config, boom_factory)
    store: MediaStore = app["media_store"]

    async def _one_chunk() -> AsyncIterator[bytes]:
        yield PNG_BYTES

    stored = await store.save("alice@example.com", "pic.png", _one_chunk())

    server = TestServer(app)
    async with TestClient(server) as c:
        # Contrast: the identical config 401s an ordinary route with no JWT.
        control = await c.get("/")
        assert control.status == 401

        resp = await c.get(f"/media/{stored.token}.{stored.ext}")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert await resp.read() == PNG_BYTES
