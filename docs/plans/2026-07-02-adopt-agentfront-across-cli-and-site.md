# Build Plan — Adopt agentfront across CLI and site

slug: `adopt-agentfront-across-cli-and-site` · status: `exported` ·
from frame: `adopt-agentfront-across-cli-and-site`

> irc-lens runs on the agentfront runtime: the CLI is rendered from one App
> registry and the site serves the agent-facing HTTP front (markdown docs,
> sitemap.xml, llms.txt, front) — the whole tool is legible to agents by
> construction

## Tasks

### t1 — Packaging: agentfront dependency and Python 3.12 floor

- covers: c12, h7
- acceptance:
  - pyproject.toml declares agentfront[mcp] at 0.20.0 or later in project
    dependencies and requires-python is 3.12 or later; uv sync --extra dev
    resolves and uv run python -c "import agentfront, agentfront.testing"
    exits 0
  - CI workflow pins Python 3.12 so the floor bump is exercised on every
    push; CHANGELOG notes the 3.11 to 3.12 floor bump; the diff touches only
    pyproject.toml, uv.lock, ci.yml and CHANGELOG.md

### t2 — Error layer: AfiError becomes the AgentfrontError triple

- depends on: t1
- covers: c9, h4
- acceptance:
  - src/irc_lens/_errors.py maps AfiError onto agentfront.errors semantics:
    identical (code, message, remediation) dict shape and
    EXIT_SUCCESS/EXIT_USER_ERROR/EXIT_ENV_ERROR values equal to the
    agentfront constants; the cli/_errors.py shim keeps its public names
  - a bogus verb exits 1 printing an error line and a hint line with no
    Python traceback; parse-time errors with --json emit the (code, message,
    remediation) JSON on stderr — pinned by the existing error cases in
    tests/test_cli.py passing

### t3 — App registry assembly: build_app and derived meta-verbs

- depends on: t1, t2
- covers: c6, h1, c11, h6
- acceptance:
  - a new build_app() (colleague blueprint: per-command register_into(app)
    hooks auto-assembled) constructs App(name="irc-lens") and
    irc_lens.cli:main dispatches via agentfront run_cli; the
    [project.scripts] entry is unchanged
  - learn/explain/overview/doctor all derive from the registry: learn exits
    0 with at least 200 chars mentioning purpose, commands, exit codes,
    --json and explain; overview --json returns subject and sections; doctor
    --json returns a healthy bool plus a checks list where failed checks
    carry remediation
  - the hand-rolled cli/_commands/learn.py, explain.py, overview.py and the
    src/irc_lens/explain/ package are deleted, not shimmed;
    tests/test_cli.py passes against the rendered CLI with marker updates
    only where wording (not contract) changed

### t4 — Host commands: serve and config through app.add_command

- depends on: t3
- covers: c8, h3
- acceptance:
  - serve.py and config_cmd.py gain register_into(app) hooks using
    add_command with a configure hook that recreates every existing flag
    (--config, --host, --port, --nick, --web-port, --bind, --icon, --open,
    --seed, --log-json) and the CF-mode guards; the old register(sub) wiring
    is removed
  - tests/test_serve_cli.py and tests/test_config_init.py pass unmodified:
    same flags, same config-file resolution, same exit codes; a bogus serve
    flag exits 1 with a hint and no traceback

### t5 — Live-verb catalog: ephemeral-session registry tools

- depends on: t3, t4
- covers: c28, h13
- acceptance:
  - a new tools module registers every CommandType verb wired in the web
    console dispatch table (send, join, part, read, channels, who, mesh,
    switch, topic, me, icon) as agentfront tools; a test diffs the registry
    tool set against the session dispatch table so no web-only verb is left
    behind
  - tools obtain a Session ephemerally — connect, execute, disconnect with
    LensConfig credentials; an e2e test round-trips irc-lens channels --json
    against the in-tree fake AgentIRC server (tests/_agentirc_server.py)
  - no file under src/irc_lens/irc/ nor commands.py is modified

### t6 — Purpose-authored agent pages as registry docs

- depends on: t3
- acceptance:
  - registry docs are authored in-package via add_doc covering at least:
    what irc-lens is, how to drive the chat console, the tool catalog, exit
    codes and --json conventions; a test asserts no page body is a copy of
    any file under docs/
  - the same doc slugs render on the CLI explain surface — pinned by the
    surface-agreement gate, not a parallel list

### t7 — HTTP front mount: WSGI bridge under the agent prefix, behind auth

