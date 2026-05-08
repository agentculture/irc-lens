# Live-testing irc-lens behind Cloudflare Access (operator runbook)

Sister document to [deployment-cloudflare-access.md](deployment-cloudflare-access.md).
That one walks through a manual self-managed deployment from scratch.
This one covers the **live-test path**: you've been handed an
auto-provisioned env file by a tool like `cfafi`/`cultureflare` (or
the bundled `scripts/cf-roundtrip/setup.sh`), and you want to bring up
`cloudflared` + `irc-lens serve` locally and validate that the public
hostname round-trips end-to-end. It also captures the gotchas that
will eat your afternoon if you skip them.

The shape of the handoff: a gitignored `.cf-roundtrip.env` (mode 0600)
in the repo root, populated with seven `IRC_LENS_TEST_*` variables —
see `scripts/cf-roundtrip/README.md` for the canonical list. The
provisioner has already created the Cloudflare Tunnel, DNS record,
Access app, allow-policy, and service token. You're plugging into the
local end.

Throughout this doc, replace `<your-host>` with the public hostname
the provisioner used.

## Prerequisites

- `.cf-roundtrip.env` present in the repo root, with all seven
  `IRC_LENS_TEST_*` keys populated.
- `cloudflared` on `$PATH` (any recent version).
- AgentIRC running on `127.0.0.1:6667` (or wherever the lens config
  will dial). The round-trip test asserts behavior past the auth
  layer; without an IRC backend, slash-commands will land but get
  503'd downstream.
- `uv` for managing Python deps.

## Run the round-trip

All commands from the repo root.

### 1. Load the env file

```bash
set -a; source .cf-roundtrip.env; set +a
```

Sanity check (every line should print `set`):

```bash
for v in IRC_LENS_TEST_AUD IRC_LENS_TEST_HOSTNAME IRC_LENS_TEST_TEAM_DOMAIN \
         IRC_LENS_TEST_CLIENT_ID IRC_LENS_TEST_CLIENT_SECRET \
         IRC_LENS_TEST_TOKEN_NAME IRC_LENS_TEST_TUNNEL_TOKEN; do
  printf '%s=%s\n' "$v" "${!v:+set}"
done
```

### 2. Bring up the cloudflared connector

```bash
nohup cloudflared tunnel run --token "$IRC_LENS_TEST_TUNNEL_TOKEN" \
  >/tmp/cloudflared.log 2>&1 &
```

Wait for four `Registered tunnel connection` lines in the log (one per
edge connection, ~5 s).

### 3. Write the lens config and start `irc-lens serve`

```bash
mkdir -p /tmp/lens-cf-live
cat >/tmp/lens-cf-live/config.yaml <<EOF
auth:
  mode: cloudflare-access
  cloudflare:
    aud: ${IRC_LENS_TEST_AUD}
    team_domain: ${IRC_LENS_TEST_TEAM_DOMAIN}
  allowed_emails:
    - <your-email>
  allowed_service_tokens:
    - ${IRC_LENS_TEST_CLIENT_ID}
server:
  name: <agentirc-server-name>
  host: 127.0.0.1
  port: 6667
web:
  bind: 127.0.0.1
  port: 8765
EOF
chmod 600 /tmp/lens-cf-live/config.yaml

nohup uv run irc-lens serve --config /tmp/lens-cf-live/config.yaml \
  >/tmp/lens.log 2>&1 &
```

Wait for local `/healthz`:

```bash
until curl -fsS http://127.0.0.1:8765/healthz >/dev/null; do sleep 1; done
```

### 4. Smoke through the public hostname

```bash
H_ID="CF-Access-Client-Id: $IRC_LENS_TEST_CLIENT_ID"
H_SEC="CF-Access-Client-Secret: $IRC_LENS_TEST_CLIENT_SECRET"
URL="https://$IRC_LENS_TEST_HOSTNAME"

# Edge blocks unauth → 302 to SSO (or 401 if no email policy).
curl -isS -o /dev/null -w '%{http_code}\n' "$URL/"

# Service-token authed → lens responds.
curl -isS -H "$H_ID" -H "$H_SEC" -o /dev/null -w '%{http_code}\n' "$URL/healthz"
curl -isS -H "$H_ID" -H "$H_SEC" -H "Origin: $URL" \
  -o /dev/null -w '%{http_code}\n' "$URL/"
curl -isS -X POST -H "$H_ID" -H "$H_SEC" -H "Origin: $URL" \
  --data 'text=/help' -o /dev/null -w '%{http_code}\n' "$URL/input"
```

Expected: 302/401, 200, 200, 204 (or 503 if AgentIRC is offline).

### 5. Run the formal test

```bash
uv run pytest -m cloudflare -v tests/test_cf_roundtrip.py
```

The pytest fixture spawns its own `irc-lens serve` on `:8765`, so stop
your manual lens (see step 7) before running this and restart it
afterwards if you want the URL to stay live.

### 6. Browser path

Open `https://<your-host>` in a browser; SSO via your email allow-
policy. The chat UI loads; sending messages POSTs to `/input` (which
is what the curl smoke at step 4 covered).

### 7. Teardown

```bash
pkill -f 'cloudflared tunnel run'
pkill -f '\.venv/bin/irc-lens serve'
```

## Gotchas we hit (and what to look for)

