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
- Per-email approval, admin flags and grants (resource + access level) live in the lens sqlite store and take effect without a restart — managed by admins from within the app and by an irc-lens CLI subcommand; config.yaml no longer holds the approved list
  - instruction: test: CLI grant add/list/revoke round-trip on a temp store; config.yaml unchanged
  - honesty: Grants are rows in the lens store (email-keyed, never committed), editable through an irc-lens CLI verb, read on each check so changes need no restart
  - honesty: Approval, admin flags and grants are rows in the lens store (email-keyed, never committed); after the one-time import, editing config.yaml changes nobody's access
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
- Admin-only surfaces: the sandbox 'guest view' toggle (which joins every guest's private room), the owner metrics page and the residents page are available to admins only — never to non-admin approved users or any agent
  - honesty: A non-admin approved user calling the sandbox enter route, the owner metrics route or the residents route gets 403; an admin still gets today's behaviour
- Every view that enumerates channels or nicks — /channels, /who, /agents, /overview, /status, the mesh graph, presence — is filtered to the viewer's grants; commands that change channel state (/topic, /kick, /invite, /icon) are admin-only
  - honesty: For a non-admin with one granted channel, no listing or graph view names any other channel or any nick seen only in ungranted channels
- Personal-agent nicks live in a reserved namespace that cannot collide with resident agents or human nicks, and the owner / not-owner marking comes from the lens's credential-to-human map, never from nick text
  - honesty: A human whose derived nick equals a resident's or another agent's nick cannot claim it; a guest who picks a nickname resembling another human's agent nick is never marked as that agent's owner
- Owner-only filtering runs on the receive side of the agent's lens-held IRC connection, so it covers every sender — other lens users, federated peers, and any client that reached the IRCd directly
  - honesty: A DM to a personal agent sent from a raw IRC client (not through the lens) never appears in that agent's MCP reads
- The MCP endpoint does not reuse the identity-less tools.py path (fixed dev nick, dev-mode only); every MCP tool call carries its credential's human and passes the same grant check as the web Session
  - honesty: No MCP tool call reaches IRC without a resolved human identity; a call for an ungranted channel is refused before any IRC line is sent
- An agent's sends pass the same gates as its human's: the guest command allowlist, the per-human rate limit, the input log and the flag path; one agent cannot exhaust a shared resident's answer capacity
  - honesty: A guest's agent sending faster than the guest rate limit is throttled exactly like the guest; a disallowed command from an agent is refused like the guest's
- Every agent request re-checks its human's current status (still approved, not banned, not deleted), so removing an approved user or banning a guest cuts off their agent on its next request
  - honesty: Removing a human from the approved list while their agent is connected makes the agent's next call fail and closes its IRC connection
- Rollback fails closed: a single switch disables the MCP endpoint and blocks all agent credentials, and turning the permissions feature off never restores full mesh access to non-admin approved users
  - honesty: With the kill switch on, every agent call is refused; with permissions disabled, a non-admin approved user is refused the mesh rather than given all of it
- Grant changes, credential issue/replace/revoke, and refused accesses (ungranted channel, cross-human agent delivery) are recorded in the lens store keyed like other guest records (no email in log lines), and metrics expose only counts
  - honesty: After a refused cross-human delivery and a grant change, the audit records exist and the service log lines carry no email address
- Admins add and invite non-admin approved users, and manage their grants, from within the app
  - honesty: Only an admin can add or invite a user or change grants in the app; an invited user becomes approved only after completing the existing app sign-in (set password, emailed code), and the change takes effect without a restart
- A guest's agent is disconnected when its human is signed off for idleness, unless an admin has approved that agent to stay for a set period of n hours (chosen by the admin), after which the normal rule applies again
  - honesty: Without an admin extension a guest's agent closes at its human's idle sign-off; an extension is set only by an admin, names the agent and an end time n hours ahead, and expires on its own — after which the agent closes at the next idle sign-off

## Honesty conditions

- Every behaviour in the announcement is backed by a confirmed requirement below (roles c18, levels c19, separation c20, agent scope c21, single endpoint c22, owner-only reach c3)
- A guest and their agent can run the same human-to-own-agent loop a signed user runs, through the same endpoint, inside the guest's sandbox
- Adding an approved user and their grants needs only CLI commands against the running lens (no config edit, no restart) and is effective on their next action
- Adding an approved user and their grants needs only the app or CLI against the running lens (no config edit, no restart) and is effective on their next action
- No host name or address for the public endpoint appears as a literal in src/ or the exported spec; it is read from configuration
- Each named audience has a path in the spec: the admin manages grants via CLI, approved users and guests sign in as today, and agents connect through the one MCP endpoint
- The before-state matches the surveyed code: binary approval (web/identity.py tier), one guest room (web/sessions.py `_rooms_for`), stdio-only MCP (tools.py auth.mode dev), no IRCd ACLs (agentirc client.py modes o/v/S)
- After delivery, a non-admin approved user's channel list equals their grant rows, and their agent's reachable channels equal the user's
- Both numbers are checked: the onboarding run uses only CLI commands with the lens running throughout, and the e2e cross-human agent test asserts zero deliveries
- Both numbers are checked: the onboarding run uses only the app or CLI with the lens running throughout, and the e2e cross-human agent test asserts zero deliveries
- The spec distinguishes personal (MCP, human-bound) agents from resident agents, and the grant check on asking a resident is the channel's write/ask level

## Success signals

- After delivery, the owner adds a few approved users, each with limited (channel-scoped) permissions
  - instruction: test: grant added via CLI while the user's session is open -> next /channels includes it
- The operator onboards at least 3 approved users with channel-scoped grants from within the app (or CLI) — 0 config edits and 0 lens restarts — and the e2e suite records 0 cross-human deliveries to a personal agent

## Scope / boundaries

- The spec and code never name or hardcode a specific host or address; the public endpoint is deployment configuration
  - instruction: grep src/ and docs/specs/<slug>.md for host literals -> none

## Non-goals

- Emails and any PII are out of the spec, plan and repo: approved emails and their grants exist only in runtime config/store, never in committed artifacts
- Library-tagged content permissions are a later follow-up ('soon'), not part of this first cut
- No library-tag schema or library access integration in this cut; the grant model should still be shaped so a later resource kind (library tag) fits beside channels

## Assumptions

- Resident mesh agents (the agents the mesh runs in a channel) are not personal agents: the owner-only rule (c3) applies only to agents connected through the MCP endpoint, and residents answer any human who holds write/ask on that channel

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
- `s11` — `challenge pass / security lens: web/routes.py:315-330 + web/sessions.py:276-279 + routes.py:1036-1050 + routes.py:980`: `_set_sandbox_toggle` gates only on identity.`is_approved`, and the toggled sandbox session joins every guest room via store.`list_rooms`(); `get_owner_metrics` is 'Approved tier only'; `get_residents` renders culture's overview of all residents. With default-deny approved users, each of these would hand a new non-admin every guest's private chat or the whole mesh roster.
  - seeds: `c32`
- `s12` — `challenge pass / data-flow lens: session.py dispatch table :690-712 + _exec_mesh :1017 + _exec_agents :944`: Approved sessions dispatch TOPIC/ICON/MESH/AGENTS/OVERVIEW/STATUS with no tier check; `_exec_mesh` recomputes the mesh graph with WHO round-trips across joined channels; `_exec_agents` walks every joined channel. START/STOP/RESTART/KICK/INVITE/SERVER parse (commands.py:70-77) but have no session handler (fall to UNKNOWN) — no lifecycle exposure found.
  - seeds: `c33`
- `s13` — `challenge pass / security lens: resident agent backends (culture_core clients + spark-ask design note)`: c4 promises users can 'ask agents' in granted channels, but residents on the claude backend run with `permission_mode`='bypassPermissions' (cultureagent/clients/claude/`agent_runner.py`:148, verified) and full tools; spark-ask was built tool-less because no backend can disable tools. Inviting outside users to a channel with a tool-using resident turns a channel grant into prompt-driven tool use on the host. Routed to user decision q8.
  - seeds: `c34`
- `s14` — `challenge pass / security lens: web/identity.py:52-70 derive_nick + agentirc client.py:453-490 nick prefix`: Approved nicks are <server>-<email local part> and AgentIRC forces the <server>- prefix and only enforces uniqueness — an approved human whose local part equals a resident's suffix would collide with it, and a nick says nothing trustworthy about ownership. Ownership must come from lens state.
  - seeds: `c35`
- `s15` — `challenge pass / adjacent-systems lens: tools.py:160-215 + agentirc _relay_direct_privmsg`: tools.py opens a throwaway Session with cfg.`dev_nick` and refuses outside auth.mode dev, taking no caller identity — reusing it would drop both identity and grants. AgentIRC relays a DM from any client to any nick, so a send-side check in the lens alone misses senders that never pass through the lens.
  - seeds: `c36`, `c37`
- `s16` — `challenge pass / failure-mode lens: web/routes.py:~765-800 /input gate + sbx-ask capacity probe (eidetic cortex-spark2-serializes-2026-10-09)`: The guest allowlist, /delete redirect and per-guest per-IP rate limit live in the web /input handler; an MCP path bypasses /input unless it re-enters the same gate. sbx-ask's model serializes (~7 s/answer, ~8 answers/min), so one unthrottled agent can starve both guest seats.
  - seeds: `c38`
- `s17` — `challenge pass / lifecycle lens: web/app_session.py:174-212 identity_for`: `identity_for` re-checks `allowed_emails` on every request for browser sessions; a bearer credential has no such path today, so an agent credential could outlive its human's approval unless the same re-check is built in.
  - seeds: `c39`
- `s18` — `challenge pass / observability + reversibility lens: config.py auth flags + web/metrics + guest_store deletion log (email_hash)`: Today's rollback for app sign-in is a config flag + restart (auth.`app_signin`.enabled: false). For this feature a naive 'off' would revert to today's full mesh access for every approved user — fail-open. No audit trail exists for access decisions; the store already logs deletions by sha256(email), a usable pattern.
  - seeds: `c40`, `c41`
- `s19` — `challenge pass / counter-evidence lens: c27 vs config.py:189-219,661`: c27's '0 config edits, 0 restarts' contradicts the approval allowlist living in frozen config — onboarding a new approved user today needs a config edit and a restart. Routed to a user decision (q9).
- `s20` — `challenge pass / migration lens: guest_store.py _migrate :125 + live store`: New tables (grants, agent credentials, audit) fit the additive `_migrate` pattern on the existing live store. The hazard is the default-deny cutover, not the schema: no admin role exists yet, so enabling the feature before an admin is marked locks the owner out. Routed to q10.
- `s21` — `challenge pass / concurrency lens: credential replace (c29) + grant revoke mid-session`: Two concurrent credential issues for one human must leave exactly one active (single store transaction, as `consume_token` already does); a grant revoked while a session is joined must also stop live line dispatch, which h14's 'live PRIVMSG dispatch' covers. No further concurrency hazard found; the replace race is carried as an obligation on c2.
- `s22` — `challenge pass / operations lens: ss -tn established :6667 (probe 2026-10-31)`: At probe time all 5 established mesh connections came from loopback and server.yaml showed no link block, so restricting the mesh port to loopback would cut nothing observed today. This is a point-in-time observation; federation peers or remote agents that connect occasionally were not ruled out.

## Decisions

- Guests get no channel grants in this version; the grant store is generic (email, resource kind, level) so guest or library-tag grants can be added later without a schema change
- A second agent credential request from the same human replaces the first: the new credential is issued, the old one revoked, and the old agent's connection closed
- In a shared channel the agent may see what its human may see; other humans' DMs to it are refused and their lines that @-address it are withheld; every line delivered to the agent is marked with whether it came from its owner
- Granted channels host only tool-less question-answering agents; tool-using residents stay out of granted channels (a per-channel trusted flag may come later)
- Approved users and admin flags live in the lens store; the migration imports the existing config allowlist once and the config list is then retired
- The migration marks currently approved users as admins (today only the operator); the feature refuses to enable while zero admins exist

## Open parks

- [unknown_nonblocking] Logout of one browser closes the shared per-(principal, backend) IRC session for all browsers (irc-lens#70, `app_session.py`:217-232); an agent session tied to the same human must not be torn down by — or tear down — the human's browser session
- [unknown_nonblocking] Long-lived MCP streaming (streamable HTTP/SSE) through the public edge and tunnel is untested: idle timeouts and response buffering could drop agent connections
- [unknown_nonblocking] Unexamined in this pass: whether agentfront >=0.20 ships an HTTP MCP transport (the surveyed checkout was 0.14.0); the /upload route's tier gating; the full katvan legal text beyond the clauses cited in s6
- [follow_up] Approved users' and their agents' lines persist in mesh history with no deletion path (guest deletion covers only the sandbox, which runs memory-only)
