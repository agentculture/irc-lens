# Slash commands

The lens parses every input line via `irc_lens.commands.parse_command`.
Plain text becomes a `CHAT` command targeting the active channel;
lines starting with `/` are dispatched as slash commands.

The parser table is byte-faithful to `culture@57d3ba8` —
`src/irc_lens/commands.py` carries the verb dictionary verbatim
(see `CITATION.md`). Phase 10 documents the *current* server-side
behaviour: most verbs parse cleanly, a subset are wired to actual
execution, and the rest publish a non-fatal `error` event so the UI
keeps responding.

## Wired in v1

| Verb | Args | Behaviour |
| --- | --- | --- |
| (none) | text | `CHAT` — sends a PRIVMSG to the active channel; publishes a local-echo `chat` event. |
| `/join` | `#channel` | Joins the channel, sets it active, publishes `roster` + `info`. |
| `/part` | `#channel` | Parts the channel, publishes `roster` + `info`. |
| `/send` | `<target> <text…>` | Sends a PRIVMSG to an explicit target (channel or nick); local-echoes a `chat` event. |
| `/help` | — | Switches the info pane to the `help` view; publishes `view` + `info`. |
| `/overview` | — | Switches the info pane to the `overview` view. |
| `/status` | — | Switches the info pane to the `status` view. |

## Recognised but not-yet-supported

These slashes parse but currently publish an `error` event of the
form `<command>: not yet supported`. Adding any of them is a
non-breaking change — wire a new `_exec_*` helper into
`Session._exec_dispatch`.

| Verb | Args | Spec intent |
| --- | --- | --- |
| `/topic` | `<channel> <text…>` | Set channel topic via `TOPIC`. |
| `/channels` | — | List channels reachable on the server. |
| `/who` | `<channel>` | Refresh the roster from `WHO`. |
| `/read` | `<channel> [n]` | Re-read recent buffer for a channel. |
| `/agents` | — | List agent participants. |
| `/start` | `<agent>` | Start a managed agent. |
| `/stop` | `<agent>` | Stop a managed agent. |
| `/restart` | `<agent>` | Restart a managed agent. |
| `/icon` | `<emoji>` | Update the lens's `ICON`. |
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
/help                 → switches info pane to the help view
/foo                  → publishes `error: unknown command: /foo`
```
