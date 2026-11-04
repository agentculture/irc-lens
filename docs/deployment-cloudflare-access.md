# Deploying irc-lens behind Cloudflare Access on your own domain

This runbook walks you through hosting irc-lens at a public hostname
on your own Cloudflare-managed zone, gated by Cloudflare Access. The
result: anyone you allowlist can sign in via your IdP and reach a
private AgentIRC mesh; nobody else can.

## Prerequisites

- A Cloudflare account with `<your-zone>` (e.g. `example.com`) managed.
- An IdP set up in Cloudflare Access (Google SSO, GitHub, etc.).
  Cloudflare's dashboard has a wizard.
- `cloudflared` installed on the host machine. See Cloudflare's docs
  for current install instructions.
- `irc-lens` and AgentIRC running (or about to run) on the same host.

## 1. Pick a hostname

Decide on `<your-hostname>` under `<your-zone>` (e.g.
`lens.<your-zone>`). The runbook uses these placeholders below.

## 2. Create the tunnel

```bash
cloudflared tunnel login                   # one-time, browser-based
cloudflared tunnel create irc-lens
```

This writes a credentials JSON to `~/.cloudflared/<tunnel-uuid>.json`.

## 3. Route DNS

```bash
cloudflared tunnel route dns irc-lens <your-hostname>
```

Cloudflare creates a CNAME `<your-hostname>` →
`<tunnel-uuid>.cfargotunnel.com`.

## 4. Configure cloudflared

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-uuid>
credentials-file: /home/<user>/.cloudflared/<tunnel-uuid>.json

ingress:
  - hostname: <your-hostname>
    service: http://localhost:8765
  - service: http_status:404
```

## 5. Run cloudflared as a service (systemd example)

```bash
sudo cloudflared service install
sudo systemctl start cloudflared
sudo systemctl enable cloudflared
```

## 6. Create the Access application

In the Cloudflare dashboard → Zero Trust → Access → Applications:

- **Application type**: Self-hosted.
- **Application domain**: `<your-hostname>`.
- **Identity providers**: select your configured IdP.
- **Add policy**: Allow + `Emails` → list the same emails you'll put in
  `auth.allowed_emails`.

Note the **Application Audience (AUD) Tag** — you need this for the
lens config.

## 7. Configure irc-lens

Run `irc-lens config init` to drop a starter file
(`~/.config/irc-lens/config.yaml`; respects `$XDG_CONFIG_HOME`). If
the file already exists, pass `--force` to overwrite it, or use
`--path <custom-path>` to write to a different location.

Then edit the file and replace the dev-mode stanza with:

```yaml
auth:
  mode: cloudflare-access
  cloudflare:
    aud: <your-aud-tag>
    team_domain: <your-team>.cloudflareaccess.com
  allowed_emails:
    - <your-email>
  # Add these when a routing Worker fronts the origin. Enforcement remains
  # off until the Worker is live; the YAML names an env var, never its value.
  public_origin: https://<your-hostname>
  require_edge_credential: false
  edge_credential_env: IRC_LENS_EDGE_TOKEN
server:
  name: <agentirc-server-name>
  host: 127.0.0.1
  port: 6667
web:
  bind: 127.0.0.1
  port: 8765
```

## 8. Start in this order

1. AgentIRC (so it's ready when the lens connects).
2. `irc-lens serve` (validates JWKS on startup, binds `127.0.0.1:8765`).
3. `cloudflared` (already running as a systemd unit after step 5).

Reverse on shutdown.

## Worker-to-origin trust and staged rollout

When a Cloudflare Worker routes the public hostname to a separate tunnel
hostname, browser POSTs retain the public `Origin`. Set `auth.public_origin` to
that exact browser-visible origin so the CSRF check accepts it; an Origin other
than the configured one is still refused. Without this setting, the existing
request-host comparison remains unchanged.

The Worker must overwrite both of these request headers before forwarding:

- `X-Lens-Edge-Token`: the shared Worker credential.
- `X-Lens-Edge-Client-IP`: the visitor address from `CF-Connecting-IP`.

The lens trusts `X-Lens-Edge-Client-IP` only when the token verifies. A direct
client cannot forge the rate-limit address by sending the IP header alone. The
credential is compared in constant time and is never logged.

Keep the secret out of the YAML. The value of `auth.edge_credential_env` is the
name of the lens process environment variable that holds it. For example, set a
strong random value as the Worker's `EDGE_TOKEN` secret and set the same value
in `IRC_LENS_EDGE_TOKEN` for the lens service. If the named environment
variable is absent or empty, verification fails closed.

Roll this out without creating an origin outage:

1. Deploy the lens config above with `require_edge_credential: false`.
2. Deploy the Worker with the matching secret and confirm GET, browser POST,
   event streaming, and the visitor IP on each origin.
3. Change `require_edge_credential` to `true` on each origin and restart it.
4. Confirm a Worker request succeeds and a direct request without
   `X-Lens-Edge-Token` gets HTTP 403, including `/healthz` and `/readyz`.

The switch defaults to `false`, `public_origin` defaults to unset, and the
credential environment-variable name defaults to unset. Therefore an existing
deployment with none of these keys keeps its previous behavior. For emergency
rollback, set `require_edge_credential: false` and restart the lens; do not
remove or expose the Worker secret.

## 9. Verify

- Visit `https://<your-hostname>/healthz` → `{"ok": true}` (no auth
  required at the lens; checks the HTTP process only).

  > **Note on `/healthz`**: The lens itself bypasses auth on `/healthz`
  > (so cloudflared-local probes work). Cloudflare Access at the edge,
  > however, gates **all** paths under the protected hostname by default —
  > so an unauthenticated browser hitting `https://<your-hostname>/healthz`
  > from the public internet will be redirected to SSO. If you want
  > unauthenticated uptime probes to reach the lens, add a Bypass policy
  > in the Access app (Cloudflare dashboard → Access → Applications →
  > your app → Policies → Add policy → Action: **Bypass** → Include:
  > Everyone → Path: `/healthz`). With the bypass policy in place,
  > external probes get the lens's `{"ok": true}` directly.

