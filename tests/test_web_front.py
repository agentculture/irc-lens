"""HTTP front mount tests (t7) — the agentfront WSGI surface under ``/agent``.

The bridge (``irc_lens.web.front``) mounts agentfront's stdlib WSGI HTTP
surface inside the aiohttp console under the ``/agent`` prefix, behind the
console's auth middleware. This file pins:

* **dev mode** — index, ``llms.txt``, ``sitemap.xml``, ``front`` and every
  registry doc slug return 200; the doc slugs are iterated from
  ``build_app()`` rather than hardcoded (no second driftable list);
* the **sitemap** ``<loc>`` URLs carry the ``/agent`` prefix;
* **cloudflare-access mode** — an unauthenticated GET on the prefix is 401,
  and the middleware exempt lists still contain *exactly* ``/static`` /
  ``/healthz`` / ``/media`` (``/agent`` must never be exempted);
* the **security headers** (nosniff + referrer-policy) ride on ``/agent``
  responses via the existing middleware, with no CSP (these are not HTML);
* a **blind fetch-only traversal**: start at ``/agent/llms.txt``, enumerate
  the tool catalog, fetch every linked page without executing JavaScript,
  assert each is 200 + non-empty, and assert none of those page bodies is a
  whitespace-normalized copy of (or a substring of) any ``docs/**/*.md`` file.
"""

from __future__ import annotations

import inspect
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.cli import build_app
from irc_lens.config import LensConfig
from irc_lens.session import Session
from irc_lens.web import make_app

from _jwks_server import FakeJWKS
from helpers import make_app_for as _make_app_for

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _REPO_ROOT / "docs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry_doc_slugs() -> list[str]:
    """The doc slugs the front is expected to serve — iterated, not hardcoded."""
    return [entry.slug for entry in build_app().list_docs()]


def _registry_tool_names() -> set[str]:
    return {tool.name for tool in build_app().list_tools()}


def _normalize(text: str) -> str:
    """Collapse whitespace runs to one space and strip — mirrors
    ``test_front_docs._normalize`` so the copy comparison is apples-to-apples."""
    return re.sub(r"\s+", " ", text).strip()


def _cf_config(jwks: FakeJWKS, allowed: list[str]) -> LensConfig:
    """A cloudflare-access LensConfig wired to the FakeJWKS server.

    Mirrors ``test_auth_middleware._cf_config`` — kept local so this file
    doesn't import another test module."""
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=tuple(allowed),
        allowed_service_tokens=(),
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
    )


# ---------------------------------------------------------------------------
# Fixtures (dev mode)
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    # The front handler never touches the session; a bare, unconnected one
    # is enough (same pattern as test_web_skeleton / test_security_headers).
    return Session(host="127.0.0.1", port=6667, nick="lens-test")


@pytest_asyncio.fixture
async def client(session: Session) -> AsyncIterator[TestClient]:
    app = _make_app_for(session)
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# (a) dev-mode 200s for the whole surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/agent", "/agent/", "/agent/llms.txt", "/agent/front"])
async def test_front_top_level_paths_return_200(client: TestClient, path: str) -> None:
    resp = await client.get(path)
    assert resp.status == 200, f"{path} should return 200"
    body = await resp.read()
    assert body, f"{path} returned an empty body"


async def test_front_sitemap_returns_200_xml(client: TestClient) -> None:
    resp = await client.get("/agent/sitemap.xml")
    assert resp.status == 200
    assert resp.content_type == "application/xml"
    assert (await resp.read())


async def test_front_index_is_markdown(client: TestClient) -> None:
    resp = await client.get("/agent/")
    assert resp.status == 200
    assert resp.content_type == "text/markdown"


async def test_every_registry_doc_slug_returns_200(client: TestClient) -> None:
    slugs = _registry_doc_slugs()
    assert slugs, "build_app() registered no docs — nothing to serve"
    for slug in slugs:
        resp = await client.get(f"/agent/{slug}")
        assert resp.status == 200, f"/agent/{slug} should return 200"
        assert resp.content_type == "text/markdown"
        body = await resp.text()
        assert body.strip(), f"/agent/{slug} returned an empty doc body"


