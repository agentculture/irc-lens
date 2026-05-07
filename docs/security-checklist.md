# Security Checklist — public-facing irc-lens

Before opening your hostname to the internet, walk this list. Every
item is a yes/no.

## Network exposure

- [ ] AgentIRC is bound to `127.0.0.1` (or a private mesh interface),
      never `0.0.0.0`.
- [ ] `irc-lens serve`'s `web.bind` is `127.0.0.1`. CF mode coerces
      this automatically; verify with `ss -lntp | grep 8765`.
- [ ] No firewall rule forwards an inbound port to either service.
      cloudflared is the only reachable surface.

## Cloudflare config

- [ ] `auth.mode: cloudflare-access` in the active config file.
- [ ] `auth.cloudflare.aud` matches the AUD shown in the Access app's
      "Application Audience (AUD) Tag" field.
- [ ] `auth.cloudflare.team_domain` matches your Cloudflare team
      domain (`<team>.cloudflareaccess.com`).
- [ ] `auth.allowed_emails` matches the Access policy's email list.
      Both must say yes for a request to land — defense in depth.
- [ ] The IdP (Google SSO, GitHub, etc.) enforces MFA on every
      account in `auth.allowed_emails`.

## cloudflared

- [ ] cloudflared's tunnel `ingress` rule points to
      `http://localhost:<web.port>`, not `http://0.0.0.0:<web.port>`.
- [ ] cloudflared and irc-lens both run as a non-root user.
- [ ] cloudflared's `tunnel-credentials` file is `chmod 600`.

## Operational

- [ ] Logs are persisted somewhere auditable (systemd journal is
      fine). Review `auth=denied` lines after each test session.
- [ ] `GET /healthz` returns `{"ok": true}` and nothing else; the
      response does not reveal whether any user has a live session.
- [ ] You have a way to rotate the IdP credentials and the CF API
      token without downtime.

## Things to revisit later

These ship in v1 with a known floor; tighten as the deployment grows:

- CSRF defense on `POST /input` is currently Origin/Referer only. See
  [issue #27](https://github.com/agentculture/irc-lens/issues/27)
  for the CSRF-token uplift.
- The CF round-trip test runs only manually / via agent. Automation
  on GitHub Actions is tracked in
  [issue #28](https://github.com/agentculture/irc-lens/issues/28).
- Group-based authorization (using the `groups` JWT claim) is not
  consumed in v1.
