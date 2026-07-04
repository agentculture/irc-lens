# `irc-lens` architecture

Reference for how the lens is wired internally. Spec: `docs/superpowers/specs/2026-04-27-irc-lens-handover-design.md`.
Per-phase build plan: `docs/superpowers/plans/2026-04-27-irc-lens-build-plan.md`.

## Runtime topology

```text
┌────────────────────┐      ┌──────────────────────┐      ┌──────────────────┐
│ Browser (HTMX+SSE) │ HTTP │ aiohttp web app     │ TCP  │ AgentIRC server  │
│ lens.js/lens.css   │ ◄──► │   GET /             │ ◄──► │ (mesh)           │
│ EventSource        │     │   POST /input       │      │                  │
└────────────────────┘      │   GET /events (SSE) │      └──────────────────┘
                            │   GET /static/*     │
                            │                     │
                            │  ┌────────────────┐ │
                            │  │ Session        │ │
                            │  │ ├─ Transport   │─┘ TCP read loop publishes
                            │  │ ├─ Buffer      │    inbound messages into
                            │  │ ├─ EventBus   ─┼──► subscribed SSE responses
                            │  │ └─ execute()   │
                            │  └────────────────┘
                            └──────────────────────┘
```

* One `Session` per process, owned by the `aiohttp.web.Application`
  (`app["session"]`).
* IRC reads run on the same event loop as the web server — `serve.py`
  uses one `asyncio.run(_serve_async(...))` so the read task survives
  until shutdown.
* Server-rendered HTML fragments are the unit of update over SSE; the
  browser does no client-side templating.

## Module layout

```text
src/irc_lens/
├── __init__.py            # __version__ via importlib.metadata
├── __main__.py            # python -m irc_lens entry point
├── _errors.py             # AfiError (subclasses agentfront.errors.AgentfrontError) + EXIT_*
├── front_docs.py          # purpose-authored agent doc pages (about/console/tools/conventions)
├── tools.py               # the 11 live-verb ephemeral-session tools (send/join/part/...)
├── cli/
│   ├── __init__.py        # build_app() registry assembly + main() dispatch
│   ├── _meta.py           # overview/doctor: the agentfront-reserved-name routing deviation
│   ├── _errors.py         # re-export shim onto irc_lens._errors (stable-contract)
│   ├── _output.py         # stdout/stderr split + --json (stable contract)
│   └── _commands/         # serve / config_cmd / cli_noun / mcp_cmd / tui_cmd —
│                          # each a register_into(app) hook
├── commands.py            # parse_command + verb dictionary (cited)
├── config.py              # LensConfig + resolve_config
├── irc/
│   ├── message.py         # wire-line parser (cited)
│   ├── transport.py       # IRCTransport (cited, with add_listener hook)
│   └── buffer.py          # MessageBuffer (cited, with optional timestamp)
├── session.py             # Session, SessionEventBus, Subscription, dispatch
├── seed.py                # apply_seed / load_seed (Phase 8)
├── web/
│   ├── __init__.py        # public make_app re-export
│   ├── app.py             # Application factory + client_max_size
│   ├── front.py           # WSGI bridge mounting the agentfront HTTP surface under /agent
│   ├── routes.py          # HTTP route handlers
│   ├── auth.py            # Cloudflare Access JWT verification + middleware
│   ├── identity.py        # Identity dataclass + nick derivation
│   ├── sessions.py        # SessionFactory / SessionRegistry (per-principal sessions)
│   ├── media.py           # classify_url + render_message_html + compose_media_message
│   ├── store.py           # MediaStore (blob-file management + token safety)
│   ├── render.py          # Jinja2 env + render_index/render_fragment
│   └── events.py          # format_sse + SessionEvent re-export
├── templates/             # *.html.j2 (index + fragments)
└── static/                # lens.js, lens.css, media.js, vendor/
```

