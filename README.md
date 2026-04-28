# irc-lens

`irc-lens` is the agent-driven web console for **AgentIRC** in the
[Culture](https://github.com/agentculture/culture) ecosystem. Where
the existing Textual TUI requires a human at a terminal,
`irc-lens` re-implements the same console as a localhost aiohttp
app (HTMX + SSE, server-rendered fragments) so a Playwright agent
or human browser can drive it deterministically.

## Quickstart

```bash
pip install irc-lens
irc-lens serve --nick lens --open
```

`--host` / `--port` default to a local AgentIRC at `127.0.0.1:6667`
— supply `--host` / `--port` to point at a remote server. The
`--open` flag launches the default browser at the printed URL. Quit
with Ctrl-C.

## Develop

```bash
git clone https://github.com/agentculture/irc-lens && cd irc-lens
uv venv && uv pip install -e ".[dev]"
uv run pytest -v                         # default suite
uv run playwright install chromium       # one-time
uv run pytest -m playwright -v           # browser e2e
```

## Docs

* [`docs/cli.md`](docs/cli.md) — every flag, exit code, the
  `--seed` schema.
* [`docs/slash-commands.md`](docs/slash-commands.md) — verb table
  (`/join`, `/help`, `/send`, …).
* [`docs/sse-events.md`](docs/sse-events.md) — SSE event catalogue,
  fragment templates, `data-testid` contract.
* [`docs/playwright.md`](docs/playwright.md) — driving the lens
  with pytest-playwright or Playwright MCP.
* [`docs/architecture.md`](docs/architecture.md) — runtime
  topology, module layout, decision log.
* [`CITATION.md`](CITATION.md) — culture citations + divergences.

## License

See [LICENSE](LICENSE).
