# Email-keyed permissions + one sponsored agent

> The lens console grants each user permissions by email: admins have full access, other approved humans reach only their granted mesh channels, guests stay in their separate sandbox, and each human may bring one personal agent over the lens's single MCP endpoint — authenticated through the human's email, present wherever its human is with the same permissions, and reachable only by its own human
> instruction: check each clause of c1 maps to a confirmed requirement in the exported spec

## Audience

- The lens operator (admin), the few approved users they invite, guests trying the app, and each human's one personal agent connecting as an MCP client

## Before → After

- Before: Approval is binary: every approved user can reach the whole mesh; guests get one private sandbox room; no agent can connect remotely (MCP is local stdio only); the IRCd itself has no channel or DM access control
- After: Each human sees exactly their granted channels at their granted level (admins see everything), and can connect one agent over MCP that acts within the same scope and that no other human can reach

## Why it matters

- Guests (and their one agent) get a real sense of how the app works for signed users
  - instruction: e2e: guest connects one agent via the MCP endpoint and exchanges lines with it in the guest's room

## Requirements

- Every human (approved or guest) may bring exactly one personal agent; the agent connects over MCP, is authenticated through its human's email, and counts as that human (same tier, same permissions, no extra seat)
  - instruction: test: guest + agent connected -> `active_guests` == 1; second agent credential for the same human -> issued, old one revoked and the old agent's connection closed
  - honesty: A human never has more than one active agent credential: issuing a new one revokes the old one and closes the old agent's connection; a connected agent never adds to the guest seat count
- A human can talk with their own agent; no human can talk with another human's agent (user A can't reach user B's agent, user A can reach user A's agent)
  - instruction: e2e: humans A and B with agents in one granted channel; B's DM and @mention of A's agent never reach A-agent's MCP read, A's do; symmetric for B
  - honesty: Only an agent's own human can drive it: other humans' DMs to it are refused, their lines that @-address it in a shared channel are withheld from its MCP reads, and every line the agent does receive is marked owner / not-owner
- Permissions keyed by email place an approved (signed) user on specific mesh channels, where they can read the content and ask agents about it; their agent inherits the same channels. Guests get no grants (q7)
  - instruction: test: user with grant on #x only -> /channels shows #x only; /join #y and HISTORY #y refused
  - honesty: A user granted #x can see #x, read its history and live lines, and (at write/ask) ask the agents there; channels they hold no grant for are neither listed nor joinable nor readable
- Per-email grants (channel + access mode) live in a new table in the lens sqlite store and are managed by an irc-lens CLI subcommand, taking effect without a restart — not in config.yaml, which is a frozen dataclass loaded once
  - instruction: test: CLI grant add/list/revoke round-trip on a temp store; config.yaml unchanged
  - honesty: Grants are rows in the lens store (email-keyed, never committed), editable through an irc-lens CLI verb, read on each check so changes need no restart
- Channel permissions are enforced server-side in the lens Session for every path — join, send, read/history, channels listing, roster/who, raw /input — not only by hiding channels in the UI
  - instruction: test matrix: each path x (granted, ungranted) for a non-admin approved user
  - honesty: Every session path that can reveal or emit channel content (join, send, read/history, channels, who/roster, raw /input, live PRIVMSG dispatch) consults the grant check
- The single remote MCP endpoint (HTTP, not stdio) authenticates each agent with its own bearer credential: issued through its human's existing approval path (allowlist sign-in for approved users, email code for guests — per q5), stored hashed, bound to the human, revocable and expiring; the agent gets its own IRC connection and a nick visibly tied to its human
  - instruction: test: revoked credential -> next request 401 and agent IRC connection closed
  - honesty: The endpoint rejects requests without a valid unexpired unrevoked credential; credentials are stored only as hashes; revocation closes the agent's live connection
- The agent is accounted as its human everywhere the human is: it takes no extra guest seat, is checked against `is_banned`(email, ip), its flags use the human's key, idle sign-off and deletion (`delete_guest_inputs`, `delete_sessions_for_email`) and the retention sweep cover it
  - instruction: test: ban human -> agent request refused; delete human -> agent credential rows gone
  - honesty: Banning, deleting or idle-signing-off a human applies to their agent: a banned human's agent cannot connect; deletion removes the agent's credential and records
- Consent names the agent: the guest's consent record links the agent to its human, and the Terms/Privacy gain clauses for agents acting on a user's behalf and for approved users with channel access — a material change that bumps the legal version so guests re-consent (katvan hand-off)
  - instruction: test: guest with consent at the old version -> re-prompted; consent row carries the agent identity
  - honesty: Consent records name the human's agent, and the legal version bump forces guests to re-consent before connecting one
- Sandbox agents (sbx-ask, sbx-bolt) recognise a visiting personal agent as an agent, not a human, so the hop-limit loop guard holds (culture hand-off)
  - instruction: culture test: 3-agent room, visitor addresses sbx-ask -> at most `max_agent_hops` agent replies
  - honesty: With a visiting personal agent in a room with sbx-ask and sbx-bolt, the agent-to-agent hop cap still bounds every exchange
