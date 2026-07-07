# residents presence page

> The irc-lens console now has a residents page: one glance shows every mesh
> resident's presence state, current task, and token spend — rendered from
> culture's live resource view, behind the same CF Access as the rest of the
> console, and degrading to a clear notice (never an error page) when presence
> or the mesh is unavailable.

## Audience

- The operator viewing the console at chat.agentculture.org — a human behind CF
  Access, not agents (agents read presence via culture's own surfaces)

## Before → After

- Before: Culture tracks resident presence (six-state signal + token counters
  over the PRESENCE verb) and serves an aggregated resource view, but the
  console operator has no way to see it; the 2026-07-03 incident showed the
  console 500ing when the IRCd is down
- After: A residents page renders one row per resident — nick, server, state,
  since, current-task hint, tokens in/out, budget % with warning highlight, and
  a clear presumed-hung flag — sorted by nick

## Why it matters

- One glance tells the operator who is busy, hung, or burning budget — culture
  presence v1 is observe-only, so the console page IS the operator-facing
  deliverable of the whole feature (culture plan task t8)

## Requirements

- Data source is culture's GET /residents.json (served by 'culture mesh overview
  --serve', loopback-only bind by default), consumed SERVER-SIDE by irc-lens so
  the endpoint never needs public exposure; the browser talks only to the
  console
  - honesty: The rendered HTML never references the upstream URL or port; only
    the console origin is visible to the browser
- Payload contract is byte-identical to 'culture residents --json' (culture's
  canonical serializer); nullable fields (task, tokens_in/out,
  token_budget/budget_used_pct/budget_warning) render as dashes, never as errors
  - honesty: A test renders a resident with every nullable field null and
    asserts dashes with no error, and asserts rows are sorted by nick
- Graceful degrade, the hard requirement: three states render HTTP 200 with a
  notice, never a 500 — supported:false → 'presence pending agentirc release'
  notice; endpoint answers 503 → 'IRCd down' notice; endpoint unreachable →
  'resource view unavailable' notice
  - honesty: An automated test drives all three degrade states (supported:false,
    upstream 503, endpoint unreachable) end-to-end through GET /residents and
    asserts HTTP 200 plus each state's specific notice text
- Upstream failure bodies are structured per culture's contract: 503 (server
  unreachable or presence stream stalled) and 500 (internal) both carry
  culture's {code, message, remediation} JSON with Content-Type
  application/json; supported:false is a 200 'known mesh state', never an error
  (docs/resident-presence.md, Response cases)
  - honesty: The fetch layer distinguishes 200-supported, 200-unsupported,
    structured 503, and any other failure, mapping each to its specific notice;
    a malformed 503 body still degrades to the notice rather than raising
- Any failure to obtain a valid supported payload — connection refused, timeout,
  upstream 503 or 500, non-JSON body — renders the same 200-with-notice degrade
  path; the page never proxies the upstream status code to the browser
  - honesty: A test feeds each failure mode (connection refused, timeout,
    upstream 503, upstream 500, non-JSON body) through GET /residents and
    asserts the response is 200 in every case, with the upstream status code
    appearing nowhere in the body
- The server-side fetch reuses the repo's one existing outbound-HTTP pattern
  (aiohttp ClientSession, ClientTimeout(total=5), catching both
  aiohttp.ClientError and asyncio.TimeoutError) as established in web/auth.py's
  JWKS refresh
  - honesty: The residents fetch sets ClientTimeout(total=5) and its except
    clause names both aiohttp.ClientError and asyncio.TimeoutError — verifiable
    by inspection in the PR diff
- GET /residents stays OFF both auth-middleware exempt lists (dev middleware in
  web/app.py and CF middleware in web/auth.py) so Cloudflare Access applies to
  it exactly as to the console root
  - honesty: Neither middleware exempt list (dev in web/app.py, CF in
    web/auth.py) mentions /residents, and a test proves an unauthenticated
    request to it is rejected in auth-enforcing mode
