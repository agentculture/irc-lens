# Server-Sent Events

`GET /events` opens a long-lived SSE stream. Every event published on
`Session.event_bus` is serialised by `irc_lens.web.events.format_sse`
and streamed to subscribers. Each subscriber owns a bounded queue
(default 256 events) with a drop-oldest policy plus a single-shot
`error: events dropped` notice.

## Event catalogue

| Event | Payload | Template | Target |
| --- | --- | --- | --- |
| `chat` | HTML | `_chat_line.html.j2` | append `#chat-log` |
| `roster` | HTML | `_sidebar.html.j2` | replace `#sidebar` |
| `info` | HTML | `_info.html.j2` | replace `#info` |
| `typing` | HTML (empty when nobody is thinking) | `_typing.html.j2` | replace `#typing` |
| `view` | JSON view state | — | `<body data-view>` |
| `error` | JSON message | — | `#toast-region` |

The `view` and `error` payloads are spec-strict (handover-design
spec lines 162–163) — additional fields are intentionally not
emitted to keep the contract tight. Chat fragments may now contain a
`.lens-media` block when the message text includes media URLs; no new
SSE event types are introduced by media rendering.

## Publish points

Source: `src/irc_lens/session.py`.

| Trigger | Events published |
| --- | --- |
| `_exec_chat(text)` (plain text) | `chat` (local echo) |
| `_exec_send(target, text)` (`/send`) | `chat` (local echo) |
| `_exec_join(channel)` (`/join`) | `roster` + `info` |
| `_exec_part(channel)` (`/part`) | `roster` + `info` |
| `_switch_view(name)` (`/help`, `/overview`, `/status`) | `view` + `info` |
| Inbound `PRIVMSG` to active channel | `chat` |
| `_exec_chat` in a sandbox room the sandbox agent is in | `typing` ("`<agent>` is thinking") |
| Inbound `PRIVMSG` from the sandbox agent, `THINKING_TIMEOUT_S` (90 s), room switch (`log`) | `typing` (cleared, or the new room's state) |
| Inbound `JOIN` / `PART` (server-confirmed) | `roster` |
| Any `_publish_error` (invalid input, unsupported verb) | `error` |
| Subscriber queue overflow | one-shot `error` (`events dropped`) |

## "Agent is thinking" (`typing`)

Sandbox sessions (guest and an approved user's Guest view) set
`Session.thinking_agent` to the sandbox agent's nick. Sending a message in
a room the agent is in (per the live roster) shows "`<agent>` is
thinking" with animated dots (static under `prefers-reduced-motion`)
under the log, until the agent's next message in that room, a 90 s
timeout, or disconnect. With several agents in the room (roster type
`agent`), a message that names one (`@sbx-bolt …`), whether the guest's
or another agent's, shows that agent thinking instead; an untagged guest
message shows the sandbox agent, which answers every human message. The real mesh never shows it: agents there don't
answer every message. This is lens-side inference for now; once AgentIRC
relays IRCv3 `TAGMSG` and the agent sends `+typing`, the same event will
be driven by the real signal.

## Wire format

`format_sse` produces one event block per `SessionEvent`:

```text
event: <name>
data: <line 1>
data: <line 2>
…

```

Multi-line HTML payloads are split into one `data:` line per source
line, terminated by a blank line, per the SSE spec. The wire-format
round-trip is pinned by `tests/test_web_events.py`; per-fragment
template testids (the `chat`/`roster`/`info` payload bodies) are
pinned by `tests/test_render.py`.

## DOM contract (`data-testid` + IDs)

`data-testid` attributes are stable contracts for Playwright agents
(`docs/playwright.md`) and are pinned by `tests/test_e2e_playwright.py`.

| Selector | Source template | Purpose |
| --- | --- | --- |
| `[data-testid="connection-status"]` | `index.html.j2` | Conn-state badge |
| `[data-testid="tier-badge"]` | `index.html.j2` | Guest / Real mesh / Sandbox preview pill |
| `[data-testid="identity"]` / `[data-testid="header-room"]` | `index.html.j2` | Nick and active room in the header (never host:port) |
| `[data-testid="sandbox-banner"]`, `sandbox-enter`, `sandbox-leave` | `index.html.j2` | Amber "Guest view" banner and toggle buttons |
| `[data-testid="command-palette"]` | `index.html.j2` | Inline slash-command palette (`chatui.js`) |
| `[data-testid="agent-state"]` | `_presence.html.j2` | Sandbox agent online/offline line |
| `[data-testid="rooms-toggle"]` | `index.html.j2` | Phone rooms drawer button |
| `[data-testid="sidebar"]` | `index.html.j2` | Sidebar wrapper |
| `[data-testid="sidebar-channel"]` | `_sidebar.html.j2` | Channel row + state |
| `[data-testid="sidebar-entity"]` | `_sidebar.html.j2` | Roster row |
| `[data-testid="chat-log"]` | `index.html.j2` | Chat log `#chat-log` |
| `[data-testid="chat-line"]` | `_chat_line.html.j2` | Chat line (timestamp) |
| `[data-testid="chat-line-nick"]` | `_chat_line.html.j2` | Nick span |
| `[data-testid="chat-line-text"]` | `_chat_line.html.j2` | Text span |
| `[data-testid="typing-slot"]` / `[data-testid="agent-typing"]` | `index.html.j2` / `_typing.html.j2` | "`<agent>` is thinking" slot (polite live region) and its line |
| `[data-testid="media-embed"]` | `_chat_line/media.js` | Embedded media |
| `[data-testid="media-placeholder"]` | `_chat_line.html.j2` | Media card |
| `[data-testid="media-attach"]` | `index.html.j2` | Attach button |
| `[data-testid="media-file-input"]` | `index.html.j2` | File input |
| `[data-testid="media-record"]` | `index.html.j2` | Mic record button |
| `[data-testid="chat-input"]` | `index.html.j2` | Chat input field |
| `[data-testid="chat-submit"]` | `index.html.j2` | Submit button |
| `[data-testid="info"]` | `index.html.j2` | Info pane `#info` |
| `[data-testid="view-indicator"]` | `_info.html.j2` | View state badge |

The `<form>` element itself is addressed by `id="chat-form"` (no
`data-testid`) — tests submit by clicking
`[data-testid="chat-submit"]` or pressing Enter on
`[data-testid="chat-input"]`. Every other element tests need to
drive carries a stable `data-testid`.

## Browser glue

`src/irc_lens/static/lens.js` (~89 lines) wires the EventSource:

* `chat` → append into `#chat-log` via a `<template>` parse,
  trimming the oldest line once `#chat-log` exceeds 500 children
  (mirrors the server-side `MessageBuffer` per-channel cap).
* `roster` / `info` → replace the matching container's innerHTML.
* `view` → set `document.body.dataset.view` so CSS keyed off
  `[data-view="..."]` re-skins the layout.
* `error` → render a toast in `#toast-region` (`role="alert"` /
  `aria-live="assertive"`).

EventSource transport-level errors (network drop, server restart)
flip `body[data-conn="down"]`; reconnect clears the marker.

## Backpressure

`SessionEventBus.publish()` is fire-and-forget. Each subscriber's
queue is drained by its own coroutine; if the consumer can't keep
up, the oldest events are dropped and a single `error` event with
`{"message": "events dropped"}` is enqueued so the UI can surface
the gap. The next non-overflow publish re-arms the notice. This
keeps a slow browser tab from blocking publishers without silently
losing the gap signal.
