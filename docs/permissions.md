# Email-keyed permissions and the sponsored agent

The lens console decides what each signed-in person may do by their email
address. An admin sees everything, an approved member sees only the channels
they were granted, a guest stays in the sandbox, and each person may bring one
personal agent that connects to the lens over MCP and acts with exactly its
human's permissions.

This page is the reference for the feature. It does not repeat the sign-in
mechanics ([auth.md](auth.md)) or the network side of the rollout
([deployment-permissions.md](deployment-permissions.md)); it links to them.
The design is in
[the spec](specs/2026-10-30-email-keyed-permissions-one-sponsored-agent.md).

No email address, host name or address appears in this page or in the code: the
public host is deployment configuration, and every example below uses
placeholders (`user@example.com`, `https://lens.example.com`).

## Roles and levels

Every person the lens knows is a row in the access store
(`src/irc_lens/access_store.py`, in the same sqlite file as the guest store):

- **Role**: `member` or `admin`.
- **State**: `approved`, `invited` (added but not yet signed in), or `removed`.

An admin passes every channel check. A member passes a check only through a
**grant**: a row of (email, resource kind, resource, level). Today the kind is
always `channel`; the table is generic, so a later kind needs no schema change.

A channel grant matches the channel name exactly, case included. AgentIRC
keys channels by their exact name, so `#ops` and `#OPS` are two different
rooms, and a grant on one never opens the other. Email addresses are
compared case-insensitively.

| Level | List and read history | See live lines | Send, ask agents |
| --- | --- | --- | --- |
| `read` | yes | yes | no |
| `write` | yes | yes | yes |
| `ask` | yes | yes | yes |

`write` and `ask` behave the same for the lens: both mean "may send", which is
what asking a resident agent in that channel requires. An unknown level grants
nothing.

The policy is default-deny and pure (`src/irc_lens/access.py`):

- A member with no grants has no channels: nothing is listed, joined, read or
  sent.
- Every answer is derived from the store's current rows, so revoking a
  grant takes effect on the next action, with no restart. Because the check
  runs on every received channel line, the store keeps repeated reads in
  memory only while the database file is unchanged: SQLite's change
  counter, bumped by any committed write from the lens or the CLI,
  invalidates them, and a 5-second lifetime is the backstop. A read error is
  never cached and answers "no".
- Per received line the counter is read once and shared by every check
  that line needs; the sweep that drops channels whose grant was revoked
  runs only when the counter changed since the last sweep. The checks'
  database reads wait at most 0.25 seconds for a writer holding the store
  locked; past that they answer "no" and log a warning, so a blocked writer
  never stalls the console.
- Anything unexpected (unknown or unapproved person, missing store, read error)
  answers "no". An audit failure never turns a refusal into an approval.
- Guests receive no grants. They live on the sandbox, separate from the mesh.

The checks run server-side in the lens session for every path that can reveal
or emit channel content, so hiding a channel in the page is not the control.
Views that enumerate channels or nicks are filtered to the viewer's grants,
and commands that change channel state (`/topic`, `/kick`, `/invite`,
`/icon`) are admin-only.

Direct messages follow the same default-deny rule. A target that is not a
`#channel` is a nick: an admin may message, `/who` or `/read` any nick; any
other person may reach exactly one, their own personal agent. That nick is
derived by the lens from the person's verified nick and the configured
server name, never taken from what they typed. Every other nick stays
refused and audited: other people, other people's agents, and the mesh's
resident agents, which may have tools (granted people reach resident agents
only by asking them in a channel they hold `write` or `ask` on). The sandbox "guest view" toggle, the owner metrics
page and the residents page are admin-only as well
(`src/irc_lens/web/routes.py`, `admin_only_denial`); a member gets 403.

## Grants

Grants are managed in the running console or from the CLI. Both write the same
store and both take effect on the person's next action.

In the app, an admin opens `/admin` (`src/irc_lens/web/admin.py`):

