# Delivery Summary — Email-keyed permissions + one sponsored agent

plan: `email-keyed-permissions-one-sponsored-agent` · run: `partial` · date: `2026-10-31`
baseline: `devague summary skeleton`

## Intent

> The lens console grants each user permissions by email: admins have full access, other approved humans reach only their granted mesh channels, guests stay in their separate sandbox, and each human may bring one personal agent over the lens's single MCP endpoint — authenticated through the human's email, present wherever its human is with the same permissions, and reachable only by its own human

After: Each human sees exactly their granted channels at their granted level (admins see everything), and can connect one agent over MCP that acts within the same scope and that no other human can reach

The run executed the 19-task plan across four repos (irc-lens, agentfront,
culture, katvan), then a staging browser pass and the production cutover on
2026-10-31. The run is `partial` for one reason only: every task merged, but
c31 (the mesh IRCd accepts only the lens, agents and links) holds for this
host's IRCd and not yet for every server the mesh now links to (see Drift and
Remaining Work).

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Access store: users, roles, grants, agent credentials, extensions and audit tables
- `t2` — irc-lens mounts agentfront's remote MCP transport with the lens authenticator
- `t3` — Mesh IRCd reachable only by the lens, mesh agents and links (operational)
- `t4` — culture: sandbox agents treat visiting personal agents as agents (hand-off)
- `t5` — katvan: Terms/Privacy clauses for personal agents and approved users with channel access (hand-off)
- `t6` — Policy module: role and grant resolution, default-deny
- `t7` — Approval moves to the store: identity, roles, one-time import, enable gate
- `t8` — CLI: irc-lens access verbs for users, grants, agents and extensions
- `t9` — Session enforcement: every channel path checks grants; listing views filtered; channel-changing commands admin-only
- `t10` — Admin-only routes and backend selection
- `t11` — In-app admin: add/invite users, edit grants, approve agent extensions
- `t12` — Agent credentials and endpoint authentication
- `t13` — Agent session: lens-held IRC connection, reserved nicks, owner filtering, shared input gate
- `t14` — Guest integration: seat, bans, deletion, retention, idle + extension, consent names the agent
- `t15` — Fail-closed switches, refusal audit wiring and metrics
- `t16` — Docs: permissions, agents over MCP, and before/after for operators
- `t17` — End-to-end suite: humans, agents and the 0-cross-delivery check
- `t18` — Staging browser pass and production cutover
- `t19` — agentfront: remote streamable-HTTP MCP transport with a caller-context hook (agentfront#52, hand-off)

## Actual Delivery

All 19 tasks accounted for: 18 delivered, 1 partial.

| Plan task | Status | What actually landed |
| --- | --- | --- |
| `t1` | delivered | `src/irc_lens/access_store.py` (users, roles, grants, credentials, extensions, audit); irc-lens PR #84 (0.17.0) |
| `t2` | delivered | `src/irc_lens/web/mcp_http.py` mounts agentfront's transport at `web.mcp_path` (default `/mcp`) with the lens authenticator; PR #84 |
| `t3` | partial | Production: this host's IRCd bound to loopback via a systemd drop-in on 2026-10-31 (the unit's `--host` flag overrode `mesh.yaml`); `scripts/verify_mesh_ircd.py` + `docs/deployment-permissions.md` shipped in PR #84. Missing: the mesh grew to four servers the same day; one linked server still listens on all interfaces and two bind their tailnet address with no source firewall (evidence e31) |
| `t4` | delivered | culture PR #505 (14.11.0): `personal_agent_prefixes` loop guard; production sandbox agents configured with the `sbx-pa_-` prefix at cutover |
| `t5` | delivered | katvan PR #76 (0.13.0): Terms/Privacy 1.3 live |
| `t6` | delivered | `src/irc_lens/access.py` default-deny policy; PR #84 |
| `t7` | delivered | approval, roles, one-time import and zero-admin enable gate in the store; PR #84 (+ `d1` sign-in/set-password/guests CLI) |
| `t8` | delivered | `irc-lens users`, `grants`, `agents`, `extend` (+ `users nick` in PR #86); PR #84 |
| `t9` | delivered | `Session` grant checks on every channel path, filtered listings, admin-only channel-changing commands; PR #84 (+ `d2` registry wiring) |
| `t10` | delivered | admin-only routes + `_backend_for`; PR #84; Guest view control and `/sandbox` additionally gated in PR #87 (0.18.1) |
| `t11` | delivered | `/admin` add/invite users, grants, agent extensions; PR #84 |
| `t12` | delivered | `src/irc_lens/web/agent_auth.py` hashed bearer credentials, one per human; PR #84 |
| `t13` | delivered | `src/irc_lens/web/agent_sessions.py` lens-held connection, `<server>-pa_-<nick>` namespace, owner-only receive filter, shared input gate; PR #84, review fixes PR #85 (0.17.1) |
| `t14` | delivered | seat, bans, deletion, retention, idle + n-hour extension, consent names the agent; PR #84 |
| `t15` | delivered | kill switch + three-state off switch, refusal audit, metrics; PR #84 (+ `d4`) |
| `t16` | delivered | `docs/permissions.md`, `docs/deployment-permissions.md`; PR #84 (+ `d5` host-literal ratchet) |
| `t17` | delivered | `tests/test_e2e_permissions.py` (0 cross-human deliveries); PR #84 |
| `t18` | delivered | staging: 31/31 real-browser checks on 0.18.1 + agentfront 0.21.2; production cutover completed 2026-10-31 (permissions and agents on, owner signed in as admin) |
| `t19` | delivered | agentfront PR #56 (0.21.0) `mcp_http`, `mount_aiohttp`, `current_caller`; follow-ups #57, #58, #59 |

## Mid-work Decisions

- `d1` — t11 also switches sign-in (web/entry.py x3), set-password (web/setpw.py) and the guests CLI (cli/`_commands`/guests.py) from the config allowlist to the access store, and wires t7's `install_access` into `make_app` — t7 was scoped to four files, so after its merge an invited (store-only) user would still be treated as a guest at sign-in and could not set a password, and `install_access` was never called at startup; no task covered it
- `d2` — The main agent wires Session.`set_access`(store, email) into the session registry (web/sessions.py `get_or_open`) for mesh sessions when access is enabled, as an integration commit before wave 4 — t9 built enforcement that activates only via `set_access`, and no task before t14 owned the registry; as merged, non-admin mesh sessions were unrestricted
- `d3` — t16 is assigned to a native sonnet subagent instead of colleague — colleague finished 0 of 2 tasks this run (t3, t8 both needed native finishers)
- `d4` — t15 also edited app.py, `app_session.py`, auth.py, setpw.py and routes.py (beyond its named files), changed the operator auth log line from `principal=<email>` to `who=<hash>`, and defined a three-state off switch (enabled / legacy before migration / admins-only after) — its 'no email in any log line' and fail-closed-off criteria could not be met inside the three named files; the plan's web/metrics.py does not exist (module is src/`irc_lens`/metrics.py)
- `d5` — Boundary c23 ('no host/address literal in src/') is met by a ratchet: 9 pre-existing files keep their 26 legacy literals (cannot grow), no other file may hold any; full removal is a follow-up issue — t16's scan found the production host hardcoded in pre-existing mail, config, sign-in and static files that predate this feature
- Visibility and names (owner decisions q13–q15, after the plan): guests stay invisible in their sandbox; approved users and admins share real rooms per grants and see each other; approved users choose a nickname on first sign-in, never derived from the email, kept reserved after removal or rename, with no self-rename — shipped as PR #86 (0.18.0); delta `b1`.
- Members (approved non-admins) may DM only their own agent — an owner-review finding, stricter than c3's agent-only rule; PR #85; delta `b2`.
- Staging found two defects, both fixed before cutover: members were shown the Guest view control (PR #87, delta `b3`), and MCP tool errors came back with `isError: false` (agentfront PR #58, delta `b4`).
- Production: `mcp` 2.x removed the low-level server API agentfront uses; a fresh install pulled it. agentfront 0.21.3 (#59) caps `mcp<2`, and culture 14.11.1 (#507) raises its pins (`agentfront>=0.21.3,<0.22`, `irc-lens>=0.18.1`), because culture's old `agentfront<0.21` cap made a fresh install silently fall back to irc-lens 0.16.1.
- Production: loopback binding needed a systemd drop-in, because the server unit's `--host 0.0.0.0` flag overrides `server.host` in `mesh.yaml`.
- Mesh (same day, owner request): two more servers joined, giving a full mesh of four; the new servers bind their tailnet address; link secrets moved host to host through pipes, never printed. The previously linked server needed a stale pidfile cleared and a new keyring (its old keyring password was unknown).

## Drift From Plan

| Plan item | Reason for divergence | Classification |
| --- | --- | --- |
| `t11` (`d1`) | t7 was scoped to four files, so after its merge an invited (store-only) user would still be treated as a guest at sign-in and could not set a password, and `install_access` was never called at startup; no task covered it | `acceptable` |
| `t9` (`d2`) | t9 built enforcement that activates only via `set_access`, and no task before t14 owned the registry; as merged, non-admin mesh sessions were unrestricted | `needs-follow-up` |
| `t16` (`d3`) | colleague finished 0 of 2 tasks this run (t3, t8 both needed native finishers) | `acceptable` |
| `t15` (`d4`) | its 'no email in any log line' and fail-closed-off criteria could not be met inside the three named files; the plan's web/metrics.py does not exist (module is src/`irc_lens`/metrics.py) | `acceptable` |
| `t16` (`d5`) | t16's scan found the production host hardcoded in pre-existing mail, config, sign-in and static files that predate this feature | `needs-follow-up` |
| `t3` | restriction verified for this host's IRCd only; the mesh grew to four servers and the linked servers are not restricted to the lens, agents and links (e31, `b5`) | `needs-follow-up` |
| `t13` / `t11` | chosen nicknames (q14) and same-room visibility (q13) added after the plan; no task covered them (`b1`) | `acceptable` |
| `t9` / `t13` | members may DM only their own agent, stricter than c3 (`b2`) | `acceptable` |
| `t10` | Guest view control and `/sandbox` gated beyond the planned 403 (`b3`) | `acceptable` |
| `t19` / `t2` | MCP tool errors flagged `isError: true`; `mcp<2` cap; culture pin raise — none in the plan (`b4`) | `acceptable` |
| `t18` | cutover needed unplanned operational steps (systemd drop-in, hand pin of `mcp`, two follow-up releases) before it could complete | `acceptable` |

The `d2` row keeps its recorded classification; the wiring it calls for was
done in-run (integration commit before wave 4) and is covered by
`tests/test_registry_access.py`.

## Evidence

Filed with `/validate-delivery` and confirmed by the owner on 2026-10-31:
evidence e1–e31, deltas b1–b5. Lapses l1–l9 are approved.

- tests (e1–e21, one record per obligation o1–o21): 90 mapped tests, 114 runs (parameterized), all pass at `6e69503` on 2026-10-31T21:56+02:00. irc-lens has no behavioral marker or folder, so the feature's own test modules are the behavioral set.
- tests (full suite) at `6e69503`: 1849 passed, 1 failed — `tests/test_guest_admin.py::test_passwd_stdin_sets_argon2id_hash`. It passes with an empty `HOME`; it reads the operator's real `~/.config/irc-lens/config.yaml` (no test isolation), which gained `email_permissions` at cutover. Not a product fault; see Remaining Work.
- tests (deltas): nickname suites + `test_e2e_permissions.py::test_members_see_chosen_nicknames_and_no_email` — 89 passed; member-DM tests — 9 passed (both at `6e69503`).
- staging (e22–e26): Playwright click-through, 5 browser contexts, 31/31 checks, 2026-10-31T17:31+02:00, irc-lens 0.18.1 + agentfront 0.21.2.
- production (e27–e30): `/mcp` 401 without or with a wrong bearer; `/admin` 403; `/nickname` and `/agent-credential` 401; Cloudflare `DYNAMIC` (not cached); store after migration holds exactly one admin; this host's IRCd refuses all 17 non-loopback addresses.
- production (e31, **fail**): one linked server accepts direct IRC on its LAN address; two bind their tailnet address with no source firewall.
- CI: irc-lens #87 test, playwright and SonarCloud green; agentfront #59 and culture #507 all checks green.
- commits: irc-lens `3855a1a..6e69503` (#84 `d08f0f1`, #85 `8dad6c3`, #86 `6c9849e`, #87 `6e69503`); agentfront #56 `bed4cf6`, #57 `2b39250`, #58 `071be69`, #59 `b61fcd7`; culture #505 `ae7fb37`, #507 `4e602c7`; katvan #76 `5f891dd`.

## Delivery Claims

Confidence is capped where an approved lapse touches the claim (l3/l4 → c22;
l5 → c40; l6 → c12, c36; l9 → c3). Every evidence record and delta is
owner-confirmed.

| Claim | Confidence | Evidence |
| --- | --- | --- |
| `c2` — one personal agent per human, authenticated through the human, counted as that human | high | e1 · `tests/test_access_store.py::test_concurrent_issue_leaves_one_active` · `tests/test_agent_auth.py::test_issue_again_replaces_and_closes_old_agent` |
| `c3` — a human can talk with their own agent and no other human's | medium (l9) | e2, e22 · `tests/test_e2e_permissions.py::test_zero_cross_human_deliveries` · staging 31/31 |
| `c10` — channel permissions enforced server-side on every path | high | e3, e23 · `tests/test_session_access.py::test_join_ungranted_refused_no_line` (+7) |
| `c12` — one HTTP MCP endpoint; hashed, bound, expiring, revocable bearer per agent; nick tied to its human | medium (l6) | e8, e27 · `tests/test_agent_auth.py::test_token_shown_once_and_stored_only_as_hash` · production `/mcp` 401 |
| `c13` — the agent is accounted as its human (seat, bans, deletion, idle, retention) | high | e9 · `tests/test_guest_agent.py::test_ban_refuses_and_disconnects_agent_and_removes_rows` (+5) |
| `c18` — admins see everything; for everyone else no grant means no channels | high | e4 · `tests/test_access_policy.py::test_unreadable_grants_fail_closed` (+5) |
| `c19` — two levels: read, and write/ask | high | e5 · `tests/test_access_policy.py::test_role_level_matrix` |
| `c20` — guests stay in the sandbox; approved users reach the mesh limited to grants | high | e6, e24 · `tests/test_admin_only_routes.py::test_backend_for_member_never_sandbox_even_if_stale_toggle` |
| `c21` — an agent has exactly its owner's permissions | high | e7, e25 · `tests/test_e2e_permissions.py::test_revoked_grant_mid_session_refuses_the_agents_next_calls` |
| `c22` — one MCP endpoint for all tiers | medium (l3, l4) | e11 · `tests/test_agent_auth.py::test_three_tiers_three_scopes_on_one_endpoint` |
| `c31` — the mesh IRCd accepts only the lens, agents and links | low — met for this host's IRCd only | e10, e30 (pass) · **e31 (fail)** · delta `b5` |
| `c32` — admin-only surfaces stay admin-only | high | e12, e28 · `tests/test_admin_only_routes.py::test_non_admin_approved_gets_403` · production `/admin` 403 |
| `c36` — owner-only filtering on the receive side covers every sender | medium (l6) | e13 · `tests/test_agent_sessions.py::test_raw_client_dm_never_reaches_the_agent` |
| `c37` — no identity-less tool path from the endpoint | high | e14 · `tests/test_agent_auth.py::test_production_front_exposes_no_identityless_tools` |
| `c38` — agent sends pass the human's gates | high | e15 · `tests/test_agent_sessions.py::test_guest_agent_sends_are_in_the_humans_input_log` (+3) |
| `c39` — every agent request re-checks its human | high | e16 · `tests/test_agent_auth.py::test_reapproval_is_rechecked_not_cached` |
| `c40` — rollback fails closed | medium (l5) | e17 · `tests/test_access_switches.py::test_disabled_after_migration_refuses_non_admin_the_mesh` (+5) |
| `c43` — allowlist imported once, then retired | high | e21 · `tests/test_access_store.py::test_import_admins_once` (+4) |
| `c44` — existing users become admins; zero admins refuses to enable | high | e20, e29 · `tests/test_access_approval.py::test_enable_with_zero_admins_refuses_with_hint` · production store: one admin |
| `c45` — admins add, invite and grant from within the app | high | e18, e26 · `tests/test_admin.py::test_invited_user_approved_only_after_signin_completes` · staging invite flow |
| `c46` — a guest's agent closes with its human unless an admin extension applies | high | e19 · `tests/test_guest_agent.py::test_live_extension_keeps_the_agent_after_its_human_signs_off` (+4) |

Lapse ledger evidence (all approved):

| Lapse | Code | What |
| --- | --- | --- |
| `l1` | `assumption-for-measurement` | s22 states server.yaml has no link block, but the probe only grepped for top-level keys links/peers/link; the file's real federation key name was not checked |
| `l2` | `provenance-missing` | s16 cites the guest /input gate (routes.py ~765-800) from a subagent's report; this pass did not re-read those lines |
| `l3` | `control-absent` | t19 (agentfront): tests were written first but their RED step was only an import error at collection (new modules absent), not failing assertions |
| `l4` | `control-absent` | t19 (agentfront): pre-commit hooks not run (not installed in the worktree); black/isort/flake8/markdownlint run by hand, pylint warnings left |
| `l5` | `control-absent` | t15: tests and implementation written together, no strict RED phase before implementing |
| `l6` | `assumption-for-measurement` | t13: nick-collision (433) and nick case rules were tested against the in-repo test IRCd's relay mode, modelled rather than measured on a real AgentIRC |
| `l7` | `provenance-missing` | t16: docs/permissions.md states /owner/metrics is admin-only and that guests need current consent for an agent credential from docstrings/source reading, not from running a test |
| `l8` | `control-absent` | t14: the stale-consent test drives the consent gate with a fake legal seam (a flag), not a real version comparison against the fetched legal document |
| `l9` | `control-absent` | t17: e2e assertions written with the tests, no RED step and no mutation check that the suite catches a disabled owner filter |

## Remaining Work / Follow-up

- `t3` / c31 — restrict the linked servers so the mesh also works with no internet: host firewall (or an IRCd source allowlist) admitting only mesh peers, and links over LAN addresses — culture#508 (the owner's chosen direction).
- `d5` — move the 26 legacy host literals into config: irc-lens#83.
- Presence lag — a guest signed off for idleness still sees "online" (irc-lens#75); the member-list lag noted in staging was not recorded and needs reproduction — #90 (related: #60).
- Test isolation — `tests/test_guest_admin.py::test_passwd_stdin_sets_argon2id_hash` reads the operator's real config; it needs a tmp `HOME`/config fixture — #89.
- culture — after a failed first handshake, this host's server did not retry the link to a peer until restarted (culture#510); its server was stopped once while culture's full test suite was finishing, suspected test touching the real pidfile (culture#511); a Python 3.13-only test failure (culture#512).
- Mesh operations — keyrings on headless servers start locked after a reboot, which drops their links until unlocked; the owner wants linking machines kept easy, so CLI moves for headless keyring unlock and link-secret exchange are proposed — culture#509 (today a helper script does it).
- c24 (audience) carries no instruction — non-blocking convergence warning.
