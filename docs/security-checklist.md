# Security Checklist — public-facing irc-lens

Before opening your hostname to the internet, walk this list. Every
item is a yes/no.

## Network exposure

- [ ] AgentIRC is bound to `127.0.0.1` (or a private mesh interface),
      never `0.0.0.0`.
- [ ] `irc-lens serve`'s `web.bind` is `127.0.0.1`. CF mode coerces
      this automatically; verify with `ss -lntp | grep <web.port>`
      (the port you set in the lens config; default 8765).
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

## Media

- [ ] Capability-URL model: `GET /media/{token}` is auth-exempt by design.
      Unguessable 128-bit tokens (via `secrets.token_urlsafe(16)`) are
      the capability — possession of the URL means you saw it in the
      channel (IRC trust model). Uploads (`POST /upload`) remain
      authenticated; CF Access still gates the tunnel in front.
- [ ] CSP headers: set on HTML documents:
      `default-src 'self'; script-src 'self'; img-src 'self' https: http:; media-src 'self' https: http:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`.
      The load-bearing directives are `script-src` / `object-src`;
      `img-src` / `media-src` stay broad because mesh peers advertise
      plain-HTTP LAN URLs and click-to-load is the actual gate.
- [ ] Security headers on all responses: `X-Content-Type-Options: nosniff`
      and `Referrer-Policy: no-referrer`.
- [ ] Media store size caps are enforced: `max_file_bytes` (default
      10 MiB) per upload, `max_store_bytes` (default 256 MiB) for
      eviction; oldest files by mtime are evicted past the store cap.
- [ ] Upload validation: magic-byte sniff + extension-allowlist
      agreement (PNG, JPG, GIF, WebP for images; MP3, OGG, WAV, WebM,
      M4A, FLAC for audio). SVG is deliberately excluded (stored-XSS
      vector on auth-exempt origin). Bad type → 400 error.
- [ ] `media.public_base_url` must be set to an `http(s)` URL for
      cross-machine viewers to fetch blobs. Leave empty for same-host
      only (defaults to `http://<web.bind>:<web.port>`).

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
