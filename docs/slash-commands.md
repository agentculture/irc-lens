# Slash commands

The lens parses every input line via `irc_lens.commands.parse_command`.
Plain text becomes a `CHAT` command targeting the active channel;
lines starting with `/` are dispatched as slash commands.

The parser table is byte-faithful to `culture@57d3ba8` —
`src/irc_lens/commands.py` carries the verb dictionary verbatim
(see `CITATION.md`). This doc documents the *current* server-side
behaviour, read directly from `Session._exec_dispatch` in
`src/irc_lens/session.py`: most verbs are wired to real execution, and
a handful still publish a non-fatal `error` event so the UI keeps
responding.

## CLI/MCP tool parity

Every console verb that has a meaningful effect outside a live browser
tab also exists as an `irc-lens <verb>` CLI command / MCP tool
(`src/irc_lens/tools.py`) — same name, same dispatch path
(`Session.execute` against the identical `ParsedCommand` shape
`POST /input` builds), so a console `/command` and its CLI/MCP
counterpart cannot behave differently. See `docs/cli.md`'s "Live tools"
section for arguments, return shapes, and exit-code mapping; the CLI
tool runs over its own short-lived AgentIRC session instead of the
console's long-running one, so it has no notion of a persisted "active
channel" the way the console does.

## Wired in v1

| Verb | Args | Behaviour | CLI/MCP tool |
| --- | --- | --- | --- |
| (none) | text | `CHAT` — sends a PRIVMSG to the active channel; publishes a local-echo `chat` event. | `send` (explicit target, no "active channel" needed) |
| `/join` | `#channel` | Joins the channel, sets it active, publishes `roster` + `info`, pulls recent backlog. | `join` |
| `/part` | `#channel` | Parts the channel, publishes `roster` + `info`. | `part` |
| `/send` | `<target> <text…>` | Sends a PRIVMSG to an explicit target (channel or nick). Local-echoes a `chat` event **only when `target == current_channel`**. | `send` |
| `/switch` | `#channel` | Pure view-state move to an already-joined channel — no IRC side-effect. | `switch` (joins the channel first, since an ephemeral session starts with nothing joined) |
| `/read` | `[#channel] [-n N]` | Re-reads recent buffer for a channel (default: current channel, 50 lines, capped at 500). | `read` |
| `/channels` | — | Lists channels reachable on the server (`LIST`). | `channels` |
| `/who` | `[#channel\|nick]` | Refreshes the roster via `WHO` (default: current channel). | `who` |
| `/agents` | — | Aggregates `WHO` across every currently-joined channel into one roster. | *(none — see "Console-only verbs" below)* |
| `/mesh` | — | Switches the info pane to the live agent-mesh graph and recomputes the snapshot. | `mesh` (takes an explicit channel list, since an ephemeral session has nothing pre-joined) |
| `/topic` | `<#channel> [text…]` | Sets the topic (with text) or sends a bare read-mode `TOPIC` request (without). | `topic` |
| `/me` | `<text…>` | Sends a CTCP ACTION line to the active channel. | `me` (takes an explicit `<channel>` argument) |
| `/icon` | `<emoji>` | Updates the lens's `ICON`. | `icon` |
| `/help` | — | Switches the info pane to the `help` view. | *(none — pure UI view switch)* |
| `/overview` | — | Switches the info pane to the `overview` view. | *(none — pure UI view switch; distinct from the global `irc-lens overview` meta-verb)* |
| `/status` | — | Switches the info pane to the `status` view. | *(none — pure UI view switch)* |

### Console-only verbs

`/agents`, `/help`, `/overview`, and `/status` have no CLI/MCP tool
counterpart, by design (see `src/irc_lens/tools.py`'s
`NON_TOOL_DISPATCH_VERBS` for the exact, reasoned exclusion list):

* `/help`, `/overview`, `/status` are pure browser-UI view switches with
  no IRC side effect and nothing meaningful to return outside a live
  `#info` pane.