| Route | Does |
| --- | --- |
| `POST /admin/users` | add a user now (`mode=add`) or invite them (`mode=invite`), with a role; `mode=role` changes an existing user's role and keeps their state, so it never approves a removed or invited user |
| `POST /admin/users/remove` | mark the user removed; revokes their agent credential and sessions |
| `POST /admin/grants` | set a channel grant at `read`, `write` or `ask`, or revoke it |
| `POST /admin/extension` | approve an agent extension for a number of hours, or clear it |

Every route is admin-only and reads the caller's role from the store on each
request. The last approved admin can be neither demoted nor removed.

From the CLI (`src/irc_lens/cli/_commands/access.py`), against the running
lens:

```bash
irc-lens users add user@example.com --role member
irc-lens users role user@example.com admin
irc-lens users remove user@example.com
irc-lens grants add user@example.com '#general' --level ask
irc-lens grants list user@example.com
irc-lens grants revoke user@example.com '#general'
irc-lens agents list user@example.com
irc-lens agents revoke user@example.com
irc-lens extend user@example.com --days 2
irc-lens extend user@example.com --clear
```

Revoking a grant that does not exist (for example `#ops` when the grant is
on `#Ops`) changes nothing and says "no such grant": the CLI exits 1 and
`/admin` answers 400. A grant is one row per person and channel; setting it
again changes its level in place.

Like `/admin`, `users role`, `users add` and `users remove` refuse a change
that would leave no approved admin.

Every verb opens the store, makes one change and exits. Output names only the
address you typed, plus counts, channels and levels.

`config.yaml` no longer holds the approved list. With
`auth.email_permissions.enabled: true`, `auth.allowed_emails` is read once to
import its entries as admins (an import ledger makes that idempotent, so a
later demotion is never undone by a restart) and is then ignored. The first
start that succeeds marks the store initialised even when it imported
nobody (for example an empty list and an admin added with
`irc-lens users add --role admin`), so later starts never import the list
and switching the feature off is admins-only, never legacy. A first start
refused for having no admin does not mark it.

## Invites

An admin can invite a person instead of approving them outright. The invite
stores the address as `invited` and mails the existing set-password link. An
invited person is not approved: they have no access until they finish a
verified sign-in. The first one promotes them from `invited` to `approved`
(`promote_on_signin` in `src/irc_lens/web/app_session.py`), whichever way
they sign in:

- the app sign-in (set a password, enter the emailed code), or
- with app sign-in off, the password step followed by `/login`, or any
  other request carrying a valid Cloudflare Access JWT for their address
  (`approved_on_signin`, called by the Access middleware).

A `removed` person is never promoted, and in the admins-only state (below)
nobody is. The sign-in mechanics themselves are in [auth.md](auth.md).

## Agent credential

Every human, approved or guest, may hold exactly one agent credential. It is
an opaque bearer token that their agent sends to the MCP endpoint
(`src/irc_lens/web/agent_auth.py`).

- The signed-in person opens `/agent-credential` and issues it. Guests must
  also hold current consent first.
- The token is shown once, on a `no-store` response. The store keeps only its
  SHA-256 hash.
- Issuing again replaces the credential: the new one is created, the old one is
  revoked in the same transaction, and the old agent's live connection is
  closed. A human never has two active credentials.
- A credential expires after `auth.email_permissions.agent_credential_ttl_days`
  days (default 30). It can also be revoked from the CLI
  (`irc-lens agents revoke <email>`, which revokes both the approved and the
  guest credential of that address) or by removing the person. The live
  agent connection is closed by the next agent sweep.
- It is bound to its human through a credential key: the email for an approved
  member, `guest:<email>` for a guest, so a guest's credential can never
  resolve to a member's rows even when the two share an address.
- The agent counts as its human: it takes no extra guest seat, is covered by
  bans, deletion and the retention sweep, and its sends pass the human's
  guest allowlist, rate limit and input log (`src/irc_lens/web/input_gate.py`).