- Tests use an in-tree fake culture endpoint server (the tests/_jwks_server.py
  idiom) covering: supported payload with rows, supported:false, upstream 503,
  endpoint unreachable, and an all-nullables resident row
  - honesty: The fake culture endpoint server lives under tests/ with no network
    egress, and each listed scenario is a distinct test case running in 'uv run
    pytest -q'
- Config lands as a new optional 'culture:' section plumbed through LensConfig,
  a section validator, load_config, both test config literals (tests/helpers.py
  and tests/conftest.py), and the 'config init' starter template
  - honesty: 'config init' writes a starter containing the culture: section,
    load_config accepts and validates it (including rejecting malformed values),
    and both test config literals carry the new fields
- Ship mechanics: architecture.md route-table row + decision-log entry (why
  server-side fetch), docs/cli.md config section, CHANGELOG entry, minor version
  bump, and a reply on issue #53 naming the irc-lens release so culture can note
  it in the rollout
  - honesty: The PR diff contains the architecture.md route row and decision-log
    entry, the docs/cli.md config section, a CHANGELOG entry, and the version
    bump; the issue #53 reply is posted once the release ships

## Honesty conditions

- A reviewer behind CF Access can load /residents on the deployed console and
  always gets a table or a notice — never an error page — matching the
  announcement's promise
- In cloudflare-access mode, GET /residents without a valid CF JWT is rejected
  by the middleware exactly like the console root — proving the operator-only
  audience
- Before this change GET /residents is a 404 on the console — the route table in
  docs/architecture.md gains the row in this same PR
- A rendering test asserts every promised column (nick, server, state, since,
  task, tokens in/out, budget percent with warning highlight, presumed-hung
  flag) appears for a fully-populated resident row
- Rows with budget_warning=true or presumed_hung=true are visually distinguished
  in the rendered HTML (warning highlight, hung flag) and a test asserts the
  distinguishing markup
- The page registers only a GET route and issues only GET requests upstream — no
  mutation path exists anywhere in the feature
- The degrade-state, sorting, and dash-rendering tests run in CI via 'uv run
  pytest -q'; replying on issue #53 with the release is an explicit step in the
  ship checklist

## Success signals

- GET /residents returns 200 with the correct specific notice in each of the
  three degrade states, and with a live payload renders rows sorted by nick with
  dashes for every null — verified by tests against a fake culture endpoint;
  culture notes the shipping irc-lens release on issue #53

## Scope / boundaries

- Read-only page: no actions, no enforcement (culture v1 is observe-only by
  design); CF Access like every other console page, no new auth surface

## Non-goals

- Auto-refresh is a nice-to-have, explicitly not required for v1

## Assumptions

- The overview server exposes no port flag: 'culture mesh overview --serve'
  binds 127.0.0.1 on an OS-assigned ephemeral port and writes the actual port to
  ~/.culture/pids/overview-{server_name}.port (verified in culture
  feat/resident-presence-v1: renderer_web.py serve_web, pidfile.py) — so the
  endpoint's port changes on every overview restart and discovery must cope

## Decisions

- /residents is a standalone authed aiohttp route rendering a full server-side
  HTML document per request — not a console view mode like /mesh (the console's
  single-page pattern is unchanged)
- Endpoint discovery: by default read culture's port file
  (~/.culture/pids/overview-{culture-server-name}.port) and build
  `http://127.0.0.1:{port}/residents.json`; an explicit culture.residents_url
  config key overrides; when neither resolves the page renders the 'resource
  view unavailable' notice
- v1 is reload-to-refresh; auto-refresh is parked as follow-up (each upstream
  request costs culture a fresh IRC connect+register, so polling cadence needs
  its own thought)
- The console /help pane gains a mention linking /residents; no header or layout
  change in v1

## Open / follow-up

- Auto-refresh for /residents (meta-refresh vs HTMX poll vs SSE push) — must
  weigh upstream cost: every request runs a fresh IRC connect+register on the
  culture server
