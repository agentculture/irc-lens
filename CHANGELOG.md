# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.13.0] - 2026-10-10

### Added

- **App sign-in.** Approved users sign in inside the app: password, then a
  10-minute single-use emailed code (`POST /entry/signin`, `POST
  /entry/code`), then a server-side `lens_session` cookie (7 days idle, 30
  days total) and `POST /logout`. No Cloudflare Access round trip; `/login`
  stays as break-glass. Approved users remain `allowed_emails` (no sign-up).
- **Set or reset password** by emailed single-use link (`/password`,
  `/password/<token>`, 12-character minimum, argon2id); setting one ends
  that email's sessions.
- **Delivery alerts.** On a mail provider failure or quota exhaustion the
  lens posts one alert per kind per hour to `guest_mode.mail.alert_url`
  (auth via `guest_mode.mail.alert_secret_env`) for a small Cloudflare email
  Worker to forward.
- **Guest limit.** `guest_mode.max_guests` (default 1): further visitors see
  a busy page and no code is mailed.
- Owner metrics: `signin_codes_sent`, `sessions_started`, `sessions_ended`,
  `guest_busy`, `delivery_alerts`.
- **Trusted browsers and an escalating sign-in budget** (owner decision
  r6). A **Trust this browser** checkbox on the code screen (unchecked by
  default) sets `lens_device` (one year, only its sha256 stored per email);
  that browser has no sign-in attempt limit for that email; logout keeps it,
  setting a password revokes it. Untrusted browsers share one per-email
  budget of password and code attempts: 3 per 15 minutes, 2 per 30 minutes
  after exhaustion until 24 quiet hours, plus 3 attempts per 15 minutes
  per IP (password and code together); blocked attempts look exactly like
  unblocked ones. Replaces the per-email 5 code tries, the 5 codes per 15
  minutes and the per-IP 5 password checks on the app sign-in path.
- `auth.app_signin.enabled` (default true) and `auth.app_signin.base_url`.
- `tests/test_log_hygiene.py`: end-to-end check that no log record contains a
  password, code, token, session id or cookie value.

### Changed

- Guest idle sign-off (`guest_mode.idle_close_s`, default 900) now counts
  actions (messages and commands), not open tabs.
- Docs: `docs/cli.md` and `docs/guest-mode-config.md` describe all of the
  above, including the rollback switch.

### Security

- Rollback: `auth.app_signin.enabled: false` restores the 0.12.2 password ->
  `/login` behavior.
- Set-password tokens are redacted from request-path logs; no log line
  contains a code, session id or password. The Cloudflare tunnel and cache
  configuration are unchanged.

## [0.12.2] - 2026-10-10

### Changed

- Guest data deletion warns before the **Delete my data** button that it
  is permanent and nothing is kept, and the done page now says the data was
  deleted instead of "Done.".
- The deletion email has its own subject and text (it reused the guest-seat
  sign-up email), still identical for every address.

### Fixed

- A guest typing `/delete` in the chat is redirected to the deletion page
  instead of getting "Not in guest view"; `/delete` is listed in the guest
  palette and help pane as a page link.

## [0.12.1] - 2026-10-10

### Fixed

- **Guest code mail never sent via Resend.** The adapter posted with
  urllib's default `Python-urllib/3.x` User-Agent, which Cloudflare (in
  front of Resend's API) rejects with error 1010 / HTTP 403. Requests now
  carry `User-Agent: irc-lens/<version>`; verified live against Resend's
  `delivered@resend.dev` test inbox.

### Added

- **`culture.yaml` with a `gate:` section only** (no `agents:`): the test
  gate the culture-rules PR fixer runs (`uv sync --extra dev`, then
  `uv run pytest -q`, same selection as the CI `test` job without coverage
  or Sonar) before it pushes a fix. irc-lens still declares no mesh agent.
  No version bump: nothing shipped changes.

## [0.12.0] - 2026-10-10

### Changed

- **Guest deletion keeps nothing** (owner deviation d9). The anonymized
  Q&A corpus is gone: deletion no longer copies anything, `keep_corpus`,
  `list_corpus` and `corpus.anonymized_pairs` are removed, the export has no
  `anonymous`/`qa` rows, and an existing `corpus` table is dropped on
  startup.
- **The deletion log stores only a hash**: `deletions.email` holds
  `sha256(lower(email))` (`guest_store.email_hash`); existing plaintext rows
  are hashed on startup.
- **The redacted export includes only opted-in guests**: a guest's inputs
  are exported only when their most recent consent row has `train = 1`.

### Added

- **Separate, optional training consent** on the guest step: "Use my
  conversations to improve culture.dev's models (optional)", unchecked by
  default and not required. Stored as `consents.train` (migrated with
  `ALTER TABLE` on existing stores); `record_consent(..., train=)`,
  `GuestStore.training_opt_in`, `training_emails` and `set_training` (for
  owner-applied withdrawal).
- **Retention sweep** (`GuestStore.sweep`, `web.retention`): at startup and
  hourly while guest mode is on, guests inactive for
  `guest_mode.retention_days` (new, default 90) are erased through the same
  path as a self-service deletion (`deletion.erase_guest_data`: store rows,
  uploads, flag-log lines); tokens and rate-limit attempts older than a day
  and bans older than 365 days expire.