- Roles: admins have full access to every channel; for everyone else no grant means no channels (default-deny)
  - instruction: test both roles against the live test IRCd fixture
  - honesty: A non-admin approved user with zero grants sees zero mesh channels and every join/read/send is refused; an admin sees and can join every channel
- Two per-channel access levels: read, and write/ask
  - instruction: test: read-level user's send to granted #x -> refused; history read -> allowed
  - honesty: At 'read' a user can list a channel, read history and see live lines, but every send (chat, /me, topic, raw PRIVMSG) is refused — asking an agent requires write/ask
- Guests stay separate on the sandbox; approved (signed) users reach the real mesh, limited to their granted channels
  - instruction: test: guest session backend is always sandbox; approved JOIN of ungranted channel never emitted to the IRCd
  - honesty: Guest sessions (and guests' agents) only ever connect to the sandbox IRCd; approved non-admin sessions on the mesh can JOIN only granted channels, enforced in the lens before the line reaches the IRCd
- A personal agent can be wherever its owner is, with exactly its owner's permissions
  - instruction: test: revoke the human's grant on #x while agent is connected -> agent's next read/send on #x refused
  - honesty: An agent's effective permissions are computed from its human's current grants at every check, never copied at issue, so revoking a human's grant takes effect for their agent immediately
- One MCP endpoint for all tiers (admin, approved, guest), reached through the lens's public host; for now an agent is approved the same way a human is today
  - instruction: test: three credentials (admin/approved/guest humans) on the same endpoint get three different scopes
  - honesty: One MCP endpoint serves admin, approved and guest agents; tier and grants derive only from the human the credential is bound to; an agent credential is obtainable only by a human who has passed their normal sign-in
- The mesh IRCd accepts connections only from the lens, the mesh's own agents and its federation links — restricting it is part of this feature, because the IRCd has no access control and lens-side grants are otherwise bypassable
  - instruction: verify from a non-allowed host: a direct IRC connect to the mesh port fails; lens, mesh agents and links still connect
  - honesty: A direct IRC connection to the mesh from anything other than the lens, the mesh's own agents or its federation links is refused, so no channel grant can be bypassed by skipping the lens

## Honesty conditions

- Every behaviour in the announcement is backed by a confirmed requirement below (roles c18, levels c19, separation c20, agent scope c21, single endpoint c22, owner-only reach c3)
- A guest and their agent can run the same human-to-own-agent loop a signed user runs, through the same endpoint, inside the guest's sandbox
- Adding an approved user and their grants needs only CLI commands against the running lens (no config edit, no restart) and is effective on their next action
- No host name or address for the public endpoint appears as a literal in src/ or the exported spec; it is read from configuration
- Each named audience has a path in the spec: the admin manages grants via CLI, approved users and guests sign in as today, and agents connect through the one MCP endpoint
- The before-state matches the surveyed code: binary approval (web/identity.py tier), one guest room (web/sessions.py `_rooms_for`), stdio-only MCP (tools.py auth.mode dev), no IRCd ACLs (agentirc client.py modes o/v/S)
- After delivery, a non-admin approved user's channel list equals their grant rows, and their agent's reachable channels equal the user's
- Both numbers are checked: the onboarding run uses only CLI commands with the lens running throughout, and the e2e cross-human agent test asserts zero deliveries

## Success signals

- After delivery, the owner adds a few approved users, each with limited (channel-scoped) permissions
  - instruction: test: grant added via CLI while the user's session is open -> next /channels includes it
- The operator onboards at least 3 approved users with channel-scoped grants through the CLI alone — 0 config edits and 0 lens restarts — and the e2e suite records 0 cross-human deliveries to a personal agent

## Scope / boundaries

- The spec and code never name or hardcode a specific host or address; the public endpoint is deployment configuration
  - instruction: grep src/ and docs/specs/<slug>.md for host literals -> none

## Non-goals

- Emails and any PII are out of the spec, plan and repo: approved emails and their grants exist only in runtime config/store, never in committed artifacts
- Library-tagged content permissions are a later follow-up ('soon'), not part of this first cut
- No library-tag schema or library access integration in this cut; the grant model should still be shaped so a later resource kind (library tag) fits beside channels

## Scope exploration

- `s1` — `irc-lens src/irc_lens/guest_store.py + config.py`: `guest_store.py`:60-102 is the email-keyed sqlite store with a `_migrate` pattern (:125) and a CLI editor (cli/`_commands`/guests.py); config.py:661 `load_config` builds a frozen LensConfig held in app\['config'\] with no reload path, so `allowed_emails` (config.py:30,189-219) changes need a restart. Approved-ness is binary: Identity(principal,nick,`raw_jwt_subject`,tier) in web/identity.py:33-41 has no per-user attributes.
  - seeds: `c9`
- `s2` — `irc-lens src/irc_lens/session.py + web/routes.py (approved path)`: Approved users route to `BACKEND_MESH` (routes.py:134-166) with the full palette; `_exec_join` (session.py:738) only checks the leading '#', `_exec_channels` (session.py:912) lists everything unfiltered; no read-only/hidden channel concept exists anywhere. Today approved = full mesh access.
  - seeds: `c10`
- `s3` — `irc-lens src/irc_lens/web/sessions.py + commands.py (guest path)`: Guests get one room from store.`room_id`(email) (`guest_store.py`:195-208) and may only CHAT/HELP/WHO/ME/READ (commands.py:127-135, verified); the non-guest branch of `_rooms_for` (sessions.py:276-279, verified) joins all rooms from store.`list_rooms`() — the approved Guest view — so reusing it for an agent would expose every guest's room.
  - seeds: `c11` (rejected)
- `s4` — `irc-lens tools.py + cli/_commands/mcp_cmd.py + web/front.py + agentfront mcp_surface`: irc-lens mcp is stdio only (`mcp_cmd.py` -> agentfront `serve_stdio`); tools open a throwaway Session per call with the fixed cfg.`dev_nick` and refuse unless auth.mode == dev (tools.py:169-179, verified); tool functions take no caller identity; /agent (web/front.py) is a read-only agentfront docs mount; no bearer-token auth path exists in the middleware. Reusable: `guest_store` tokens table (email, purpose, hashed, issue/verify/consume at :274-330) and mail.py.
  - seeds: `c12`
- `s5` — `irc-lens guest seat/ban/deletion paths`: Seats count 'guest:<email>' principals on the sandbox backend (web/sessions.py `active_guests`/`_counts_as_guest`, `max_guests` default 1, live 2); bans (email, ip) checked at every entry step (entry.py) and by web/bans.py; deletion `guest_store.py`:620-644 + sessions :424; retention `inactive_guests` :646-659 — none know about a second connection or an agent, so each must be extended.
  - seeds: `c13`
- `s6` — `irc-lens consents table + katvan site-astro legal pages`: consents(email, ip, `tos_version`, `privacy_version`, ts, train) (`guest_store.py`:222-238) has no agent field; katvan site-astro/src/data/legal.ts is at terms/privacy 1.2 (effective 2026-10-30) and terms.md:16 / privacy.md:55 describe only Culture's own sandbox agent — no user-owned agents and no approved-user class; dist/legal/version.json is stale at 1.1 (rebuild needed). Irc-lens#79 already listed this legal clause.
  - seeds: `c14`
- `s7` — `culture culture_core/sandbox/agent.py`: `_should_answer` (agent.py:686-712) treats any nick not in the static `peer_agents` config dict (:150, `_peer` :669) as human: a visiting agent's line resets `agent_hops` (:703) and gets unaddressed answers (`answer_unaddressed` default True, :129), so sbx-ask+sbx-bolt+visitor can loop without bound. The sandbox agent has no owner/sponsor notion; `owner_channel` (:120) is only the NSFW flag sink. Docs to update: docs/sandbox-agent.md, docs/guest-sandbox.md, docs/reference/cli/index.md.
  - seeds: `c15`
- `s8` — `agentirc agentirc/client.py + skills/history.py + skills/rooms.py`: Channel modes are only o/v/S(+R) (client.py:755, verified) — no +i/+k/+b/+m/+s, no INVITE handler; any client can JOIN any '#' and DM any nick (`_relay_direct_privmsg`); HISTORY of a '#' target has no membership check (history.py:441-442, verified) though DM history is pair-keyed; no SASL/ACCOUNT; ROOMINVITE is advisory; bot CAP grants no privilege; extension-api.md:516 'Per-bot ACLs are a future issue'. `culture_core` has no IRCd of its own (pyproject pins agentirc-cli>=9.13,<10).
  - seeds: `c16` (rejected)
- `s9` — `culture-library core/models.py + server/auth.py`: culture-library items carry kind/title/author/sha256 and a binary visibility ('private','public', core/models.py:26, default private); no tag field exists (only OpenAPI route tags); access is per-key Principal scopes + private flag (server/auth.py:33-79); agents can already read it over its MCP/RAG endpoints; irc-lens never references the library. 'Library-tagged content' needs a new tag field + per-user principal there.
  - seeds: `c17`
- `s10` — `spark listening sockets (ss -ltn)`: The mesh IRCd listens on all interfaces while the guest sandbox IRCd listens on loopback only, so gateway-only enforcement holds for the sandbox but not for the mesh unless the mesh port is closed to everything except the lens, the mesh's own agents and its links (now requirement c31).
  - seeds: `c16` (rejected), `q1` (question)

## Decisions

- Guests get no channel grants in this version; the grant store is generic (email, resource kind, level) so guest or library-tag grants can be added later without a schema change
- A second agent credential request from the same human replaces the first: the new credential is issued, the old one revoked, and the old agent's connection closed
- In a shared channel the agent may see what its human may see; other humans' DMs to it are refused and their lines that @-address it are withheld; every line delivered to the agent is marked with whether it came from its owner

## Open parks

- [unknown_nonblocking] Logout of one browser closes the shared per-(principal, backend) IRC session for all browsers (irc-lens#70, `app_session.py`:217-232); an agent session tied to the same human must not be torn down by — or tear down — the human's browser session
