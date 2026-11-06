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
  max_pending_per_address: 1 # unopened seat reservations per client address
  require_bot_check: false  # set true only after both Turnstile keys are live
  retention_days: 90        # erase guests inactive this many days
  store_path: ~/.local/share/irc-lens/guests.db   # default: $XDG_DATA_HOME/irc-lens/guests.db
  legal_version_url: https://culture.dev/legal/version.json
  mail:                     # sender for guest token emails
    provider: none
    from: "CULTURE.DEV <no-reply@culture.dev>"
    api_key_env: IRC_LENS_MAIL_API_KEY            # env var holding the provider key
    daily_budget: 50          # guest code mail across all visitors per 24 hours
    hourly_budget: 20         # guest code mail across all visitors per hour
    ip_hourly_budget: 5       # guest code mail from one IP per hour
    total_daily_ceiling: 90   # all mail, every path, per rolling day
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
| `max_pending_per_address` | `guest_max_pending_per_address` | `1` |
| `require_bot_check` | `guest_require_bot_check` | `false` |
| `retention_days` | `guest_retention_days` | `90` |
| `store_path` | `guest_store_path` | `$XDG_DATA_HOME/irc-lens/guests.db` |
| `legal_version_url` | `guest_legal_version_url` | `https://culture.dev/legal/version.json` |
| `mail.provider` | `guest_mail_provider` | `none` |
| `mail.from` | `guest_mail_from` | `""` |
| `mail.api_key_env` | `guest_mail_api_key_env` | `IRC_LENS_MAIL_API_KEY` |
| `mail.daily_budget` | `guest_mail_daily_budget` | `50` |
| `mail.hourly_budget` | `guest_mail_hourly_budget` | `20` |
| `mail.ip_hourly_budget` | `guest_mail_ip_hourly_budget` | `5` |
| `mail.total_daily_ceiling` | `guest_mail_total_daily_ceiling` | `90` |
| `mail.alert_url` | `guest_mail_alert_url` | unset (alerts off) |
| `mail.alert_secret_env` | `guest_mail_alert_secret_env` | unset |
| `rate_limits.entry_per_min` | `guest_rate_entry_per_min` | `10` |
| `rate_limits.messages_per_min` | `guest_rate_messages_per_min` | `20` |
| `rate_limits.password_attempts_per_15min` | `guest_rate_password_attempts_per_15min` | `5` |

Validation: every sub-section must be a mapping; unknown keys anywhere in the
section are rejected (typo guard); `enabled` must be a boolean, ports go
through the shared port check, `legal_version_url` must be an `http(s)` URL
with a host, mail budgets must be non-negative integers, rate limits must be
integers, and `idle_close_s` and
`retention_days`, `max_guests`, and `max_pending_per_address` must be positive
integers.
`mail.alert_url`, when set, must be an `https` URL (plain `http` is accepted
only for `127.0.0.1` / `localhost`).

## Bot check: enable, verify, then require

Guest mode starts without a bot check unless both
`IRC_LENS_TURNSTILE_SITE_KEY` and `IRC_LENS_TURNSTILE_SECRET` are present in
the service environment. With either key missing, startup logs this warning:

```text
guest mode is on with no bot check: set IRC_LENS_TURNSTILE_SITE_KEY and IRC_LENS_TURNSTILE_SECRET to turn on Turnstile (docs/guest-mode-config.md)
```

Leave `require_bot_check: false` while first deploying the keys. Verify a real
guest sign-in through every public ingress, run `irc-lens doctor`, then set the
switch to `true` and restart. If the switch is true but the keys are absent,
guest mail is refused; approved sign-in remains available. For rollback, set
the switch back to false and restart. Removing the keys also disables the
widget, but it is not the preferred rollback because it makes the deployment
state less explicit.

## Operator view and logs

An approved operator can open `GET /owner/metrics` (an admin when email-keyed
permissions are enabled); anonymous and guest users cannot. The existing
`counters`, `access`, and `agent` objects are unchanged.
Two additional objects expose the hardening state without reading
`guests.db`:

- `bot_check` has `configured`, `required`, `status`, and `warning`. Status is
  `disabled`, `off`, `optional`, `required`, or `missing` (required but not
  configured). When guest mode is enabled with no check configured, `warning`
  is the same text emitted at startup.
