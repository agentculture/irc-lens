# Adopt agentfront across CLI and site

> irc-lens runs on the agentfront runtime: the CLI is rendered from one App
> registry and the site serves the agent-facing HTTP front (markdown docs,
> sitemap.xml, llms.txt, front) — the whole tool is legible to agents by
> construction

## Audience

- AI agents consuming irc-lens (CLI verbs with --json, fetch-tool HTTP
  navigation) and Culture mesh operators who deploy it

## Before → After

- Before: the CLI is hand-cited AFI scaffolding (src/irc_lens/cli/ from the
  retired afi cli cite pattern) verified only by an external afi binary that
  CI never runs (tests/test_afi_verify.py skip-guards on shutil.which); the
  site is a human-only HTMX/SSE console with zero machine-readable state
  surface; nothing structurally prevents CLI/site drift
- After: an agent can learn, explain, overview and doctor the CLI, and with
  only a fetch tool can start at llms.txt or sitemap.xml and read every doc
  page plus the front mirror — all derived from one registry, with
  assert_surfaces_agree pinning that the surfaces cannot drift

## Why it matters

- agent-first via agentfront is the stated AgentCulture org norm (agentfront
  docs/agentculture.md) and the colleague repo proved the
  import-dont-duplicate migration; irc-lens is the lens onto the agent mesh
  yet today its own surfaces are the least agent-legible in the org

## Requirements

- the CLI is rendered from one agentfront App registry — import, dont
  duplicate: the hand-rolled scaffolding in src/irc_lens/cli/ (custom
  _ArgumentParser, _dispatch, emit_result/emit_error, hand-written
  learn/explain/overview) is replaced, following the colleague blueprint of
  per-verb register_into(app) hooks
  - honesty: run_cli against the rendered App reproduces every contract
    pinned by tests/test_cli.py (learn markers, error triple on stderr, exit
    codes, no traceback) with the hand-rolled scaffolding deleted — not
    shimmed alongside
- serve and config register as host commands via app.add_command (a
  long-running server launcher is not a pure tool — agentfront
  docs/consumer-cli.md host-launcher-verb contract), preserving all serve
  flags, config resolution, and CF-mode guards byte-compatibly
  - honesty: irc-lens serve and irc-lens config init behave byte-compatibly
    through agentfront dispatch: same flags, same config-file resolution and
    CF-mode guards, same exit codes — pinned by the existing
    test_serve_cli.py / test_config_init.py passing unmodified
- AfiError maps to AgentfrontError: identical dataclass shape (code, message,
  remediation), identical 0/1/2 exit-code policy, no-traceback guarantee
  preserved on every failure path including argparse-time errors
  - honesty: no failure path leaks a Python traceback to stderr, including
    argparse-time errors with --json; a bogus verb exits 1 with an error
    line and a hint line
- tests/test_afi_verify.py (external afi subprocess, skip-guarded) is replaced
  by in-process agentfront.testing gates — assert_surfaces_agree(app) plus
  run_cli smoke tests — that actually run in CI on every push
  - honesty: the drift gate runs unconditionally in CI — no skip-guard, no
    external binary: a deliberate surface drift (e.g. a tool registered on
    CLI but not listed in llms.txt) fails the build
- the derived CLI gains the doctor meta-verb (rubric bundle 7: healthy bool +
  checks list, failed checks carry remediation), which the current AFI-era CLI
  lacks
  - honesty: irc-lens doctor exits 0 when healthy and its --json payload
    carries healthy plus a checks list where every failed check names a
    remediation
- requires-python bumps from 3.11 to 3.12 (the agentfront floor; local
  interpreter is 3.12.3)
  - honesty: no supported deployment of irc-lens still runs on Python 3.11 —
    the floor bump strands nobody
- docs and meta sweep: CLAUDE.md (currently an AFI manifesto that still says
  the repo has no source code), docs/cli.md, docs/architecture.md decision
  log, and the cicd-skill AFI-rubric guardrails are rewritten to the
  agentfront contract; no doc still instructs regenerating .afi/reference or
  running afi cli verify
  - honesty: grep across docs/, CLAUDE.md and .claude/skills/ finds no
    remaining instruction to regenerate .afi/reference or to run afi cli
    verify
- the site exposes the agent-facing HTTP front under a path prefix on the
  console origin: purpose-authored agent pages about the running lens,
  llms.txt discovery, sitemap.xml, and the front markdown mirror — explicitly
  NOT a mirror of the repo docs/ tree (superseding rejected c7)
  - honesty: an agent with only a fetch tool, starting blind at the prefix
    root or llms.txt, can enumerate the tool catalog and read every agent
    page without executing JavaScript — and none of those pages is a copy of
    a file under docs/
- the MCP surface ships in v1: app.mcp_server() behind the agentfront[mcp]
  extra, exposed via a host verb (e.g. irc-lens mcp) serving stdio — the
  single run tool carries the full irc-lens command catalog
  - honesty: call_mcp(app, command) returns the result-or-error-triple
    payload for every registered tool, and the stdio server starts and
    answers a run round-trip in an e2e test
