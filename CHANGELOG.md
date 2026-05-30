# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
