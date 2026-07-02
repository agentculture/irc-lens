# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working
with code in this repository.

## What this repo is

`irc-lens` is the lens CLI and web console for **AgentIRC** in the
**Culture** ecosystem (a sibling repo named `culture` — typically checked
out alongside this one). It is a reactive `aiohttp` + HTMX + SSE console
that puts a browser front end on a single AgentIRC connection
(`irc-lens serve`), plus an agent-facing command surface for driving the
same verbs headlessly. Source lives under `src/irc_lens/`; tests under
`tests/`.

## Runtime contract: one registry, four surfaces

irc-lens's CLI, MCP, HTTP, and TAUI (terminal UI) surfaces are all
*rendered* from a single [`agentfront`](https://github.com/agentculture/agentfront)
`App` registry — they are not four hand-maintained implementations of the
same command list. `irc_lens.cli.build_app()`
(`src/irc_lens/cli/__init__.py`) assembles that registry:

- it registers the four purpose-authored doc pages
  (`src/irc_lens/front_docs.py`: `about`, `console`, `tools`,
  `conventions`), then
- it calls every command module's `register_into(app)` hook — `serve`,
  `config`, `cli` (the CLI-introspection noun), `mcp`, `tui`, and
  `tools` (the live-verb catalog) — each living in its own file under
  `src/irc_lens/cli/_commands/` or `src/irc_lens/tools.py`.

Adding a command means adding one `register_into(app)` hook and one line
in `_iter_command_modules()` — never hand-wiring a second parser, a
second MCP tool list, or a second doc page. From that one registry,
`agentfront` derives:

- the **CLI** — `irc_lens.cli.main()` dispatches through
  `agentfront.cli_surface.run_cli`, which renders `learn`/`explain`,
  per-verb `--json`, and the `error:`/`hint:` diagnostics;
- the **MCP** surface — `irc-lens mcp` serves the registry over stdio as
  a single `run` tool (`src/irc_lens/cli/_commands/mcp_cmd.py`);
- the **TAUI** surface — `irc-lens tui` drives an
  `agentfront.taui.driver.LiveDriver` over the same registry at a real
  TTY, or prints the registry's markdown front when piped
  (`src/irc_lens/cli/_commands/tui_cmd.py`);
- the **HTTP** surface — mounted *inside* the existing `aiohttp` console
  under `/agent` via a WSGI bridge
  (`src/irc_lens/web/front.py`, `mount_agent_front`), serving the index,
  `llms.txt`, `sitemap.xml`, `/agent/front`, and every doc slug.

### The drift gate

Because every surface reads the same registry, they cannot silently
diverge — but the guarantee is enforced, not just architectural.
`tests/test_front_agreement.py::test_surfaces_agree` calls
`agentfront.testing.assert_surfaces_agree(build_app())` unconditionally in
CI (no external binary, no opt-in marker, no skip condition): it fails
loudly, naming the disagreeing pair, the moment any one surface's tool or
doc list stops matching the registry. That test replaced the deleted
`tests/test_afi_verify.py`, which used to shell out to a sibling-checkout
external verifier binary and silently skipped whenever that binary
wasn't on `PATH` — a gate that could go dark without anyone noticing.
There is no citation-reference regeneration step in this repo anymore,
and no external verifier to run before a PR; `uv run pytest -q` is the
whole story.

## Hard contracts that remain

Adopting `agentfront` changed *how* the surfaces are built, not the
contracts they honor. These are unchanged and still load-bearing:

- **Exit-code policy** — `0` success, `1` user error (bad input,
  unreachable AgentIRC, bad seed/config), `2` environment error (port in
  use, permission denied, missing dependency), `3+` reserved. Every
  failure raises `AfiError` (`src/irc_lens/_errors.py`, a verbatim
  subclass of `agentfront.errors.AgentfrontError`); the dispatcher
  catches it and exits with `err.code`. No Python traceback ever leaks.
- **stdout/stderr split** — results to stdout, errors and diagnostics to
  stderr, in text mode and `--json` mode alike. The streams are never
  mixed.
- **Errors have shape `{code, message, remediation}`** — text-mode
  renders as `error: <msg>` / `hint: <remediation>` on stderr.
- **The web console's HTTP error shape is a *different*, deliberately
  separate contract**: `{error, hint}` JSON on 4xx/5xx responses from
  `POST /input`, `/upload`, and the Cloudflare Access middleware — not
  the CLI's `{code, message, remediation}` triple. See
  `docs/architecture.md`'s decision log ("Why HTTP error JSON is
  `{error, hint}` and not `{code, message, remediation}`"), ratified on
  PR #7 and reaffirmed since; do not "fix" the two shapes to match each
  other.

## Command surface

Four meta-verbs, generated from the registry, exist purely so an agent
can introspect the tool without reading source:

| Verb | Answers | Notes |
| --- | --- | --- |
| `learn` | "what is this tool, broadly" | ≥ 200 chars, `--json` parseable, stderr clean. |
| `explain [path]` | "what does this one verb/noun do" | Resolves a registered CLI path; doc-slug resolution is a known gap (see below). |
| `overview [path]` | "what is present here right now" | Descriptive, never hard-fails — a bogus path exits 0 with a warning section. |
| `doctor [path]` | "what is wrong, and how do I fix it" | The one meta-verb that fails (exit 1) when something is unhealthy. |

Host verbs (each its own `register_into(app)` hook):

- `serve` — launch the `aiohttp` web console against an AgentIRC server.
- `config init` / `config overview` — write or describe the config file.
- `mcp` — serve the registry over MCP stdio.
- `tui` — open the terminal UI (or print the front when piped/non-TTY).

Eleven live tools (`src/irc_lens/tools.py`), each an ephemeral
connect-execute-disconnect session against the configured AgentIRC
server (using `LensConfig`, resolved exactly like `serve` resolves it):
`send`, `join`, `part`, `read`, `channels`, `who`, `mesh`, `switch`,
`topic`, `me`, `icon`. Every one of these is also a console slash-command
(`/join`, `/send`, ...) that a human drives through the browser — the
tool and the console verb dispatch through the same
`Session.execute`/`ParsedCommand` path, so the two can't drift in
behavior. `auth.mode: cloudflare-access` configs raise a clear
`AfiError` for the live tools (no per-request JWT to derive an identity
from outside a browser session).

## The `cli/_meta.py` routing deviation

`overview` and `doctor` do **not** go through `agentfront.cli_surface.run_cli`
like every other verb. `agentfront` 0.20.0 reserves both names on the
`App` (so they cannot be re-registered as ordinary tools) but its own
stock implementations produce shapes too thin for this repo's rubric:
stock `overview --json` returns a bare list of nouns instead of
`{subject, sections}`, and stock `doctor` has no `--json` at all. So
`irc_lens.cli.main()` intercepts those two verb names and routes them to
`irc_lens.cli._meta` (`overview_command` / `doctor_command`), which still
*derives* its output from the same `App` registry (`list_commands()`,
`list_tools()`, `list_docs()`, `agentfront.doctor_live.run_doctor()`) —
there is no second source of truth, just a second render path for two
names. This is a documented, deliberate deviation, not an oversight; a
follow-up may fold these back into `agentfront` once its stock
meta-verbs grow the richer shapes.

Known upstream gaps worth remembering while working on this surface
(document them honestly rather than working around them silently):

- `agentfront` 0.20.0's `explain <slug>` resolves only tool paths, not
  doc slugs — `irc-lens explain about` currently misses even though
  `about` is a registered doc. Docs are discoverable via `learn`,
  `/agent/llms.txt`, and the sitemap instead.
- its `http_surface` ignores WSGI `SCRIPT_NAME` — the `/agent` bridge
  (`src/irc_lens/web/front.py`) sets it anyway (the WSGI-correct
  signal, honored by a hypothetical future `agentfront`) and separately
  rewrites root-relative link targets in the response body so
  `/agent/llms.txt` and the sitemap carry the right prefix today.
- its CLI dispatcher calls a tool's function directly and does not
  await a coroutine result — so every tool in `tools.py` is a plain sync
  function wrapping its async implementation in `asyncio.run`, callable
  identically from the CLI, the MCP `run` tool, and any future surface.

## Dev workflow

```bash
uv sync --extra dev
uv run pytest -q
```

`requires-python = ">=3.12"` (`pyproject.toml`). Linting:
`flake8`, `pylint`, `bandit -r src/`, `black`, `isort`. Markdown via
`markdownlint-cli2 "path/to/file.md"` (picks up whatever markdownlint
config is active on the machine; the repo commits none and CI runs no
markdown lint gate).

## Versioning and PR conventions

Single source of truth for the version is `pyproject.toml`'s
`[project].version`; `irc_lens.__version__` reads it back via
`importlib.metadata` at runtime. Bump it (and update `CHANGELOG.md`)
before a PR that ships user-visible behavior. Branch, implement, bump
version, open a PR, address review, merge — see `.claude/skills/cicd/`
for the mechanics of that loop in this repo (opening PRs, polling
Qodo/Copilot, replying to and resolving threads).

## Workspace context (Culture / AgentIRC)

In this workspace's standard layout, `irc-lens` is checked out alongside
`culture` and `agentfront` as sibling directories. The exact location
varies by contributor; what matters is the relationship, not the
absolute path.

- **Culture** is an IRC-based agent mesh; AgentIRC is the protocol/IRCd
  component. `irc-lens` is its **lens** — a pure client, holding no
  state beyond one session and one browser tab. Confirm exact scope
  against the parent project before adding write paths.
- **`agentfront`** is the Agent First Interface scaffolder this repo
  depends on (`agentfront[mcp]>=0.20.0`) for its CLI/MCP/HTTP/TAUI
  surfaces — see "Runtime contract" above. It is an ordinary PyPI
  dependency (not cited), versioned independently.
- **Culture citation** — `src/irc_lens/irc/transport.py`,
  `src/irc_lens/irc/buffer.py`, `src/irc_lens/irc/message.py`, and
  `src/irc_lens/commands.py` are **cited, not imported**, from
  `agentculture/culture` (cite-don't-import: copied and adapted, not
  pulled in as a dependency, to avoid dragging in `culture`'s entire
  agent-loop/telemetry graph for a handful of reused modules). See
  `CITATION.md` for the exact source paths, commit SHAs, and every
  tracked divergence.
- **All-backends rule** (Culture): if a feature lands on one agent
  backend (`claude` / `codex` / `copilot` / `acp`), it must be
  propagated to all of them. If `irc-lens` grows backend-aware code,
  that rule applies here too.

## Further reading

- `docs/architecture.md` — module layout, request shapes, decision log
  (including the `agentfront` adoption entry and its precedents).
- `docs/cli.md` — every verb, flag, and exit code on the rendered
  surface.
- `docs/slash-commands.md` — the console verb table and its CLI/MCP
  tool parity.
- `CITATION.md` — the culture citations and their divergences.