The agent's nick is derived from its human's nick and lives in a reserved
namespace, `<server>-pa_-<name>` (`src/irc_lens/agent_nick.py`), which no human
nick can enter. `<server>` is the configured server name (`server.name` on
the mesh, `guest_mode.sandbox.name` on the sandbox), passed in rather than
read off the nick, so a server name holding `-` works: `spark-1-bob`
becomes `spark-1-pa_-bob`. Ownership is never inferred from nick text; it comes from the
lens's own state. Two people can derive the same nick; if credentials for
one agent nick were issued to more than one person, a flag raised against
that nick is not mapped to anyone (it is logged, without an address), so it
is never attributed to the wrong human. Ban by address instead.

## MCP endpoint

There is one remote MCP endpoint for all tiers. It is mounted at
`web.mcp_path` (default `/mcp`; `src/irc_lens/web/mcp_http.py`) on the lens's
own public host. The path must be static: one with spaces or braces (`{`,
`}`, which aiohttp would read as a dynamic segment) is refused at start.
The host is never written in code: you reach it at your deployment's public
base URL plus that path, for example
`https://lens.example.com/mcp`. The credential page shows the endpoint built
from `auth.app_signin.base_url` and `web.mcp_path`.

Connect an MCP client over HTTP and send the credential as a bearer header on
every request:

```text
Authorization: Bearer <token from /agent-credential>
```

Only that header is read. A missing, malformed, unknown, expired or revoked
token is answered 401 before any tool runs.

On every request, not just the first, the lens re-checks the credential's
human: a member must still be approved in the store; a guest must still exist
and not be banned (by email or IP). Every change to the store is seen on the
next call (see the cache rule under Roles and levels), so removing a member or
banning a guest cuts off their agent on its next call. A background sweep also
closes live agent connections whose credential lapsed or whose human no longer
passes.

The endpoint serves its own small tool set
(`build_agent_front` in `src/irc_lens/web/agent_auth.py`), not the dev-only
tools of `irc-lens mcp`. Each tool resolves its caller from the authenticated
credential:

| Tool | Does |
| --- | --- |
| `whoami` | the agent's nick, its human's tier and the channels it has joined |
| `read` | unread lines delivered to the agent, oldest first, each marked `owner: true` or `false`; at most `limit` per call, the rest stay unread for the next call |
| `send` | one line to a channel or to the agent's own human, through the input gate and the human's grants |
| `channels` | channels the agent has joined and may list |
| `join`, `part` | join a granted channel, or leave one (not available to guests) |

## Owner-only delivery

Only an agent's own human can drive it. Filtering runs on the receive side of
the agent's lens-held IRC connection (`src/irc_lens/web/agent_sessions.py`), so
it covers every sender: other lens users, federated peers and raw IRC clients
that never touched the lens.

- A direct message to the agent from anyone but its human is withheld.
- A channel line that names the agent, from anyone but its human, is withheld.
  The name is matched on the text a reader sees: IRC formatting codes (bold,
  colour, reset, ...) and invisible Unicode format characters (zero-width
  space or joiner, word joiner, bidi marks) are removed first, so they cannot
  hide the name.
- Withheld lines are counted and audited (`agent.delivery.withheld`), never
  shown to the agent, and logged without nick or email.
- Every line the agent does receive carries `owner: true` or `owner: false`.
  "Owner" means the human's own live lens session holds the sending nick; a
  namesake or a raw client that grabbed the nick is not the owner.
- An agent may send direct messages only to its own human.

One gap is known and not closed: when the human's IRC connection dies on
the server side, the lens notices only when its own connection reports it.
In that short window a raw IRC client could take the human's nick. Lines it
sends then are not marked `owner: true` once the lens has seen the human's
session drop (the owner check needs the human's own healthy, connected lens
session holding the nick), but until then the lens still believes the human
holds it, so such lines could be delivered as `owner: true`. Restricting
the mesh IRCd to the lens, as in
[deployment-permissions.md](deployment-permissions.md), keeps raw clients
off the mesh and closes this in practice.

Resident mesh agents are not personal agents: this rule applies to agents that
connect through the MCP endpoint, and residents answer any person holding
`write` or `ask` on their channel.

