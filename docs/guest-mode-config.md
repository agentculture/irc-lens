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
  idle_close_s: 600         # close a session with no open tab
  store_path: ~/.local/share/irc-lens/guests.db   # default: $XDG_DATA_HOME/irc-lens/guests.db
  legal_version_url: https://culture.dev/legal/version.json
  mail:                     # sender for guest token emails
    provider: none
    from: ""
    api_key_env: IRC_LENS_MAIL_API_KEY            # env var holding the provider key
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
| `idle_close_s` | `guest_idle_close_s` | `600` |
| `store_path` | `guest_store_path` | `$XDG_DATA_HOME/irc-lens/guests.db` |
| `legal_version_url` | `guest_legal_version_url` | `https://culture.dev/legal/version.json` |
| `mail.provider` | `guest_mail_provider` | `none` |
| `mail.from` | `guest_mail_from` | `""` |
| `mail.api_key_env` | `guest_mail_api_key_env` | `IRC_LENS_MAIL_API_KEY` |
| `rate_limits.entry_per_min` | `guest_rate_entry_per_min` | `10` |
| `rate_limits.messages_per_min` | `guest_rate_messages_per_min` | `20` |
| `rate_limits.password_attempts_per_15min` | `guest_rate_password_attempts_per_15min` | `5` |

Validation: every sub-section must be a mapping; unknown keys anywhere in the
section are rejected (typo guard); `enabled` must be a boolean, ports go
through the shared port check, `legal_version_url` must be an `http(s)` URL
with a host, and rate limits must be integers.

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
| `POST /entry/signin` | Approved email + correct password → 303 `/login`; otherwise `Email or password is wrong` |
| `POST /entry/guest` | Nickname (`sbx-` prefix) + Terms/Privacy consent |
| `POST /entry/guest/start` | Emails a single-use, 15-minute code (one fixed template; the mail says "code", matching the Code field) |
| `GET /login` | Post-SSO return target (Cloudflare Access forwards the user here): always 303 `/`, `Cache-Control: no-store`, any tier, never 404 and never reveals approval; served even with guest mode off |
| `POST /entry/verify` | Code check → guest + consent recorded, `lens_guest` cookie, 303 `/` |

The `/entry*` routes answer 404 while `guest_mode.enabled` is false. Sign-in failures are
indistinguishable: an unknown email still pays one (dummy) argon2id verify, and
every sign-in response is padded to a fixed floor (0.5 s). Sign-in and token
verification are limited per email and per IP by
`rate_limits.password_attempts_per_15min`; token requests by
`rate_limits.entry_per_min` — over the limit the same body returns as 429. The
visitor IP is `CF-Connecting-IP` (cloudflared) when present.

Guest nicks are `sbx-<nickname>`: lowercased, reduced to `[a-z0-9_-]`, 2–16
characters, unique among recorded guests, never derived from the email, and
`ask` is reserved for the sandbox agent.

Consent is recorded against `irc_lens.legal.current_legal_versions(cfg)` (the
`legal_version_url` JSON, cached in-process for 5 minutes; a failed fetch with
nothing cached blocks entry rather than recording an unknown version).

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

A sandbox session with no open browser tab is closed after `idle_close_s`.

### Guest chat records and deletion

The sandbox IRCd runs memory-only (`culture server start --no-persist`): it
writes no channel history to disk. The guest store is the one durable record
of guest chat: each guest's messages (`kind="message"`) and the answers
sbx-ask posts in their room (`kind="answer"`).

A confirmed deletion first keeps the guest's question-and-answer pairs in an
anonymized `corpus` table — email, IP, nickname and room dropped, PII scrubbed
(the same heuristic as the export), day-only date, a random row id, and
NSFW-declined pairs left out — then erases the guest's profile, inputs,
consents, tokens, flags and room id from the guest store, their uploads, and
their lines in sbx-ask's flag log (`sandbox.flag_log`). Bans are kept. The
corpus is not deleted; `irc-lens guests export` includes it as `anonymous`
`qa` rows. Without `flag_log` the lens logs a warning at startup.

The scrub is heuristic (see the export section): a corpus row can still carry
an identifying detail the scrub misses, such as a third party's name in plain
prose. Review the corpus before publishing it.

The agent state badge (`agent online` / `agent offline`) reflects whether the
agent is in the session's room right now, read from the live member list. Each
15-second badge poll re-reads the room with `WHO`: AgentIRC sends no `QUIT`
for a client holding the `agentirc.io/bot` capability (as `sbx-ask` does) or
for an abrupt disconnect, so a stopped agent shows `agent offline` on the next
poll rather than never.
