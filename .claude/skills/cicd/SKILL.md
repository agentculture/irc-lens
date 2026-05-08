---
name: cicd
description: >
  irc-lens CI/CD lane: open PR (auto-wait for Qodo/Copilot), push fixes
  (re-poll bots), triage feedback, reply, resolve. Adds a portability lint
  (no absolute /home paths, no per-user dotfile refs in committed docs),
  an alignment-delta check when CLAUDE.md or culture.yaml change, and
  greenfield-aware test/version-bump steps. Use when: creating PRs in
  irc-lens, handling review feedback, polling CI status, or the user says
  "create PR", "review comments", "address feedback", "resolve threads".
  Vendored from steward's `cicd` skill — see the `irc-lens notes` section
  below for the AFI-rubric guardrails specific to this repo.
---

# CI/CD — irc-lens edition

irc-lens is the lens CLI for AgentIRC in the Culture ecosystem; its PRs touch
the AFI-rubric CLI surface, mesh-observability code paths, and committed
docs that other contributors clone fresh. The two recurring bug classes that
this skill is built to catch up front:

- **Path leaks** — committing absolute home-directory paths that work only on
  the author's machine.
- **Per-user config dependencies** — referencing a dotfile under the user's
  home directory in repo guidance, breaking reproducibility for other
  contributors and CI.

The workflow is encapsulated in `scripts/workflow.sh` — follow that, not a
manual checklist. AFI-rubric-specific PUSHBACK rules and bug classes live in
the `## irc-lens notes` section at the bottom.

## Prerequisites

Hard requirements: `gh` (GitHub CLI), `jq`, `bash`, `python3` (stdlib only),
`curl` (used by `pr-status.sh`).

Optional: `.claude/skills.local.yaml` declares per-machine
`sibling_projects` paths used by the `delta` subcommand. With no file
present, `delta` is a no-op — fine for irc-lens until cross-project
alignment work starts.

## How to run

`scripts/workflow.sh` is the entry point. Subcommands:

| Command | Purpose |
|---------|---------|
| `workflow.sh lint` | Portability lint on the current diff (staged + unstaged). |
| `workflow.sh open-pr --title T [--body-file F] [--wait SECS] [...]` | `gh pr create` then sleep 180s (or `--wait SECS`) and fetch reviewer comments in one shot. Use after pushing the initial branch. |
| `workflow.sh poll <PR>` | Fetch and display all review comments. |
| `workflow.sh poll-readiness <PR> [--max-iters N] [--interval SECS] [--require LIST]` | Loop until all required reviewers are ready (default `qodo`; pass `--require qodo,copilot` to also gate on Copilot) — or the PR closes / iteration cap hits. Headline on stdout, per-iteration diagnostics on stderr. Direct wrapper around `scripts/poll-readiness.sh`. |
| `workflow.sh wait-after-push <PR> [--wait SECS]` | Sleep 180s (or `--wait SECS`) then re-fetch comments. Use after pushing fixes. |
| `workflow.sh await <PR>` | Poll for reviewer readiness (default: 30 × 60s ≈ 30 min cap, requires qodo only; tune with `STEWARD_PR_AWAIT_ITERS`, `STEWARD_PR_AWAIT_INTERVAL`, and `STEWARD_PR_REVIEWERS`), then run `pr-status.sh` (CI checks + SonarCloud quality gate, OPEN issues, hotspots) and `pr-comments.sh` (inline / issue / top-level / SonarCloud-new-issues sections). Exits non-zero on SonarCloud `ERROR` or unresolved threads. Setting the legacy `STEWARD_PR_AWAIT_SECONDS=<n>` falls back to a fixed sleep with a deprecation warning. |
| `workflow.sh delta` | Dump each sibling project's `CLAUDE.md` head + `culture.yaml`. |
| `workflow.sh reply <PR>` | Batch reply (JSONL on stdin) and resolve threads. |
| `workflow.sh help` | Print this list. |

The vendored single-comment helpers — `pr-reply.sh`, `pr-status.sh` — live
next to `workflow.sh` and are usable directly when batching isn't appropriate.

