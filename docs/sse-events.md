# Server-Sent Events

`GET /events` opens a long-lived SSE stream. Every event published on
`Session.event_bus` is serialised by `irc_lens.web.events.format_sse`
and streamed to subscribers. Each subscriber owns a bounded queue
(default 256 events) with a drop-oldest policy plus a single-shot
`error: events dropped` notice.

## Event catalogue

| Event | Payload | Fragment template | Browser target |
| --- | --- | --- | --- |
| `chat` | rendered `_chat_line.html.j2` (HTML) | `templates/_chat_line.html.j2` | append into `#chat-log` |
| `roster` | rendered `_sidebar.html.j2` (HTML) | `templates/_sidebar.html.j2` | replace `#sidebar` innerHTML |
| `info` | rendered `_info.html.j2` (HTML) | `templates/_info.html.j2` | replace `#info` innerHTML |
| `view` | JSON `{"view": "chat" \| "help" \| "overview" \| "status"}` | — | set `<body data-view>` attribute |
| `error` | JSON `{"message": "..."}` | — | toast region (`#toast-region`) |

The `view` and `error` payloads are spec-strict (handover-design
spec lines 162–163) — additional fields are intentionally not
emitted to keep the contract tight.

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
| Inbound `JOIN` / `PART` (server-confirmed) | `roster` |
| Any `_publish_error` (invalid input, unsupported verb) | `error` |
| Subscriber queue overflow | one-shot `error` (`events dropped`) |

## Wire format

`format_sse` produces one event block per `SessionEvent`:

```
event: <name>
data: <line 1>
data: <line 2>
…

```

Multi-line HTML payloads are split into one `data:` line per source
line, terminated by a blank line, per the SSE spec. Tests pin the
round-trip in `tests/test_render.py` and `tests/test_web_events.py`.

## DOM contract (`data-testid` + IDs)

`data-testid` attributes are stable contracts for Playwright agents
(`docs/playwright.md`) and are pinned by `tests/test_e2e_playwright.py`.

| Selector | Source template | Purpose |
| --- | --- | --- |
| `[data-testid="connection-status"]` | `index.html.j2` | Conn-state badge (`lens-conn--healthy` / `lens-conn--down`). |
| `[data-testid="sidebar"]` | `index.html.j2` | Sidebar wrapper. |
| `[data-testid="sidebar-channel"]` | `_sidebar.html.j2` | Channel row; carries `data-channel="#x"` and `lens-channel--active` for the current channel. |
| `[data-testid="sidebar-entity"]` | `_sidebar.html.j2` | Roster row. |
| `[data-testid="chat-log"]` | `index.html.j2` | Container `#chat-log`. |
| `[data-testid="chat-line"]` | `_chat_line.html.j2` | One rendered chat line (timestamp + nick + text spans). |
| `[data-testid="chat-line-nick"]` | `_chat_line.html.j2` | Nick span. |
| `[data-testid="chat-line-text"]` | `_chat_line.html.j2` | Text span. |
| `[data-testid="chat-input"]` | `index.html.j2` | The input element (id `chat-input`). |
| `[data-testid="chat-submit"]` | `index.html.j2` | Submit button. |
| `[data-testid="info"]` | `index.html.j2` | Info-pane container `#info`. |
| `[data-testid="view-indicator"]` | `_info.html.j2` | Carries `data-view="chat\|help\|overview\|status"`. |

The form element itself is `id="chat-form"` (no `data-testid`); the
testid lives on the submit button. Tests assert against the testid
contract above.

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