* `/agents` aggregates `WHO` across whichever channels a *persistent*
  browser session happens to have joined already — an ephemeral
  one-shot session has no such backlog of joined channels to
  aggregate, so it can add nothing beyond calling `who`/`mesh` on an
  explicit channel, which those tools already cover.
* A top-level tool literally named `overview` would also collide with
  `agentfront`'s reserved global `overview` meta-verb.

## Recognised but not-yet-supported

These slashes parse but currently publish an `error` event of the
form `<command>: not yet supported`. Adding any of them is a
non-breaking change — wire a new `_exec_*` helper into
`Session._exec_dispatch`, then (per the CLI/MCP parity rule above) a
matching tool into `src/irc_lens/tools.py`.

| Verb | Args | Spec intent |
| --- | --- | --- |
| `/start` | `<agent>` | Start a managed agent. |
| `/stop` | `<agent>` | Stop a managed agent. |
| `/restart` | `<agent>` | Restart a managed agent. |
| `/kick` | `<channel> <nick>` | Kick a participant. |
| `/invite` | `<channel> <nick>` | Invite a participant. |
| `/server` | — | Server-meta query. |
| `/quit` | — | Quit the IRC session. |

## Errors

Any slash that parses but fails downstream — invalid channel name,
empty `/send` text, unknown verb — publishes a single `error` SSE
event (`{"message": "..."}`). The browser surfaces it as a toast via
`lens.js`. Valid input that can't reach AgentIRC (e.g. the
connection was lost mid-session) returns HTTP `503` from `POST
/input` with `{"error": "...", "hint": "..."}` per the lens's HTTP
error-shape contract — see `docs/sse-events.md` for the full event
catalogue and `docs/architecture.md` for the HTTP contract notes.

## Examples

```text
hello world           → CHAT to current channel
/join #general        → JOIN, sets current channel, refreshes sidebar+info
/send #ops standup    → PRIVMSG to #ops without switching the active pane
/switch #ops           → view-state move to #ops (must already be joined)
/mesh                  → switches the info pane to the live agent-mesh graph
/help                  → switches info pane to the help view
/foo                   → publishes `error: unknown command: /foo`
```

```bash
# The same effects, driven headlessly via the CLI:
irc-lens join '#general'
irc-lens send '#ops' 'standup' --json
irc-lens mesh '#ops' --json
```

## Tier-aware palette and guest allowlist

Typing `/` opens an inline command palette above the input (command in
mono green, 1-3 word description). One table (`irc_lens.commands.PALETTE`)
drives the palette, the help pane, and the server-side allowlist:

| Tier | Commands offered |
| --- | --- |
| Approved (real mesh) | `/help` `/who` `/me` `/read` `/join` `/channels` `/agents` `/mesh` `/residents` and, with guest mode on, `/sandbox` (Guest view) |
| Guest, Sandbox preview | `/help` `/who` `/me` `/read` and plain chat |

`POST /input` enforces the same list for any session routed to the
sandbox (`SANDBOX_ALLOWED`): everything else (`/kick /start /stop
/restart /invite /server /icon /topic /send /part /join /switch /channels
/agents /mesh /overview /status /quit`, unknown commands) is refused with
`403 {"error": "Not in guest view"}` and never reaches IRC. Guests are also
rate limited to `guest_mode.rate_limits.messages_per_min` messages a minute
(per guest email and per IP, 60 s window): over the limit is
`429 {"error": "Slow down"}` with `Retry-After: 60`. Refused commands count.

An approved user on the mesh typing `/sandbox` (or using the header's
"Guest view" button) switches to the sandbox preview; "Back to mesh" in the
amber banner switches back (`HX-Refresh` reloads the page).

`GET /presence` returns the sandbox agent state fragment (`agent online` /
`agent offline`, silent while the agent has never been seen) polled by htmx
every 15 s in guest and sandbox-preview sessions.