- the TAUI surface ships in v1: a host TTY verb (e.g. irc-lens tui) supplies
  the terminal loop over agentfront LiveDriver, so humans and agents drive one
  live session with state parity
  - honesty: assert_agent_human_parity holds: an agent SelectorAction and a
    human key-navigation land on identical TAUI state for the same target
- the full live-verb catalog registers as agentfront tools: the
  CommandType/_exec_dispatch table in session.py (send, join, part, read,
  channels, who, mesh, switch, topic, me, icon...) becomes the registry tool
  catalog, identical across CLI, MCP run, and TAUI dispatch
  - honesty: every CommandType verb wired in the web console is reachable as
    a registry tool with the same effect a console /command has — one
    dispatch table, no web-only verbs left behind

## Honesty conditions

- legible by construction is testable: a fresh agent given only the installed
  CLI and a fetch tool against the running site can discover every capability
  (learn, llms.txt) and exercise one end-to-end without reading the repo
  source
- each named audience has a concrete v1 surface: agents get CLI --json, MCP
  run, and the fetchable front; operators keep the same serve deployment they
  run today
- the described gaps are verifiable in-repo today: tests/test_afi_verify.py
  skip-guards on shutil.which(afi), and the web layer emits no
  machine-readable state beyond /healthz and upload receipts
- demonstrated end-to-end: learn, explain, overview and doctor exit 0; the
  front enumerates the tool catalog from the running site;
  assert_surfaces_agree passes in CI
- the cited norm and precedent are real and readable: agentfront
  docs/agentculture.md states the org expectation, and the shipped colleague
  register_into migration is the working blueprint
- the pre-existing console suite (web skeleton, security headers, origin
  check, auth middleware, playwright) passes unmodified in behavior
- the adoption diff touches no file under src/irc_lens/irc/, nor commands.py,
  nor CITATION.md
- media tests (test_media*, test_serve_media_origins, playwright media and
  mic) pass unmodified and /media capability URLs stay auth-exempt
- every listed gate is a CI-visible pass/fail on every push, and grep for afi
  scaffolding references in src/ returns empty

## Success signals

- CI is green with the new gates: assert_surfaces_agree(app) passes, run_cli
  smoke tests pass, agentfront cli doctor . passes, the entire pre-existing
  suite (web skeleton, security headers, origin check, playwright) passes
  unmodified in behavior, and grep for afi scaffolding references in src/
  returns nothing

## Scope / boundaries

- the human HTMX/SSE console stays intact: the aiohttp routes (/, /input,
  /events, /upload, /media, /healthz), CF Access auth middleware, CSP and
  security headers, the Origin CSRF floor, and the ratified error shape
  (error, hint) on console JSON responses (docs/architecture.md 195-204, PR 7)
  are not restructured by this adoption
- the culture-cited domain layer is untouched: irc/transport.py,
  irc/message.py, irc/buffer.py, commands.py and CITATION.md keep their
  cite-dont-import provenance from agentculture/culture — adoption changes
  interface surfaces, not domain logic
- media behavior is unchanged: links-not-binaries on the wire and auth-exempt
  /media capability URLs (standing operator principle,
  docs/superpowers/specs/2026-07-02-media-support-design.md)

## Non-goals

- no tool execution over HTTP: the agentfront HTTP front is GET-only by design
  (agentfront/http_surface.py serves docs and listings only) — the web write
  path remains POST /input on the authenticated human console

## Assumptions

- agentfront-generated learn/explain/overview output satisfies the contracts
  pinned by tests/test_cli.py (learn over 200 chars with
  purpose/commands/exit-codes/--json/explain markers; overview JSON with
  subject, path, sections) — the derived meta-verbs were built to the same
  rubric, and colleague migrated 26 verbs against them
- the WSGI agent front can be served from the irc-lens serve process without
  new third-party dependencies (agentfront core is pure stdlib; aiohttp is
  already a dependency; the bridge or port split is an implementation choice)

## Decisions

- the WSGI agent front mounts inside the aiohttp console under a path prefix —
  single port, single origin, one deployment
- the agent front serves no repo docs (the GitHub repo is for that): registry
  docs are purpose-authored agent pages about the running tool — how to drive
  the lens, the tool catalog, the front mirror
- all four agentfront surfaces ship in v1: CLI, HTTP front, MCP server, TAUI
  cockpit
- the full CommandType catalog (send, join, part, read, channels, who, mesh,
  and the remaining live verbs) registers as agentfront tools in v1 — the lens
  becomes a full agent front to the mesh, superseding the read-only-ish
  framing in CLAUDE.md
- tool invocations obtain a Session ephemerally — connect, execute, disconnect
  per invocation using LensConfig credentials; read/history fidelity is
  bounded by what the server replays, accepted for v1
- the agent-front prefix sits behind CF Access in cloudflare-access mode —
  zero new auth exemptions; anonymous discovery is not a goal

## Open / follow-up

- daemon-client session mode: tool invocations reusing the running serve
  daemon buffers over a structured local RPC surface — follow-up after v1
  ephemeral mode