- depends on: t6
- covers: c25, h10
- acceptance:
  - a small in-repo bridge serves agentfront make_http_app inside the
    aiohttp console under a path prefix (e.g. /agent/): index, llms.txt,
    sitemap.xml, front and every doc slug return 200 in dev mode; sitemap
    URLs carry the prefix; no new third-party dependency
  - in cloudflare-access mode the prefix requires a valid CF JWT exactly
    like the console root: unauthenticated GET on the prefix returns 401 and
    the exempt list stays /static, /healthz, /media only; security-header
    middleware applies to prefix responses
  - a fetch-only traversal test starts blind at llms.txt, enumerates the
    tool catalog and fetches every linked page without executing JavaScript,
    and asserts none of those pages matches any docs/*.md file

### t8 — MCP surface: irc-lens mcp host verb serving stdio

- depends on: t5
- covers: c26, h11
- acceptance:
  - a new mcp host verb (add_command) serves app.mcp_server() on stdio; an
    e2e test performs a run round-trip against the stdio server
  - call_mcp(app, command) returns the result-or-error-triple payload for
    every registered tool via a test parametrized over the full catalog —
    without importing the mcp package

### t9 — TAUI surface: irc-lens tui host verb over LiveDriver

- depends on: t5
- covers: c27, h12
- acceptance:
  - a new tui host verb supplies the terminal loop over agentfront
    LiveDriver at a TTY; piped or non-TTY invocation prints help or a hint
    and exits without hanging
  - assert_agent_human_parity passes for a pure-navigation selector, and
    drive() executing a tool SelectorAction lands the same state as the
    equivalent CLI invocation

### t10 — Test migration: in-process drift gates replace the external afi verifier

- depends on: t3
- covers: c10, h5
- acceptance:
  - tests/test_afi_verify.py is deleted; a new tests/test_front_agreement.py
    asserts assert_surfaces_agree(build_app()) with zero skip conditions, no
    subprocess and no external binary, plus run_cli smoke tests for each
    meta-verb
  - the gate provably bites: a test registers a deliberate one-surface drift
    on a scratch App and asserts AssertionError naming the disagreeing pair
  - the new tests run in the default pytest selection (no marker), hence
    inside the existing CI pytest step on every push

### t11 — Docs and meta sweep: agentfront contract replaces the AFI manifesto

- depends on: t4, t7, t8, t9
- covers: c13, h8, c5, h17
- acceptance:
  - grep across CLAUDE.md, docs/ and .claude/skills/ finds no remaining
    instruction to regenerate .afi/reference or run afi cli verify;
    CLAUDE.md describes the agentfront runtime contract (one registry, four
    surfaces, drift gate) and no longer claims the repo has no source code
  - docs/cli.md documents the rendered surface including doctor, mcp and tui
    with the unchanged exit-code policy; docs/architecture.md decision log
    records the adoption citing the agentfront org-norm doc
    (docs/agentculture.md) and the colleague register_into blueprint as the
    precedent
  - markdownlint-cli2 passes on every touched markdown file

### t12 — End-to-end validation, boundary regression sweep and release prep

- depends on: t10, t11
- covers: c1, h9, c2, h14, c3, h15, c4, h16, c14, h18, c15, h19, c16, h20,
  c18, h21
- acceptance:
  - fresh-agent legibility e2e: with only the installed CLI and HTTP fetches
    against a seeded serve, a scripted flow discovers capabilities via learn
    and the prefix llms.txt and exercises one tool end to end, never reading
    the repo source
  - boundary sweep: the pre-existing suite passes unmodified in behavior
    (web skeleton, security headers, origin check, auth middleware, media,
    playwright markers); the adoption diff touches no file under
    src/irc_lens/irc/, commands.py or CITATION.md; media capability URLs
    stay auth-exempt
  - success signals are CI-visible: surface agreement, run_cli smokes and
    doctor pass in the CI log; grep for afi scaffolding references in src/
    returns nothing; version bumped with a CHANGELOG entry covering the
    adoption

## Risks

- [unknown_nonblocking] WSGI-in-aiohttp bridge mechanics: aiohttp has no
  native WSGI support — direct-call vs thread offload, and CSP header
  interplay on bridged responses, settled inside t7 (task t7)
- [unknown_nonblocking] agentfront-derived learn/explain wording may differ
  from the hand-rolled text pinned in tests/test_cli.py markers and
  docs/cli.md examples — contract stays, wording updates land in t3/t11 (task
  t3)
- [unknown_nonblocking] ephemeral-session read/history fidelity is bounded by
  what the AgentIRC server replays on join — accepted for v1 per the frame
  decision; verified against the fake server in t5 (task t5)
- [unknown_nonblocking] TAUI TTY loop is the largest new UI item (agentfront
  ships no runnable loop; the host supplies it) — t9 may grow; keep it
  last-wave and independently mergeable (task t9)
- [follow_up] daemon-client session mode (tools reusing the running serve
  daemon buffers over a structured local RPC surface) — follow-up after v1
  ephemeral mode
