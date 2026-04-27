# `irc-lens` Build Plan

**Status:** Ready to execute
**Date:** 2026-04-27
**Spec:** [`docs/superpowers/specs/2026-04-27-irc-lens-handover-design.md`](../specs/2026-04-27-irc-lens-handover-design.md)
**Audience:** The agent building this repo. Read the spec first, then this plan, then start at Phase 1.

---

## How to use this plan

The spec defines **WHAT** to build and **WHY**. This plan defines **HOW** and **ORDER**. Each phase is small, independently committable, and ends with a verification block. Do not skip phases or merge them — the verification gates exist so a regression is caught one phase later, not eleven.

If something in this plan disagrees with the spec, the spec wins; flag the discrepancy in your commit message.

If something in this plan disagrees with `CLAUDE.md` or with the AFI rubric (`afi cli verify`), `CLAUDE.md` and the rubric win — they encode contracts the spec was written against, not optional advice.

---

## Prerequisites

Before Phase 1:

- `culture` is checked out at `../culture/` relative to this repo. The citation-source files live there. Pin to the commit SHA you cite from; record it in `CITATION.md` (Phase 2).
- `citation-cli` (`afi`) is on `PATH`. Verify with `afi --help`. If missing, install per `../citation-cli/README.md` before continuing — Phase 1 depends on it.
- `uv` ≥ 0.5 installed.
- Python 3.11+.
- A reachable AgentIRC server for end-to-end testing in Phase 9. The simplest option: `cd ../culture && culture server start --name local && culture server status local` to get a host/port. Tear it down at the end with `culture server stop local`.
- `chromium` installed for Playwright (`uv run playwright install chromium` after deps land in Phase 1).

---

## Phase 1 — Repo bootstrap

**Goal:** A working `pip install -e .` and `irc-lens learn` that exits 0 with rubric-passing output.

1. Run `afi cli cite python-cli` from the repo root. This populates `.afi/reference/python-cli/`. **Read `MANIFEST.json` and `AGENT.md` before writing any CLI code.**
2. Substitute tokens (`{{project_name}}` → `irc-lens`, `{{slug}}`/`{{module}}` → `irc_lens`) and copy `stable-contract` files verbatim into `src/irc_lens/`:
   - `cli/_errors.py` (`AfiError`, exit codes)
   - `cli/_output.py` (stdout/stderr split, `--json` helpers)
   - `cli/_commands/explain.py`
   - `explain/` (catalog resolver)
3. Shape-adapt `cli/__init__.py` (parser + `_dispatch`, including `_ArgumentParser` override and try/except wrapping `AfiError`), `cli/_commands/learn.py` (TEXT body + JSON payload mentioning purpose, commands, exit codes, `--json`, `explain`), `explain/catalog.py`, package `__init__.py` / `__main__.py`, and `tests/test_cli.py` per `MANIFEST.json`.
4. Write `pyproject.toml`:
   - `[project]` with name `irc-lens`, version `0.1.0`, Python 3.11+
   - `[project.scripts]` registers `irc-lens = "irc_lens.cli:main"`
   - Runtime deps: `aiohttp`, `jinja2`, `pyyaml`
   - Dev deps: `pytest`, `pytest-asyncio`, `pytest-aiohttp`, `playwright`, `pytest-playwright`
5. Write a minimal CI skeleton at `.github/workflows/ci.yml`: `uv sync`, `pytest`, `afi cli verify`. Skip the Playwright job for now (added in Phase 9).
6. Write `CITATION.md` with a placeholder block for culture's source SHA (filled in Phase 2).

**Verification:**

```bash
uv venv && uv pip install -e ".[dev]"
irc-lens --help                # exits 0, lists global verbs (learn, explain)
irc-lens learn                  # exits 0, stdout ≥ 200 chars, mentions all 5 rubric items
irc-lens learn --json | jq .    # parseable JSON, stderr clean
irc-lens nope                   # exits non-zero, stderr has 'error:' and 'hint:', no Python traceback
afi cli verify                  # all five rubric bundles pass
```

**Commit:** "phase 1: bootstrap repo with afi cli cite python-cli pattern"

---

## Phase 2 — Citation lift from culture

**Goal:** The IRC transport, message buffer, and command parser from culture are present, tagged with their source SHA, and importable in `irc-lens`.