async def test_unknown_slug_returns_404(client: TestClient) -> None:
    resp = await client.get("/agent/no-such-slug")
    assert resp.status == 404


# ---------------------------------------------------------------------------
# (b) sitemap <loc> URLs carry the /agent prefix
# ---------------------------------------------------------------------------


async def test_sitemap_loc_urls_carry_agent_prefix(client: TestClient) -> None:
    resp = await client.get("/agent/sitemap.xml")
    assert resp.status == 200
    xml = await resp.text()
    root = ET.fromstring(xml)
    locs = [loc.text for loc in root.iter("loc")]
    assert locs, "sitemap contained no <loc> entries"
    for loc in locs:
        assert loc is not None and loc.startswith("/agent/"), (
            f"<loc> {loc!r} does not carry the /agent prefix"
        )
    # Every registry doc slug must appear under the prefix.
    for slug in _registry_doc_slugs():
        assert f"/agent/{slug}" in locs, f"sitemap missing /agent/{slug}"


async def test_index_and_llms_links_carry_agent_prefix(client: TestClient) -> None:
    for path in ("/agent/", "/agent/llms.txt"):
        body = await (await client.get(path)).text()
        links = re.findall(r"\]\((/[^)]+)\)", body)
        assert links, f"{path} had no markdown links to check"
        for link in links:
            assert link.startswith("/agent/"), (
                f"{path} link {link!r} does not carry the /agent prefix"
            )


# ---------------------------------------------------------------------------
# (e) security headers on /agent responses (via the existing middleware)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/agent/", "/agent/llms.txt", "/agent/sitemap.xml"])
async def test_front_responses_have_baseline_security_headers(
    client: TestClient, path: str
) -> None:
    resp = await client.get(path)
    assert resp.status == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    # These are markdown/XML, not HTML — CSP (which polices markup) must not
    # be present, matching every other non-HTML console response.
    assert "Content-Security-Policy" not in resp.headers


# ---------------------------------------------------------------------------
# (c) cloudflare-access: prefix is behind auth; exempt list unchanged
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cf_client(jwks: FakeJWKS) -> AsyncIterator[TestClient]:
    config = _cf_config(jwks, allowed=["alice@example.com"])

    def boom_factory(_nick: str):
        raise AssertionError("session factory must not run during front auth tests")

    app = make_app(config, boom_factory)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_cf_unauthenticated_llms_txt_returns_401(cf_client: TestClient) -> None:
    resp = await cf_client.get("/agent/llms.txt")
    assert resp.status == 401
    body = await resp.json()
    assert "error" in body and "hint" in body


@pytest.mark.parametrize(
    "path", ["/agent", "/agent/", "/agent/sitemap.xml", "/agent/front", "/agent/about"]
)
async def test_cf_unauthenticated_every_front_path_returns_401(
    cf_client: TestClient, path: str
) -> None:
    resp = await cf_client.get(path)
    assert resp.status == 401, f"{path} must be behind auth in cloudflare-access mode"


