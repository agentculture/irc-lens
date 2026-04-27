# Playwright + irc-lens

`irc-lens` was built so a browser-automation agent (Playwright MCP
or pytest-playwright) can drive every feature deterministically.
Every interactive element carries a `data-testid` (catalogued in
`docs/sse-events.md`); the seed loader (`docs/cli.md` →
[Seed schema](cli.md#seed-schema)) gives every test a known
starting DOM without scripting an entire AgentIRC conversation.

## When to use Playwright

* Verifying the SSE → DOM swap for a new event type end-to-end.
* Pinning a UI invariant the unit tests can't see (focus order,
  CSS state cues, scroll behaviour).
* Reproducing a bug a human reported in the browser.

For pure server-side behaviour, prefer the in-process HTTP e2e
suite (`tests/test_e2e_http.py`) — it's an order of magnitude
faster and uses the same fixture stack.

## Suite layout

```
tests/
├── _agentirc_server.py      # ~145-line in-tree AgentIRC test server
├── conftest.py              # agentirc_server / lens_session / lens_client / seeded_lens_client
├── fixtures/basic.yaml      # canonical seed fixture
├── test_e2e_http.py         # in-process HTTP e2e (no browser)
└── test_e2e_playwright.py   # opt-in browser e2e (@pytest.mark.playwright)
```

`pyproject.toml` sets `addopts = "-m 'not playwright'"`, so a bare
`pytest` skips the browser layer. The `playwright` job in
`.github/workflows/ci.yml` overrides via `pytest -m playwright`.

## Local run

```bash
uv pip install -e ".[dev]"        # one-time
uv run playwright install chromium  # one-time browser install
uv run pytest -m playwright -v
```

To run *both* default and Playwright tests:

```bash
uv run pytest -m "" -v
```

## Async-API requirement

`tests/test_e2e_playwright.py` uses `playwright.async_api`, not the
sync `page` fixture from pytest-playwright. The repo runs under
pytest-asyncio's auto loop; the sync API trips
`RuntimeError: Cannot run the event loop while another loop is
running` when invoked from inside an active loop. Use
`async with async_playwright() as p: …` instead.

## Worked transcript: drive a slash command

The pattern below is what every test in `test_e2e_playwright.py`
follows. The `seeded_lens_client` fixture preloads
`tests/fixtures/basic.yaml`, so `#general` is active with two
historical chat lines before the browser ever navigates.

```python
import pytest
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.playwright

async def test_view_switch_via_help_command(seeded_lens_client):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(str(seeded_lens_client.make_url("/")))

            indicator = page.locator('[data-testid="view-indicator"]')
            await expect(indicator).to_have_attribute(
                "data-view", "chat", timeout=5000
            )

            await page.locator('[data-testid="chat-input"]').fill("/help")
            await page.locator('[data-testid="chat-input"]').press("Enter")

            await expect(indicator).to_have_attribute(
                "data-view", "help", timeout=5000
            )
        finally:
            await browser.close()
```

Three things make this deterministic:

1. **Seeded state.** The fixture overlays a known `current_channel`
   and chat-line count before the page loads.
2. **Stable testids.** `[data-testid="chat-input"]` and
   `[data-testid="view-indicator"]` survive template churn —
   they're pinned by tests on both sides.
3. **Auto-retrying assertions.** `expect(...).to_have_attribute(...)`
   polls until the SSE swap arrives or the timeout
   (`_LOCATOR_TIMEOUT_MS = 5000`) expires.

## Driving via Playwright MCP

The same patterns translate to a Playwright MCP transcript. Boot
the lens against any reachable AgentIRC, navigate the MCP browser
to the printed URL, then drive elements by `data-testid`:

```text
1. Shell: irc-lens serve --host 127.0.0.1 --port 6667 --nick agent \
                          --seed tests/fixtures/basic.yaml --web-port 8765
2. MCP:   navigate "http://127.0.0.1:8765/"
3. MCP:   read_page  → confirm chat-line count, sidebar contents
4. MCP:   form_input [data-testid="chat-input"]  text="/join #ops"
5. MCP:   wait for [data-testid="sidebar-channel"][data-channel="#ops"]
6. MCP:   form_input [data-testid="chat-input"]  text="hello"
7. MCP:   wait for chat-line text="hello"
```

No human in the loop — every assertion has a stable testid hook
and every state mutation flows through the same `POST /input` →
SSE path the browser uses.

## Tips

* Bump `_LOCATOR_TIMEOUT_MS` if a CI runner gets noticeably hot;
  one constant covers every assertion in the file.
* The `chat-form` element is `id="chat-form"` only. Submit via the
  `[data-testid="chat-submit"]` button or by pressing Enter on
  `[data-testid="chat-input"]`.
* The browser is not aware of seed state vs live state — once the
  page loads, every subsequent change must come through `POST
  /input` (or the test server publishing into the SSE bus directly).
* If you need the raw SSE stream for debugging, the in-process HTTP
  e2e suite already exercises `GET /events` —
  `tests/test_e2e_http.py` is a working example.