1. Capture the source SHA: `git -C ../culture rev-parse HEAD`. Record it in `CITATION.md` under a "Sources" section.
2. Copy and adapt per the spec's citation table:
   - `../culture/packages/agent-harness/irc_transport.py` → `src/irc_lens/irc/transport.py`. Strip `CAP REQ :message-tags` (the lens doesn't render IRCv3 tags — see `culture/console/client.py:50-55` for precedent). Keep the persistent-connection + read-loop shape.
   - `../culture/packages/agent-harness/message_buffer.py` → `src/irc_lens/irc/buffer.py`. Use as-is unless an import-time conflict surfaces.
   - `../culture/culture/console/commands.py` → `src/irc_lens/commands.py`. Lift wholesale.
3. Add `src/irc_lens/irc/__init__.py` re-exporting the public symbols.
4. Adjust imports inside the cited files to use the new module paths. Do **not** introduce abstractions; the goal is byte-faithful citation plus minimum-viable rewiring.

**Verification:**

```bash
uv run python -c "from irc_lens.irc.transport import IRCTransport; print(IRCTransport)"
uv run python -c "from irc_lens.irc.buffer import MessageBuffer; print(MessageBuffer)"
uv run python -c "from irc_lens.commands import parse_command, CommandType; print(parse_command('/join #ops'))"
afi cli verify                  # still passing
```

**Commit:** "phase 2: cite irc_transport, message_buffer, commands from culture@<sha>" (one commit, or one per file — the agent's call).

---

## Phase 3 — Session class

**Goal:** A `Session` object that owns the IRC connection and can answer all the queries the spec's slash commands need.

1. Create `src/irc_lens/session.py` with a `Session` class that:
   - Constructs an `IRCTransport` and `MessageBuffer`.
   - Exposes `connect()`, `disconnect()`, `send_privmsg(target, text)`, `join(ch)`, `part(ch)`, `send_raw(line)`.
   - Implements `list_channels()`, `who(target)`, `history(channel, limit)` using the **future-based collect-buffer pattern** from `../culture/culture/console/client.py:206-288`. Reuse the pattern shape; do **not** import the file.
   - Maintains view state: `current_channel: str`, `joined_channels: set[str]`, `view: Literal["chat","help","overview","status"]`, `roster: list[EntityItem]`.
   - Defines `LensConnectionLost(ConnectionError)` and raises it when `send_raw` fails.
2. Define `SessionEvent` dataclass and `SessionEventBus` with bounded `asyncio.Queue` (default 256, drop-oldest with overflow `error` event) — but only the bus interface; SSE wiring lands in Phase 5.
3. Add `tests/test_session_unit.py` for state transitions that don't require a live server (e.g., `current_channel` updates, `joined_channels` after `join`/`part` no-op when not connected).

**Verification:**

```bash
uv run pytest tests/test_session_unit.py -v
```

**Commit:** "phase 3: Session class with future-based query methods"

---

## Phase 4 — aiohttp app skeleton

**Goal:** A static index page renders, returns 200, has all required `data-testid` attributes.

1. Create `src/irc_lens/web/app.py` with an aiohttp `Application` factory that takes a `Session` and registers routes.
2. Create `src/irc_lens/web/routes.py`:
   - `GET /` → render `templates/index.html.j2` with the current Session state. Stable `data-testid` attributes on every element listed in the spec's DOM contract section.
   - `POST /input` → stub returning `204 No Content`. Wired in Phase 5.
   - `GET /events` → SSE stub that emits one `chat` event saying "irc-lens online" then closes. Wired properly in Phase 5.
   - `GET /static/{path}` → static file handler for `lens.css`, `lens.js`.
3. Create `src/irc_lens/web/render.py` with the Jinja2 environment and a `render_fragment(template, **ctx)` helper.
4. Create `templates/index.html.j2` with the three-pane layout (sidebar, chat, info) and the SSE listener `<script>` tag (HTMX vendored or CDN — document the choice in `docs/architecture.md`).
5. Add `_chat_line.j2`, `_sidebar.j2`, `_info.j2` as empty placeholders rendered with current state.

**Verification:**

```bash
# Manual: in one shell
irc-lens serve --host 127.0.0.1 --port 6667 --nick test --web-port 8765 &
# In another shell
curl -fsS http://127.0.0.1:8765/                  # returns HTML with all required data-testid attrs
curl -fsS -I http://127.0.0.1:8765/static/lens.css # 200 OK
curl -fsS -N http://127.0.0.1:8765/events         # one SSE event, then EOF
kill %1
```

(If no AgentIRC server is running, `serve` will fail-fast at `Session.connect`; that is expected and tested in Phase 9. For Phase 4 verification, point at any reachable IRC server or stub the connect call.)

**Commit:** "phase 4: aiohttp app skeleton with index + SSE stub + static"

---

## Phase 5 — Wire the SSE event bus

**Goal:** Real-time updates work. Receiving an IRC PRIVMSG triggers a `chat` SSE event in an open browser stream.

1. In `src/irc_lens/web/events.py`, finalize `SessionEvent` (`name: Literal["chat","roster","info","view","error"]`, `data: str`) and SSE serialization (`event: <name>\ndata: <data>\n\n`).
2. `SessionEventBus.subscribe()` returns an `AsyncIterator[SessionEvent]` backed by a per-subscriber queue. On overflow, drop oldest and emit a one-shot `error` event with payload `{"message": "events dropped"}`.
3. Rewrite `GET /events` to subscribe and stream until client disconnect (`ConnectionResetError`). On disconnect, unsubscribe and unwind cleanly.
4. In `Session.dispatch(msg)`, when `msg.command == "PRIVMSG"` and the channel matches `current_channel`, render `_chat_line.j2` and publish a `chat` event.
5. In `routes.py`, `POST /input` parses the body via `parse_command()`, dispatches via `Session.execute()`, returns `204`. On `LensConnectionLost`, return `503`.
6. `Session.execute()` for `JOIN`/`PART` updates `joined_channels`, then publishes a `roster` event with re-rendered `_sidebar.j2`.

**Verification:**

```bash
# Terminal 1
irc-lens serve --host <real> --port <real> --nick lens-test --web-port 8765
# Terminal 2 — open SSE stream
curl -fsS -N http://127.0.0.1:8765/events &
# Terminal 3 — submit /join
curl -fsS -X POST -d '{"text":"/join #general"}' -H "Content-Type: application/json" \
     http://127.0.0.1:8765/input
# Terminal 2 should receive a `roster` event within 100ms
```

**Commit:** "phase 5: wire SSE event bus + POST /input through Session"

---

## Phase 6 — Render fragments

**Goal:** Each SSE event type produces a real, populated fragment that HTMX swaps correctly.

For each event type in the spec's table (`chat`, `roster`, `info`, `view`, `error`), in this order:

1. Flesh out the corresponding template (`_chat_line.j2`, `_sidebar.j2`, `_info.j2`; `view` and `error` are JSON-payload, not templates).
2. Wire the trigger point in `Session` and confirm the fragment renders correctly via `tests/test_render.py` (added properly in Phase 9 unit-tests; for now, ad hoc).
3. Confirm `data-testid` attributes from the DOM contract are present.

One commit per event type is fine; combine `view` + `error` into one commit since they're JSON-only.

**Verification:** open the page in a real browser, type `/join #general`, observe the sidebar update without page reload. Type a message — observe it append to `#chat-log`.

**Commit:** four commits, one per fragment template (plus the `view`+`error` JSON commit).

---

## Phase 7 — Browser glue (`lens.js` + `lens.css`)

**Goal:** ≤ 50 lines of JS that wire SSE to HTMX and toggle view classes on `view` events.

1. `lens.js`:
   - On page load, open `EventSource('/events')`.
   - Listen for `chat` events → `htmx.process(...)` after appending the fragment to `#chat-log`.
   - Listen for `roster`, `info` events → `outerHTML`-swap the targeted element.
   - Listen for `view` events → toggle `data-view` on `<body>`; CSS handles visibility.
   - Listen for `error` events → render a transient toast in `#toast-region`.
2. `lens.css`: bare-minimum three-pane grid layout. No theming. Reuse colors from culture's TUI where reasonable for visual continuity.
3. Form submission: HTMX `hx-post="/input"` on the `<form id="chat-form">`. On 204, clear the input box. On 503, surface a toast via the `error` listener (the server can also emit an `error` SSE event, choose one path and document).

**Verification:** load `/`, type `/help`, see help fragment in `#info`. Type chat text, see it appear in `#chat-log`. Resize the window — three panes reflow.

**Commit:** "phase 7: browser SSE/HTMX glue"

---

## Phase 8 — `--seed` flag

**Goal:** `irc-lens serve --seed tests/fixtures/basic.yaml` starts with deterministic UI state.

1. Add YAML loading in `cli.py`. Schema matches the spec's `--seed` example exactly.
2. After `Session.connect()` succeeds, if `--seed` is set, load the YAML and overlay it onto Session state (`joined_channels`, `current_channel`, `roster`, `_message_buffer`).
3. Validate the YAML schema; on shape errors, raise `AfiError` with a remediation hint (rubric requires `hint:` line on errors).
4. Document the schema in `docs/cli.md` and `docs/playwright.md`.

**Verification:**

```bash
irc-lens serve --host <real> --port <real> --nick lens-test --seed tests/fixtures/basic.yaml --web-port 8765 &
curl -fsS http://127.0.0.1:8765/ | grep "data-testid=\"chat-line\"" | wc -l   # ≥ 2 from the seed
kill %1
```

**Commit:** "phase 8: --seed YAML fixture"

---

## Phase 9 — Tests

Three layers, three commits:

### 9a — Unit tests

- `tests/test_commands.py` — every `CommandType` round-trips through `parse_command`. Coverage target: 100% on `commands.py`.
- `tests/test_render.py` — every fragment template renders correctly given fixture session state. Coverage target: every branch in each template.
- `tests/test_session_unit.py` — extend Phase 3 tests to cover all view-state transitions.

**Commit:** "phase 9a: unit tests for commands, render, session state"

### 9b — HTTP e2e

- `tests/conftest.py` — fixture that spawns a real AgentIRC server on a random port. Two paths per the spec:
  - **Recommended:** import the fixture from `culture` as a dev dep pinned to a SHA. Pros: zero IRC code in this repo's tests. Cons: requires `culture` source available at test time.
  - **Fallback:** copy a thin AgentIRC test server into `tests/_agentirc_server.py`. More work; cleaner boundary. Document the choice in `tests/README.md`.
- `tests/test_e2e_http.py` — drives `irc-lens` via `aiohttp.ClientSession`. Asserts:
  - `GET /` returns 200 with all required `data-testid` attrs
  - `POST /input` with `/join #x` triggers a `roster` SSE event within 1s
  - `POST /input` with chat text causes the message to appear in the channel via the test server
  - `LensConnectionLost` produces a 503 from `POST /input`

**Commit:** "phase 9b: HTTP e2e tests"

### 9c — Playwright e2e

- Configure `pytest -m playwright` opt-in marker.
- `tests/test_e2e_playwright.py` — uses `--seed` fixture for determinism. Asserts:
  - The seeded chat lines render with correct `data-testid="chat-line"` count
  - Typing into `data-testid="chat-input"` and submitting triggers a new `chat-line`
  - Clicking a `data-testid="sidebar-channel"` updates the active channel and triggers an `info` swap
- Add a separate CI job for Playwright (skipped on the default job).

**Commit:** "phase 9c: Playwright e2e tests (opt-in)"

---

## Phase 10 — Documentation deliverables

The new repo's `docs/` must contain at least:

- `docs/cli.md` — every flag, exit code, example invocations, the `--seed` schema with annotations.
- `docs/slash-commands.md` — full inherited command list with usage, example I/O.
- `docs/sse-events.md` — every SSE event type, payload shape, fragment template name, HTMX target.
- `docs/playwright.md` — how to drive `irc-lens` with Playwright MCP. Include a worked transcript: launch `irc-lens serve --seed ... &`, navigate to URL, locate elements by `data-testid`, drive the chat flow.
- `docs/architecture.md` — runtime architecture diagram from the spec, module layout, decision log (especially "why SSE not WS").
- `README.md` — quickstart (one paragraph), install (one line), link to `docs/`. Keep the file < 50 lines; depth lives in `docs/`.

**Verification:** every link in every doc resolves. `docs/cli.md` enumerates every flag in `cli.py`. `docs/sse-events.md` enumerates every event published by `Session`.

**Commit:** "phase 10: docs deliverables"

---

## Phase 11 — Release workflow

**Goal:** Tagging `v0.1.0` produces a wheel on PyPI.

1. Add `.github/workflows/release.yml`: triggers on `tags: v*`. Builds with `uv build`, publishes with `pypa/gh-action-pypi-publish` using a PyPI Trusted Publisher (no long-lived token in the repo).
2. Tag `v0.1.0` and push. Confirm the release lands on PyPI.
3. Update `README.md` install line to `pip install irc-lens`.

**Commit:** "phase 11: release workflow + first release"

---

## Done criteria

The build is complete when the spec's verification section passes verbatim:

1. `pip install irc-lens` succeeds (Phase 11).
2. `irc-lens serve --host localhost --port 6667 --nick test` starts and prints the localhost URL (Phase 4 + Phase 5).
3. The three-pane layout renders with no joined channels (Phase 4).
4. `/join #general` makes the channel appear in the sidebar via SSE `roster` event (Phase 5).
5. Typing `hello` posts a PRIVMSG and the message appears in the chat log (Phase 5).
6. A second IRC client posting in the same channel appears in the chat log within SSE delivery latency (Phase 5).
7. `pytest` passes (Phase 9a/9b); Playwright tests opt-in via `-m playwright` (Phase 9c).
8. Playwright MCP can navigate to the URL, locate elements by `data-testid`, drive the same flow (Phase 9c).
9. `afi cli verify` passes throughout — every phase ends with this still green (Phases 1–11).

Open a PR per phase if the user prefers small reviews, or batch into 3–4 PRs (`bootstrap`, `core`, `tests`, `docs+release`) if they prefer fewer. Default to small PRs unless told otherwise.