async def test_cf_valid_jwt_reaches_front(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(
        aud="aud-test", claims={"email": "alice@example.com", "sub": "front-1"}
    )
    resp = await cf_client.get(
        "/agent/llms.txt", headers={"Cf-Access-Jwt-Assertion": token}
    )
    assert resp.status == 200
    assert (await resp.text()).strip()


async def test_cf_exempt_paths_are_not_401(cf_client: TestClient) -> None:
    """The prefix is guarded, but the three exempt families still are not:
    a JWT-less request to /healthz, /static/*, /media/* must NOT be 401."""
    for path in ("/healthz", "/static/does-not-exist.js", "/media/bogus.png"):
        resp = await cf_client.get(path)
        assert resp.status != 401, f"{path} must remain auth-exempt"


def _exempt_paths_in(func) -> set[str]:
    """Extract the exempt path literals from an auth-middleware factory's source.

    Both middlewares gate on ``request.path.startswith("/x/")`` /
    ``request.path == "/x"``; this pulls those literals out so the test can
    assert the exempt set is *exactly* the three expected families."""
    src = inspect.getsource(func)
    startswith = re.findall(r"request\.path\.startswith\(\s*[\"']([^\"']+)[\"']", src)
    equals = re.findall(r"request\.path\s*==\s*[\"']([^\"']+)[\"']", src)
    return set(startswith) | set(equals)


def test_exempt_lists_contain_exactly_static_healthz_media() -> None:
    """Neither middleware may add /agent (or anything else) to the exempt set.

    Guards decision c30: the WSGI front sits behind CF Access with zero new
    auth exemptions. The exempt families stay exactly {/static, /healthz,
    /media}."""
    from irc_lens.web import app as app_module
    from irc_lens.web import auth as auth_module

    expected = {"/static/", "/healthz", "/media/"}
    dev_exempt = _exempt_paths_in(app_module._dev_identity_middleware)
    cf_exempt = _exempt_paths_in(auth_module.build_cloudflare_middleware)

    assert dev_exempt == expected, f"dev exempt set drifted: {dev_exempt}"
    assert cf_exempt == expected, f"CF exempt set drifted: {cf_exempt}"
    # Redundant but explicit — the thing this task must never do.
    assert not any("/agent" in p for p in dev_exempt | cf_exempt)


# ---------------------------------------------------------------------------
# (d) blind fetch-only traversal
# ---------------------------------------------------------------------------


def _extract_tool_catalog(llms_txt: str) -> list[str]:
    """Parse the ``## Tools`` section of llms.txt into tool names.

    Tools are listed as ``- <name>: <description>`` (not hyperlinks), so this
    reads the catalog the way a blind agent would."""
    names: list[str] = []
    in_tools = False
    for line in llms_txt.splitlines():
        if line.startswith("## "):
            in_tools = line.strip() == "## Tools"
            continue
        if in_tools:
            m = re.match(r"-\s+([^:]+):", line)
            if m:
                names.append(m.group(1).strip())
    return names


async def test_blind_traversal_from_llms_txt(client: TestClient) -> None:
    """Start blind at /agent/llms.txt, enumerate the tool catalog, fetch every
    linked page (no JavaScript), and assert none is a copy of a docs/ file."""
    # 1. Blind entry point.
    entry = await client.get("/agent/llms.txt")
    assert entry.status == 200
    llms_txt = await entry.text()

    # 2. Enumerate the tool catalog — matches build_app()'s registered tools.
    catalog = _extract_tool_catalog(llms_txt)
    assert catalog, "llms.txt advertised no tools under ## Tools"
    assert set(catalog) == _registry_tool_names(), (
        f"tool catalog {catalog} does not match the registry {_registry_tool_names()}"
    )

    # 3. Extract every linked page (root-relative markdown links) and fetch it.
    linked = re.findall(r"\]\((/agent/[^)]+)\)", llms_txt)
    assert linked, "llms.txt listed no linked pages"
    # De-dupe while preserving order.
    seen: set[str] = set()
    linked = [p for p in linked if not (p in seen or seen.add(p))]

    fetched_bodies: list[str] = []
    for path in linked:
        resp = await client.get(path)
        assert resp.status == 200, f"linked page {path} did not return 200"
        body = await resp.text()
        assert body.strip(), f"linked page {path} returned an empty body"
        fetched_bodies.append(body)

    # The linked set must cover every doc slug plus /front (per the surface).
    for slug in _registry_doc_slugs():
        assert f"/agent/{slug}" in linked, f"llms.txt did not link /agent/{slug}"
    assert "/agent/front" in linked, "llms.txt did not link /agent/front"

    # 4. No fetched page may be a whitespace-normalized copy of, or substring
    #    of, any docs/**/*.md file (neither direction).
    md_files = sorted(_DOCS_DIR.rglob("*.md"))
    assert md_files, "expected docs/ to contain markdown files to compare against"
    file_norms = {
        path: _normalize(path.read_text(encoding="utf-8")) for path in md_files
    }
    for body in fetched_bodies:
        page_norm = _normalize(body)
        assert page_norm, "a fetched page normalized to empty"
        for path, file_norm in file_norms.items():
            rel = path.relative_to(_REPO_ROOT)
            assert page_norm not in file_norm, (
                f"a fetched front page is a substring of {rel} — the front is "
                "serving a verbatim copy of the human docs tree"
            )
            assert file_norm not in page_norm, (
                f"{rel} is a substring of a fetched front page — the front is "
                "serving a verbatim copy of the human docs tree"
            )