These are the ones that produced the longest debugging sessions; flag
them early if your numbers in step 4 disagree with the expected
shapes.

### Provisioner-side gaps

A `cfafi`/`cultureflare`-style provisioner can hand you a complete-
looking env file while leaving the tunnel and the Access app under-
configured. Two pieces are easy to omit:

- **Tunnel needs a public-hostname → service mapping.** Token-based
  tunnels pull ingress from the Cloudflare dashboard, not local
  config. If the public hostname isn't bound to a service URL, the
  cloudflared log will say "No ingress rules were defined… cloudflared
  will return 503 for all incoming HTTP requests" and every public
  request comes back as 503 from cloudflared itself, never reaching
  the lens. Fix: have the provisioner add the mapping, or configure
  it manually under Zero Trust → Networks → Tunnels → your tunnel →
  Public Hostname → Service URL = `http://localhost:8765`.

- **Access app needs a `non_identity` service-token policy.** An
  email-allow policy alone admits browser SSO but rejects programmatic
  service-token auth. Symptom: with `CF-Access-Client-Id` /
  `CF-Access-Client-Secret` headers, you get 302 → SSO redirect
  anyway. Decoded `meta=` JWT in the redirect URL shows
  `service_token_status: false`. Fix: add a
  `decision: non_identity` policy (commonly named `allow-svc-token`)
  whose `include` references the service token's client_id. The
  bundled `scripts/cf-roundtrip/setup.sh` does this; some external
  provisioners don't.

### Lens-side gotchas

- **`auth.allowed_service_tokens` takes the client_id, not the token
  display name.** Cloudflare puts the service token's *client_id*
  (the `<uuid>.access` form, e.g. from
  `IRC_LENS_TEST_CLIENT_ID`) in the JWT's `common_name` claim, which
  is what the lens checks against. The token's *display name* (from
  `IRC_LENS_TEST_TOKEN_NAME`) is what you'd see in the dashboard but
  isn't what arrives in the JWT. If you hit a 403 with the body
  `service token <uuid>.access not on allowlist` after the edge
  passed, that's this. List the client_id in
  `auth.allowed_service_tokens`. (The README in
  `scripts/cf-roundtrip/` predates this clarification; trust the
  config snippet in step 3 above.)

- **AgentIRC may enforce a nick prefix.** The lens derives nicks as
  `<server.name>-<sanitized-principal>`. If your AgentIRC daemon
  rejects nicks that don't start with a specific prefix, set
  `server.name` to that prefix. Example symptom: lens log says
  `LensConnectionLost: nick rejected: Nickname must start with
  spark-`. Set `server.name: spark` in the config.

- **`POST /input` from a TLS-terminating proxy needs
  `X-Forwarded-Proto`.** The Origin CSRF floor compares
  `(host, port)` tuples and uses `request.url.scheme` to derive the
  port default. Cloudflared TLS-terminates and forwards plain HTTP, so
  without XFP-handling the request side resolves to port 80 while the
  origin side says 443 → 403 on every public POST. As of the PR that
  introduced this runbook the lens honors `X-Forwarded-Proto: https`
  and lifts the request port to 443. If you see
  `WARNING irc_lens.web.routes: origin_mismatch` lines with
  `scheme=http request_port=80` despite XFP being set upstream, your
  proxy isn't sending the header — for cloudflared, the connector
  should send it by default; check the connector version.

  This XFP path is **interim**: it trusts a header any local process
  on the host can forge. The replacement (an explicit
  `auth.allowed_origins` config) is tracked in
  [issue #39](https://github.com/agentculture/irc-lens/issues/39) and
  the long-term cryptographic-CSRF fix in #27.

### Static-asset cache busting

The HTML template emits hashed query strings on `/static/*` URLs
(`lens.css?v=<hash>`, `lens.js?v=<hash>`, etc.). Restart the lens to
update the hash; all browsers refetch on next load — no manual cache
clear required. If you edit a static file and the browser still serves
the old copy, confirm you restarted the process (the hash is computed
once at startup).

## Threat-model notes

- The lens binds `127.0.0.1` in CF mode; nothing on the public
  internet can reach `:8765` directly. Anyone on the host can.
- The XFP-honoring Origin floor (above) trusts `X-Forwarded-Proto`
  unconditionally. Loopback-resident attackers can forge it. Issue
  #39 tracks the replacement.
- Service-token credentials are highly privileged in this setup
  (they bypass identity entirely). Treat `IRC_LENS_TEST_CLIENT_SECRET`
  and `IRC_LENS_TEST_TUNNEL_TOKEN` like any other production secret —
  they're already gitignored via `.cf-roundtrip.env`'s mode 0600 and
  the repo's `.gitignore`, but don't echo them to logs or share env
  files between deployments.

## Cross-references

- [`scripts/cf-roundtrip/README.md`](../scripts/cf-roundtrip/README.md)
  — exact provisioning script and env-file format.
- [`tests/test_cf_roundtrip.py`](../tests/test_cf_roundtrip.py) — the
  formal `@pytest.mark.cloudflare` round-trip test; canonical config
  shape lives in its `lens_subprocess` fixture.
- [`docs/deployment-cloudflare-access.md`](deployment-cloudflare-access.md)
  — manual deployment guide; this runbook is the live-test cousin.
- [`docs/security-checklist.md`](security-checklist.md) — walk this
  before exposing a hostname to the public internet.
