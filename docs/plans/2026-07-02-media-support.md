# Build Plan — media support

slug: `media-support` · status: `exported` · from frame: `media-support`

> irc-lens ships image and audio support: media URLs render
> inline in the chat console (click-to-load for remote hosts),
> and the console can send media via file upload, drag-drop,
> paste, and mic recording — blobs hosted by the lens and
> shared as links over the mesh

## Tasks

### t1 — URL classification + escape-first linkify engine (web/media.py)

- covers: c10, h11, c14, h1
- acceptance:
  - classify_url maps image (png jpg jpeg gif webp) and audio
    (mp3 ogg wav webm m4a flac) extensions and returns link for
    everything else; only http/https schemes are ever linkified
    (javascript: and data: URLs stay inert text) — pinned in
    tests/test_media.py
  - render_message_html escapes ALL input text before any markup
    is injected; adversarial tests cover HTML in text, quotes
    inside URLs, and the existing test_render.py autoescape
    guard stays green
  - a helper composes outbound media messages as plain URL text
    and a test asserts every composed message line stays under
    8192 bytes (agentirc MAX_INBOUND_LINE)
  - task owns only src/irc_lens/web/media.py and tests/test_media.py

### t2 — Chat-line media embeds: template + render wiring + CSS

- depends on: t1
- covers: c14, h1
- acceptance:
  - _chat_line.html.j2 renders a lens-media block under the
    text when media URLs are present: lens-hosted URLs become
    img loading=lazy or audio controls preload=metadata; remote
    URLs become a click-to-load placeholder button with
    data-src and data-kind (markup only, no JS here)
  - chat/log SSE fragment contract unchanged: data-testid
    chat-line still present, test_render.py and
    docs/sse-events.md DOM table stay valid
  - lens.css gains lens-media rules with max-width/max-height
    caps; task owns templates/_chat_line.html.j2,
    web/render.py or session.py glue, static/lens.css, and
    their tests

### t3 — Security headers middleware: CSP + nosniff + referrer-policy

- covers: c15, h2
- acceptance:
  - all HTML responses carry CSP with script-src 'self' and
    object-src 'none' (img-src/media-src allow self https:
    http:); every response carries X-Content-Type-Options
    nosniff and Referrer-Policy no-referrer — pinned in
    tests/test_security_headers.py
  - no inline scripts remain in index.html.j2; console still
    boots and htmx/sse/lens.js/mesh.js load under script-src
    'self' (existing e2e suite green)
  - task owns web/app.py middleware addition, templates/index.html.j2, tests/test_security_headers.py

### t4 — Config: optional media section

- covers: c18, h5
- acceptance:
  - LensConfig gains media fields (enabled, dir,
    max_file_bytes, max_store_bytes, public_base_url,
    remote_embeds, trusted_hosts); `_validate_media_section`
    follows the `_validate_web_section` pattern; a config file
    with NO media section yields working defaults (feature on,
    XDG data dir, 10 MiB per-file cap) — pinned in
    tests/test_config_loader.py
  - config init starter template includes the media keys and
    round-trips through load_config; afi cli verify still
    passes
  - task owns src/irc_lens/config.py,
    cli/_commands/config_cmd.py, tests/test_config_loader.py,
    tests/test_config_init.py

### t5 — Media blob store: tokens, sniffing, caps, eviction

- depends on: t4
- covers: c11, h12, c16, h3
- acceptance:
  - store writes to dir/principal/token.ext with token =
    secrets.token_urlsafe(16); magic-byte sniff must agree
    with the extension allowlist; SVG (and any non-allowlisted
    type) is rejected with a typed error the route layer maps
    to 400 {error, hint} — SVG rejection pinned by an explicit
    test
  - per-file cap enforced while streaming (no full-file
    buffering before the size check); total-store cap evicts
    oldest-by-mtime; both pinned in tests/test_media_store.py
  - task owns new src/irc_lens/web/store.py and tests/test_media_store.py only

### t6 — Routes: authed POST /upload + capability GET /media/

