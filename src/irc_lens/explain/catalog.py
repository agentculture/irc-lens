"""Markdown catalog for ``irc-lens explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty
tuple and ``("irc-lens",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# irc-lens

Reactive web console for AgentIRC. One process owns one IRC connection
and serves one browser tab. Server-rendered HTML fragments delivered via
SSE keep the DOM deterministic and Playwright-driveable.

## Verbs

- `irc-lens learn` — structured self-teaching prompt.
- `irc-lens explain <path>` — markdown docs for any noun/verb.
- `irc-lens overview [path]` — descriptive rollup across surfaces.
- `irc-lens serve` — launch the web console (lands in a later phase).

## Nouns

- `cli` — meta-introspection of the CLI surface (`cli overview`).
- `media` — image + audio upload, viewing, and configuration.

## Exit-code policy

- `0` success / clean shutdown
- `1` user-input error (bad flag, AgentIRC unreachable)
- `2` environment / setup error (web port in use)
- `3+` reserved

## See also

- `irc-lens explain learn`
- `irc-lens explain explain`
- `irc-lens explain overview`
- `irc-lens explain cli`
"""

_LEARN = """\
# irc-lens learn

Prints a structured self-teaching prompt covering irc-lens's purpose,
command map, exit-code policy, `--json` support, and `explain` pointer.

## Usage

    irc-lens learn
    irc-lens learn --json
"""

_EXPLAIN = """\
# irc-lens explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help`
(terse, positional), `explain` is global and addressable by path.

## Usage

    irc-lens explain irc-lens
    irc-lens explain learn
    irc-lens explain --json <path>
"""


_OVERVIEW = """\
# irc-lens overview [path]

Descriptive rollup across irc-lens's interface surfaces (globals, nouns,
runtime). Unlike `afi cli verify`, `overview` never hard-fails on an
unknown path — it emits a warning section and a zero-target report
alongside the full rollup, then exits 0.

## Usage

    irc-lens overview                # full rollup
    irc-lens overview cli            # restricted to the cli noun
    irc-lens overview --json         # structured payload
"""

_CLI = """\
# irc-lens cli

Meta-introspection of the irc-lens CLI surface itself. Currently exposes
a single verb:

- `irc-lens cli overview` — rollup of the CLI surface (delegates to the
  global `overview` with subject pinned to `cli`).
"""

_CLI_OVERVIEW = """\
# irc-lens cli overview

Rollup of the irc-lens CLI surface, equivalent to `irc-lens overview cli`
but reachable through the `cli` noun. Honours the same contract as the
global `overview`: descriptive, never hard-fails — any extra path tokens
after `cli overview` are treated as unknown sub-subjects and produce a
warning section, not an error.

## Usage

    irc-lens cli overview
    irc-lens cli overview --json
"""

_SERVE = """\
# irc-lens serve

Launches the aiohttp web console (HTMX + SSE, server-rendered HTML
fragments) against an AgentIRC server. One process owns one IRC
connection and serves one browser tab.

## Usage

    irc-lens serve --host <agentirc-host> --port <agentirc-port> --nick <nick>
                   [--web-port 8765] [--bind 127.0.0.1] [--icon <emoji>]
                   [--open] [--seed <yaml>] [--log-json]

## Lifecycle

* Connects to AgentIRC synchronously before binding the web port —
  unreachable server exits 1 with `error:` + `hint:` and aiohttp
  never binds.
* `--bind 0.0.0.0` prints a loud stderr warning (no auth in v1).
* Web port already in use exits 2 (env error).
* Foreground only; Ctrl-C is the supported shutdown.
"""

_MEDIA = """\
# Media support

Humans and agents share images and audio through the lens. The wire
carries short `http(s)` URLs only (AgentIRC's 8192-byte inbound line cap
rules out inline bytes); blobs move over HTTP directly between consumer
and host.

## Configuration

Add a `media:` section to `config.yaml`:

- `enabled` — `true` or `false` (default: `true`).
- `dir` — blob-store directory (default: `$XDG_DATA_HOME/irc-lens/media`).
- `max_file_bytes` — per-upload cap in bytes (default: 10 MiB).
- `max_store_bytes` — total store cap; oldest files evicted past this
  (default: 256 MiB). Must satisfy `max_file_bytes <= max_store_bytes`.
- `public_base_url` — advertised base URL for cross-machine peers
  (default: derives from `web.bind`/`web.port`; must be `http(s)` if set).
- `remote_embeds` — `click` (click-to-load), `auto` (auto-embed), or
  `off` (plain links) for remote media URLs (default: `click`).
- `trusted_hosts` — list of hostnames that auto-embed even when
  `remote_embeds: click` (default: empty list).

## Security

* `GET /media/{token}` is auth-exempt. The 128-bit unguessable token
  *is* the capability — possession of the URL means you saw it in the
  channel (IRC trust model). Uploads (`POST /upload`) remain authed.
* Upload validation: magic-byte sniff + extension-allowlist (PNG, JPG,
  GIF, WebP; MP3, OGG, WAV, WebM, M4A, FLAC). SVG is excluded (XSS
  vector on auth-exempt origin).
* CSP headers restrict markup injection and set `X-Content-Type-Options:
  nosniff` + `Referrer-Policy: no-referrer` on all responses.
* Remote embeds use click-to-load by default to avoid leaking viewer
  IP/headers to arbitrary hosts; `trusted_hosts` restores auto-embed.

See `docs/security-checklist.md` for the full media checklist.
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("irc-lens",): _ROOT,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI_OVERVIEW,
    ("serve",): _SERVE,
    ("media",): _MEDIA,
}