- `threat` is the current 15-minute window. It contains only fixed-name
  integer `counts`, `distinct_sources`, `source_count_capped`, and
  `window_seconds`. It contains no address, path, user agent, email, or source
  hash. The counter window resets in memory; this is operational telemetry,
  not an audit log.

The threat count names are `requests`, `probes`, `refused_responses`,
`entry_posts`, `mail_sends`, `reserved_addresses`, `failed_sends`,
`budget_hits`, `messages_blocked`, `messages_flagged`, and
`messages_unchecked`. `budget_hits` means a guest request reached a mail
budget with no slot left; `mail_sends` means a guest slot was reserved before
the background adapter call, not that the provider delivered it.

Process counters such as `delivery_alerts` and `failed_sends` reset on
restart. Guest budget attempts remain persistent in the configured guest
store. A provider failure logs `mail send failed: HTTP <status>` (or
`network error`) without logging the recipient, code, provider response, or
remediation. Recipient-specific 400/404/422 failures increment
`failed_sends` but do not send delivery alerts. Review service logs with the
deployment's normal journal tooling, for example:

```sh
journalctl -u irc-lens --since today | grep -E 'no bot check|mail send failed|fixed threat rule'
```

Opening the database is neither required nor recommended during incident
triage: use the startup/doctor warning, service logs, and `/owner/metrics`.

## Advisory threat verdicts

Fixed threat rules work without Clef and are unchanged when this section is
absent. To add a counts-only advisory verdict to rule alerts, configure the
gateway URL and name the environment variable holding its credential:

```yaml
threat_rules:
  decider_url: http://127.0.0.1:8001/v1/systemone
  decider_credential_env: CLEF_GATEWAY_KEY
```

Both keys are required together. Plain HTTP is accepted only for a loopback
gateway. Clef receives aggregate 15-minute counts, never addresses, paths,
user agents, or other visitor strings. It never blocks traffic: rule alerts
remain urgent regardless of its advisory `genuine`, `abusive`, or `unclear`
verdict. Calls time out after five seconds, are never retried or queued, and
are capped at one per counter window and 24 per rolling day. With Clef off,
unavailable, busy, or capped, the alert says `not assessed`.

## Guest mail budget and rollback

Every mail the console sends, from every path (guest codes, approved sign-in
codes and notices, set-password links and deletion codes), passes one ceiling
of `mail.total_daily_ceiling` per rolling day, set below the provider's
100-a-day limit. The last quarter of it is kept for approved users' sign-in
codes and new-browser notices, so set-password, deletion and guest mail can
never starve them. A refusal logs `mail send refused`, counts in
`failed_sends` and raises a `ceiling` alert. It is counted in memory, so a
restart starts it again; the guest budget below is the persistent limit. Set-password and deletion mail are
also limited to 5 per address per hour, beside the usual per-IP limit.

Guest code mail has a persistent global budget of `mail.daily_budget` per
rolling 24 hours and `mail.hourly_budget` per rolling hour. One source IP may
use at most `mail.ip_hourly_budget` of those per hour, and never more than one
less than the hourly budget, so a slot always remains for another guest; an
hourly budget of one therefore permits no source to send guest code mail.
Counts are stored in the guest database before a send is scheduled, so they
survive restarts and cannot be bypassed by slow background sends. Approved
sign-in codes, new-browser notices and set-password mail do not use this guest
budget. Setting either budget to `0` stops guest code mail while approved mail
continues.

To restore guest mail after a budget was deliberately lowered or exhausted,
raise `guest_mode.mail.daily_budget` and/or
`guest_mode.mail.hourly_budget` in the private config, then restart
`irc-lens serve`. The existing rolling counts remain in the guest database;
the newly loaded limits take effect immediately after that restart. Keep live
deployment values in the private config rather than in this public document.

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
- **Untrusted browsers** have one budget per email that counts **wrong**
  password submissions (`POST /entry/signin`) only: 3 per 15 minutes. Once
  it is exhausted the email is strict, 2 per 30 minutes, until 24 hours pass
  with no blocked attempt (table `signin_budget`). Untrusted browsers are
  also limited per IP to 3 wrong password submissions per 15 minutes (fixed;
  `rate_limits.password_attempts_per_15min` stays the guest and
  rollback-path limit). Both count submissions for approved emails only; a
  sign-in for any other email is a guest sign-in and is not counted.