The `STEWARD_PR_*` env vars keep their historical prefix because the scripts
are vendored verbatim from the upstream steward skill. They work the same
regardless of which repo vendors the skill — no rename is needed, and
keeping the prefix makes upstream tracking clean.

## Polling for reviewer readiness

`scripts/poll-readiness.sh` watches a PR until its required reviewers post
real (not placeholder) feedback, the PR closes, or an iteration cap fires.
It fetches `gh api` JSON directly — never `pr-comments.sh` output — so
truncation can't bias the gate. Default required set is qodo only
(see header comments and `--help` for tunables, env vars, and the `qodo` /
`copilot` heuristics; Copilot is detected but not required because its
review bot is silent on agentculture repos in 2026). Heartbeats stream to
stderr; the final headline is the only thing on stdout.

Two ways to drive it:

- **Synchronous** — `workflow.sh await <PR>` after `gh pr create`. The
  main session burns context during the wait; fine up to ~5 minutes.
- **Asynchronous** — for longer waits, run the looper inside a background
  subagent (Agent tool, `run_in_background: true`) so the main session
  only pays the cache cost when readiness fires. The subagent's only job
  is to invoke `poll-readiness.sh` and echo its headline back. The
  parent triages with `workflow.sh await <PR>` when the notification
  arrives. The user can interrupt with TaskStop.

