# `irc-lens` — Reactive Web Console for AgentIRC

**Status:** Design / handover spec
**Date:** 2026-04-27
**Audience:** The agent that will build `irc-lens` in a new sibling repo. Read this end-to-end before opening a worktree.

---

## Context

Culture's interactive admin console (`culture mesh console`) is a Textual TUI today. It works well for humans at terminals, but it can't be driven by a browser-automation agent (Playwright MCP), which makes iteration on the UI slow — every change requires a human in the loop to verify.

This spec defines `irc-lens`, a separate sibling repo that re-implements the console as an aiohttp-served reactive web app. The web frontend is server-rendered (HTMX + SSE), so DOM is deterministic and Playwright-testable. The TUI in culture stays as-is; `irc-lens` is **not** a replacement that lives behind a `--html` flag — it ships as its own CLI, on its own release cadence, in its own repo.

`irc-lens` speaks AgentIRC over a plain TCP socket. It depends on culture only insofar as it cites code from `packages/agent-harness/`. It does not import culture at runtime, does not read `~/.culture/server.yaml`, and is not coupled to culture's nick/manifest conventions.

The expected outcome: a `pip install irc-lens && irc-lens --host x --port y --nick z` flow that launches a localhost web app the user (or an agent driving Playwright) can use to admin any AgentIRC server.

---

## Repo identity

- **Name:** `irc-lens`
- **GitHub:** `agentculture/irc-lens` (new repo, sibling to `agentculture/culture`)
- **PyPI:** `irc-lens`
- **CLI entry point:** `irc-lens`
- **License:** match culture's
- **Python:** 3.11+ (match culture)
- **Dependency manager:** `uv` (match culture)
- **Web framework:** `aiohttp`
- **Templating:** Jinja2
- **Frontend:** vanilla JS + HTMX. **Vendored** under `src/irc_lens/static/vendor/` (pinned `htmx.min.js` + `sse.js`), not loaded from a CDN — irc-lens runs on localhost, drives Playwright in offline-friendly agent loops, and must boot deterministically without outbound network. The pinned version is documented in `docs/architecture.md`.

---

## CLI shape

The repo bootstraps from the `afi cli cite python-cli` pattern (see `irc-lens/CLAUDE.md`). The user-facing entry point is therefore the AFI rubric (`learn`, `explain`, plus per-tool verbs) extended with a `serve` verb that launches the web console.

```
irc-lens learn [--json]          # AFI-required: tool overview
irc-lens explain [path] [--json] # AFI-required: catalog explainer
irc-lens serve --host <agentirc-host> --port <agentirc-port> --nick <nick>
               [--web-port 8765]
               [--bind 127.0.0.1]
               [--icon <emoji>]
               [--open]            # auto-launch default browser
               [--seed <path>]     # YAML fixture for tests/dev (see Testing)
               [--log-json]        # emit stderr logs as JSON lines
```

Behavior:

- `serve` requires `--host`, `--port`, `--nick`. No defaults; refuse to start without them.
- `--bind 0.0.0.0` prints a loud stderr warning before listening (no auth in v1).
- Foreground only. No daemonization, no PID file. Ctrl-C is the supported shutdown.
- All failures raise `AfiError` (cited from the python-cli pattern). Exit codes: `0` success / clean shutdown; `1` user error (bad args, AgentIRC unreachable); `2` env error (web port in use); `3+` reserved.
- stdout/stderr split is preserved per AFI rubric — even in `--log-json`, results go to stdout, diagnostics to stderr.
- argparse errors route through the rubric's `_ArgumentParser` override so unknown verbs/flags print `error:` + `hint:` and exit non-zero with no Python traceback.

---

## Citation from culture

`irc-lens` follows culture's **cite-don't-import** rule. At repo bootstrap, copy these files from `agentculture/culture@<sha>` and adapt them. Record the source commit SHA in a `CITATION.md` at repo root so future updates can be diffed against the upstream.

The primary citation source is `packages/agent-harness/` (transport-level reusable code). `commands.py` is the one exception — it lives in `culture/console/` because the slash-command parser is console-specific, never needed by an agent. The lift is still the same pattern: copy, adapt, track the source SHA.

