# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-07-02

### Added

- **Adopted the `agentfront` runtime across the whole CLI and site.**
  irc-lens is now legible to agents by construction: one `agentfront.App`
  registry backs every surface, and a suite of in-process drift gates
  keeps them from ever disagreeing.
  - **agentfront[mcp] dependency:** `agentfront[mcp]>=0.20.0` in project
    dependencies.
  - **CLI rendered from one registry:** `irc-lens.cli:build_app()`
    assembles a single `agentfront.App`; `learn`, `explain`, `overview`,
    and `doctor` are all derived from it rather than hand-maintained.
  - **11 live verbs as ephemeral-session tools:** `send`, `join`, `part`,
    `read`, `channels`, `who`, `mesh`, `switch`, `topic`, `me`, and `icon`
    are registered as agentfront tools, reachable identically from the
    CLI, MCP, and (future) TAUI surfaces — each opens a throwaway
    AgentIRC session, performs one verb, and disconnects.
  - **`irc-lens mcp`:** a new host verb serving `app.mcp_server()` over
    stdio, so any MCP client can drive the same tool catalog.
  - **`irc-lens tui`:** a new host verb supplying a terminal cockpit over
    agentfront's `LiveDriver`.
  - **`/agent` HTTP front:** an agentfront WSGI bridge mounted inside the
    aiohttp console under `/agent`, behind the console's existing auth
    (dev or cloudflare-access) — serves `llms.txt`, `sitemap.xml`,
    `front`, and purpose-authored doc pages (what irc-lens is, driving
    the chat console, the tool catalog, exit codes and `--json`
    conventions). `/media` capability URLs remain auth-exempt.
  - **In-process drift gate:** `tests/test_front_agreement.py`'s
    `assert_surfaces_agree(build_app())` replaces the external `afi cli
    verify` binary — no subprocess, no skip conditions, runs in the
    default test selection on every push.

### Changed

- **Python 3.12 floor:** `requires-python` bumped from `>=3.11` to `>=3.12`,
  aligning with agentfront's Python version support.
- **`AfiError` → `AgentfrontError`:** `irc_lens._errors.AfiError` is now a
  verbatim subclass of `agentfront.errors.AgentfrontError`; the
  `(code, message, remediation)` shape and exit-code policy (0/1/2/3+)
  are unchanged, and `cli/_errors.py`'s public names are preserved as a
  stable-contract re-export shim.
- **Docs swept for the agentfront contract:** `CLAUDE.md`,
  `docs/cli.md`, and `docs/architecture.md` describe the one-registry,
  four-surface runtime and its drift gate in place of the retired AFI
  scaffolding manifesto.

## [0.8.0] - 2026-07-02

### Added

- **Media (image + audio) support:** humans and agents share images and
  audio through the lens via HTTP capability URLs. `POST /upload` accepts
  multipart/form-data files (PNG, JPG, GIF, WebP for images; MP3, OGG,
  WAV, WebM, M4A, FLAC for audio); `GET /media/{token}` serves them with
  auth-exempt capability-URL tokens (128-bit unguessable URLs matching
  the IRC trust model). Console upload UI: file picker, drag-drop, paste,
  and microphone recording (MediaRecorder → opus/webm). Lens-hosted URLs
  auto-embed as `<img>` / `<audio>`; remote URLs render as click-to-load
  placeholder cards by default (configurable per `media.remote_embeds`).
  Configuration: new optional `media:` section with `enabled`, `dir`,
  `max_file_bytes` (10 MiB), `max_store_bytes` (256 MiB with eviction),
  `public_base_url`, `remote_embeds`, and `trusted_hosts`. Security
  headers (CSP, nosniff, referrer-policy) land with this feature.

## [0.7.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `~/.eidetic/memory` surface, so this agent (Claude and its colleague backend)
  can persist facts across sessions and recall them later, sharing one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.6.1] - 2026-06-07

### Changed

- **License: MIT → Apache 2.0.** Replaced the MIT license text with the
  Apache License 2.0 (`LICENSE`) and aligned the declared license in
  `pyproject.toml` (`license = "Apache-2.0"` plus the OSI classifier).
  Copyright holder is Ori Nachum.

## [0.6.0] - 2026-05-30

### Added

- Live agent-mesh graph view in the web console (`/mesh`). A vanilla-JS
  port of katvan's MeshIsland Canvas renderer (`static/mesh.js`) draws
  joined channels as rooms and their members as agent/human nodes, with
  membership edges and travelling message particles. It is fed live over
  a new `mesh` SSE event: `Session.build_mesh_snapshot()` emits katvan's
  mesh.json contract (`{nodes:[{id,label,kind,server}],
  edges:[{source,target}]}`), refreshed on JOIN/PART (coalesced into a
  single-flight rebuild) and on a per-session timer. The per-nick WHO
  `server` field maps onto MeshIsland's federated server bands. Selecting
  a channel returns from the mesh view to chat. The agent-vs-human
  classification is a documented v1 heuristic pending a canonical rule
  from katvan (tracked cross-repo).

## [0.5.1] - 2026-05-08

### Added

- `.claude/skills/cicd/` — CI/CD workflow skill vendored from
  `agentculture/steward` (replaces `pr-review`). Adds `workflow.sh`
  entry point with portability lint, reviewer-readiness polling, batch
  reply, alignment-delta check, and SonarCloud quality-gate
  integration. Includes an `irc-lens notes` section preserving
  AFI-rubric guardrails (recurring bug classes, PUSHBACK rules for
  `.afi/reference/` and stable-contract files).
- `.claude/skills/communicate/` — cross-repo + Culture-mesh
  communication skill vendored from `agentculture/steward`. Files
  GitHub issues on sibling repos with auto-signature
  `- irc-lens (Claude)` and sends Culture mesh channel messages.

### Changed

- `.gitignore` narrowed from `.claude/` (entire directory) to steward's
  selective pattern: per-user state (`settings.local.json`,
  `scheduled_tasks.lock`, `skills.local.yaml`, `*/local.yaml`,
  `*/scripts/*.local.sh`) stays ignored; tracked skills become
  visible. Closes #30.

### Removed

- `.claude/skills/pr-review/` — superseded by `.claude/skills/cicd/`
  (steward 0.7.0 rename).

### Fixed

- `cicd/scripts/pr-batch.sh` — guarded `jq -r` parses with
  `2>/dev/null || echo "null"` so a malformed JSONL line yields the
  sentinel that the existing null-check catches and skips, instead of
  aborting the entire batch under `set -e`. (Divergence from upstream
  steward; will reconcile when steward picks up the same patch.)
- `cicd/scripts/pr-status.sh` — wrapped each `curl -s` call in command
  substitution with `|| echo '{}'` so transient SonarCloud / network
  failures yield empty JSON instead of empty stdout, preventing
  `workflow.sh await` from crashing on a flaky API call. (Divergence
  from upstream steward; will reconcile.)
