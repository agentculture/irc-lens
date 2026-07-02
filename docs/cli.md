# `irc-lens` CLI reference

Reference for every command exposed by `irc-lens`. The CLI is *rendered*
from a single `agentfront.app.App` registry (`irc_lens.cli.build_app()`,
`src/irc_lens/cli/__init__.py`), which also backs the MCP and HTTP
(`/agent`) surfaces — see `docs/architecture.md` for how the registry is
assembled and how the surfaces are kept from drifting apart. This doc
enumerates the resulting user-facing surface.

## Globals

| Flag | Purpose |
| --- | --- |
| `--version`, `-V` | Print package version and exit. |
| `--help`, `-h` | Print top-level usage and exit. |

## Meta-verbs

Four verbs exist purely so an agent can introspect `irc-lens` without
reading source. All four accept `--json`, exit 0 on success, and never
leak a Python traceback — these are `agentfront`'s agent-first rubric
contracts.

| Verb | Answers | Exit behavior |
| --- | --- | --- |
| `learn` | "what is this tool, broadly" | Always 0. |
| `explain [path]` | "what does this one verb/noun do" | 0 for a known path; non-zero with `error:`/`hint:` for a bogus one. |
| `overview [path]` | "what is present here right now" | Always 0 — descriptive, not verifying (see below). |
| `doctor [path]` | "what is wrong, and how do I fix it" | 0 when healthy, 1 when a check fails. |

`learn` and `explain` are rendered straight through
`agentfront.cli_surface.run_cli`. `overview` and `doctor` are the two
exceptions: `agentfront` 0.20.0 reserves both names but its own stock
shapes are too thin for this repo (`overview --json` would return a bare
noun list instead of `{subject, sections}`; stock `doctor` has no
`--json` at all), so `irc_lens.cli.main()` routes those two verbs to
`irc_lens.cli._meta`, which still derives its output from the same App
registry. See `docs/architecture.md`'s decision log for the full
rationale.

```bash
irc-lens learn
irc-lens learn --json
irc-lens explain
irc-lens explain serve
irc-lens explain --json serve
irc-lens overview
irc-lens overview --json
irc-lens cli overview          # noun-scoped variant — every noun with
                                # action-verbs exposes its own overview
irc-lens doctor
irc-lens doctor --json
```

`overview <bogus-path>` exits **0** with a warning section — `overview`
is descriptive, not a verifier; hard-failing on a missing target would
belong to a verb like `doctor`, not `overview`. `overview --json` returns
`{"subject", "path", "sections"}`; `doctor --json` returns `{"healthy",
"checks"}`, where every failed check carries a non-empty `remediation`.

Known gap: `agentfront` 0.20.0's `explain <path>` resolves only
registered *tool* paths, not doc slugs — `irc-lens explain about` misses
even though `about` is a real doc page. Reach the doc pages via `learn`,
`irc-lens learn --json`, or the HTTP front's `/agent/llms.txt` and
`/agent/sitemap.xml` instead.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | User error (bad input, unreachable AgentIRC, bad seed/config) |
| `2` | Environment error (port in use, permission denied, missing dependency) |
| `3+` | Reserved |

Every failure raises `AfiError` (`src/irc_lens/_errors.py` — a verbatim
subclass of `agentfront.errors.AgentfrontError`); the dispatcher catches
it and exits with `err.code`. Precedent in
`src/irc_lens/cli/_commands/serve.py`: the `LensConnectionLost` branch in
`_serve_async` raises `AfiError(code=EXIT_USER_ERROR)`, while the
`TCPSite.start()` `OSError` branch raises
`AfiError(code=EXIT_ENV_ERROR)`. Symbol references are used here
deliberately — line numbers rot.

Every failure renders on stderr as:

```text
error: <message>
hint: <remediation>
```

`--json` mode emits the same shape as `{"code": N, "message": "...",
"remediation": "..."}` on stderr instead. Stdout stays clean on success
and is never mixed with stderr. This is the CLI's error contract only —
the web console's HTTP error responses use a separate `{error, hint}`
shape; see `docs/architecture.md`'s decision log.

## Configuration

irc-lens reads `~/.config/irc-lens/config.yaml` by default (override
with `--config <path>`, respecting `$XDG_CONFIG_HOME`). Initialize with:

```bash
irc-lens config init
irc-lens config overview
```

The starter file is in `auth.mode: dev`, suitable for a local AgentIRC
on `127.0.0.1:6667`. To deploy behind Cloudflare Access, switch
`auth.mode` to `cloudflare-access` and set `auth.cloudflare.aud`,
`auth.cloudflare.team_domain`, and `auth.allowed_emails`. See
[deployment-cloudflare-access.md](deployment-cloudflare-access.md). A
config file is required for `serve` and for every live tool below —
`irc-lens config init` is the remediation every one of them points at
when it's missing.

### `--nick` and `--bind`

