"""t12 — fresh-agent legibility end-to-end test.

CONTRACT: the scripted flow below models an agent that knows nothing about
irc-lens except (a) the installed CLI (``agentfront.testing.run_cli`` against
``irc_lens.cli.build_app()`` — the in-process stand-in for "run the installed
`irc-lens` binary", the same convention ``tests/test_front_agreement.py`` and
``tests/test_tools_live.py`` already use) and (b) HTTP responses from a
running console. The flow logic never calls ``open()`` on anything under
``src/`` or ``docs/`` to decide what to assert — every assertion below is
derived from CLI stdout/JSON or HTTP response bodies, exactly the view a
real fresh agent would have. (Importing ``irc_lens.cli``/``irc_lens.web`` and
the in-tree fake AgentIRC server is test *infrastructure* that stands in for
"the installed CLI" and "a seeded serve", not a source-reading shortcut —
the plan's own acceptance text names these as the sanctioned harness.)

Three steps, per the plan's acceptance criterion:

1. discovery via ``learn --json`` — assert it names the live tools and docs.
2. site discovery via ``GET /agent/llms.txt`` on a running console — parse
   the tool catalog out of the body, assert ``channels`` is listed, follow
   at least one linked doc page and assert 200.
3. exercise one tool end to end via the CLI path (``join`` then
   ``channels --json``) against the fake AgentIRC server, asserting the
   result reflects the state the ``join`` call created.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from agentfront.testing import run_cli
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.cli import build_app
from irc_lens.config import LensConfig
from irc_lens.session import Session
from irc_lens.web import make_app

from _agentirc_server import ThreadedAgentIRCTestServer

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


# ---------------------------------------------------------------------------
# Fixtures — sync (no pytest_asyncio): the CLI path calls sync entry points
# whose live-verb tools each open their own event loop via asyncio.run()
# (see irc_lens/tools.py), so the fake AgentIRC server needs its own
# thread/loop rather than the pytest_asyncio fixtures in conftest.py —
# same reasoning tests/test_tools_live.py documents.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_server():
    server = ThreadedAgentIRCTestServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def dev_config_path(tmp_path, fake_server: ThreadedAgentIRCTestServer):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "auth:\n"
        "  mode: dev\n"
        "  dev:\n"
        "    nick: lens-test\n"
        "    email: dev@local\n"
        "server:\n"
        "  name: testsrv\n"
        f"  host: {fake_server.host}\n"
        f"  port: {fake_server.port}\n"
    )
    return cfg


# ---------------------------------------------------------------------------
# Step 2 helper — serve the real console app in-process and fetch
# /agent/llms.txt plus one linked doc page.
# ---------------------------------------------------------------------------


def _dev_config(host: str, port: int) -> LensConfig:
    return LensConfig(
        auth_mode="dev",
        dev_nick="lens-test",
        dev_email="dev@local",
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host=host,
        server_port=port,
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


def _extract_tool_catalog(llms_txt: str) -> list[str]:
    """Parse the ``## Tools`` section of llms.txt into tool names — the way
    a blind agent reading the body would, not via registry introspection."""
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


def _discover_site(host: str, port: int) -> tuple[str, str, int]:
    """Fetch ``/agent/llms.txt`` from a real in-process console, then follow
    the first linked doc page. Runs its own event loop via ``asyncio.run()``;
    the fake AgentIRC server lives on a separate thread/loop
    (``ThreadedAgentIRCTestServer``), so this never nests inside a running
    loop."""

    async def _run() -> tuple[str, str, int]:
        config = _dev_config(host, port)
        # The front handler never touches the session (see test_web_front.py)
        # — an unconnected Session is enough to serve the /agent surface.
        session = Session(host=host, port=port, nick="lens-test")
        app = make_app(config, lambda _nick: session)
        app["registry"].register(config.dev_email, session)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            entry = await client.get("/agent/llms.txt")
            assert entry.status == 200
            llms_txt = await entry.text()

            links = re.findall(r"\]\((/agent/[^)]+)\)", llms_txt)
            assert links, "llms.txt advertised no linked pages to follow"
            first_link = links[0]
            doc_resp = await client.get(first_link)
            return llms_txt, first_link, doc_resp.status
        finally:
            await client.close()

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# The scripted flow.
# ---------------------------------------------------------------------------


def test_fresh_agent_discovers_and_exercises_a_tool_end_to_end(
    fake_server: ThreadedAgentIRCTestServer, dev_config_path
) -> None:
    app = build_app()

    # --- Step 1: discovery via the installed CLI's `learn --json`. -------
    learn_result = run_cli(app, ["learn", "--json"])
    assert learn_result.exit_code == 0
    assert learn_result.stderr == ""
    learn_payload = json.loads(learn_result.stdout)

    tool_names = {tool["name"] for tool in learn_payload["tools"]}
    assert "channels" in tool_names, f"learn --json did not name `channels`: {tool_names}"
    assert "join" in tool_names, f"learn --json did not name `join`: {tool_names}"

    doc_slugs = {doc["slug"] for doc in learn_payload["docs"]}
    assert doc_slugs, "learn --json advertised no docs"
    assert "tools" in doc_slugs, f"learn --json did not name a tool-catalog doc: {doc_slugs}"

    # --- Step 2: site discovery via HTTP against a running console. ------
    llms_txt, followed_link, followed_status = _discover_site(
        fake_server.host, fake_server.port
    )
    site_tool_names = set(_extract_tool_catalog(llms_txt))
    assert "channels" in site_tool_names, (
        f"/agent/llms.txt did not list `channels`: {site_tool_names}"
    )
    assert followed_status == 200, f"following {followed_link} from llms.txt did not return 200"

    # --- Step 3: exercise one tool end to end via the CLI path. ----------
    # Join a channel first (via the join tool) so the channels assertion
    # below reflects real state the fake server now holds, not an empty list.
    join_result = run_cli(
        app, ["join", "#agora", "--json", "--config", str(dev_config_path)]
    )
    assert join_result.exit_code == 0, join_result.stderr
    assert join_result.stderr == ""
    join_payload = json.loads(join_result.stdout)
    assert join_payload["channel"] == "#agora"

    channels_result = run_cli(
        app, ["channels", "--json", "--config", str(dev_config_path)]
    )
    assert channels_result.exit_code == 0, channels_result.stderr
    assert channels_result.stderr == ""
    channels_payload = json.loads(channels_result.stdout)
    assert "#agora" in channels_payload, (
        f"channels --json did not reflect the joined channel: {channels_payload}"
    )
