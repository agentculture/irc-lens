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
- [ ] CSP headers on HTML documents. Load-bearing directives are
      `script-src: 'self'` and `object-src: 'none'`. Broad `img-src` /
      `media-src` (https + http) because mesh peers advertise plain-HTTP
      LAN URLs and click-to-load is the gate. See
      [`docs/architecture.md`](architecture.md) for full CSP string.
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

## Public guest sign-in

- [ ] `guest_mode.mail.total_daily_ceiling` is below the provider's daily
      allowance and leaves headroom for operational mail.
- [ ] Guest `daily_budget`, `hourly_budget`, and `ip_hourly_budget` match the
      traffic you are prepared to fund. One IP can never consume the final
      hourly guest slot; approved sign-in codes do not consume this guest
      budget.
- [ ] The alert URL and secret are configured and a test provider outage
      reaches the operators. Recipient-specific HTTP 400/404/422 failures
      must log and count, but must not alert.
- [ ] Both `IRC_LENS_TURNSTILE_SITE_KEY` and
      `IRC_LENS_TURNSTILE_SECRET` are injected into the service. A real guest
      sign-in works through each public ingress before
      `guest_mode.require_bot_check` is changed to `true`.
- [ ] `irc-lens doctor` has no "guest mode is on with no bot check" warning.
      If the check is intentionally off, the warning is recorded in the
      deployment's accepted-risk notes.
- [ ] Reserved documentation domains (`example.com`, `example.net`,
      `example.org`, and the reserved TLDs) never reach the mail provider.
- [ ] The guest store and its parent directory are writable only by the
      irc-lens service account. Operators use the supported metrics and CLI
      surfaces rather than querying `guests.db` during incidents.

## Guest-mode observability

- [ ] An approved operator (an admin when email-keyed permissions are enabled)
      can open `GET /owner/metrics`; an anonymous or guest request is denied.
- [ ] `/owner/metrics.bot_check` agrees with the deployed Turnstile keys and
      `guest_mode.require_bot_check`. `off` and `missing` include the startup
      warning text; investigate either state on a public console.
- [ ] `/owner/metrics.threat` shows the current 15-minute fixed-name counts
      and source cardinality only. Confirm it contains no email, address,
      path, user agent, or source hash.
- [ ] Service logs are persisted and searchable for `no bot check`,
      `mail send failed`, and `fixed threat rule`. Every failed provider call
      includes its HTTP status or `network error`, but no recipient or code.
- [ ] During a budget test, `budget_hits` rises, provider attempts stay at or
      below the configured budget, reserved-address attempts remain zero,
      and an approved user's correct-password code still reaches the mail
      adapter when both guest budgets are zero.

## Operational

- [ ] Logs are persisted somewhere auditable (systemd journal is
      fine). Review `auth=denied` and the guest-mode status lines above after
      each test session.
- [ ] `GET /healthz` returns `{"ok": true}` and nothing else; the
      response does not reveal whether any user has a live session.
- [ ] Public `GET /readyz` returns only `{"ok": true}` (200) or
      `{"ok": false}` (503), probing the main IRCd with a two-second
      timeout and a shared five-second cache; responses use `Cache-Control: no-store`.
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
