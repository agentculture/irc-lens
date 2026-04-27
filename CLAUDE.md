# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`irc-lens` is the lens CLI for **AgentIRC** in the **Culture** ecosystem (see `../culture/`). The repo is a fresh skeleton — at the time of writing there is no source code yet, only:

- `README.md` — one-line purpose statement.
- `.gitignore` — standard Python ignores.
- `.afi/reference/python-cli/` — a cited reference (see below). Not source; not packaged. `.afi/` is gitignored.
- `.claude/settings.local.json`.

The first non-trivial task in this repo will be to bootstrap the Python package by integrating the `.afi/reference/python-cli/` pattern. Read that pattern *before* writing any CLI scaffolding from scratch.

## The `.afi/reference/python-cli/` reference (READ BEFORE CODING)

This directory was placed by `afi cli cite` (from the `citation-cli` / formerly `assimilai` project in the parent workspace). It is the **agent-first Python CLI** pattern that this project is expected to conform to. The rules are documented in `.afi/reference/python-cli/AGENT.md` and `MANIFEST.json` — defer to them; the summary below is just orientation.

### Token substitution

`{{project_name}}`, `{{slug}}`, `{{module}}` are placeholders. They are **not** substituted in the reference itself — substitute them when you copy a file into the host package. For this repo the natural choices are:

- `{{project_name}}` → `irc-lens`
- `{{slug}}` / `{{module}}` → `irc_lens`

### File roles

`MANIFEST.json` tags each file as `stable-contract` or `shape-adapt`:

- **`stable-contract`** — copy verbatim, then token-substitute. Don't reshape unless the host already has equivalents. Covers `cli/_errors.py` (`AfiError`, exit codes), `cli/_output.py` (stdout/stderr split, `--json`), `cli/_commands/explain.py`, and `explain/` (catalog resolver).
- **`shape-adapt`** — keep the structure, rewrite the content for the host tool. Covers `cli/__init__.py` (parser + `_dispatch`, including the `_ArgumentParser` override and try/except), `cli/_commands/learn.py` (TEXT body + JSON payload), `explain/catalog.py`, the package `__init__.py` / `__main__.py`, and `tests/test_cli.py`.

### Hard contracts to preserve

These are checked by `afi cli verify` and must not be weakened when shape-adapting:

1. **Exit-code policy** — `0` success, `1` user error, `2` env error, `3+` reserved. All failures raise `AfiError`; the dispatcher catches and exits with `err.code`. No Python traceback ever leaks.
2. **stdout/stderr split** — results to stdout, errors and diagnostics to stderr, even in `--json` mode. The streams are never mixed.
3. **Errors have shape `{code, message, remediation}`** — text-mode errors render as `error: <msg>\nhint: <remediation>`. The `hint:` prefix is required by the rubric.
4. **`_ArgumentParser` override** — argparse errors must route through `emit_error` so unknown verbs/flags exit with `error:` + `hint:` and no traceback.
5. **Globals `learn` and `explain` exist at the top level** — not nested under a noun. New command groups are registered as siblings via `register(sub)` in `cli/__init__.py` at the marked location.
6. **`learn` output rubric** — stdout ≥ 200 chars, mentions purpose, commands, exit codes, `--json`, and `explain`. `learn --json` is parseable with stderr clean.

### `afi cli verify` rubric bundles

The five bundles checked by the verifier (run from the host project root once code exists):

1. **Structure** — `pyproject.toml` with `[project.scripts]`, `tests/` dir, `<tool> --help` exits 0.
2. **Learnability** — `<tool> learn` exits 0, stdout ≥ 200 chars, mentions purpose / commands / exit codes / `--json` / `explain`.
3. **JSON** — `<tool> learn --json` parseable; stderr clean on success; `<tool> explain --json` works.
4. **Errors** — bogus verb exits non-zero with a `hint:` line and no Python traceback.
5. **Explain** — `explain`, `explain <tool>`, and bogus-path-failure with hint all work.

After integration completes and the rubric passes, the reference can be removed (`rm -rf .afi/reference/`) or refreshed with `afi cli cite`.

## Workspace context (Culture / AgentIRC)

This repo lives under `~/git/` alongside `culture` and `citation-cli`. Worth knowing when scoping changes:

- **Culture** is an IRC-based agent mesh; AgentIRC is the protocol/IRCd component. `irc-lens` is its **lens** CLI — read-only-ish observability/inspection tooling, by name. Confirm exact scope against the parent project before adding write paths.
- **citation-cli** (`afi`) provided the `.afi/reference/` here. The pattern is *cited, not imported* — this project owns its copy and may modify it, but divergences from the rubric should be deliberate.
- **All-backends rule** (Culture): if a feature lands on one agent backend (`claude` / `codex` / `copilot` / `acp`), it must be propagated to all of them. If `irc-lens` grows backend-aware code, that rule applies here too.

## Tooling expectations once code exists

The parent workspace `CLAUDE.md` (`~/git/CLAUDE.md`) sets the defaults — none of this is enforced *yet* because there's no code, but the next contributor should expect:

- **`uv`** for dependency management: `uv venv && uv pip install -e ".[dev]"`, then `pytest`.
- **Linting**: `flake8`, `pylint`, `bandit -r src/`, `black`, `isort`. Markdown via `markdownlint-cli2`.
- **Versioning**: a single source of truth in `pyproject.toml` / `irc_lens/__init__.py`. Bump before PRs.

When wiring `pyproject.toml`, register the CLI under `[project.scripts]` as `irc-lens = "irc_lens.cli:main"` (matching the reference's `cli.main`).
