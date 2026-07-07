---
name: verify
description: Launch and drive the real irc-lens web console to verify a change at its surface — build/launch/drive recipe for this repo (serve against a live AgentIRC, curl the routes, degrade-state probes). Use when verifying nontrivial changes end-to-end before committing, when asked to run or screenshot the console, or when tests alone can't show runtime behavior.
---

# verify — drive the real console

Runtime observation for irc-lens changes: launch `irc-lens serve`, hit
the surface with `curl` (or Playwright for pane-level checks), capture
what it shows. Tests are CI's evidence; this skill is for what tests
can't show — the running app.

## Launch recipe

```bash
SCRATCH=$(mktemp -d)
cat > "$SCRATCH/config.yaml" <<'EOF'
auth:
  mode: dev
  dev:
    nick: spark-lensverify   # MUST carry the live server's nick prefix (see gotchas)
    email: verify@local
server:
  name: spark                # match the live AgentIRC server's name
  host: 127.0.0.1
  port: 6667
web:
  bind: 127.0.0.1
  port: 8931                 # pick a free port
media:
  enabled: false
EOF
nohup uv run irc-lens serve --config "$SCRATCH/config.yaml" \
  > "$SCRATCH/serve.log" 2>&1 &
sleep 3 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8931/healthz
```

`serve` **requires a live AgentIRC connection at startup** — it exits 1
if nothing listens on `server.host:port`. On a machine running the
Culture mesh, `ss -tlnp | grep 6667` confirms; check
`~/.culture/pids/` for what's running.

## Flows worth driving

- `GET /` — the console page (opens a real session against the IRCd).
- `GET /residents` — always 200; drive all its states:
  - no overview server → "Resource view unavailable" notice;
  - real overview (`uv run --project <culture-checkout> culture mesh
    overview --serve`, writes `~/.culture/pids/overview-<name>.port`) →
    live payload or the pending-agentirc notice;
  - stale port file (kill the overview hard) → unavailable notice;
  - supported table: serve a static `residents.json` via
    `python3 -m http.server` and set `culture.residents_url` to it.
- `POST /input` — slash-verb dispatch (needs the session).
- Degrade probes: wrong method (405), trailing slash (404), security
  headers on HTML (`curl -sI` → `nosniff` + CSP).

## Gotchas (cost time once)

- **Nick prefix**: a live AgentIRC enforces `<server-name>-` prefixed
  nicks; `auth.dev.nick: lens-verify` against a server named `spark` is
  rejected at startup with a clear hint. Use `spark-<something>`.
- **`uv run` wraps the process**: killing the `nohup` PID kills the
  wrapper, not the server. Find the real PID via
  `ss -tlnp | grep <port>` before a hard-kill probe.
- **Clean up culture run files**: a hard-killed overview leaves stale
  `~/.culture/pids/overview-<name>.{pid,port}` — remove them after.
- The installed `culture` CLI may be older than the sibling checkout;
  run branch-only surfaces via `uv run --project <culture-checkout>`.