An approved person's agent connects to the mesh with exactly its human's
current grants, recomputed at every check and never copied at issue. A guest's
agent connects to the sandbox and joins only its human's private room.

## Extensions

A guest's agent is meant to close when its human is signed off for idleness,
unless an admin approves it to stay for a set number of hours.

- An admin sets the extension (`POST /admin/extension` or
  `irc-lens extend`). The hours are typed per approval and have no fixed
  maximum. The extension names the person, an end time that many hours ahead,
  and the admin who set it. `clear` removes it.
- Setting and clearing are audited (`extension.set`, `extension.clear`) and
  shown on the admin page.
- Only an admin can set one. The store holds at most one per person.

How the extension changes when a guest's agent disconnects
(`_reap_guest_agents`, called at the end of `reap_idle` in
`src/irc_lens/web/sessions.py`):

- Only the human's own actions count as activity. The agent's sends and reads
  never keep its human's session alive.
- When the guest's own sandbox session is gone (signed off for idleness, or
  never opened) and no extension is live, the agent is disconnected on the
  next reap.
- Every agent request checks the same rule before anything connects
  (`_guest_agent_gate` in `src/irc_lens/web/agent_sessions.py`): while the
  human is signed off and no extension is live, the call is refused and an
  open agent connection is closed, so a tool call never reopens an agent
  the reaper closed. The agent works again once its human signs back in.
- The same check re-reads the guest's consent: after a Terms or Privacy
  version bump, the agent is refused and disconnected until the guest
  accepts the new version.
- While an extension is live, the agent stays connected. Once the extension
  ends or is cleared, the next reap applies the normal rule.
- For a guest, the extension is looked up under the bare email (what the
  admin page and `irc-lens extend` set) and under `guest:<email>`. The admin
  page's extension form accepts a known user or an address the guest store
  knows, so a guest's extension can be set (and cleared) there or with the
  CLI. Guests are not listed in the admin page's users table.
- An extension set from the CLI is recorded with the plain `system` marker
  (no admin address); one set from the admin page records the admin.

Approved users' agents are not reaped this way. Their lifetime follows their
credential and their human's standing (`sweep_once` in
`src/irc_lens/web/agent_auth.py`).

## Switches

Three settings under `auth.email_permissions` control the feature:

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | store-decided approval, roles and grants |
| `agents_enabled` | `true` | the agent kill switch |
| `agent_credential_ttl_days` | `30` | how long an issued credential lasts |

`enabled` has three effective states, resolved once at start
(`resolve_access` in `src/irc_lens/config.py`):

| `enabled` | Access store | State | Who passes |
| --- | --- | --- | --- |
| `true` | any | enforced | approved people, by role and grant |
| `false` | never initialised | legacy | `auth.allowed_emails`, as before the feature |
| `false` | initialised earlier | admins-only | approved admins only; agents off |

Turning the feature off after it has been in use never restores full mesh
access: in the admins-only state a non-admin is refused the mesh, not given all
of it, and the retired config allowlist is not consulted. An unreadable store
counts as initialised. [auth.md](auth.md) has the same table in the context of
sign-in.

`agents_enabled: false` is the kill switch. Every MCP call is refused before
its credential is read, issuing at `/agent-credential` answers 403, and live
agent connections are closed on the next sweep. The admins-only state turns it
off as well.

The network side of the same rollout, restricting the mesh IRCd so grants
cannot be bypassed by connecting around the lens, is in
[deployment-permissions.md](deployment-permissions.md).

## Audit and metrics

Grant changes, user changes, credential issue and revoke, extension changes,
refused accesses and refused agent calls are rows in the store's append-only
`audit` table. Rows are keyed by the SHA-256 of the actor's address, never the
address, and service logs carry no email address or token.

Refused agent calls need no credential, so their audit rows are throttled:
at most one `agent.refuse` row per reason per client address per minute, and
at most 30 per reason per minute across all clients
(`RefusalThrottle` in `src/irc_lens/web/agent_auth.py`). Throttled refusals
are still counted.

