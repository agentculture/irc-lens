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

## Agent-facing surfaces

Beyond the browser console, irc-lens is legible to an agent by
construction: its CLI, MCP server, HTTP front, and terminal UI are all
rendered from one [`agentfront`](https://github.com/agentculture/agentfront)
registry, so they can't drift apart.

```bash
irc-lens learn                 # what this tool is, in agent-onboarding form
irc-lens join '#ops' --json    # every console verb is also a CLI/MCP tool
irc-lens mcp                   # serve the same catalog over MCP stdio
irc-lens tui                   # a keyboard-driven cockpit; prints the front when piped
```

`irc-lens serve` also exposes the registry over HTTP at `/agent` on the
console's own origin (`/agent/llms.txt`, `/agent/sitemap.xml`,
`/agent/front`, and one page per topic) — a fetch-tool-only agent can
start there and discover everything else. See
[`docs/cli.md`](docs/cli.md) for the full command reference and
[`CLAUDE.md`](CLAUDE.md) for how the registry and its surfaces fit
together.

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

## Production deployment

To host irc-lens behind Cloudflare Access on your own domain, see
[docs/deployment-cloudflare-access.md](docs/deployment-cloudflare-access.md).

## License

See [LICENSE](LICENSE).
