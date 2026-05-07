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

## Dev mode

In `auth.mode: dev`, the same middleware is installed but synthesizes
`Identity(principal=auth.dev.email, nick=auth.dev.nick, ...)` on every
request. Handlers see the same contract as in CF mode.

## Failure modes

| Status | Cause |
| --- | --- |
| 401 | missing or invalid JWT |
| 403 | allowlist denied / Origin mismatch on POST /input |
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