`GET /owner/metrics` (admin-only) adds an `access` object of counts only:
`grant_changes`, `credential_events`, `access_denials` and `agent_refusals`.
The counts are derived from the audit table, so they survive restarts;
`agent_refusals` also adds the refusals the throttle kept out of the audit
since the lens started.

## For admins

- Turn the feature on with `auth.email_permissions.enabled: true`; the first
  start imports `auth.allowed_emails` as admins. Check that at least one admin
  exists before relying on it.
- Add, invite, remove and grant from `/admin` or the CLI. No restart.
- Approve an agent extension for a guest's agent when needed, and revoke an
  agent at any time.
- Keep the kill switch (`agents_enabled: false`) in mind as the fast stop for
  all agents.
- Restrict the mesh IRCd as described in
  [deployment-permissions.md](deployment-permissions.md).

## For approved users

- You see the channels you were granted, at the level you were granted. Without
  a grant you see none.
- Sign in as usual ([auth.md](auth.md)). If you were invited, your first
  successful sign-in activates you.
- Issue your one agent credential at `/agent-credential`. Your agent reaches
  exactly your channels and nothing more, and only you can talk to it.

## For guests

- You stay on the sandbox, in your own private room. Guests have no channel
  grants.
- You may bring one agent too, through the same endpoint. It joins only your
  room, shares your guest seat and rate limit, and follows the sandbox command
  allowlist.
- Your agent is bound to your email and to your recorded consent. Banning or
  deleting you removes it.

## For agent authors

- Get the token from the human at `/agent-credential`; it is shown once. Store
  it as a secret.
- Call the single MCP endpoint of the deployment, with
  `Authorization: Bearer <token>` on every request, using the `web.mcp_path`
  your operator configured.
- Expect `401` for a bad, expired or revoked token, and tool errors for any
  channel your human is not granted at the needed level. A permission is
  re-evaluated on every call, so a revoked grant stops working immediately.
- Treat lines with `owner: false` as untrusted input: they come from other
  people in a shared channel. Direct messages from anyone but your human never
  reach you.
- Mind the rate limit: your sends are throttled with your human's, exactly like
  a guest.
- Only one credential exists per human. Issuing a new one disconnects the old
  agent.

## Before and after

The spec's scope findings (`s1` to `s10`) surveyed the code before this
feature. The after-state below names the merged files.

| Before (surveyed) | After (merged) |
| --- | --- |
| Binary approval: an identity carries only a tier (`web/identity.py`), and approved-ness comes from the frozen `allowed_emails` list, so every approved user reaches the whole mesh and a change needs a restart (s1, s2) | Per-person role, state and channel grants in the access store (`src/irc_lens/access_store.py`), enforced by `src/irc_lens/access.py` on every path, read on each check, so changes need no restart |
| One guest room: a guest gets a single private room, and the non-guest branch of `_rooms_for` in `web/sessions.py` joins every room, so reusing it would expose every guest (s3, s5) | A guest stays on the sandbox; an agent joins only its human's own room (`src/irc_lens/web/agent_sessions.py`), never the all-rooms view; the guest view and metrics are admin-only |
| stdio MCP: `irc-lens mcp` is local stdio only, and `tools.py` uses a fixed dev nick and refuses outside dev mode, with no caller identity and no bearer auth (s4) | One HTTP MCP endpoint at `web.mcp_path` (`src/irc_lens/web/mcp_http.py`) with per-human bearer credentials (`src/irc_lens/web/agent_auth.py`); its tools carry the credential's human and never reuse the dev path in `src/irc_lens/tools.py` |
| No IRCd ACLs: AgentIRC has only o/v/S channel modes, any client can join any channel or message any nick, so lens-side checks can be bypassed (s8, s10) | Still true of the IRCd, so the lens enforces grants server-side and filters agent delivery on the receive side; the mesh port is closed to everything but the lens, mesh agents and links as in [deployment-permissions.md](deployment-permissions.md) |

The first three rows are code in this repository. The fourth is not: the
lens cannot add access control to the IRCd, so that part of the after-state is
a deployment control, and the grants are only as strong as that restriction.
