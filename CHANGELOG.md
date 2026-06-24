# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-06-24

### Added

- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention (scope localized to this
  repo's nick) so the vendored `remember` / `recall` skills are actually used,
  not just present: `/recall` before non-trivial work to build on prior
  decisions instead of re-deriving them, and `/remember` when a non-obvious
  decision, constraint, fix-and-why, or hard-won gotcha surfaces. The section
  documents this repo's memory as **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared). Inserted
  idempotently (skipped if already present), slotted under an existing
  "Conventions and workflow" heading when one exists, else appended.

### Changed

- **Refreshed the `remember` + `recall` wrappers from eidetic-cli 0.10.0**
  (cite-don't-import) — picks up eidetic's **project-local store default**: the
  files backend now resolves per record by visibility — PUBLIC records inside a
  git repo go to `<repo-root>/.eidetic/memory` (committed, team-shared), PRIVATE
  records (or any record outside a repo) go to `$HOME/.eidetic/memory` (never
  committed), an explicit `EIDETIC_DATA_DIR` still wins, and recall reads both
  stores and merges. Also carries the 0.9.3 hardening (interactive-stdin guard,
  `help` as a search term, SIGPIPE-safe suffix parsing). **Recipe policy
  override (the wrappers here are NOT byte-verbatim):** the injected default
  visibility is flipped from eidetic's `private` to **`public`**, so a plain
  `/remember` lands the note in `./.eidetic/memory` in this repo, kept as part
  of the repo — pass `--visibility private` to route a record to `$HOME`
  instead. `remember` drives `eidetic remember` (idempotent upsert of one JSON
  record or an NDJSON batch on stdin); `recall` drives `eidetic recall` with
  four search modes (exact / approximate / keyword / hybrid). Each `SKILL.md` is
  localized only in the illustrative `--scope <nick>` examples (Provenance keeps
  "First-party to eidetic-cli"). Runtime dep: the `eidetic` CLI on PATH (else a
  local eidetic-cli checkout with `uv`) — **`eidetic >= 0.10.0`** for the
  in-repo routing; on an older CLI the public records still work but are stored
  in `$HOME/.eidetic/memory` instead of in-repo. Propagated by rollout-cli's
  `eidetic-memory` recipe.

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

## [Unreleased]

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