- **A correct password is not counted.** Each submission is counted before
  the password is checked (so the limit check stays atomic and a blocked one
  is never checked), and taken back when it turns out right. The count is
  never visible, so this reveals nothing, and a correct password still needs
  the emailed code; brute force, a run of wrong guesses, stays fully
  limited. Several new browsers on one home IP no longer lock each other
  out.
- **Code entries are never limited or counted** (`POST /entry/code`). A code
  is 256 random bits, valid once for 10 minutes, and only in the browser
  that entered the right password, so guessing one is infeasible; limiting
  code entries would only lock out the real user (a second browser on the
  same IP did, on go-live day). Any number of wrong entries leaves the right
  code working.
- **A blocked password attempt looks exactly like an unblocked one**: the
  same code screen (same status, body and floor), and no code is mailed.
- **New-browser notice.** A completed sign-in from a browser that is not
  trusted for that email mails the user "New sign-in to chat.culture.dev"
  with the time (UTC), the IP and the browser (User-Agent, cleaned and cut
  to 120 characters), and points to **Set or reset password** if it wasn't
  them. A browser already trusted for that email gets no notice, even from
  a new IP. An untrusted browser has nothing that identifies it, so every
  sign-in from it sends one. The notice carries no code, session id or
  device id and goes out in the background; if it fails, the sign-in still
  succeeds (a provider failure raises the usual delivery alert).

Approved users are still exactly the `allowed_emails` list: there is no
sign-up and no account creation. The Cloudflare Access `/login` path keeps
working as break-glass.

### Guests sign in with just their email

With app sign-in on, **Sign in is the only entry**: there is no Guest mode
button. A guest has no password; the password page says *No password? Leave
it empty. We'll email you a code.* (true for everyone, so it reveals nothing).

1. `POST /entry/signin` with an email that is **not** in `allowed_emails`
   ignores whatever was typed as the password and mails a **guest code** (the
   guest template, "Your chat.culture.dev code", valid 15 minutes) from a
   background task, bound to that browser's `lens_signin` cookie like a
   sign-in code. The response is the same code screen, status, cookie and
   time floor as every other sign-in, so nothing tells a guest from an
   approved user before the code. An approved email with a wrong password,
   a banned email or IP, an implausible address and a blocked attempt mail
   nothing. The password-attempt limits count approved emails only, so
   guests on a shared IP never use up an approved user's attempts. Guest
   mail has its own cap, `rate_limits.entry_per_min` per email and per IP,
   counted for **every** submission alike (approved or not), so a guest's
   inbox can't reveal whether another email is approved. Over the cap
   nothing is mailed and the page is the same.
2. `POST /entry/code` with the guest code: a wrong, expired, reused or
   other-browser code is the one error `Wrong or expired code`, exactly as
   for approved users. Only after a right code:
   - the guest seat is taken: the busy page; the code is **not** used up,
     so it works again once the seat frees;
   - a returning guest whose consent matches the current Terms/Privacy
     versions: `lens_guest` cookie and 303 `/`, straight in;
   - otherwise: the **one-time welcome form**, nickname (`sbx-` prefix)
     plus Terms/Privacy consent and the optional training opt-in, carrying
     a single-use 15-minute `welcome` token.
3. `POST /entry/welcome` checks the token (bound to the email; a stale or
   foreign token sends the visitor back to the start with `That took too
   long. Sign in again.`), the consent box and the nickname (a form error
   keeps the token), re-checks the seat under the lock, then records the
   guest and its consent, sets `lens_guest` and 303s to `/`. When the
   Terms/Privacy version changes, a returning guest gets the form once more.
