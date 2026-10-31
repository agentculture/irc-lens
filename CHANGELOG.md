# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.18.1] - 2026-10-31

### Fixed

- The console no longer shows the Guest view button, the /sandbox palette and help entry, or accepts the /sandbox command for non-admin members when email permissions are on; the enter and leave routes were already admin-only. One helper, may_use_sandbox_view, now decides for the routes and the page. Legacy mode is unchanged.

## [0.18.0] - 2026-10-31

### Added

- Approved users choose a nickname (q14). With email permissions on, an approved
  user or admin without one is asked to pick it right after sign-in, before the
  console, and gets no IRC session until they do (this covers existing users and
  the operator after the one-time import). Their IRC nick is
  `<server>-<nickname>` and their agent's `<server>-pa_-<nickname>`; nothing is
  derived from the email any more, so other users never see part of an address.
  Nicknames follow the guest rules, are unique without regard to case, and
  refuse addresses, reserved names, the `pa_` agent namespace and nicks a
  resident agent or anyone on the mesh holds.
- Admins rename users from a nickname field on `/admin` or with `irc-lens users
  nick <email> <nickname>`; `irc-lens users add` takes `--nick`. A rename closes
  the user's IRC sessions and agent connection so they reconnect under the new
  nick; the agent credential is revoked and must be re-issued. Setting and
  renaming are audited with the nickname as target, never the email.
- The own-agent DM rule (0.17.1) follows the chosen nickname: a member reaches
  `<server>-pa_-<nickname>` on the configured server name, and after a rename
  only the new agent nick. The nickname sweep reads the store under one pinned
  change stamp with the short hot-path busy timeout; a locked store closes
  nothing that round. An agent call that finds the store locked is refused as
  busy without dropping the agent's connection.

### Changed

- The access store's `users` table gains a `nickname` column (additive
  migration, unique index) and a `nickname_holders` table: every nickname a
  user has held stays reserved for them after a rename or a removal (another
  user is refused "taken", the same user may take it back). The migration runs
  in one `BEGIN IMMEDIATE` transaction, so two processes opening an old store
  at once no longer race on the `ALTER`.
- A rename revokes the user's agent credential in the same transaction, so the
  old token stops verifying at once and an agent nick never maps to two
  humans.
- docs/permissions.md: who sees whom (guests invisible to everyone; approved
  users and admins see each other in granted channels, by nickname).
  docs/cli.md: `users nick` and `users add --nick`.

### Fixed

Review of PR #86, before release:

- A person's own choice (`POST /nickname`) only ever sets a first nickname
  (`AccessStore.claim_nickname`, one conditional update): a second in-flight
  POST from another tab gets the console and changes nothing, instead of
  renaming.
- `irc-lens users nick` and `users add --nick` refuse a nick a resident agent
  holds (the configured culture residents list, as `/admin` checks it); the
  CLI cannot see live rosters, so the IRCd keeps the final say.
- `users add --nick` approves the user and sets the nickname in one
  transaction: a refused nickname adds nobody.
- The nickname gate no longer blocks deleting one's data or consenting:
  `/delete`, `/delete/request`, `/delete/confirm` and `/consent` are exempt.
- An agent whose human was removed or is not approved (including an
  admins-only switch) is told its access ended (logged
  `reason=human-not-approved`); only a real nick mismatch gets the "re-issue
  the agent credential" advice.
- `/nickname` and `/admin/users/nickname` show a form error on a store error,
  never a 500, and the admin rename reads and writes the store off the event
  loop.
- Every rename reconnect (the console's own reopen, an admin rename, the
  nickname sweep) goes through one registry helper that also forgets which app
  session opened the old connection, so signing out on the old device no
  longer closes the session another device reopened under the new nick.
- Every nickname check uses `normalize_approved`.

## [0.17.1] - 2026-10-31

### Fixed

- A non-admin may DM, /who and /read their own personal agent (and no other nick); DMs to other humans and to mesh agents stay refused.
- An agent's MCP read past its limit leaves the rest unread (returned oldest first next time); concurrent agent tool calls no longer see each other's errors.
- An invited user is promoted to approved on any verified sign-in, including the Cloudflare Access /login path.
- Agent nicks use the configured server name, so server names containing a dash work.
- The admin page can set and clear a guest's agent extension; irc-lens extend audits as system.
- The per-line grant check reads the store's change counter once per line, sweeps revocations only after a change, and fails closed after a 0.25 s busy wait instead of blocking the event loop.
- Owner metrics count the audit table with one GROUP BY off the event loop.
- The last-admin guard is one atomic store operation shared by /admin and the CLI.
- The admin-only command set moved out of the cited commands.py, which is byte-faithful again.

## [0.17.0] - 2026-10-31

### Added