The CLI, MCP, HTTP, and TAUI surfaces are all *rendered* from one
`agentfront.app.App` registry assembled by `cli.build_app()` — see
`CLAUDE.md`'s "Runtime contract" section for how the registry is
assembled and kept from drifting across surfaces. There is no more
`explain/` catalog-resolver module or hand-rolled `_ArgumentParser`
override: `agentfront.cli_surface.run_cli` renders `learn`/`explain`
and per-verb `--json` from the registry, and `cli/_meta.py` derives
`overview`/`doctor` from the same registry via the one documented
deviation (see `CLAUDE.md`). `cli/_errors.py` and `cli/_output.py`
remain as the stable-contract shims they always were; `_errors.py`'s
`AfiError` is now a verbatim subclass of
`agentfront.errors.AgentfrontError`. The IRC transport, message
buffer, and slash-command parser are **cited from `culture@57d3ba8`**
(see `CITATION.md`) — not installed as a dependency. Divergences from
the citation source (the `add_listener` hook on the transport, the
optional `timestamp` kwarg on `MessageBuffer.add`) are tracked in
`CITATION.md`.

The app factory (`make_app` in `web/app.py`) stamps security headers
via a middleware: `Content-Security-Policy` (on HTML documents),
`X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`.
See `docs/security-checklist.md` for the CSP directives and rationale.

## Request shapes

| Route | Verb | Body | Response |
| --- | --- | --- | --- |
| `/` | `GET` | — | 200 HTML |
| `/input` | `POST` | JSON or form-encoded | 204/400/413/503 |
| `/upload` | `POST` | multipart/form-data | 201/400/413/403/404 |
| `/media/{token}.{ext}` | `GET` | — | 200/404 media |
| `/events` | `GET` | — | 200 SSE stream |
| `/static/{path}` | `GET` | — | 200 vendored assets |
| `/agent`, `/agent/{slug}` | `GET` | — | 200 markdown/XML (agentfront HTTP front) |

Details: `/input` accepts JSON or form-encoded `text` field; errors are
bad JSON, oversize, unhealthy. `/upload` media responses include
content with `{url, kind}` for success or error details on type/size
mismatch. `/media/{token}` serves auth-exempt blobs via capability URLs
(128-bit tokens). `/events` uses `text/event-stream` with no-cache.
`/agent` and every `/agent/{slug}` — the index, `llms.txt`,
`sitemap.xml`, `front`, and each purpose-authored doc page — are
ordinary, **non-exempt** routes: unlike `/static`, `/healthz`, and
`/media`, they sit *behind* the same identity middleware as the console
root (dev mode's synthetic identity, or a valid Cloudflare Access JWT in
`cloudflare-access` mode). See `web/front.py` and the decision log entry
below.

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

| Surface | Cap | Source | Overflow behavior |
| --- | --- | --- | --- |
| Subscriber queue | 256 events | `SessionEventBus` | drop-oldest + error |
| `MessageBuffer` | 500 messages | `MessageBuffer` | drop-oldest |
| `POST /input` body | 4 KiB | `routes._MAX_INPUT_BODY` | 413 |
| Chat log DOM | 500 lines | `lens.js` `CHAT_LOG_CAP` | trim oldest |

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

