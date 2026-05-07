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

## 9. Verify

- Visit `https://<your-hostname>/healthz` → `{"ok": true}` (no auth
  required; safe for uptime probes).

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
