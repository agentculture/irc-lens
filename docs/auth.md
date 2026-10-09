# Authentication and Identity

irc-lens supports two auth modes, selected by `auth.mode` in the config
file: `dev` (no auth, single synthetic identity) and
`cloudflare-access` (per-user JWT-validated identity behind Cloudflare
Access).

## Identity model

- One `Session` per authenticated principal, opened lazily on first
  request.
- Nick is derived: `<server_name>-<sanitized-local-part>`. Sanitization
  drops everything outside `[a-z0-9-]` from the email's local part (or
  from the service-token common name).
- The principal — `email` for interactive SSO, `common_name` for
  service tokens — keys the registry. Two browsers signed in as the
  same principal share one Session and one IRC connection.

## JWT validation rules

For each authenticated request the lens:

1. Reads the JWT from the `Cf-Access-Jwt-Assertion` header (preferred)
   or the `CF_Authorization` cookie.
2. Looks up the signing key by `kid` against an in-process JWKS cache.
3. On `kid` miss: refreshes the cache once (rate-limited to once every
   5 s) and retries; permanent miss → 401.
4. Verifies signature, audience (`auth.cloudflare.aud`), and issuer
   (`https://<auth.cloudflare.team_domain>`).
5. Reads `email` (or `common_name`) and checks against
   `auth.allowed_emails` / `auth.allowed_service_tokens`.
6. Derives the nick and stashes the `Identity` on `request["identity"]`.

## Tiers

Every `Identity` carries a `tier` (`src/irc_lens/web/identity.py`):

- `approved` — a verified Access JWT (`Cf-Access-Jwt-Assertion` header or
  `CF_Authorization` cookie) whose `email` is on `auth.allowed_emails`, an
  allowlisted service-token `common_name`, or the `auth.mode: dev`
  identity. The only tier that may reach the real mesh.
- `guest` — reserved for the signed guest-session cookie (not issued by the
  auth middleware). Sandbox only.
- `anonymous` — everything else, only when `guest_mode.enabled`. Reaches
  only routes marked `allows_anonymous`.

Rules:

- `approved` is derived **only** from a verified JWT. Headers such as
  `Cf-Access-Authenticated-User-Email`, query parameters, and any cookie
  other than `CF_Authorization` are never read for identity.
- Allowlisted service tokens are `approved`: they are operator-issued,
  Access-verified, and listed in the lens config — the same trust basis as
  an approved email.
- `Identity.tier` defaults to `anonymous`, so an identity built without an
  explicit tier fails closed.
- **Guest mode off** (default): unchanged — missing/invalid JWT → 401,
  allowlist deny → 403.
- **Guest mode on**: a missing JWT, an unverifiable JWT (bad signature,
  wrong `aud`/`iss`, expired), or a verified JWT for a principal not on the
  allowlist resolves to `anonymous` (the shared `ANONYMOUS_IDENTITY`: empty
  principal and nick — nothing from a rejected JWT is carried forward).
  The request reaches its handler only if the route handler is decorated
  with `irc_lens.web.auth.allows_anonymous`; every other route — console,
  `/events`, `/input`, `/upload`, `/residents`, `/agent` — answers one
  uniform 401 (`approved sign-in required`), whatever the reason, so the
  response leaks no membership signal. Deny by default: a new route stays
  approved-only unless it opts in, and an opted-in handler must branch on
  `request["identity"].tier` (`Identity.is_approved`) before touching the
  real mesh. JWKS-unreachable (502) and nick-derivation (500) failures are
  server faults and surface unchanged.

## Dev mode

In `auth.mode: dev`, the same middleware is installed but synthesizes
`Identity(principal=auth.dev.email, nick=auth.dev.nick, ...)` on every
request. Handlers see the same contract as in CF mode.

## Failure modes

| Status | Cause |
| --- | --- |
| 401 | missing/invalid JWT; guest mode: anonymous on approved-only route |
| 403 | allowlist denied (guest mode off) / Origin mismatch on POST /input |
| 500 | nick derivation produced empty (server-config bug) |
| 502 | JWKS unreachable on first fetch |
| 503 | Session unhealthy / cannot reach AgentIRC |

## Audit log

Every authenticated request emits one structured line on stderr:

    auth=ok principal=<email-or-common-name> nick=<derived> method=<verb> path=<path>

Auth denials log:

    auth=denied principal=<...> reason=<short-tag>

When `--log-json` is passed, the same data goes through the
`_JsonLineFormatter` and lands as one JSON object per line.

## Service tokens

Cloudflare Access supports service tokens for non-interactive callers
(CI, scripts). They authenticate via `CF-Access-Client-Id` and
`CF-Access-Client-Secret` headers; Cloudflare mints a JWT that carries
`common_name` instead of `email`. List the allowed common-names under
`auth.allowed_service_tokens`. Service tokens have no MFA and no SSO
identity; treat them as long-lived secrets.