This pattern is borrowed from sibling repo
[`agentculture/cfafi`](https://github.com/agentculture/cfafi)'s `poll`
skill — only the looper is vendored here rather than promoting `poll`
to its own first-class skill until other verbs need the same primitive.

## End-to-end flow

```text
git checkout -b <type>/<desc>
# ... edit ...
.claude/skills/cicd/scripts/workflow.sh lint
git commit -am "..." && git push -u origin <branch>
gh pr create --title "..." --body "..."   # title <70 chars, body signed "- <nick> (Claude)"
.claude/skills/cicd/scripts/workflow.sh await <PR>   # readiness loop, then CI + SonarCloud + all comments
# triage; if CLAUDE.md/culture.yaml/.claude/skills changed:
.claude/skills/cicd/scripts/workflow.sh delta
# fix, re-lint, push
.claude/skills/cicd/scripts/workflow.sh reply <PR> < replies.jsonl
gh pr checks <PR>
# Wait for human merge — never merge yourself.
```

Branch naming: `fix/<desc>`, `feat/<desc>`, `docs/<desc>`, `skill/<name>`,
`add-<desc>`.
PR / comment signature: `- <nick> (Claude)`, where `<nick>` comes from
the agent's own `culture.yaml` — first agent's `suffix` — falling back
to the git-repo basename when no `culture.yaml` is present. irc-lens has
no `culture.yaml` today, so the resolver falls back to `irc-lens` and
signs `- irc-lens (Claude)`. The reply script resolves this via
`scripts/_resolve-nick.sh` and auto-appends the signature only when the
body isn't already signed, so JSONL reply entries can include or omit it.
Hand-rolled `gh pr create` and `gh issue comment` calls should follow the
same convention.

## Triage rules

For every comment, decide **FIX** or **PUSHBACK** with reasoning.

Default to **FIX** for: portability complaints (always valid for irc-lens —
recurring bug class), test or doc requests, style nits aligned with workspace
conventions, AFI-rubric divergence (see irc-lens notes below).

Default to **PUSHBACK** for: architecture opinions that conflict with workspace
`CLAUDE.md`, `.afi/reference/python-cli/`, or the all-backends rule;
greenfield false-positives (e.g. "add tests" before there's any source —
defer to a later PR, don't refuse). Always cite the *reason* — workspace
convention, rubric clause, or scoping. PUSHBACK without reasoning is just
a refusal.

### Alignment-delta rule

If the PR touches `CLAUDE.md`, `culture.yaml`, or anything under
`.claude/skills/`, run `workflow.sh delta` **before** declaring FIX or
PUSHBACK on each comment. The script dumps the head of every sibling
project's `CLAUDE.md` plus the full `culture.yaml`, using `sibling_projects`
from `skills.local.yaml`. Note any sibling that needs a follow-up PR and
mention it in your reply. With no `skills.local.yaml`, this no-ops cleanly.

## Greenfield-aware steps

The lint and the workflow script are always-on. Stack-specific steps are
conditional and run only when their preconditions exist:

```bash
[ -d tests ] && [ -f pyproject.toml ] && uv run pytest tests/ -x -q
[ -f pyproject.toml ] && bump_version_per_project_convention   # see project README
[ -f .markdownlint-cli2.yaml ] && markdownlint-cli2 "$(git diff --name-only --cached '*.md')"
```

irc-lens has `pyproject.toml` and `tests/` once code lands, so these
activate as the package matures. Revisit each line as the corresponding
stack element evolves.

## Reply etiquette

Every comment must get a reply — no silent fixes. Always pass `--resolve`
when batch-replying so threads close automatically. Reference the
review-comment IDs in the fix-up commit message.

SonarCloud is queried in two places: `pr-status.sh` (quality gate, OPEN
issues, hotspots) and the Section-4 dump in `pr-comments.sh` (new-issue
list). Both derive the project key as `<owner>_<repo>`; override with
`SONAR_PROJECT_KEY=<key>` for non-standard naming, and they silently skip
when the project isn't on SonarCloud. irc-lens is registered as
`agentculture_irc-lens` on SonarCloud, so the default derivation works.

irc-lens isn't yet a registered mesh agent, so the post-merge IRC ping
that Culture's stock `pr-review` includes is still skipped — that returns
when irc-lens joins the mesh.

## irc-lens notes

irc-lens conforms to the agent-first Python CLI rubric cited from
`citation-cli` (see top-level `CLAUDE.md` and `.afi/reference/python-cli/`).
The cited material is authoritative — these notes summarize the
guardrails this skill should enforce on PRs.

### Recurring bug classes

- **Path leaks** — caught by `scripts/portability-lint.sh`. Default lint
  ignores `~/.claude/skills/<x>/scripts/` (vendored tool calls) and
  `~/.culture/` (Culture-mesh data; irc-lens may read this once it has
  IRC inspection code).
- **Per-user config dependencies** — referencing `~/.<dotfile>` paths in
  committed docs/configs.
- **Rubric divergence** — `afi cli verify` enforces 6 bundles (Structure,
  Learnability, JSON, Errors, Explain, Overview). Any change to the
  CLI core surface must keep them green.

### PUSHBACK rules specific to irc-lens

- **"Vendor `.afi/reference/` into `docs/`"** → no, by design it is cited
  via `afi cli cite` and gitignored. The right fix is to document the
  regen command, not to commit the reference.
- **"Merge `cli/_errors.py` into `cli/__init__.py`"** (or any consolidation
  of the stable-contract files: `cli/_errors.py`, `cli/_output.py`, the
  `_ArgumentParser` override, the `learn`/`explain` surface) → no, the
  split is part of the rubric contract.
- **"Hard-fail on missing target in `overview`"** → no, `overview` is
  descriptive, not verifying. `overview <bogus-path>` exits 0 with a
  warning section; hard-failing is `afi cli verify`'s job.

### Stable-contract checklist

When PRs touch `cli/_errors.py`, `cli/_output.py`, the `_ArgumentParser`
override, or the `learn` / `explain` / `overview` surface:

- Exit-code policy intact: `0` success, `1` user error, `2` env error,
  `3+` reserved. All failures raise `AfiError`; the dispatcher catches
  and exits with `err.code`. No Python traceback ever leaks.
- stdout/stderr split intact: results to stdout, errors and diagnostics
  to stderr, even in `--json` mode.
- Errors keep shape `{code, message, remediation}`; text-mode renders as
  `error: <msg>\nhint: <remediation>`.
- `_ArgumentParser` override still routes argparse errors through
  `emit_error` so unknown verbs/flags exit with `error:` + `hint:` and
  no traceback.
- Globals `learn`, `explain`, `overview` still exist at the top level —
  not nested under a noun.
- `learn` output rubric still passes (≥ 200 chars, mentions purpose,
  commands, exit codes, `--json`, `explain`).
