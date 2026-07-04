# `irc-lens` test layout

Three layers:

| Layer | Files | Drives |
| --- | --- | --- |
| Unit | `test_commands.py`, `test_render.py`, `test_session_unit.py`, `test_session_dispatch.py`, `test_seed.py`, `test_lens_js.py`, `test_web_skeleton.py`, `test_serve_cli.py` | Pure functions, single classes, single Jinja2 templates. No sockets. |
| HTTP e2e | `test_e2e_http.py` (Phase 9b) | Real `aiohttp.web.Application` driven by `aiohttp.test_utils.TestClient` against a real connected `Session` against a thin AgentIRC server (`_agentirc_server.py`). |
| Catalog / live-verb tools | `test_tools_catalog.py`, `test_tools_live.py` (t5) | `test_tools_catalog.py` diffs the registered `irc_lens.tools` catalog against `Session._exec_dispatch`. `test_tools_live.py` runs every live verb through the real CLI surface (`agentfront.testing.run_cli`) against `ThreadedAgentIRCTestServer` — each tool call is its own ephemeral `Session` (connect/execute/disconnect), so the fake server runs on a background thread/loop rather than the `pytest_asyncio` fixtures below. |
| Browser e2e | `test_e2e_playwright.py` (Phase 9c, opt-in) | Same fixture stack as HTTP e2e + a chromium browser via pytest-playwright. Marker: `@pytest.mark.playwright`. Run via `pytest -m playwright`. |

## AgentIRC test fixture: why option (b)

The Phase 8 spec offered two paths:

- **(a)** Pull `culture` as a pinned dev dep and import its AgentIRC server fixture.
- **(b)** Carry a thin AgentIRC test server in this repo (`_agentirc_server.py`).

We took **(b)**. `culture/agentirc/ircd.py` transitively imports `culture.bots.virtual_client`, telemetry/OTel infrastructure, skills, history-store, and protocol modules — pulling those in as a dev dep would massively bloat irc-lens's test environment for ~10 e2e cases. The thin server is ~120 lines and has zero dependencies outside `asyncio`.

The fallback is documented in the build plan (Phase 9b, lines 272–274) as the cleaner-boundary option even if it's slightly more work.

## What `_agentirc_server.py` does

- Binds `127.0.0.1:0` (random port). Tests read the assigned port back.
- Per connection, line-buffered read loop. Records every line into `server.received: list[_ReceivedLine]`.
- Sends `001 RPL_WELCOME` right after `USER` — `Session.connected`/`wait_for_welcome()` complete instead of hanging (added for t5's live-verb tools, which `wait_for_welcome()` before doing anything).
- For `JOIN #x` and `PART #x`, echoes `:<nick>!<nick>@test JOIN :#x` (matches what real ircds send) and updates an in-memory `channel_members: dict[str, set[str]]` map.
- Answers `LIST`, `WHO <channel>`, and `HISTORY RECENT <channel> <limit>` immediately from that membership map — `LIST`/`WHO` reflect real JOIN/PART state; `HISTORY` always replies with zero entries (the fixture tracks membership, not message history) but replies fast enough that `Session.QUERY_TIMEOUT` (10s) never fires.
- For `PRIVMSG`/`TOPIC`/`ICON`/`QUIT` — silently consumes; `Session` doesn't wait on any of these (fire-and-forget sends), and real IRC daemons don't echo PRIVMSGs to the sender.
- `ThreadedAgentIRCTestServer` wraps the above on a background thread with its own event loop, for tests that must drive a *sync* entry point (e.g. `agentfront.testing.run_cli`) whose implementation opens a fresh event loop per call — see `test_tools_live.py`.

## Adding a new HTTP e2e test

```python
async def test_my_thing(lens_client, agentirc_server):
    # 1. drive the lens via the test client
    resp = await lens_client.post("/input", json={"text": "/join #x"})
    assert resp.status == 204

    # 2. assert what the lens sent on the wire
    line = await _wait_for_received(agentirc_server, "JOIN", "#x")
    assert line.params == ["#x"]
```

`_wait_for_received` is the polling helper in `test_e2e_http.py`. Use it instead of `asyncio.sleep` — it has a 1 s default timeout and prints what *was* received on failure, which makes flakes easy to diagnose.

## Playwright (Phase 9c, opt-in)

Not yet written. Will land in a separate PR with:

- `tests/test_e2e_playwright.py` using `--seed tests/fixtures/basic.yaml` for deterministic DOM.
- A separate CI job that runs `pytest -m playwright` after `playwright install chromium`.
- Default `pytest` invocation continues to skip the browser tests.