- Email-keyed permissions: admins have full access; other approved users reach only the mesh channels granted to them, at read or write/ask, and nothing by default. Approval, roles and grants live in the lens store (one-time import of the old allowlist) and take effect without a restart.
- In-app admin page (/admin): add or invite users, set roles and channel grants, approve n-hour agent extensions. Invited users become approved on their first sign-in. Matching CLI verbs: `irc-lens users`, `grants`, `agents` and `extend`.
- Personal agents over MCP: every human (admin, approved or guest) may connect one agent to the remote MCP endpoint (streamable HTTP, path from config) with a credential issued at /agent-credential. The agent has exactly its human's permissions, its own lens-held IRC connection in a reserved nick namespace, and only its own human can reach it. Lines from anyone else that DM or @-address it are withheld, and every delivered line is marked owner or not-owner.
- Switches: an agent kill switch and a fail-closed off switch (enabled / legacy before migration / admins-only after). Refusals and grant/credential events are audited, and owner metrics show counts.
- docs/permissions.md and docs/deployment-permissions.md (restricting the mesh IRCd, plus a verify script).

### Changed

- Admin-only: the sandbox guest view, owner metrics and the residents page. /topic, /kick, /invite and /icon are admin-only, and listing views are filtered to the viewer's grants.
- Sign-in, set-password and the guests CLI decide approval from the store when permissions are enabled.
- Guests: bans, deletion, retention and idle sign-off also cover the guest's agent, and the consent record names it.
- The auth log line carries a hashed `who=` instead of the email. No log line carries an email.

## [0.16.1] - 2026-10-30

### Fixed

- The entry page's first screen now carries the trademark sign: the hero
  reads "Ask CULTURE.DEV™." (the most prominent CULTURE.DEV use on the
  sign-in page), not only the later password step. The ™ is a small
  superscript (`&trade;`, the same entity the chat wordmark uses), so it
  no longer fills a full monospace cell at heading size.
- The password step no longer repeats the mark: its separate
  "CULTURE.DEV™" line is gone now that the hero carries the ™ on every
  entry step (one ™ per surface).

## [0.16.0] - 2026-10-30

First irc-lens release exposing the CULTURE.DEV brand on chat.culture.dev —
first-use evidence for the chat surface of the CULTURE.DEV trademark (culture
spec `docs/specs/2026-10-30-culture-dev-on-all-public-surfaces.md`).

### Added

- Page `<title>`s, the entry hero ("Ask CULTURE.DEV."), a CULTURE.DEV™ brand
  line on the sign-in step, the chat header wordmark (CULTURE.DEV™, logo alt
  `CULTURE.DEV`), og/description meta, and the notice "CULTURE.DEV is a
  trademark of Ori Nachum." in the chat UI.
- `TRADEMARKS.md`.

### Changed

- Phone layout (≤760px) keeps the CULTURE.DEV wordmark visible (logo and
  identity hide first).
- Emails sign off "The CULTURE.DEV team"; the documented sender is
  `CULTURE.DEV <no-reply@culture.dev>`. Subjects keep the hostname.
- PyPI summary names CULTURE.DEV.

## [0.15.1] - 2026-10-26

### Fixed

