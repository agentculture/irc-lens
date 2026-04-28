# `irc-lens` CLI reference

Phase 10 reference for every flag exposed by `irc-lens`. Source of truth
is `src/irc_lens/cli/`; this doc enumerates the user-facing surface.

## Globals

| Flag | Purpose |
| --- | --- |
| `--version` | Print package version and exit. |
| `--help`, `-h` | Print top-level usage and exit. |

The top-level globals (`learn`, `explain`, `overview`) are AFI-rubric
contracts — each exits 0 on success, supports `--json`, and never
leaks a Python traceback. See `docs/architecture.md` for the rationale
on the agent-first CLI shape.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | User error — bad input, unreachable AgentIRC, missing seed file, malformed YAML. |
| `2` | Environment error — failure to act on a resource that exists (port collision on `--web-port`, permission denied while reading a seed file). |
| `3+` | Reserved. |

The split is mandated by the AFI rubric; precedent in
`src/irc_lens/cli/_commands/serve.py`: the `LensConnectionLost`
branch in `_serve_async` raises `AfiError(code=EXIT_USER_ERROR)`,
while the `TCPSite.start()` `OSError` branch raises
`AfiError(code=EXIT_ENV_ERROR)`. Symbol references are used here
deliberately — line numbers rot.

Every failure renders on stderr as:

```
error: <message>
hint: <remediation>
```

`--json` mode emits the same shape as `{"code": N, "message": "...",
"remediation": "..."}` on stderr instead. Stdout stays clean on
success and is never mixed with stderr.

## `irc-lens learn`

Print the agent-onboarding text — purpose, command list, exit codes,
`--json`, `explain`. ≥ 200 chars on stdout. `--json` emits the same
content as a JSON payload.

```bash
irc-lens learn
irc-lens learn --json
```

## `irc-lens explain [path]`

Resolve a CLI path to its catalog entry. `irc-lens explain` lists
every documented entry; `irc-lens explain serve` prints just the
`serve` entry. Bogus paths exit non-zero with an `error:` + `hint:`
line per the rubric.

```bash
irc-lens explain
irc-lens explain serve
irc-lens explain --json serve
```

## `irc-lens overview [path]`

Descriptive (not verifying) walk through the project. Bogus paths
exit **0** with a warning section — the rubric reserves
hard-failure on missing targets to `afi cli verify`. `irc-lens cli
overview` is the noun-scoped variant required by the rubric for
every noun with action-verbs.

```bash
irc-lens overview
irc-lens overview --json
irc-lens cli overview
```

## `irc-lens serve`

Launch the aiohttp web console against an AgentIRC server. The
process establishes the IRC connection first (fail-fast) and only
then binds the local web port.

| Flag | Required | Default | Purpose |
| --- | --- | --- | --- |
| `--host` | no | `127.0.0.1` | AgentIRC server host. |
| `--port` | no | `6667` | AgentIRC server port. |
| `--nick` | yes | — | Nick to register on AgentIRC. |
| `--web-port` | no | `8765` | Local HTTP port for the lens UI. |
| `--bind` | no | `127.0.0.1` | Bind address for the local web app. `0.0.0.0` prints a no-auth warning to stderr. |
| `--icon` | no | none | Optional emoji passed to AgentIRC `ICON`. |
| `--open` | no | off | Auto-launch the default browser at the lens URL after binding. |
| `--seed` | no | none | Path to a YAML fixture preloading view state — see [Seed schema](#seed-schema). |
| `--log-json` | no | off | Emit stderr logs as one JSON object per line. |

### Lifecycle

1. Argparse validates the required flags.
2. `--bind 0.0.0.0` prints a stderr warning (no auth in v1).
3. `Session.connect()` against AgentIRC. Failure → exit `1` with
   `error: cannot reach AgentIRC at <host>:<port>: …`.
4. `--seed PATH` overlays YAML state on the connected `Session`.
   Errors propagate as exit `1` (user content) or `2`
   (environment failure on an existing file).
5. `aiohttp.web.AppRunner.setup()` + `TCPSite.start()`. Port-in-use
   → exit `2` with `error: cannot bind web port …`.
6. The display URL is printed on stderr (`irc-lens serving on
   http://…/`). When binding to `0.0.0.0`, the printed URL uses
   `127.0.0.1` so it is browser-routable.
7. Wait until SIGINT / SIGTERM. On signal, disconnect the IRC
   session, clean up the runner, exit `0`.

### Examples

```bash
# Common case — host/port default to a local AgentIRC at 127.0.0.1:6667:
irc-lens serve --nick lens --open

# Same, with a deterministic preload (Phase 9c Playwright pattern):
irc-lens serve --nick lens --seed tests/fixtures/basic.yaml

# Point at a remote AgentIRC:
irc-lens serve --host irc.example.org --port 6667 --nick ops

# Bind to all interfaces (warning printed, no auth in v1):
irc-lens serve --nick ops --bind 0.0.0.0 --web-port 8080

# JSON-line stderr for log shipping:
irc-lens serve --nick lens --log-json
```

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

* Unknown top-level keys raise (typo guard).
* Per-section type errors raise with the field name in the message.
* `current_channel` must appear in `joined_channels`.
* Timestamps must be finite and renderable by `time.localtime` —
  `NaN` / `Inf` / out-of-range values are rejected at seed time
  rather than crashing the initial HTML render.

Errors raise `AfiError` per the exit-code policy above. The
canonical fixture lives at `tests/fixtures/basic.yaml`.
