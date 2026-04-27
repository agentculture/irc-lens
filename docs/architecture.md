# `irc-lens` architecture

Reference for how the lens is wired internally. Spec: `docs/superpowers/specs/2026-04-27-irc-lens-handover-design.md`.
Per-phase build plan: `docs/superpowers/plans/2026-04-27-irc-lens-build-plan.md`.

## Runtime topology

```
┌──────────────────────┐      ┌────────────────────────────┐      ┌──────────────────┐
│ Browser (HTMX + SSE) │ HTTP │ aiohttp.web.Application    │ TCP  │ AgentIRC server  │
│ lens.js + lens.css   │ ◄──► │   GET /                    │ ◄──► │ (culture mesh)   │
│ EventSource("/events")│     │   POST /input              │      │                  │
└──────────────────────┘      │   GET /events  (SSE)       │      └──────────────────┘
                              │   GET /static/*            │
                              │                            │
                              │  ┌──────────────────────┐  │
                              │  │  Session             │  │
                              │  │  ├─ IRCTransport     │──┘ TCP read loop publishes
                              │  │  ├─ MessageBuffer    │    inbound messages into
                              │  │  ├─ SessionEventBus ─┼──► subscribed SSE responses
                              │  │  └─ execute()        │
                              │  └──────────────────────┘
                              └────────────────────────────┘
```

* One `Session` per process, owned by the `aiohttp.web.Application`
  (`app["session"]`).
* IRC reads run on the same event loop as the web server — `serve.py`
  uses one `asyncio.run(_serve_async(...))` so the read task survives
  until shutdown.
* Server-rendered HTML fragments are the unit of update over SSE; the
  browser does no client-side templating.

## Module layout

```
src/irc_lens/
├── __init__.py            # __version__ via importlib.metadata
├── __main__.py            # python -m irc_lens entry point
├── cli/
│   ├── __init__.py        # parser + _dispatch + _ArgumentParser override
│   ├── _errors.py         # AfiError + EXIT_* (stable contract)
│   ├── _output.py         # stdout/stderr split + --json (stable contract)
│   └── _commands/         # learn / explain / overview / serve
├── commands.py            # parse_command + verb dictionary (cited)
├── irc/
│   ├── transport.py       # IRCTransport (cited, with add_listener hook)
│   └── buffer.py          # MessageBuffer (cited, with optional timestamp)
├── session.py             # Session, SessionEventBus, Subscription, dispatch
├── seed.py                # apply_seed / load_seed (Phase 8)
├── web/
│   ├── __init__.py        # public make_app re-export
│   ├── app.py             # Application factory + client_max_size
│   ├── routes.py          # get_index / post_input / get_events
│   ├── render.py          # Jinja2 env + render_index/render_fragment
│   └── events.py          # format_sse + SessionEvent re-export
├── templates/             # *.html.j2 (index + fragments)
└── static/                # lens.js, lens.css, vendor/
```

The CLI scaffold (`cli/_errors.py`, `cli/_output.py`, the dispatcher,
the `learn` / `explain` commands, and the `_ArgumentParser` override)
came from the AFI `python-cli` reference at bootstrap; see
`CLAUDE.md` for the citation source and rubric contracts. The IRC
transport, message buffer, and slash-command parser are **cited
from `culture@57d3ba8`** (see `CITATION.md`) — not installed as a
dependency. Divergences from the citation source (the
`add_listener` hook on the transport, the optional `timestamp`
kwarg on `MessageBuffer.add`) are tracked in `CITATION.md`.

## Request shapes

| Route | Verb | Body | Response |
| --- | --- | --- | --- |
| `/` | `GET` | — | 200 HTML (`render_index`). |
| `/input` | `POST` | JSON `{"text": "..."}` *or* form-encoded `text=...` | 204 success, 400 bad JSON, 413 oversize, 503 unhealthy. |
| `/events` | `GET` | — | 200 SSE stream (`text/event-stream`, `Cache-Control: no-store`). |
| `/static/{path}` | `GET` | — | Vendored assets + `lens.js` / `lens.css`. |

`POST /input` content-negotiates: `application/json` triggers JSON
parsing, anything else (including HTMX's default
`application/x-www-form-urlencoded`) reads the `text` field via
`request.post()`.

## Event flow

A typical `/join #general` round-trip:

1. Browser submits `<form>` → HTMX POSTs `text=/join+%23general` to
   `/input`.
2. `post_input` reads the body, runs `parse_command`, calls
   `await session.execute(parsed)`.
3. `Session._exec_join` validates the channel name, calls
   `Session.join` (which sends `JOIN #general` over the wire),
   updates state, then `_publish_roster()` + `_publish_info()`.
4. Each `publish` enqueues a `SessionEvent` on every subscriber's
   bounded queue.
5. The open `GET /events` response drains its `Subscription` and
   writes `format_sse(event)` bytes.
6. `lens.js`'s `EventSource` listener swaps `#sidebar` / `#info`
   innerHTML.

Inbound traffic uses the transport's per-command listener list:
`Session.connect` registers `Session.dispatch` for `PRIVMSG`,
`JOIN`, and `PART`. The transport's primary handler still runs
first (buffer-add, etc.); the listener emits the user-visible SSE
event.

## Backpressure + bounded memory

Several caps keep a long session deterministic:

| Surface | Cap | Source | Behaviour on overflow |
| --- | --- | --- | --- |
| Subscriber queue | 256 events | `SessionEventBus` | drop-oldest + single-shot `error: events dropped` |
| `MessageBuffer` | 500 messages per channel | `MessageBuffer` | drop-oldest |
| `POST /input` body | 4 KiB | `routes._MAX_INPUT_BODY` + `client_max_size` | 413 |
| Browser chat log | 500 `<div data-testid="chat-line">` nodes | `lens.js` `CHAT_LOG_CAP` | trim oldest |

