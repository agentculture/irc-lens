# Guest mode configuration

The optional `guest_mode:` section of the lens config switches on the public
guest sandbox for chat.culture.dev. **It is off by default**: with the section
absent or `enabled: false`, guest routes return 404 and authentication behaves
exactly as before — approved emails pass, anyone else gets 403.

```yaml
guest_mode:
  enabled: false            # the switch, and the kill switch during abuse
  sandbox:                  # the isolated, unlinked IRCd guests connect to
    name: sbx
    host: 127.0.0.1
    port: 6668
    room_prefix: "#g-"      # private room per guest; must match sbx-ask
    flag_log: ~/.culture/sandbox/flags.jsonl  # deletion purges it
  idle_close_s: 900         # sign off a guest after this long with no action
  max_guests: 1             # concurrent guests allowed (Guest view never counts)
  retention_days: 90        # erase guests inactive this many days
  store_path: ~/.local/share/irc-lens/guests.db   # default: $XDG_DATA_HOME/irc-lens/guests.db
  legal_version_url: https://culture.dev/legal/version.json
  mail:                     # sender for guest token emails
    provider: none
    from: ""
    api_key_env: IRC_LENS_MAIL_API_KEY            # env var holding the provider key
    alert_url: ""             # optional delivery-alert webhook (https; http only for 127.0.0.1/localhost)
    alert_secret_env: ""      # env var holding the alert webhook secret
  rate_limits:
    entry_per_min: 10
    messages_per_min: 20
    password_attempts_per_15min: 5
```

| Key | `LensConfig` field | Default |
| --- | --- | --- |
| `enabled` | `guest_enabled` | `false` |
| `sandbox.name` | `guest_sandbox_name` | `sbx` |
| `sandbox.host` | `guest_sandbox_host` | `127.0.0.1` |
| `sandbox.port` | `guest_sandbox_port` | `6668` |
| `sandbox.room_prefix` | `guest_room_prefix` | `#g-` |
| `sandbox.flag_log` | `guest_sandbox_flag_log` | unset (required for full deletion) |
| `idle_close_s` | `guest_idle_close_s` | `900` |
| `max_guests` | `guest_max_guests` | `1` |
| `retention_days` | `guest_retention_days` | `90` |
| `store_path` | `guest_store_path` | `$XDG_DATA_HOME/irc-lens/guests.db` |
| `legal_version_url` | `guest_legal_version_url` | `https://culture.dev/legal/version.json` |
| `mail.provider` | `guest_mail_provider` | `none` |
| `mail.from` | `guest_mail_from` | `""` |
| `mail.api_key_env` | `guest_mail_api_key_env` | `IRC_LENS_MAIL_API_KEY` |
| `mail.alert_url` | `guest_mail_alert_url` | unset (alerts off) |
| `mail.alert_secret_env` | `guest_mail_alert_secret_env` | unset |
| `rate_limits.entry_per_min` | `guest_rate_entry_per_min` | `10` |
| `rate_limits.messages_per_min` | `guest_rate_messages_per_min` | `20` |
| `rate_limits.password_attempts_per_15min` | `guest_rate_password_attempts_per_15min` | `5` |

Validation: every sub-section must be a mapping; unknown keys anywhere in the
section are rejected (typo guard); `enabled` must be a boolean, ports go
through the shared port check, `legal_version_url` must be an `http(s)` URL
with a host, rate limits must be integers, and `idle_close_s` and
`retention_days` must be positive integers, as must `max_guests`.
`mail.alert_url`, when set, must be an `https` URL (plain `http` is accepted
only for `127.0.0.1` / `localhost`).

## App sign-in switch

```yaml
auth:
  app_signin:
    enabled: true   # default; false restores 0.12.2 (correct password -> /login)
```

Maps to `LensConfig.app_signin_enabled`. Unknown keys under `auth.app_signin`
and non-boolean values are rejected at load.

```yaml
auth:
  app_signin:
    base_url: https://chat.culture.dev   # base of emailed set-password links
```

`auth.app_signin.base_url` (`LensConfig.app_signin_base_url`, default unset)
must be an `https` URL (plain `http` only for `127.0.0.1` / `localhost`). It is
the base of the links mailed by the set-password flow; when unset,
`media.public_base_url` is used. Links are never built from a request's `Host`
header. If neither key is set, `POST /password` still answers with the same
"check your email" page but sends nothing and logs one error.

## App sign-in

With `auth.app_signin.enabled: true` (default) and guest mode on, an approved
user signs in inside the app, without Cloudflare Access:

1. `POST /entry/signin` (email + password) always returns the same code screen
   and sets the pending cookie `lens_signin` (`SameSite=Strict`, `Path=/entry`,
   10 minutes). Only a right password for an address in `auth.allowed_emails`
   mails a code (from a background task); the response never depends on the
   password, and is padded to a 0.5 s floor.