- **A defused agent tag no longer looks like a real one.** The sandbox
  agents defuse a tag to another agent the guest didn't ask them to involve
  (culture 14.7.0: "@", a zero-width space, the nick), so "@sbx-bolt" in
  sbx-ask's answer looked like a tag but bolt, correctly, never answered.
  The lens now shows it in red, with a small red note under the line: "Not
  a tag: sbx-bolt won't see this. To ask sbx-bolt, start your message with
  @sbx-bolt." The zero-width space stays, so it is still defused for the
  agents. Only lines *from* an agent in the room are marked (sandbox
  sessions only; a guest's pasted defused tag does reach the agent), only
  the room's agent nicks count, matched with the agents' own `@nick\b`
  boundary, and never inside a URL (the text between URLs is searched, so
  link markup stays intact). One pass renders the markup and the note.
  Real tags and plain lines are unchanged.

## [0.15.0] - 2026-10-26

### Added

- **"sbx-ask is thinking" in sandbox rooms.** After a guest (or an approved
  user's Guest view) sends a message in a room the sandbox agent is in, the
  room shows "`sbx-ask` is thinking" with animated dots under the log until
  the agent answers there (or 90 s pass). With several agents in a room, a
  message that tags one (`@sbx-bolt …`, from the guest or from another
  agent) shows that agent thinking instead. New SSE event `typing`
  (`_typing.html.j2`, `#typing`, a polite live region; dots are static
  under `prefers-reduced-motion`). Never shown on the real mesh. Lens-side
  for now: the real IRCv3 `TAGMSG +typing` signal needs AgentIRC and the
  agent first.

## [0.14.0] - 2026-10-26

### Changed

- **Guests sign in with just their email, through Sign in.** The Guest mode
  button is gone: with app sign-in on, Sign in is the only entry. An email
  that is not an approved user gets a single-use guest code mailed on Sign in
  (whatever was typed as the password; the page now says *No password? Leave
  it empty. We'll email you a code.*), from a background task and bound to
  the browser, on the same code screen, status, cookie and time floor as an
  approved sign-in, so nothing tells a guest from a signed user before the
  code. Before, a visitor without a password pressed Sign in, saw "a code is
  on its way" and never got one, and the Guest mode path buried the code mail
  three screens deep behind a nickname form.
- **Guest sign-ins never use up an approved user's password attempts, and
  guest mail reveals nothing about them.** The password-attempt limits (per
  IP, per-email budget) count approved emails only (found in a real-browser
  click-through: two guests and one typo from a shared IP silently blocked
  the owner's right password). Guest mail has its own cap,
  `rate_limits.entry_per_min` per email and per IP, counted for every
  submission alike, so an attacker's own inbox can't tell whether some
  other email is approved (found in review: with a refund-based fix, 3
  wrong passwords then a guest sign-in from the same IP did). Over the cap
  nothing is mailed and the page is the same.
- **Nickname and consent are a one-time form after the code**
  (`POST /entry/welcome`). A returning guest whose consent matches the
  current Terms/Privacy versions goes straight in; a version change asks once
  more.
- **"Sandbox busy" only after a right code.** The guest code is mailed even
  while the seat is taken; the busy page comes after the code, which stays
  usable for a retry.
- The code-screen note now reads "Check your email for a code. Nothing after
  a minute? Go back and check your email and password." for everyone.
- With `auth.app_signin.enabled: false` (rollback) the 0.12.2 Guest mode
  button flow is unchanged; with app sign-in on, `/entry/guest`,
  `/entry/guest/start` and `/entry/verify` answer 404.

### Added

- **CI fails a PR with an unresolved SonarCloud issue.** A code smell leaves
  the quality gate green, so a new step after the scan
  (`scripts/sonar-pr-issues.py`, cited verbatim from culture-rules, with its
  tests) waits for the analysis of the PR's head commit and fails while any
  issue is OPEN or CONFIRMED; ACCEPTED and FALSE_POSITIVE ones pass.
- **Guest log out** (`POST /guest/logout`, the guest's Log out button):
  closes the guest's sandbox session, frees the guest seat at once and
  clears `lens_guest`. Before, a guest could only free the seat by deleting
  their data or waiting out the 15-minute idle sign-off.

### Fixed

- **The Enter key submits Verify / Enter, not Back.** On the code and guest
  steps the first submit button in the form was Back, so pressing Enter in
  the code or nickname field went back a step. The forward button is now
  first in the DOM (shown on the right).
- The email step shows its error line (used by an expired welcome form).

## [0.13.1] - 2026-10-10

### Fixed

- **App sign-in code entries are no longer limited or counted** (owner
  decision c41, deviation d7). On go-live a second browser on the same IP had
  its correct code silently refused: the first browser's sign-in had used the
  per-IP slots, and code entries counted toward the per-IP limit and the
  per-email budget. A code is 256 random bits, valid once for 10 minutes and
  only in the browser that entered the right password, so guessing one is
  infeasible; counting code entries only locked out the real user. The per-IP
  limit (3 per 15 minutes) and the per-email untrusted budget now count
  password submissions only, and any number of wrong code entries leaves the
  right code working. Blocked password attempts still look exactly like
  unblocked ones.
- **Only wrong passwords count** (owner decision c42, deviation d8, from
  review of this fix). A correct password is taken back from the per-IP
  limit and the per-email budget once checked, so several new browsers on
  one home IP no longer lock each other out at the password step. The count
  is never visible, so this reveals nothing; brute force stays fully
  limited, and a blocked attempt (never checked) is not refunded.

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
- **New-browser sign-in notice** (owner decision c40). A completed sign-in
  from a browser not trusted for that email mails the user the time, IP and
  browser, and how to reset the password if it wasn't them. Trusted
  browsers get none, even from a new IP.
- `auth.app_signin.enabled` (default true) and `auth.app_signin.base_url`.
  App sign-in needs a mail provider: with `guest_mode.mail.provider: none`
  it stays off (approved users use `/login`) and the console logs why.
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
- `auth.allowed_emails` is compared without case on every app sign-in path
  (session check, revocation sweep, set-password), as it already was at the
  password step.
- A set-password request with a non-https base URL issues no token.
- Delivery alerts for HTTP 429 say "quota used up or rate-limited" (Resend
  uses 429 for both); finished alert threads are no longer kept.

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