The browser cap mirrors the server cap so a long session can't grow
unbounded DOM even if every message is rendered.

## Decision log

### Why SSE, not WebSockets

The lens needs *server → browser* updates only; the input flow is
plain HTTP `POST`. SSE is one-way over a long-lived HTTP response,
auto-reconnects in the standard library (`EventSource`), and works
through every middlebox that allows HTTP/1.1 chunked responses.
WebSockets would add a second wire format, a second middlebox class
to debug, and zero capability we'd actually use.

### Why HTMX + server-rendered fragments

The whole point of v1 is that a Playwright agent can drive the UI
deterministically. Server-rendered fragments mean every state
change is verifiable from the wire (e.g. `tests/test_e2e_http.py`
asserts on the rendered HTML fragments without a browser). HTMX is
a thin attribute layer on top of stable HTML — no transpile step,
no virtual DOM, no per-request hydration mismatch.

### Why vendor HTMX instead of a CDN

The lens runs on localhost, drives Playwright in offline-friendly
agent loops, and must boot deterministically without outbound
network. Vendored assets live under
`src/irc_lens/static/vendor/` and ship in the wheel. Refreshing
them is a one-line `curl` per pin (see [Vendored frontend
assets](#vendored-frontend-assets)).

### Why cite-don't-import from culture

Importing `culture` as a dev dep would pull in
`culture.bots.virtual_client`, `culture.constants`,
`culture.telemetry`, and the entire `culture.protocol` /
`culture.agentirc` graph for three reused modules. We need
`IRCTransport`, `MessageBuffer`, and the parser table — nothing
else. Citing keeps the dep graph tight and lets us diverge
deliberately (e.g. the `add_listener` hook on the transport).
Divergences are tracked in `CITATION.md`.

### Why an in-tree AgentIRC test server

Phase 9b needs an IRC peer for HTTP e2e tests. Pulling culture as
a dev dep was the spec's first option; rejected for the same
footprint reason as the citation choice. The thin
`tests/_agentirc_server.py` (~145 lines) accepts the connection,
echoes JOIN/PART, records every line, and ships zero IRC protocol
semantics beyond what the lens's read loop demands. Same fixture
stack powers the Playwright suite (Phase 9c).

### Why `--seed` overlays state on a real connection

Spec line 261: even seed mode must verify the AgentIRC server is
reachable. Headless rendering of preloaded chat lines is a
side-effect, not the core contract. `--seed` is the determinism
switch for tests and demos; the connection is the trust boundary.

### Why HTTP error JSON is `{error, hint}` and not `{code, message, remediation}`

`{code, message, remediation}` is the **CLI** contract enforced by
`afi cli verify`. The spec is silent on HTTP-error JSON shape, and
the chosen `{error, hint}` mirrors the text-mode CLI rendering
(`error: X` / `hint: Y`) without coupling the two surfaces.
Ratified on PR #7 merge after a Qodo pushback; the same
challenger raised it again on PR #12 and was directed back to the
ratification.

### Why exit code 1 vs 2 splits user input vs environment

User-supplied bad input (unreachable AgentIRC endpoint, missing
seed file, malformed YAML) → `EXIT_USER_ERROR (1)`. Environment
failure to act on a resource that exists (port collision,
permission denied while reading a seed file) → `EXIT_ENV_ERROR
(2)`. Canonical precedents: `serve.py:107` (`LensConnectionLost`
→ `1`) vs `serve.py:154` (port-bind `OSError` → `2`).

### Why dispatch-table entries stay `async def`

`Session.execute` calls `await handler(parsed)`, so every entry in
`Session._exec_dispatch` must be `async def`. Helpers underneath
(`_switch_view`, etc.) go sync when they have no real async use
case so SonarCloud's S7503 rule clears for the inner body. The
outer `_exec_*` methods accept S7503 with the dispatch-contract
rationale.

## Vendored frontend assets

`irc-lens` ships HTMX vendored under
`src/irc_lens/static/vendor/`, not loaded from a CDN. The assets
ship in the wheel via `tool.hatch.build.targets.wheel`'s package
include.

| File | Pin | Source |
| --- | --- | --- |
| `htmx.min.js` | `htmx.org@2.0.4` | `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` |
| `sse.js` | `htmx-ext-sse@2.2.2` | `https://unpkg.com/htmx-ext-sse@2.2.2/sse.js` |

To refresh:

```bash
curl -fsSL https://unpkg.com/htmx.org@<VERSION>/dist/htmx.min.js \
  -o src/irc_lens/static/vendor/htmx.min.js
curl -fsSL https://unpkg.com/htmx-ext-sse@<VERSION>/sse.js \
  -o src/irc_lens/static/vendor/sse.js
```

…and update the version pins in this table. Don't bump versions
without verifying the SSE event-listener API still matches what
`src/irc_lens/static/lens.js` expects.

## Further reading

* [`docs/cli.md`](cli.md) — every flag, exit code, the seed schema.
* [`docs/slash-commands.md`](slash-commands.md) — verb table.
* [`docs/sse-events.md`](sse-events.md) — every event, fragment, testid.
* [`docs/playwright.md`](playwright.md) — driving the lens with
  pytest-playwright or Playwright MCP.
* [`CITATION.md`](../CITATION.md) — culture citations + divergences.
* [`docs/superpowers/specs/2026-04-27-irc-lens-handover-design.md`](superpowers/specs/2026-04-27-irc-lens-handover-design.md) — spec.
* [`docs/superpowers/plans/2026-04-27-irc-lens-build-plan.md`](superpowers/plans/2026-04-27-irc-lens-build-plan.md) — build plan.