## [0.11.0] - 2026-10-09

### Added

- **Guest mode for chat.culture.dev** (culture spec `guest-mode-sandbox`;
  config `guest_mode:`, see `docs/guest-mode-config.md`, off by default).
  One site for everyone: approved users sign in through a path-scoped
  Cloudflare Access app on `/login`; anyone else can enter as a guest.
  - **Entry card** (`/`, `/entry/*`): email, then one identical window for
    every address (password + "Guest mode"), so membership is never
    revealed — same body, status and padded timing. Approved email +
    correct argon2id password -> `303 /login` (Access SSO). Guests choose
    a nickname, accept the Terms and Privacy Policy (versioned, read from
    `legal_version_url`), and enter a single-use code emailed to them
    (15 min). Optional Turnstile bot check; per-IP and per-email rate limits.
  - **Tiers**: approved (Access JWT + allowlist), guest (signed
    `lens_guest` cookie, HttpOnly/Secure/SameSite=Strict), anonymous
    (deny by default). CSRF middleware on state-changing routes.
  - **Sandbox routing**: guests only ever connect to the isolated sandbox
    IRCd. Each guest chats with sbx-ask in a private room
    `#g-<random id>`; guests never see each other. Approved users can open
    "Guest view" (`/sandbox/enter`, nick `sbx-op-<name>`) to watch and
    switch between every guest room, and go "Back to mesh".
  - **Guest data**: the guest store (SQLite) holds consents, the guest's
    messages and sbx-ask's answers. Self-serve deletion at `/delete`
    (emailed code) erases everything linked to the guest — profile,
    inputs, consents, tokens, flags, room id, uploads and their sbx-ask
    flag-log lines — keeping only an anonymized Q&A corpus (no email, IP,
    nick or room; PII-scrubbed; day-only date) and the deletion record.
    `irc-lens guests export` emits a PII-redacted transcript plus the
    corpus.
  - **Moderation**: `irc-lens guests {passwd,ban,unban,list,flags,export}`;
    `ban --flag <id>` blocks a guest from sbx-ask's NSFW flag log; banned
    sessions drop within the sweep interval. Guests may only run sandbox
    commands (`/help`, `/who`, `/me`, `/read`) and are rate-limited.
  - **Owner metrics** at `/owner/metrics` (entries, active guest sessions,
    429s, failed sign-ins, agent state).
  - `scripts/cf_access_login_path.py`: narrow (or restore) the existing
    Access app to `<host>/login`, dry-run by default.
- **Chat UI uplift (Option A)**: header shows tier badge, nick and room
  (never host:port); inline command palette on `/`, tier-aware; live
  "In this room" member list; agent online/offline badge by room
  membership; phone layout with a rooms drawer; empty state hints.

### Fixed

- `GET /login` no longer 404s after Access SSO; it redirects to `/`.
- The chat log opens at the newest message and only follows new lines
  while you are at the bottom.
- Room rows keep working after a live sidebar update (htmx re-processed).
- No CSP violations: htmx indicator styles off, `allowEval` off, event
  filters replaced by a delegated keydown handler.
- SSE keepalive every 15 s so closed tabs unsubscribe; idle sandbox
  sessions close after `guest_mode.idle_close_s`.

## [0.10.0] - 2026-07-07

### Added

- **`GET /residents` — culture resident-presence page** (issue #53; culture
  presence-v1 plan task t8): a standalone, authed console page rendering
  culture's live resource view — one row per resident (nick, server, state,
  since, current-task hint, tokens in/out, budget % with warning highlight,
  presumed-hung flag), sorted by nick, every nullable field as a dash. The
  payload is fetched **server-side** from culture's loopback-only
  `/residents.json`: the endpoint is discovered via the overview server's
  port file (`culture.overview_name`, defaulting to `server.name`) or an
  explicit `culture.residents_url` override, so it needs no public exposure
  and Cloudflare Access stays the only auth surface. Hard graceful degrade —
  every upstream state renders HTTP 200 with a kind-specific notice, never
  an error page: `supported: false` → "presence pending the agentirc
  release (agentirc#53)", upstream 503 → "IRCd down", endpoint unreachable
  or unconfigured → "resource view unavailable". New optional `culture:`
  config section (validated, in the `config init` starter), a help-pane
  mention, an in-tree fake culture endpoint server for tests, and 39 new
  tests across config, fetch classification, rendering, and the route.
  Spec and plan exported via devague under `docs/specs/` and `docs/plans/`.
  Auto-refresh is deliberately deferred (each upstream request costs
  culture a fresh IRC connect+register).

## [0.9.2] - 2026-07-04

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

## [0.9.1] - 2026-07-03

### Fixed

- Retired the one-shot `test_adoption_diff_does_not_touch_forbidden_paths`
  boundary guard from `tests/test_adoption_boundaries.py`. It asserted the
  agentfront-adoption branch had a nonempty `git diff main...HEAD`; once
  PR #51 merged into `main` that diff became empty and the test failed on
  `main`. The remaining boundary guards (media auth-exempt paths,
  no afi-scaffolding references) are unaffected.

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
