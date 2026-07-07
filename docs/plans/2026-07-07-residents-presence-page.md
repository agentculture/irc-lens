# Build Plan — residents presence page

slug: `residents-presence-page` · status: `exported` · from frame:
`residents-presence-page`

> The irc-lens console now has a residents page: one glance shows every mesh
> resident's presence state, current task, and token spend — rendered from
> culture's live resource view, behind the same CF Access as the rest of the
> console, and degrading to a clear notice (never an error page) when presence
> or the mesh is unavailable.

## Tasks

### t1 — Config: optional culture section through LensConfig

- covers: c17, h16
- acceptance:
  - LensConfig gains culture_residents_url and culture_overview_name (both
    str-or-None) parsed from an optional culture: section with keys
    residents_url and overview_name in src/irc_lens/config.py; an absent section
    yields both None and stays valid
  - Malformed culture values (non-string, or a residents_url that is not an
    absolute http/https URL) are rejected by load_config with the existing
    config error contract, covered by tests
  - The config init starter template (src/irc_lens/cli/_commands/config_cmd.py)
    includes a commented culture: section; both test config literals
    (tests/helpers.py DEV_CONFIG and tests/conftest.py _dev_config) carry the
    new fields; uv run pytest -q passes

### t2 — Fetch layer: residents client, URL discovery, classification

- covers: c6, c11, c13, c14, c16, h10, h13, h15
- acceptance:
  - New module src/irc_lens/web/residents.py exposes
    resolve_residents_url(residents_url, overview_name) returning the explicit
    URL when set, else `http://127.0.0.1:{port}/residents.json` with the port
    read from `~/.culture/pids/overview-{overview_name}.port`, else None; a
    missing or garbled port file yields None and never raises
  - fetch_residents(url) classifies outcomes: HTTP 200 with supported true ->
    SUPPORTED(payload); 200 with supported false -> UNSUPPORTED; 503 (even with
    a malformed body) -> UNREACHABLE; connection refused, timeout, upstream 500,
    or non-JSON body -> UNAVAILABLE; it uses aiohttp ClientSession with
    ClientTimeout(total=5) and its except clause names both aiohttp.ClientError
    and asyncio.TimeoutError
  - tests/_culture_server.py provides an in-tree fake culture endpoint (no
    network egress) able to serve every scenario; tests/test_residents_fetch.py
    covers each classification branch as a distinct case and runs in uv run
    pytest -q

### t3 — Rendering: residents template, notices, dashes, flags

- covers: c4, c5, c7, h2, h7, h8, h3
- acceptance:
  - templates/residents.html.j2 renders a full HTML document: for SUPPORTED a
    table with columns nick, server, state, since, task, tokens in/out, budget
    percent, and flags; for UNSUPPORTED a presence-pending-agentirc-release
    notice; for UNREACHABLE an IRCd-down notice; for UNAVAILABLE a
    resource-view-unavailable notice
  - Null task/tokens/budget fields render as dashes; rows render sorted by nick
    (defensively re-sorted at render time); budget_warning true and
    presumed_hung true rows carry distinguishing markup — all asserted in
    tests/test_render_residents.py including a fully-populated row asserting
    every promised column
  - The rendered HTML never contains the upstream URL, host, or port; the
    console help pane (templates/_info.html.j2) gains a one-line mention of
    /residents

### t4 — Route: authed GET /residents wired end-to-end, 200 in every outcome

- depends on: t1, t2, t3
- covers: c1, c2, c3, c8, c9, c15, c23, h1, h4, h5, h6, h9, h12, h14
- acceptance:
  - GET /residents is registered in web/app.py and handled in web/routes.py: it
    resolves LensConfig culture fields, fetches server-side via the t2 module,
    renders via the t3 template, and returns HTTP 200 text/html in every outcome
    — connection refused, timeout, upstream 503, upstream 500, non-JSON body —
    with the upstream status code appearing nowhere in the response body
  - /residents appears on NEITHER middleware exempt list (dev middleware in
    web/app.py, CF middleware in web/auth.py); tests prove an unauthenticated
    request is rejected in auth-enforcing mode, and the feature registers only a
    GET route with no mutation path
  - tests/test_residents_route.py drives the three degrade states (supported
    false, upstream 503, endpoint unreachable) end-to-end against the fake
    culture server asserting HTTP 200 plus each state-specific notice text, and
    asserts a supported payload renders the table sorted by nick
  - Route docstrings/comments note the deliberate contrast: this page returns
    200-with-notice rather than the {error, hint} JSON contract used by POST
    endpoints — matching the graceful-degrade requirement

### t5 — Ship: docs, changelog, version bump, and the issue-53 release reply

- depends on: t4
- covers: c18, h17, h6, h11, c3
- acceptance:
  - docs/architecture.md gains the /residents route-table row and a decision-log
    entry explaining the server-side fetch and port-file discovery choice;
    docs/cli.md documents the culture: config section
  - CHANGELOG.md gains the feature entry and pyproject.toml [project].version
    gets a minor bump; uv run pytest -q passes on the final tree
  - After merge/release, a reply is posted on issue 53 naming the shipping
    irc-lens release so culture can note it in the rollout — recorded as an
    explicit ship-checklist step in the PR description

## Risks

- [unknown_nonblocking] The port-file path (~/.culture/pids) is a culture
  internal, not a published contract; if culture relocates it, discovery
  silently degrades to the unavailable notice until the residents_url override
  is set
- [follow_up] culture may later grow a pinned --serve-port flag on mesh overview
  --serve; if it does, a static residents_url config becomes the simpler primary
  path
- [follow_up] Auto-refresh is deferred by decision (spec parked item v1); any
  future cadence must weigh that each upstream request costs culture a fresh IRC
  connect+register