4. `POST /guest/logout` (the guest's **Log out** button) closes the guest's
   sandbox session, frees the seat at once and clears `lens_guest`
   (`303 /`, or `HX-Redirect: /` for htmx).

**Trust this browser** is ignored for guests (the checkbox stays on the
shared code screen): a browser trusted for a guest email would skip the
password-attempt limits if that email were approved later. If the guest seat
fills while a first-time guest is on the welcome form, **Enter** brings the
form back with the busy message and its token unused, so it works again once
the seat frees. A legal-versions outage answers `Try again later` (503)
without counting a failed sign-in. The Enter key submits the forward
button (Verify / Enter) on every step, not Back.

**Rollback switch.** `auth.app_signin.enabled: false` restores 0.12.2: a
correct password answers `303 /login` (wrong ones `Email or password is
wrong`), `/entry/code`, `/entry/welcome`, `POST /logout` and
`POST /guest/logout` do not exist, `lens_session` is not read, the
set-password routes are not mounted, and guests use the Guest mode button
flow (`/entry/guest*`, `/entry/verify`) again.

## Delivery alerts

When a send fails in a way that means mail isn't flowing, and
`guest_mode.mail.alert_url` is set, the lens posts `{"kind": ..., "message": ...}`
as JSON to that URL with `Authorization: Bearer <secret>`, the secret read from
the env var named by `guest_mode.mail.alert_secret_env`. The kinds:

| Kind | Cause |
| --- | --- |
| `authentication` | HTTP 401: the API key is wrong or revoked |
| `forbidden` | HTTP 403: the sender is rejected, or Cloudflare blocked the lens's requests |
| `quota` | HTTP 429: the quota is used up, or the short per-second rate limit |
| `provider` | HTTP 5xx from the provider |
| `network` | No HTTP answer |

Other statuses, such as 400, 404 and 422, are about one recipient. They don't
alert and don't use up a kind's hourly slot, so a bad address can't hide a real
outage. The receiver is a small Cloudflare email Worker that mails the approved
users. The message says which mail isn't being delivered (guest and sign-in
codes, sign-in notices, set-password links and deletion codes) and that
approved users can still sign in at `/login`. At most one alert per kind per
hour; the post runs on a daemon thread and any failure is logged and swallowed.
The payload never contains a recipient address, a code or the provider's
response. With `alert_url` unset (default) alerts are off.

Every failed send logs `mail send failed: HTTP <status>` (or `network error`)
and nothing else about the send, and increments `failed_sends`. Each alert
posted increments `delivery_alerts`.

## Threat rules

The console counts request signals in memory in 15-minute windows: requests,
probes of unrouted paths, refused (401 and 403) responses, entry posts, guest
mail sends, reserved-address refusals, failed sends and budget hits. It keeps
counts and hashed source addresses only, never paths, addresses or user
agents. A check every 5 seconds, off the request path, compares the counts
with fixed triggers and posts one alert per window through the delivery-alert
URL, within the usual one-per-kind-per-hour limit. The alert carries counts
only; a budget-hit alert adds that real guests may be getting no mail.

The triggers are a top-level `threat_rules:` section, each a positive count
per window:

```yaml
threat_rules:
  refused_responses: 50
  entry_posts: 10
  reserved_addresses: 1
  failed_sends: 1
  budget_hits: 1
```

Alert kinds: `rule-budget-hit`, `rule-failed-send`, `rule-reserved-address`
and `rule-burst` (refused responses or entry posts).

## Message validation (Clef)

With `guest_mode.sandbox.validator` set, each chat or `/me` line a guest types
in the sandbox is sent to the Clef decider before the demo agents see it.
Approved users' lines, mesh lines and a guest's personal-agent lines are never
sent. Clef answers a fixed choice question whose only keys are `allow` and
`block`, so a line that asks Clef to allow it, or to answer in prose, can only
produce one of those two keys; any other answer counts as no verdict.

```yaml
guest_mode:
  sandbox:
    validator:
      url: http://127.0.0.1:8001/v1/systemone
      credential_env: CLEF_GATEWAY_KEY   # the env var holding the credential
      prompt_file: ~/.config/irc-lens/clef-prompt.yaml
      mode: flag                         # flag (default) or block
      daily_cap: 500                     # calls per rolling day
```

`url`, `credential_env` and `prompt_file` are set together or not at all.
The prompt file stays outside the repo and is never logged; edits take effect
on the next line, with no restart. Its shape:

```yaml
state: <facts about the demo and what the agents may be asked>
instructions: <the question Clef answers>
criteria:
  allow: <what an acceptable line looks like>
  block: <what should never reach the agents>
```

The guest's line is appended to `state` as data. One call runs at a time per
guest, with a 5-second timeout. When Clef errs, times out, the prompt file is
missing or invalid, or `daily_cap` is used up, the line is sent and counted as
unchecked (`messages_unchecked`).