2. `POST /entry/code` takes the code. It must come from the same browser as
   step 1, is single use and valid for 10 minutes. Success creates a fresh
   session id, clears `lens_signin`, sets `lens_session` (`HttpOnly; Secure;
   SameSite=Lax; Path=/`) and redirects to `/`. The code screen has a
   **Trust this browser** checkbox (unchecked by default, always shown); see
   [Trusted browsers and attempt limits](#trusted-browsers-and-attempt-limits).
3. The session is server-side: the guest store keeps only `sha256(id)`. It
   expires after 7 days idle or 30 days total, and the email is re-checked
   against `allowed_emails` on every request. The resulting identity is the
   same as an Access JWT for that email.
4. `POST /logout` (approved, CSRF-checked) ends the session, closes the IRC
   session(s) it opened and clears the cookie (`303 /`, or `HX-Redirect: /`
   for htmx). Setting a new password, removing the address from
   `allowed_emails` and expiry end sessions the same way (swept on the ban
   sweeper's interval).

### Trusted browsers and attempt limits

- **Trusted browser.** When the code is entered with **Trust this browser**
  ticked, the browser also gets `lens_device` (a fresh random id;
  `HttpOnly; Secure; SameSite=Lax; Path=/`, one year). The guest store keeps
  only `sha256(id)` with the email (table `trusted_devices`). That browser is
  exempt from every sign-in attempt limit, for that email only (one browser
  can be trusted for several emails). Unticked, sign-in works the same but
  the browser stays untrusted; an already trusted browser that signs in
  unticked keeps its trust. Logout keeps `lens_device`; setting or resetting
  the password (web link or `irc-lens guests passwd`) revokes every trusted
  browser of that email. The sweep drops trust rows after 365 days.
- **Untrusted browsers** share one budget per email that counts password
  submissions (`POST /entry/signin`) and code entries (`POST /entry/code`)
  together: 3 per 15 minutes. Once it is exhausted the email is strict, 2 per
  30 minutes, until 24 hours pass with no blocked attempt (table
  `signin_budget`). Untrusted browsers are also limited per IP to 3
  attempts per 15 minutes, password submissions and code entries counted
  together (fixed; `rate_limits.password_attempts_per_15min` stays the guest
  and rollback-path limit).
- **A blocked attempt looks exactly like an unblocked one.** A blocked
  password step gets the same code screen (same status, body and floor) and
  no code is mailed; a blocked code entry gets the one `Wrong or expired
  code` error without checking or using up the code.

Approved users are still exactly the `allowed_emails` list: there is no
sign-up and no account creation. The Cloudflare Access `/login` path keeps
working as break-glass.

**Rollback switch.** `auth.app_signin.enabled: false` restores 0.12.2: a
correct password answers `303 /login` (wrong ones `Email or password is
wrong`), `/entry/code` and `POST /logout` do not exist, `lens_session` is
not read, and the set-password routes are not mounted.

## Delivery alerts

When the Resend adapter fails to send (any HTTP error, including 429 quota
exhaustion, or a network error) and `guest_mode.mail.alert_url` is set, the
lens posts `{"kind": "quota" | "send_failed", "message": ...}` as JSON to that
URL with `Authorization: Bearer <secret>`, the secret read from the env var
named by `guest_mode.mail.alert_secret_env`. The receiver is a small
Cloudflare email Worker that mails the approved users; the message says
sign-in and guest codes are not being delivered and that approved users can
still sign in at `/login`. At most one alert per kind per hour; the post runs
on a daemon thread and any failure is logged and swallowed. The payload never
contains a recipient address, a code or the provider's response. With
`alert_url` unset (default) alerts are off. Each alert increments
`delivery_alerts`.

## Set or reset password

With guest mode and app sign-in on, `src/irc_lens/web/setpw.py`
(`templates/setpw.html.j2`) adds, all anonymous-allowed:

| Route | Step |
| --- | --- |
| `GET /password` | Email form (linked from the entry card's password step as "Set or reset password") |
| `POST /password` | Always the same "If this address can sign in here, we've emailed a link…" page. Only an address in `auth.allowed_emails` gets a single-use link `<base_url>/password/<token>` (token purpose `setpw`, 30 minutes); the token is issued and mailed in a background task so timing reveals nothing. Limited per email and per IP by `rate_limits.entry_per_min` (429, same page) |
| `GET /password/<token>` | New-password form for a live token; never consumes it (mail scanners prefetch links). Otherwise one neutral "This link has expired or was already used" page (400) linking back to `/password` |
| `POST /password/<token>` | CSRF/Origin-checked like every POST; limited per IP by `rate_limits.password_attempts_per_15min`. Passwords under 12 characters (or over 1024) or a mismatched confirmation re-show the form and leave the token valid; otherwise the token is consumed, an argon2id hash stored, and every app session of that email ended (their live IRC sessions closed) |

The 12-character minimum applies only when a password is set here; an older,
shorter password still signs in until it is replaced through this flow. Store
helpers: `GuestStore.peek_token(token, purpose=...)` (non-consuming) and
`GuestStore.consume_token(token, purpose=...)` (single use), both keyed by the
token alone. Tokens, links and passwords are never logged: a filter on the
loggers that write request paths (`aiohttp.access`, auth, CSRF, routes)
replaces `/password/<token>` with `/password/[redacted]`.

## Owner metrics counters

`/owner/metrics` also reports `signin_codes_sent` (app sign-in codes mailed),
`sessions_started` / `sessions_ended` (app sessions), `guest_busy` (busy
pages shown) and `delivery_alerts` (alerts posted). Counts only: no code,
session id, token or password is ever logged (`tests/test_log_hygiene.py`
runs the sign-in, set-password, guest and deletion flows and checks every log
record).

## Guest session cookie and CSRF

Guests are identified by a signed cookie, `lens_guest`
(`HttpOnly; Secure; SameSite=Strict`, 1 hour expiry). Value:
`<b64url(json {"g": guest_id, "exp": unix_ts})>.<b64url(HMAC-SHA256)>`.
Code: `src/irc_lens/web/csrf.py` (`issue_guest_cookie(response, guest_id)`,
`read_guest_cookie(request) -> guest_id | None`).

The HMAC secret is supplied via the **`IRC_LENS_GUEST_COOKIE_SECRET`**
environment variable (for example a systemd `EnvironmentFile` mode 0600; use
at least 32 random bytes, e.g. `openssl rand -hex 32`). It is never read from
the config file or hard-coded. If unset, a random per-process secret is used
and all guest cookies are invalidated on restart. Rotate by changing the value.

Every non-GET/HEAD/OPTIONS request (`/input`, `/upload`, and future consent
and deletion routes) passes `csrf_middleware` first: a mismatching `Origin`
is 403 (the existing same-host floor); a request carrying a guest cookie must
additionally prove same-origin (matching `Origin`, or `Sec-Fetch-Site:
same-origin`/`none`), otherwise 403 before any handler or IRC send.

## Entry card

With guest mode on, anonymous visitors get the entry card
(`src/irc_lens/web/entry.py`, `templates/entry.html.j2`, `static/entry.css`):

| Route | Step |
| --- | --- |
| `GET /entry` | Email + Continue (`get_entry`, also served on `/` for anonymous visitors) |
| `POST /entry/email` | The password window — identical for every address |
| `POST /entry/signin` | Password step. With app sign-in on (default): always the same code screen, whatever the email or password (see [App sign-in](#app-sign-in)). With `auth.app_signin.enabled: false` (0.12.2 behavior): approved email + correct password → 303 `/login`; otherwise `Email or password is wrong` |
| `POST /entry/code` | App sign-in code step: the emailed code plus the `lens_signin` cookie → `lens_session` cookie, 303 `/`; any failure is the one error `Wrong or expired code`. 404 with the switch off |
| `POST /entry/guest` | Nickname (`sbx-` prefix) + Terms/Privacy consent + optional training opt-in |
| `POST /entry/guest/start` | Emails a single-use, 15-minute code (one fixed template; the mail says "code", matching the Code field) |
| `GET /login` | Post-SSO return target (Cloudflare Access forwards the user here): always 303 `/`, `Cache-Control: no-store`, any tier, never 404 and never reveals approval; served even with guest mode off |
| `POST /entry/verify` | Code check → guest + consent recorded, `lens_guest` cookie, 303 `/` |

The `/entry*` routes answer 404 while `guest_mode.enabled` is false. Sign-in failures are
indistinguishable: an unknown email still pays one (dummy) argon2id verify, and
every sign-in response is padded to a fixed floor (0.5 s). App sign-in is
limited as described in
[Trusted browsers and attempt limits](#trusted-browsers-and-attempt-limits);
with the rollback switch off, sign-in and guest token verification are
limited per email and per IP by
`rate_limits.password_attempts_per_15min`; token requests by
`rate_limits.entry_per_min` — over the limit the same body returns as 429. The
visitor IP is `CF-Connecting-IP` (cloudflared) when present.

Guest nicks are `sbx-<nickname>`: lowercased, reduced to `[a-z0-9_-]`, 2–16
characters, unique among recorded guests, never derived from the email, and
`ask` is reserved for the sandbox agent.

Consent is recorded against `irc_lens.legal.current_legal_versions(cfg)` (the
`legal_version_url` JSON, cached in-process for 5 minutes; a failed fetch with
nothing cached blocks entry rather than recording an unknown version).

Training use is a separate consent. The guest step carries a second checkbox,
"Use my conversations to improve culture.dev's models (optional)", unchecked
by default and not required; signing up without it works. The choice travels
through the code step as a hidden field and is stored as `consents.train`
(0/1, default 0) with the consent row. Only guests whose most recent consent
row has `train = 1` appear in `irc-lens guests export`. Withdrawal arrives by
email; the owner applies it with `GuestStore.set_training(email, False)`,
which updates the guest's latest consent row. Existing stores gain the column
on startup (`ALTER TABLE ... ADD COLUMN`, guarded by `PRAGMA table_info`).

### Bot protection (Cloudflare Turnstile)

Set both **`IRC_LENS_TURNSTILE_SITE_KEY`** and **`IRC_LENS_TURNSTILE_SECRET`**
in the environment to put a Turnstile widget on the password and guest steps
(verified server-side on sign-in and token request; a failed check is the
generic error). Unset, the check is a no-op. Only responses that render the
widget get a CSP widened to `https://challenges.cloudflare.com`
(`script-src`, `frame-src`, `connect-src`); every other page keeps
`script-src 'self'`. Entry pages send `Referrer-Policy: same-origin` (not
`no-referrer`) so browsers put the real `Origin` on their same-origin form
POSTs instead of `null`, which the CSRF floor would refuse.

## Private guest rooms

Each guest session joins its own room, `<room_prefix><id>`, where `<id>` is a
random id stored with the guest (not derived from the nickname). Guests never
share a room, so they never see each other's messages or history. The sandbox
agent follows each guest into its room (`guest_room_prefix` in the agent
config, same default `#g-`) and leaves rooms that have emptied. The approved
user's Guest view joins its own room (`#g-op-<name>`) plus every current
guest's room, listed from the guest store, and can `/switch` between them.
Sandbox views hide `system-*` join and welcome lines; the real mesh view is
unchanged.

Approved users' sandbox nicks are `sbx-op-<name>` and guest nicknames may only
use `[a-z0-9_]`, so a guest can never take an approved user's nick or room.

A guest is signed off after `idle_close_s` with no message or command sent,
even with the tab still open (page loads, event streams and presence polls are
not actions). An approved user's Guest view is closed after `idle_close_s`
with no open browser tab.

### Guest limit

At most `max_guests` guests are active at once: a guest with an open sandbox
session, or one whose code was just verified (the slot is held for 120 s until
the browser opens the chat). While the sandbox is full, a visitor who picks
Guest mode sees `The sandbox is busy. Try again in a few minutes.` and no code
is emailed (the `guest_busy` counter counts these pages). Code verification
re-checks the limit under a lock, so two guests verifying at once cannot both
get in; the refused one sees the busy page and their code is not used up. A
slot frees when the guest deletes their data or is signed off for idleness.
A signed-off guest's cookie still works: they rejoin their room if a slot is
free, otherwise they see the busy page; no new code is sent. An approved
user's Guest view never counts toward the limit and is never refused.

### Guest chat records and deletion

The sandbox IRCd runs memory-only (`culture server start --no-persist`): it
writes no channel history to disk. The guest store is the one durable record
of guest chat: each guest's messages (`kind="message"`) and the answers
sbx-ask posts in their room (`kind="answer"`).

A confirmed deletion keeps nothing of the guest's conversation: it erases the
guest's profile, questions and answers, consents, tokens, flags, room id and
rate-limit counters from the guest store, their uploads, and their lines in
sbx-ask's flag log (`sandbox.flag_log`). Without `flag_log` the lens logs a
warning at startup. Bans are kept, so a banned guest cannot lift a ban by
deleting. The `deletions` log records only the SHA-256 hex digest of the
lower-cased email (plus time and row count), never the address; records
written before this change are hashed on startup. There is no anonymized
corpus any more: an older store's `corpus` table is dropped on startup.

### Retention

While guest mode is on, the lens sweeps the guest store at startup and then
hourly (`GuestStore.sweep`, run in a worker thread; the task is cancelled on
shutdown):

- guests whose last activity (the latest of their entry, their inputs and
  their consents) is older than `retention_days` (default 90) are erased by
  the same path as a self-service deletion — store rows, uploads and
  flag-log lines — and logged by hash;
- tokens issued more than a day ago and rate-limit attempts older than a day
  are removed;
- bans older than 365 days are lifted.

The agent state badge (`agent online` / `agent offline`) reflects whether the
agent is in the session's room right now, read from the live member list. Each
15-second badge poll re-reads the room with `WHO`: AgentIRC sends no `QUIT`
for a client holding the `agentirc.io/bot` capability (as `sbx-ask` does) or
for an abrupt disconnect, so a stopped agent shows `agent offline` on the next
poll rather than never.