In `auth.mode: dev`, `--nick` overrides `auth.dev.nick`. In
`auth.mode: cloudflare-access`, passing `--nick` is a hard error: the
nick is derived per user from the JWT principal (email or service-token
common-name). `auth.allowed_emails` is only the allowlist. A
non-loopback `--bind` (or `web.bind`) under CF mode is coerced to
`127.0.0.1` with a `WARNING` log line, because cloudflared terminates
locally.

## `irc-lens serve`

Launch the aiohttp web console against an AgentIRC server. The process
establishes the IRC connection first (fail-fast) and only then binds the
local web port.

| Flag | Required | Default | Purpose |
| --- | --- | --- | --- |
| `--config` | no | `~/.config/irc-lens/config.yaml` | Path to the config file (required to exist). |
| `--host` | no | from config, else `127.0.0.1` | AgentIRC server host. |
| `--port` | no | from config, else `6667` | AgentIRC server port. |
| `--nick` | no | from config | Nick to register on AgentIRC (dev mode only). |
| `--web-port` | no | from config, else `8765` | Local HTTP port for the lens UI. |
| `--bind` | no | from config, else `127.0.0.1` | Web bind address |
| `--icon` | no | none | Optional emoji for `ICON` |
| `--open` | no | off | Auto-launch browser |
| `--seed` | no | none | Path to a YAML fixture preloading view state — see [Seed schema](#seed-schema). |
| `--log-json` | no | off | Emit stderr logs as one JSON object per line. |

### Lifecycle

1. Load `LensConfig` from `--config` or the default path — missing file
   exits `1` pointing at `irc-lens config init`.
2. Argparse validates the remaining flags; CLI overrides layer on top of
   the loaded config.
3. `--bind 0.0.0.0` (or `::`) prints a stderr warning (no auth in v1).
4. `Session.connect()` against AgentIRC. Failure → exit `1` with
   `error: cannot reach AgentIRC at <host>:<port>: …`.
5. `--seed PATH` overlays YAML state on the connected `Session`.
   Errors propagate as exit `1` (user content) or `2`
   (environment failure on an existing file).
6. `aiohttp.web.AppRunner.setup()` + `TCPSite.start()`. Port-in-use
   → exit `2` with `error: cannot bind web port …`.
7. The display URL is printed on stderr (`irc-lens serving on
   http://…/`). When binding to `0.0.0.0`, the printed URL uses
   `127.0.0.1` so it is browser-routable.
8. Wait until SIGINT / SIGTERM. On signal, disconnect the IRC
   session, clean up the runner, exit `0`.

### Examples

```bash
# Common case — host/port default to a local AgentIRC at 127.0.0.1:6667:
irc-lens serve --nick lens --open

# Same, with a deterministic preload (Playwright-fixture pattern):
irc-lens serve --nick lens --seed tests/fixtures/basic.yaml

# Point at a remote AgentIRC:
irc-lens serve --host irc.example.org --port 6667 --nick ops

# Bind to all interfaces (warning printed, no auth in v1):
irc-lens serve --nick ops --bind 0.0.0.0 --web-port 8080

# JSON-line stderr for log shipping:
irc-lens serve --nick lens --log-json
```

## `irc-lens mcp`

Serve the same registry `learn`/`explain` describe as an MCP server over
stdio. Exposes exactly one MCP tool, `run`, accepting `{"command":
[...], "args": {...}}` — dispatching against the identical `App` the CLI
and TAUI surfaces read, so the tool catalog can never drift between
surfaces. Intended to be launched by an MCP client (an agent harness),
not run interactively; it blocks reading/writing stdio until the client
disconnects or the process receives Ctrl-C (exit 0 either way). If the
optional `mcp` dependency is missing, it exits `2` with a hint to
install `agentfront[mcp]` — moot in practice, since irc-lens declares
that extra as a hard dependency.

```bash
irc-lens mcp
```

## `irc-lens tui`

Open the terminal UI (TAUI): a live, keyboard-driven view over the same
App registry the CLI/MCP/HTTP surfaces share, built on
`agentfront.taui.driver.LiveDriver`.

- At a real TTY (both stdin and stdout), it enters raw/cbreak mode and
  shuttles keystrokes into the driver until you quit. Keys: `up`/`down`
  move focus between panel items, `esc` dismisses the topmost popup,
  `enter` activates a popup's bound action, `q`/Ctrl-C quits and
  restores the terminal. Exit `0` on quit.
- Piped or otherwise non-interactive (either stream is not a TTY), it
  never touches terminal mode: it writes one hint line to stderr, then
  the same markdown front the HTTP `/agent/front` route serves, to
  stdout, and returns `0`. This guarantees the non-interactive path can
  never drift from what a peer fetching the HTTP front or a human at a
  real terminal sees — it's the same registry-derived content, a
  different tier.

```bash
irc-lens tui             # interactive at a real TTY
irc-lens tui | cat        # non-interactive — prints the front, exits 0
```

## Live tools

Eleven verbs, each an ephemeral connect-execute-disconnect session
against the AgentIRC server named by the resolved config (the same
`--config` resolution `serve` uses). Every one accepts `--json` and maps
onto the same slash-command a human types into the browser console (see
`docs/slash-commands.md`), dispatching through the identical
`Session.execute`/`ParsedCommand` path — the CLI/MCP verb and the console
verb cannot behave differently.

| Tool | Args | Returns |
| --- | --- | --- |
| `send` | `<target> <text>` | `{target, text}` |
| `join` | `<channel>` | `{channel, joined_channels}` |
| `part` | `<channel>` | `{channel, joined_channels}` |
| `read` | `<channel> [--limit N]` | list of `{channel, nick, timestamp, text}` |
| `channels` | — | list of channels visible on the server |
| `who` | `<target>` | list of `{nick, user, host, server, flags, realname}` |
| `mesh` | `<channels>` (comma/space-separated) | `{nodes, edges}` mesh-graph snapshot |
| `switch` | `<channel>` | `{current_channel}` |
| `topic` | `<channel> [text]` | `{channel, topic, action}` |
| `me` | `<channel> <text>` | `{channel, action}` |
| `icon` | `<emoji>` | `{icon}` |

```bash
irc-lens join '#ops'
irc-lens send '#ops' 'deploy starting' --json
irc-lens mesh '#ops,#dev' --json
```

### Ephemeral-session semantics

Each call opens its own throwaway `Session`, performs exactly one verb,
and disconnects in a `finally` — no state persists between two calls (a
second call cannot see channels the first joined). `switch` and `mesh`
join their target channel(s) themselves first, in the same session,
since an ephemeral session never has one from a prior call. `read`'s
history is bounded by what the server replays on that one fresh
connection.

### Failure mapping

Live tools require `auth.mode: dev` — there is no per-request JWT to
derive an identity from outside a browser session, so
`auth.mode: cloudflare-access` raises `AfiError(EXIT_USER_ERROR)` up
front rather than guessing an identity.

| Failure | Exit code |
| --- | --- |
| Missing/bad `--config` | `1` (hint points at `irc-lens config init`) |
| `auth.mode: cloudflare-access` | `1` |
| Cannot reach / register with AgentIRC at all | `2` |
| Connection drops mid-verb | `2` |
| A verb's own input validation fails (bad channel format, missing target/text) | `1` |

Note the deliberate difference from `serve`: `serve` maps "cannot reach
AgentIRC" to `1` (its own long-established, test-guarded contract);
the live tools map the same class of failure to `2` (the environment
failed to deliver a resource that should exist) per this repo's general
exit-code policy. Both are documented, intentional choices for their
respective commands — not an inconsistency to "fix."

## Seed schema

`--seed PATH` reads a YAML document and overlays it onto the
freshly-connected `Session` before `aiohttp.web.Application`
binds. Every top-level key is optional; `current_channel` is only
valid when it also appears in `joined_channels`.

```yaml
joined_channels:
  - "#general"
  - "#ops"
preload_messages:
  - {channel: "#general", nick: "alice", text: "hello world", timestamp: 1714000000}
  - {channel: "#general", nick: "bob",   text: "hi alice",    timestamp: 1714000005}
roster:
  - {nick: "alice", type: "human", online: true}
  - {nick: "bob",   type: "agent", online: true}
current_channel: "#general"
```

Validation rules:

- Unknown top-level keys raise (typo guard).
- Per-section type errors raise with the field name in the message.
- `current_channel` must appear in `joined_channels`.
- Timestamps must be finite and renderable by `time.localtime` —
  `NaN` / `Inf` / out-of-range values are rejected at seed time
  rather than crashing the initial HTML render.

Errors raise `AfiError` per the exit-code policy above. The
canonical fixture lives at `tests/fixtures/basic.yaml`.

### `media` section

Optional media configuration (absent section = defaults, feature on):

| Field | Default | Type | Purpose |
| --- | --- | --- | --- |
| `enabled` | `true` | boolean | Enable/disable media |
| `dir` | `$XDG_DATA_HOME/media` | path | Store directory |
| `max_file_bytes` | `10485760` | ≥ 1 | Max file size |
| `max_store_bytes` | `268435456` | ≥ 1 | Total store cap |
| `public_base_url` | `http://…` | URL | Cross-machine base |
| `remote_embeds` | `click` | click/auto/off | Remote rendering |
| `trusted_hosts` | `[]` | list | Auto-embed hosts |

For `public_base_url`: use `http://` or `https://`. For `remote_embeds`:
controls how non-lens URLs render (click-to-load, auto, or plain link).

Example:

```yaml
media:
  enabled: true
  dir: ~/.local/share/irc-lens/media
  max_file_bytes: 10485760
  max_store_bytes: 268435456
  public_base_url: https://lens.example.com
  remote_embeds: click
  trusted_hosts:
    - cdn.example.com
    - images.example.org
```