In `flag` mode, a `block` answer is recorded and counted (`messages_flagged`)
but the line is still sent. In `block` mode the line is refused with "Message
not sent" and never reaches the sandbox (`messages_blocked`). Flagged and
blocked lines are stored with the guest's inputs, so deleting the guest
erases them, and alerts carry counts only. Start in `flag` mode, measure the
prompt on labelled lines (false blocks and misses), and switch to `block` only
after the owner accepts the rates.

`scripts/clef_lines.py` runs a labelled set (at least 40 lines labelled
`benign`, `abusive` and `injection`, kept beside the prompt file and out of
prompt tuning) through the validator exactly as the console would, and prints
the false blocks and misses per label. First run, 2026-11-06, with a draft
prompt on 48 held-out lines: 1 of 20 benign lines blocked, 12 of 12 abusive
lines blocked, and 13 of 16 injection lines blocked (the 3 misses were polite
requests for the prompt or a key, which tool-less agents can't act on).

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
pages shown), `delivery_alerts` (alerts posted) and `failed_sends` (every
failed send, alerted or not). Counts only: no code,
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
| `POST /entry/code` | App sign-in code step: the emailed code plus the `lens_signin` cookie → approved: `lens_session` cookie, 303 `/`; guest: busy page, straight in, or the one-time welcome form (see [Guests sign in with just their email](#guests-sign-in-with-just-their-email)); any failure is the one error `Wrong or expired code`. 404 with the switch off |
| `POST /entry/welcome` | One-time guest form after a first guest code: nickname + consent → guest recorded, `lens_guest` cookie, 303 `/`. 404 with the switch off |
| `POST /guest/logout` | Guest log out: closes the sandbox session, frees the seat, clears `lens_guest` |
| `POST /entry/guest` | Rollback switch off only (404 otherwise): nickname (`sbx-` prefix) + Terms/Privacy consent + optional training opt-in |
| `POST /entry/guest/start` | Rollback switch off only: emails a single-use, 15-minute code (one fixed template; the mail says "code", matching the Code field) |
| `GET /login` | Post-SSO return target (Cloudflare Access forwards the user here): always 303 `/`, `Cache-Control: no-store`, any tier, never 404 and never reveals approval; served even with guest mode off |
| `POST /entry/verify` | Rollback switch off only: code check → guest + consent recorded, `lens_guest` cookie, 303 `/` |

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
generic error). Unset, the check is a no-op, and with guest mode on
`irc-lens serve` logs one warning at startup and `irc-lens doctor` reports a
`guest bot check` warning. Doctor reads the keys from its own environment, so
when they're injected only at serve time (for example by `grant run`), run
doctor the same way.

With `require_bot_check: true` and no keys, guest codes are refused (the
sign-in page looks the same; the Guest mode start form shows the bot-check
error). Approved sign-in still works. Leave it off until Turnstile is proven
through the edge, then turn it on.

A browser trusted for an approved email (`lens_device`) skips the widget on
app sign-in, so a failing or missing widget never locks an approved user out.
Untrusted browsers still need it. Only responses that render the
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
the browser opens the chat). One client address may hold at most
`max_pending_per_address` of those unopened reservations (default `1`); a
different address can still reserve another available seat. Guest messages
are limited to `rate_limits.messages_per_min` per minute (default `20`). With
app sign-in on, a guest is mailed a code
even while the sandbox is full (the browser can't be told it's a guest
before the code) and sees `The sandbox is busy. Try again in a few minutes.`
after a right code; the code stays usable. With the rollback switch off, a
visitor who picks Guest mode sees the busy page and no code is emailed (the
`guest_busy` counter counts these pages). Code verification
re-checks the limit under a lock, so two guests verifying at once cannot both
get in; the refused one sees the busy page and their code is not used up. A
slot frees when the guest logs out, deletes their data or is signed off for
idleness.
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
- tokens issued more than a day ago are removed; rate-limit attempts are kept
  for 25 hours, giving the 24-hour guest-mail window a one-hour sweep margin;
- bans older than 365 days are lifted.

The agent state badge (`agent online` / `agent is warming up`) reflects whether the
agent is in the session's room right now, read from the live member list. Each
15-second badge poll re-reads the room with `WHO`: AgentIRC sends no `QUIT`
for a client holding the `agentirc.io/bot` capability (as `sbx-ask` does) or
for an abrupt disconnect, so a stopped agent shows `agent is warming up` on the next
poll rather than never.