- depends on: t5, t3
- covers: c16, h3, c6, h7
- acceptance:
  - POST /upload is identity-gated and origin-checked like
    /input; multipart field file; over-cap 413 and bad-type 400
    both return {error, hint}; success returns 201 with url and
    kind
  - GET /media/token.ext is exempt from auth middleware (test:
    fetch with NO auth headers succeeds — the agent-fetch
    path) and serves via FileResponse with nosniff, immutable
    private cache, inline disposition; a request without a
    valid token 404s; token pattern is strictly matched (no
    traversal)
  - client_max_size raised to the media cap while POST /input
    keeps rejecting over-4KiB bodies including chunked ones —
    the pinned test in test_web_events.py is updated
    deliberately to assert the handler-level cap
  - task owns web/routes.py, web/app.py exemption list, tests/test_upload_routes.py

### t7 — media.js: click-to-load embeds

- depends on: t2
- covers: c14
- acceptance:
  - clicking a lens-media-load placeholder swaps in the real
    img/audio element using data-src and data-kind; no innerHTML
    with unescaped input — element construction via
    createElement/setAttribute only
  - lens.js stays within its 120-line test budget (media
    behavior lives entirely in the new static/media.js, loaded
    from index.html.j2); grep-style tests added following
    tests/test_lens_js.py conventions
  - task owns static/media.js and tests/test_media_js.py

### t8 — Console upload UI: picker, drag-drop, paste, auto-send

- depends on: t7, t6
- covers: c17, h4
- acceptance:
  - attach button, drag-drop onto the chat area, and
    paste-from-clipboard all POST the file to /upload; on 201
    the returned URL is submitted through the existing POST
    /input pipeline — one send path, asserted by a test that
    observes /input receiving the URL
  - upload failures surface through the existing toast pattern
    using the {error, hint} payload; no new SSE events
    introduced
  - task owns the upload-UI half of static/media.js, the form
    template block in index.html.j2, and Playwright coverage
    for drag-drop + paste

### t9 — Mic recording: MediaRecorder capture

- depends on: t8
- covers: c17, h4
- acceptance:
  - record button captures via getUserMedia + MediaRecorder to
    audio/webm codecs=opus (audio/mp4 fallback); states idle /
    recording-with-timer / uploading; client-side max duration
    stops at 5 minutes
  - the finished recording goes through the SAME /upload then
    /input path as file uploads (one send path preserved);
    Playwright smoke uses chromium fake-media-stream flags
  - task owns the recording half of static/media.js, its
    form-bar button markup, and the fake-media Playwright test

### t10 — End-to-end + Playwright proof chain

- depends on: t9
- covers: c1, h6, c7, h8, c9, h10, c19, h13
- acceptance:
  - e2e (tests/test_e2e_http.py pattern): POST /upload then the
    URL arrives as a PRIVMSG on the fake-IRCd wire then the
    chat fragment contains the embed markup — the whole chain in
    one automated test
  - Playwright: an image embed is visible and an audio element
    is playable inside the chat log; a share initiated from the
    console lands as an ordinary channel message; click-to-load
    swap verified; all running in CI, no manual steps
  - the lens-hosted flow needs no external host: the e2e chain
    runs entirely against loopback fixtures; existing autoescape
    guard and afi cli verify run green in the same suite
  - task owns tests/test_e2e_http.py additions,
    tests/test_e2e_playwright.py additions, and any conftest
    fixtures they need

### t11 — Docs, explain catalog, version bump

- depends on: t9
- covers: c8, h9
- acceptance:
  - architecture.md (request-shapes table + decision log),
    sse-events.md DOM contract, security-checklist.md
    (capability-URL model + CSP), and cli.md (media config
    keys) all updated; learn/explain catalog mentions media per
    the AFI rubric
  - PR description records the before-state verification: no
    media/attachment concept existed in irc-lens, agentirc, or
    cultureagent main branches when this landed (re-checked at
    PR time, citing the 2026-07-02 sweep)
  - minor version bump applied per repo convention; task owns
    docs/, explain/catalog.py, pyproject.toml/__init__.py
    version only

## Risks

- [unknown_nonblocking] backend image support varies across
  claude/codex/copilot/acp — degradation shape lands in the
  cultureagent follow-up issue, not this plan
- [follow_up] cross-machine media reachability depends on
  operators setting media.public_base_url until the agentirc
  mesh-reachability proposal lands