| Cite from | Copy to | Adapt |
|---|---|---|
| `packages/agent-harness/irc_transport.py` | `src/irc_lens/irc/transport.py` | Strip CAP REQ for `message-tags` (the console doesn't use them — see `culture/console/client.py` for precedent). Keep the persistent-connection + read-loop shape. |
| `packages/agent-harness/message_buffer.py` | `src/irc_lens/irc/buffer.py` | Use as-is unless the agent finds a reason not to. |
| `culture/console/commands.py` | `src/irc_lens/commands.py` | Lift wholesale. The `CommandType` enum and `parse_command()` are the slash-command surface. |

Do **not** cite: `daemon.py`, `socket_server.py`, `ipc.py`, `webhook.py`, `telemetry.py`, `config.py` — those are agent-runtime concerns. `irc-lens` has no agent loop.

The IRC query patterns in `culture/console/client.py` (`list_channels`, `who`, `history` — futures + collect buffers for multi-line replies) are the reference shape for `Session`'s query methods. Reuse the pattern; do not import the file.

---

## Runtime architecture

```
┌──────────────────────────────────────────────────────────┐
│  irc-lens process                                        │
│                                                          │
│  ┌────────────────┐    ┌─────────────────────────────┐   │
│  │ aiohttp server │◄──►│ Session                      │  │
│  │  GET  /        │    │  - IRCTransport (cited)      │  │
│  │  POST /input   │    │  - MessageBuffer (cited)     │  │
│  │  GET  /events  │    │  - command parser            │  │
│  │       (SSE)    │    │  - view state                │  │
│  │  /static/*     │    │  - SessionEventBus           │  │
│  └────────────────┘    └─────────────────────────────┘   │
│         ▲                          │                     │
└─────────┼──────────────────────────┼─────────────────────┘
          │ HTTP + SSE                │ TCP (AgentIRC)
          ▼                           ▼
   browser tab                AgentIRC server
```

**One process owns one IRC connection and serves one browser tab.** Multi-tab and multi-user are explicit non-goals for v1. The SSE stream is per-connection but the underlying state is shared, so a refresh re-attaches without losing history.

**SSE for server → browser, plain POST for browser → server.** No WebSocket. HTMX-native, curl-debuggable, Playwright-friendly.

**Server-rendered fragments, not JSON.** Every reactive update is a Jinja2-rendered HTML fragment delivered via SSE; HTMX swaps it into the DOM. Browser-side JS is the SSE → HTMX glue and nothing else (target: ≤ 50 lines).

---

## Module layout

```
irc-lens/
├── pyproject.toml
├── README.md
├── CITATION.md             # tracks source commit SHAs from culture
├── docs/
│   ├── cli.md              # all flags, exit codes, examples
│   ├── slash-commands.md   # full inherited command list
│   ├── sse-events.md       # event types + payloads
│   └── playwright.md       # how to drive irc-lens with Playwright MCP
├── src/irc_lens/
│   ├── __init__.py
│   ├── cli.py              # argparse, wiring, asyncio.run
│   ├── session.py          # Session class
│   ├── commands.py         # cited from culture/console/commands.py
│   ├── irc/
│   │   ├── transport.py    # cited
│   │   └── buffer.py       # cited
│   ├── web/
│   │   ├── app.py          # aiohttp Application factory
│   │   ├── routes.py       # GET /, POST /input, GET /events, GET /partials/*
│   │   ├── render.py       # Jinja2 environment + render helpers
│   │   └── events.py       # SessionEvent dataclass + SSE serialization
│   ├── static/
│   │   ├── lens.css
│   │   └── lens.js
│   └── templates/
│       ├── index.html.j2
│       ├── _chat_line.j2
│       ├── _sidebar.j2
│       └── _info.j2
└── tests/
    ├── conftest.py         # AgentIRC server fixture
    ├── test_commands.py
    ├── test_render.py
    ├── test_e2e_http.py
    └── test_e2e_playwright.py  # opt-in via marker
```

---

## SSE event types

Five named event types form the entire reactive surface. Each event is `event: <name>\ndata: <html>\n\n`. The data is a pre-rendered HTML fragment unless noted.

| Event | Trigger | Payload | HTMX target |
|---|---|---|---|
| `chat` | New PRIVMSG arrived in the current channel | `_chat_line.j2` fragment | append to `#chat-log` |
| `roster` | Channel joined/parted, WHO refreshed, sidebar entity update | `_sidebar.j2` fragment | swap `#sidebar` |
| `info` | View changed (chat ↔ overview ↔ status), agent selected, channel info refreshed | `_info.j2` fragment | swap `#info` |
| `view` | Server-driven view switch (e.g. `/help`) | JSON `{view: "chat"\|"help"\|"overview"\|"status"}` | JS toggles visibility classes |
| `error` | Transient surfaceable error | `{message: str}` | JS toast |

**Resilience:** the EventBus uses a bounded per-subscriber `asyncio.Queue` (default 256). On overflow, drop oldest and emit a single `error` event indicating dropped events.

---

## Data flows

**Incoming chat:**

```
AgentIRC PRIVMSG
  → IRCTransport._read_loop
  → MessageBuffer.append(ChatMessage)
  → Session.dispatch(msg)
    if msg.channel == self.current_channel:
      fragment = render("_chat_line.j2", msg=msg)
      self.event_bus.publish("chat", fragment)
  → SSE stream sends `event: chat\ndata: <fragment>\n\n`
  → HTMX (htmx-sse extension) appends to #chat-log
```

**User input:**

```
form submit → POST /input  body: {text: "/join #ops"}
  → Session.execute(parse_command(text))
  → IRCTransport.send_raw("JOIN #ops")
  → on JOIN ack from server, Session emits `roster` event
  → HTTP response: 204 No Content
  → HTMX clears the input, SSE drives the visible update
```

The POST handler returns 204 specifically so HTMX does not attempt to swap anything from the response — all visible changes flow through SSE.

---

## Slash commands inherited from console

These come for free by lifting `culture/console/commands.py`. The agent must wire each to a Session method. The list (CommandType → handler):

`CHAT`, `JOIN`, `PART`, `CHANNELS`, `WHO`, `READ`, `SEND`, `OVERVIEW`, `STATUS`, `AGENTS`, `ICON`, `TOPIC`, `KICK`, `INVITE`, `SERVER`, `QUIT`, `HELP`. Plus `START`, `STOP`, `RESTART` — these print a message saying agent management requires culture's CLI (mirrors console behavior).

The Session's query methods (`list_channels`, `who`, `history`) follow the future-based collect-buffer pattern from `culture/console/client.py:206-288`. Reuse that pattern.

---

## DOM contract for Playwright

The building agent must add stable `data-testid` attributes on every interactive element. The Playwright e2e tests and Playwright MCP both depend on these selectors being canonical.

**Required testids:**

- `chat-input` — the text input field
- `chat-submit` — the submit button (if present; an `Enter`-only form is fine, but the button gets a testid)
- `chat-log` — the scrollable chat area
- `chat-line` — each rendered message
- `chat-line-nick` — nick within a chat line
- `chat-line-text` — text within a chat line
- `sidebar` — the sidebar container
- `sidebar-channel` — each channel row (with `data-channel="#name"`)
- `sidebar-entity` — each roster entry (with `data-nick="..."`)
- `info` — the right-pane container
- `view-indicator` — element whose text or `data-view` reflects current view
- `connection-status` — element that shows connected/disconnected state

If the agent adds new interactive elements, they get `data-testid` attributes too. Document them in `docs/playwright.md`.

---

## `--seed` fixture format

To make Playwright tests deterministic without round-tripping through a real AgentIRC server, `--seed <path>` loads a YAML file at startup and stages canned state in the Session before the SSE stream opens.

```yaml
# tests/fixtures/basic.yaml
joined_channels:
  - "#general"
  - "#ops"
preload_messages:
  - channel: "#general"
    nick: "alice"
    text: "hello world"
    timestamp: 1714000000
  - channel: "#general"
    nick: "bob"
    text: "hi alice"
    timestamp: 1714000005
roster:
  - nick: "alice"
    type: "human"
    online: true
  - nick: "bob"
    type: "agent"
    online: true
current_channel: "#general"
```

In seed mode, the IRC connection is still established (the agent must verify the server is reachable), but the initial UI state is overlaid from YAML. This lets Playwright tests start from a known DOM without scripting an entire conversation.

---

## Error handling & lifecycle

**IRC connection loss.** Define `LensConnectionLost(ConnectionError)`. Raised by `IRCTransport.send_raw` on broken pipe. `Session.execute` catches it, emits a `chat` system message ("Connection to AgentIRC lost — restart irc-lens to reconnect"), marks Session unhealthy. SSE stays open so the user sees the message; subsequent `POST /input` returns 503. **No auto-reconnect in v1.**

**Browser disconnect.** SSE generator detects `ConnectionResetError` on write, exits cleanly. Session and IRC connection survive. Refresh re-attaches and renders current state.

**Startup failures.** AgentIRC unreachable → stderr message including host/port + hint, exit 1, aiohttp never starts. Web port already in use → stderr message + hint, exit 1.

**Shutdown.** Ctrl-C → SIGINT handler → close all SSE streams (final `bye` event), send IRC `QUIT`, close TCP, exit 0.

**Logging.** Default: human-readable stderr. With `--log-json`, one JSON object per line on stderr. No log file in v1.

---

## Testing strategy

Three layers:

**1. Unit (`tests/test_commands.py`, `tests/test_render.py`):** pure pytest. No I/O. Asserts `parse_command()` shapes and that fragment rendering produces expected DOM given fixture session state. Coverage target: every `CommandType` and every fragment template.

**2. HTTP e2e (`tests/test_e2e_http.py`):** spins up irc-lens against a real AgentIRC server (fixture). Drives via `aiohttp.ClientSession` — POST /input, read SSE stream, assert event names and fragment content. Fast, deterministic, no browser. This is the primary regression net.

**3. Playwright e2e (`tests/test_e2e_playwright.py`, opt-in via `pytest -m playwright`):** launches chromium, navigates to the local URL, types into `data-testid="chat-input"`, asserts visible `data-testid="chat-line"` elements. Uses `--seed` fixtures for setup. Slower; runs in CI on a separate job.

**AgentIRC server fixture.** Use option (a): pin `culture` as a dev dependency to a commit SHA and import its `tests/conftest.py` server fixture. This keeps zero IRC-server code in `irc-lens` and lets us track upstream protocol changes by bumping the SHA. Document the pinned SHA in `tests/README.md` and `CITATION.md`.

If culture's fixture later stops fitting (e.g., the import surface changes, or culture itself splits), fall back to option (b): extract a minimal AgentIRC test server into `tests/_agentirc_server.py` and update `tests/README.md`. Do not attempt both at once.

---

## Documentation deliverables

The new repo's `docs/` directory must contain at least:

- `docs/cli.md` — every flag, exit code, example invocations
- `docs/slash-commands.md` — full command list with usage
- `docs/sse-events.md` — every SSE event type, payload, fragment template name
- `docs/playwright.md` — how to drive irc-lens with Playwright MCP, with example transcripts
- `docs/architecture.md` — the runtime architecture diagram from this spec, plus module layout
- `README.md` — quickstart, install, link to docs

The repo's CLAUDE.md should mirror culture's structure: project overview, package management, citation pattern, doc conventions, git workflow, testing.

---

## Build sequence

The operational sequence is the 11-phase build plan: [`docs/superpowers/plans/2026-04-27-irc-lens-build-plan.md`](../plans/2026-04-27-irc-lens-build-plan.md). Each phase ends with a verification block; one PR per phase.

Quick map (spec section → plan phase):

| Spec topic | Plan phase |
| --- | --- |
| Repo bootstrap (incl. `overview` global + `cli overview`) | Phase 1 |
| Citation lift from culture | Phase 2 |
| `Session` class | Phase 3 |
| aiohttp app skeleton + index render | Phase 4 |
| SSE event bus wired through `Session` | Phase 5 |
| Render fragments per event type | Phase 6 |
| Browser glue (`lens.js`, `lens.css`) | Phase 7 |
| `--seed` YAML fixture | Phase 8 |
| Tests (unit, HTTP e2e, Playwright opt-in) | Phase 9 |
| `docs/` deliverables | Phase 10 |
| Release workflow (PR → TestPyPI / push → PyPI) | Phase 11 |

Do not skip the documentation phase — agents reading this spec later will rely on those docs.

---

## Non-goals (v1)

Listing these explicitly so the building agent does not scope-creep:

- Multi-tab / multi-user / shared sessions
- Authentication, TLS termination
- Auto-reconnect to AgentIRC
- Reading culture's `~/.culture/server.yaml`
- Daemonization, PID files, systemd units
- IRCv3 message tags
- Mobile layout
- Theming beyond a single CSS file
- WebSocket transport
- Any LLM agent loop (`irc-lens` is a pure client, not an agent)

If a v2 needs any of these, that's a follow-up spec.

---

## Verification

The handover is complete when, on a clean machine:

1. `pip install irc-lens` succeeds.
2. With a culture AgentIRC server running, `irc-lens serve --host localhost --port 6667 --nick test` starts and prints the localhost URL.
3. Opening the URL shows the three-pane layout with the user joined to no channels.
4. Typing `/join #general` in the chat input causes the channel to appear in the sidebar (via SSE `roster` event).
5. Typing `hello` posts a PRIVMSG and the message appears in the chat log.
6. A second user posting from another IRC client appears in the chat log within the SSE delivery latency.
7. `pytest` passes (with Playwright tests opt-in via `-m playwright`).
8. Playwright MCP can navigate to the URL, locate elements by `data-testid`, and drive the same flow.
9. `afi cli verify` reports 22/22 across all six rubric bundles (Structure, Learnability, JSON, Errors, Explain, Overview).