- Visit `https://<your-hostname>/` → SSO redirect → lens UI.
- Send a slash-command — it dispatches into AgentIRC under the
  derived nick `<agentirc-server-name>-<sanitized-email-local>`.

## Readiness monitoring

Use `GET /readyz` for external monitors: `/healthz` does not check the IRCd.
Readiness connects to the configured main `server.host` / `server.port`,
sends IRC `PING :readyz`, and requires a `PONG` within a two-second timeout
(not the guest sandbox). It returns only `{"ok": true}` with 200 on success,
or `{"ok": false}` with 503 on failure. Failures are logged server-side only
on state change, with an info log on recovery. A monitor’s own timeout must
exceed two seconds, or it may time out instead of receiving a 503.

No identity or JWT is required by the lens. Configure an Access edge bypass
for `/readyz` as described for `/healthz` above so external monitors can reach
it without SSO. Responses carry `Cache-Control: no-store`; the lens shares
one result in memory for five seconds, including across concurrent requests,
so changes may take up to about seven seconds to appear (the two-second
probe timeout plus five-second sharing).

## Troubleshooting

**401 at `/`** — JWT not reaching the lens.
Check cloudflared logs; `cloudflared tunnel info irc-lens` should
show a healthy edge connection.

**403 not on allowlist** — email passed CF Access but is missing from
`auth.allowed_emails`. Add it to the config and restart irc-lens.

**502 on first request** — lens couldn't reach the Cloudflare JWKS
endpoint (`/cdn-cgi/access/certs`) at startup. Verify outbound egress
from the host to `<your-team>.cloudflareaccess.com`.

**503 on `/input`** — AgentIRC is unreachable. Check `server.host` /
`server.port` and the IRCd's status.

## Security checklist

Walk [security-checklist.md](security-checklist.md) before opening
the hostname to the internet.

## Standby replica

Enable standby on spark2 with:

```yaml
standby:
  enabled: true
```

It defaults to `false`. Restart the lens after changing it. Use a copy of the
existing access/guest database with approved users, chosen nicknames, grants and
passwords already present; standby does not import administrators. Keep the
existing sign-in configuration (`guest_mode.enabled` and app sign-in) when using
email/password sign-in: that switch also installs the approved sign-in machinery.
Standby overrides guest access, including previously issued guest cookies and the
approved user's sandbox view. Invited users cannot become approved during standby.
Approved users keep their usual `<server.name>-<chosen nickname>` on IRC; configure
`server.name: spark2` for this replica. A user without a chosen nickname must wait
until account changes are available again.

The single standby middleware returns HTTP 503 with a clear outage message for
account changes. The inventory traced from `access_store.py` and `guest_store.py`
is all of these POST routes:

| Routes | Account data affected |
| --- | --- |
| `/admin/users`, `/admin/users/remove` | Users, roles, approval, credential revocation |
| `/admin/users/nickname`, `/nickname` | Nicknames and reservations |
| `/admin/grants` | Channel grants |
| `/admin/extension` | Agent extensions |
| `/agent-credential` | Agent credentials and guest agent consent |
| `/entry/welcome`, `/entry/guest/start`, `/entry/verify` | Guest tokens, accounts and consents |
| `/password`, `/password/{token}` | Password reset tokens, passwords, trusted-device removal |
| `/delete/request`, `/delete/confirm` | Deletion tokens and guest data deletion |

`/entry/signin` and `/entry/code` are conditional: only already-approved emails
may proceed. Invitation promotion and guest creation through these routes are
blocked. Approved sign-in codes, attempts/rate limits, trusted-browser records,
and session creation, touch and logout remain local writes on spark2. They are
not replicated account changes. The automatic guest retention sweep is suspended.
CLI administration is outside this HTTP gate; do not run account-changing CLI
commands against the standby copy.

All other unsafe requests are denied by default except the reviewed POST paths
`/entry/email`, `/entry/signin`, `/entry/code`, `/input`, `/logout` and
`/guest/logout`. This also blocks uploads, the old Guest mode button and sandbox
switches. Cloudflare service tokens cannot act as approved humans. Agent credential pages and the configured MCP endpoint are unavailable.
The route-inventory test must be updated when adding a POST route; a new unsafe
route cannot silently pass the gate.

HTML pages display “Standby mode: agents and spark media are unavailable.”
`/healthz`, `/readyz`, `/static/` and existing capability URLs under `/media/`
continue through their usual handlers. Readiness still reflects actual IRC
reachability; the banner does not imply that locally stored media is deleted.