`{code, message, remediation}` is the **CLI** contract — originally
enforced by a retired external verifier binary, now enforced
in-process by `tests/test_front_agreement.py`'s `assert_surfaces_agree`
gate plus the `agentfront`-rendered dispatcher (see the "Why irc-lens
adopted the agentfront runtime" entry below). The spec is silent on
HTTP-error JSON shape, and the chosen `{error, hint}` mirrors the
text-mode CLI rendering (`error: X` / `hint: Y`) without coupling the
two surfaces. Ratified on PR #7 merge after a Qodo pushback; the same
challenger raised it again on PR #12 and was directed back to the
ratification.

### Why exit code 1 vs 2 splits user input vs environment

User-supplied bad input (unreachable AgentIRC endpoint, missing
seed file, malformed YAML) → `EXIT_USER_ERROR (1)`. Environment
failure to act on a resource that exists (port collision,
permission denied while reading a seed file) → `EXIT_ENV_ERROR
(2)`. Canonical precedents in
`src/irc_lens/cli/_commands/serve.py`: the `LensConnectionLost`
branch in `_serve_async` → `1`; the `TCPSite.start()` `OSError`
branch → `2`. Symbol references are used here deliberately —
line numbers rot.

### Why dispatch-table entries stay `async def`

`Session.execute` calls `await handler(parsed)`, so every entry in
`Session._exec_dispatch` must be `async def`. Helpers underneath
(`_switch_view`, etc.) go sync when they have no real async use
case so SonarCloud's S7503 rule clears for the inner body. The
outer `_exec_*` methods accept S7503 with the dispatch-contract
rationale.

### Why media URLs, not inline bytes

AgentIRC's `MAX_INBOUND_LINE` (8192 bytes) rejects inbound lines that
exceed it; even a tiny image as a base64 data URI exceeds that on one
line. The IRCv3 `batch` and `draft/multiline` caps that could carry
inline bytes are explicitly deferred by AgentIRC's accessibility spec.
A short `http(s)` URL in a PRIVMSG needs zero protocol work and leaves
the protocol-side improvements (media tag hint, cross-machine
reachability) as clean, optional follow-ups.

### Why GET /media/ is auth-exempt capability URLs

Auth-gating media would mean only browser humans could view it — agents
have no CF/JWT identity, so the "agents see images" payoff would wait
on sibling-repo credential work. Unguessable-token URLs (128-bit
`secrets.token_urlsafe(16)`) match the IRC trust model: the URL is only
ever disclosed inside a channel, so possession of the URL means you saw
it there. Uploads remain authed. In cloudflare-access mode, Cloudflare
Access still fronts the tunnel in front of everything. Ratified
2026-07-02.

### Why irc-lens adopted the agentfront runtime

Before this adoption, irc-lens's CLI was hand-cited AFI scaffolding
(the `citation-cli`/`afi` `python-cli` reference, copied in at
bootstrap and never re-synced) verified only by an external `afi cli
verify` binary that CI never actually ran —
`tests/test_afi_verify.py` skip-guarded on `shutil.which("afi")`, a
gate that could go dark with nobody noticing. The web console, in
turn, exposed zero machine-readable state surface beyond `/healthz`
and upload receipts: nothing structurally prevented the CLI and the
site from drifting apart, and an agent with only a fetch tool had no
way to discover irc-lens's own capabilities short of reading source.

Agent-first via `agentfront` is the stated AgentCulture org norm
(the `agentfront` repo's own `docs/agentculture.md`: "Agent First in
everything we do... When a design choice trades off between agent
ergonomics... and human UI conventions, the agent side wins"), and
the sibling `colleague` repo had already proved the
import-don't-duplicate migration in practice — `colleague/cli/_app.py`
assembles its CLI from one imported `agentfront.app.App`, with each
verb module contributing a `register_into(app)` hook that `build_app()`
auto-discovers, rather than hand-maintained per-verb scaffolding. irc-lens
is the lens onto the agent mesh; it made no sense for its own surfaces
to be the least agent-legible tool in the org. `irc_lens.cli.build_app()`
follows that exact blueprint (see `CLAUDE.md`'s "Runtime contract").

User-ratified decisions from that adoption:

* **The WSGI agent front mounts *inside* the aiohttp console**, under
  the `/agent` path prefix — one port, one origin, one deployment, no
  second server process to run or monitor. See `web/front.py`.
* **The agent front serves no repo docs.** The GitHub repo is for
  that. The registered doc pages (`front_docs.py`: `about`, `console`,
  `tools`, `conventions`) are fresh prose *about the running tool*,
  addressed to the agent reading them — never a reproduction of
  anything under `docs/` (`tests/test_front_docs.py`'s copy guard
  enforces this at the text level, not just by convention).
* **All four agentfront surfaces ship in v1**: CLI, HTTP front, MCP
  server (`irc-lens mcp`), and TAUI cockpit (`irc-lens tui`) — not a
  partial rollout with some surfaces deferred.
* **The full `CommandType` catalog registers as tools, ephemerally.**
  Every verb wired into `Session._exec_dispatch` (see
  `docs/slash-commands.md`) that makes sense outside a live browser
  tab gets a matching registry tool in `tools.py`. Each tool call
  opens its own throwaway `Session` against the configured AgentIRC
  server, performs exactly one verb, and disconnects — no state
  persists between calls, and read/history fidelity is bounded by
  what the server replays fresh. This was accepted as the v1 tradeoff
  in exchange for zero new daemon/RPC surface; a session-reuse mode
  against the running `serve` process is an explicit follow-up, not
  part of this adoption.
* **The `/agent` prefix sits *behind* Cloudflare Access** in
  `cloudflare-access` mode, exactly like the console root — zero new
  auth exemptions. Anonymous agent discovery of a deployed lens was
  explicitly not a goal; `/static`, `/healthz`, and `/media` remain
  the only exempt paths (see `web/app.py`).

The one deliberate implementation deviation from a pure "just use
agentfront" story is `cli/_meta.py`: `agentfront` 0.20.0 reserves the
`overview` and `doctor` verb names on the `App` but its own stock
implementations produce shapes this repo's rubric already committed to
elsewhere (`{subject, sections}` / `{healthy, checks}`) — richer than
what 0.20.0 ships. Rather than loosen the rubric to match the
dependency's current state, `irc_lens.cli.main()` intercepts those two
verb names and derives their output from the same `App` registry
through a second, narrow render path (`cli/_meta.py`). Every other verb,
including `learn`/`explain`, goes straight through
`agentfront.cli_surface.run_cli`. See `CLAUDE.md`'s "The `cli/_meta.py`
routing deviation" for the full accounting, including the other known
`agentfront` 0.20.0 gaps (doc-slug `explain` resolution, `SCRIPT_NAME`
handling) this repo works around rather than papers over.

The pre-existing `{error, hint}` HTTP contract above is untouched by
any of this — this adoption changed how the CLI/MCP/HTTP/TAUI surfaces
are *built*, not the console's own request/response contracts, its
auth middleware, or the culture-cited domain layer
(`irc/transport.py`, `irc/message.py`, `irc/buffer.py`,
`commands.py`, `CITATION.md`), none of which this adoption touched.
Superseded by this adoption: `tests/test_afi_verify.py` (deleted;
replaced by `tests/test_front_agreement.py`'s unconditional,
in-process `assert_surfaces_agree` gate) and the retired
citation-reference workflow that used to seed the CLI scaffold (no
longer part of this repo's toolchain — see `CLAUDE.md`).

## Vendored frontend assets

`irc-lens` ships HTMX vendored under
`src/irc_lens/static/vendor/`, not loaded from a CDN. The assets
ship in the wheel via `tool.hatch.build.targets.wheel`'s package
include.

| File | Pin | Source |
| --- | --- | --- |
| `htmx.min.js` | `htmx.org@2.0.4` | unpkg.com (HTMX) |
| `sse.js` | `htmx-ext-sse@2.2.2` | unpkg.com (SSE ext) |

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

* [`cli.md`](cli.md) — every flag, exit code, seed schema.
* [`slash-commands.md`](slash-commands.md) — verb table.
* [`sse-events.md`](sse-events.md) — events, fragments, testids.
* [`playwright.md`](playwright.md) — driving with pytest-playwright.
* [`CITATION.md`](../CITATION.md) — culture citations + divergences.
* [Handover design](superpowers/specs/2026-04-27-irc-lens-handover-design.md)
* [Build plan](superpowers/plans/2026-04-27-irc-lens-build-plan.md)

## Deployment modes

irc-lens has two operational modes selected by `auth.mode` in the config:

### `dev` mode

A single `Session` opens at startup against the configured AgentIRC
on `auth.dev.nick`. The identity middleware injects a synthetic
`Identity` for every request. Existing tests run in this mode.

### `cloudflare-access` mode

Each authenticated user gets their own lazy-opened `Session` with a
nick derived from their email's local part (sanitized to
`[a-z0-9-]`). The identity middleware validates the
Cloudflare-issued JWT against the team's JWKS, pins audience and
issuer, and enforces the lens-side allowlist as a second line of
defense behind the Cloudflare Access policy. JWKS is cached
in-process with kid-miss-then-refresh semantics.

The lens never opens an inbound port in CF mode; cloudflared
terminates the tunnel locally and the lens listens on
`127.0.0.1:<web.port>`.
