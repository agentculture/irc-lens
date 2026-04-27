---
name: pr-review
description: >
  irc-lens PR workflow: branch, commit, push, PR, wait for review (Qodo +
  Copilot), triage with FIX/PUSHBACK reasoning, fix, reply, resolve. Adapted
  from ghafi's pr-review skill. Use when: creating PRs in irc-lens, handling
  review feedback, or the user says "create PR", "review comments",
  "address feedback", "resolve threads".
---

# PR Review — irc-lens edition

irc-lens is the lens CLI for AgentIRC in the Culture ecosystem. The repo
is shaped by the `.afi/reference/python-cli/` agent-first CLI rubric (cited,
not imported — see `CLAUDE.md`). Once code lands, the recurring bug classes
will mirror ghafi/citation-cli:

- **Path leaks** — committing absolute home-directory paths that work only
  on the author's machine. Catch with `scripts/portability-lint.sh`.
- **Per-user config dependencies** — referencing `~/.<dotfile>` paths in
  committed docs/configs, breaking reproducibility for other contributors.
- **Rubric divergence** — `afi cli verify` enforces 5 bundles (Structure,
  Learnability, JSON, Errors, Explain). Any change to `cli/_errors.py`,
  `cli/_output.py`, the `_ArgumentParser` override, or the `learn`/`explain`
  surface must keep those bundles green.

## Prerequisites

`gh` (GitHub CLI), `bash`, `python3` (stdlib only). The portability lint
runs against `git ls-files` or the staged-vs-HEAD diff; no other tooling.

## Portability lint

Vendored from ghafi. Run from the repo root:

```bash
# Lint files modified vs HEAD (default — staged + unstaged):
bash .claude/skills/pr-review/scripts/portability-lint.sh

# Lint every tracked file:
bash .claude/skills/pr-review/scripts/portability-lint.sh --all
```

Exits 0 if clean, 1 if a leak is found. Carve-outs (won't be flagged):

- `~/.claude/skills/<x>/scripts/` — vendored tool calls.
- `~/.culture/` — Culture-mesh data (parity with steward; irc-lens may
  read this once it has IRC inspection code).

## End-to-end flow

```text
git checkout -b <type>/<desc>
# ... edit ...
bash .claude/skills/pr-review/scripts/portability-lint.sh
# (once code exists) afi cli verify .
# (once code exists) uv run pytest -n auto -v
git commit -am "..." && git push -u origin <branch>
gh pr create --title "..." --body "..."   # title <70 chars; body signed "- Claude"
# Wait for Qodo + Copilot review (~5 min after push).
gh api repos/{owner}/{repo}/pulls/<PR>/comments
gh pr view <PR> --json reviews,comments
# Triage: FIX or PUSHBACK with reasoning per comment.
# Fix, re-lint, push, reply, resolve threads.
gh pr checks <PR>
# Wait for human merge — never merge yourself.
```

Branch naming: `fix/<desc>`, `feat/<desc>`, `docs/<desc>`, `skill/<name>`,
`add-<desc>` (the existing `add-claude-md` branch is fine).
Commit/PR signature: `- Claude` (workspace convention).

## Triage rules

For every comment, decide **FIX** or **PUSHBACK** with reasoning.

Default to **FIX** for:

- Factual inaccuracies in committed docs (wrong file inventory, broken
  cross-references, "only:" lists that aren't actually exhaustive).
- Portability complaints (recurring bug class).
- Missing regeneration commands when the doc references a file that may not
  exist on a fresh clone (the `.afi/reference/` case is canonical).
- Rubric-bundle drift in CLI code.
- Style nits aligned with workspace conventions.

Default to **PUSHBACK** for:

- Architecture opinions that conflict with `CLAUDE.md` or `.afi/reference/`'s
  stable-contract files (e.g., "merge `_errors.py` into `cli/__init__.py`" —
  no, the split is part of the contract).
- Requests to vendor `.afi/reference/` into `docs/` — by design it is cited
  via `afi cli cite` and ignored. The right fix is to document the regen
  command, not to commit the reference.
- Requests to add features outside the alignment scope of the current PR.

Always cite the *reason* — workspace convention, rubric clause, or scoping —
when pushing back. PUSHBACK without reasoning is just a refusal.

## Reply etiquette

Every comment must get a reply — no silent fixes. Pass `--resolve` when
batch-replying so threads close automatically (or use the GraphQL
`resolveReviewThread` mutation for review threads). Reference review-comment
IDs in the fix-up commit message when the link is non-obvious.
