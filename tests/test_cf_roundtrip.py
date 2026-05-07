"""Real Cloudflare round-trip. Skipped unless setup.sh has run.

Requires: AgentIRC running locally on 127.0.0.1:6667 (or a fixture),
`cloudflared` installed, and the env vars in .cf-roundtrip.env loaded.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path  # noqa: F401 — available for fixture use

import aiohttp
import pytest

pytestmark = pytest.mark.cloudflare

REQUIRED_ENV = (
    "IRC_LENS_TEST_AUD",
    "IRC_LENS_TEST_HOSTNAME",
    "IRC_LENS_TEST_TEAM_DOMAIN",
    "IRC_LENS_TEST_CLIENT_ID",
    "IRC_LENS_TEST_CLIENT_SECRET",
    "IRC_LENS_TEST_TOKEN_NAME",
)


def _missing_env() -> str | None:
    for k in REQUIRED_ENV:
        if not os.environ.get(k):
            return k
    return None


@pytest.fixture(scope="module")
def lens_subprocess(tmp_path_factory) -> subprocess.Popen:
    if _missing_env():
        pytest.skip(f"missing env: {_missing_env()}")
    if not shutil.which("cloudflared"):
        pytest.skip("cloudflared not on PATH")

    cfg_dir = tmp_path_factory.mktemp("lens-cf")
    cfg = cfg_dir / "config.yaml"
    cfg.write_text(f"""
auth:
  mode: cloudflare-access
  cloudflare:
    aud: {os.environ['IRC_LENS_TEST_AUD']}
    team_domain: {os.environ['IRC_LENS_TEST_TEAM_DOMAIN']}
  allowed_emails: []
  allowed_service_tokens:
    - {os.environ['IRC_LENS_TEST_TOKEN_NAME']}
server:
  name: roundtrip
  host: 127.0.0.1
  port: 6667
web:
  bind: 127.0.0.1
  port: 8765
""")
    proc = subprocess.Popen(
        ["uv", "run", "irc-lens", "serve", "--config", str(cfg)],
        env={**os.environ, "IRC_LENS_TEST_HOOKS": "1"},
    )
    try:
        # Wait for local /healthz before yielding.
        for _ in range(30):
            if _local_healthz():
                break
            time.sleep(1)
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _local_healthz() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


async def _get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status, await r.read()


async def _post(url: str, headers: dict[str, str], data: dict) -> int:
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, data=data,
                          timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status


@pytest.mark.asyncio
async def test_cloudflare_round_trip(lens_subprocess) -> None:
    host = os.environ["IRC_LENS_TEST_HOSTNAME"]
    base = f"https://{host}"
    auth_headers = {
        "CF-Access-Client-Id": os.environ["IRC_LENS_TEST_CLIENT_ID"],
        "CF-Access-Client-Secret": os.environ["IRC_LENS_TEST_CLIENT_SECRET"],
    }

    # Wait until the public hostname starts answering — DNS may need to
    # settle on first run, and cloudflared takes a few seconds to come up.
    deadline = time.time() + 60
    while time.time() < deadline:
        status, _ = await _get(f"{base}/healthz", headers=auth_headers)
        if status == 200:
            break
        await asyncio.sleep(2)
    else:
        pytest.fail("public /healthz never came up")

    # Unauthenticated request must be blocked at the CF edge.
    status_unauth, _ = await _get(f"{base}/", headers={})
    assert status_unauth in (401, 302)

    # Service-token call lands on the lens.
    status_auth, body = await _get(f"{base}/", headers=auth_headers)
    assert status_auth == 200
    assert b"<html" in body or b"<!DOCTYPE" in body

    # POST /input via service token. With AgentIRC offline the response
    # may be 503; that still proves the auth path landed in the handler.
    status_post = await _post(
        f"{base}/input",
        headers={**auth_headers, "Origin": base},
        data={"text": "/help"},
    )
    assert status_post in (204, 503)
