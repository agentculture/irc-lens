# Remote-connect via Cloudflare Access — design

Status: draft, pending review.
Issue: [agentculture/irc-lens#26](https://github.com/agentculture/irc-lens/issues/26).

## Goal

Make `irc-lens serve` deployable behind Cloudflare Tunnel + Cloudflare Access on the operator's own domain, so an authenticated user can browse to a public hostname and reach a private AgentIRC mesh without any inbound port being exposed. Local development must stay frictionless: no Cloudflare, no SSO, no JWT plumbing in the dev path.

The design is mechanism-only. The spec and code never name a specific domain, hostname, or user. All such values live in the operator's local config file.

## Non-goals

- Multi-IdP support beyond Google SSO. CF Access supports more; the lens doesn't care which IdP issued the JWT it validates.
- Self-registration / signup. The allowlist is config-driven; new users are added by editing `auth.allowed_emails` and the matching CF Access policy.
- Group-based authorization. Only the `email` claim is consulted in v1; `groups` is a deferred enhancement.
- Reconnect / liveness UX in the browser. SSE through CF Tunnel is already supported by today's lens; nothing changes here.

## Configuration surface

### Single config file, no env vars

`~/.config/irc-lens/config.yaml` is the source of truth, location overridable with `--config <path>` and respecting `$XDG_CONFIG_HOME`. CLI flags override the file; the file overrides built-in defaults. There is no env-var layer in v1 — the goal is exactly one place to look for any setting.

### Schema

```yaml
auth:
  # "dev" | "cloudflare-access"
  mode: cloudflare-access

  # Required when mode == cloudflare-access:
  cloudflare:
    aud: <access-application-audience-tag>
    team_domain: <team>.cloudflareaccess.com
  allowed_emails:
    - <your-email>
  # Optional: service-token principals for CI / scripted access.
  allowed_service_tokens:
    - <client-id-or-common-name>

  # Required when mode == dev:
  dev:
    nick: lens
    email: dev@local

server:
  # AgentIRC server name — used to derive nicks in cloudflare-access mode.
  name: <agentirc-server-name>
  host: 127.0.0.1
  port: 6667

web:
  bind: 127.0.0.1
  port: 8765
```

### CLI changes

`irc-lens serve`:

- `--config <path>` — point at a non-default file.
- `--server-host`, `--server-port`, `--web-port`, `--bind`, `--open`, `--seed`, `--log-json` — kept; override the file.
- `--nick` — kept, but only honored when `auth.mode: dev`. Passing it under `cloudflare-access` is a hard error.
- `--server-name` is *not* a CLI flag; it lives at `server.name`.

New verb `irc-lens config init` writes a minimal commented template to the default path (or wherever `--config` points), in `dev` mode by default.

### Asymmetric defaults

| Setting | Dev mode | CF mode |
| --- | --- | --- |
| `web.bind` | accepts anything; `0.0.0.0` warns (existing) | non-loopback values are silently coerced to `127.0.0.1` with a one-line notice |
| AgentIRC connect | opened at startup against `auth.dev` identity | not opened at startup; opened lazily per authenticated user |
| `--nick` | required (overrides `auth.dev.nick`) | rejected |
| Required env at startup | none | none beyond the file |

CF mode binding to a public interface is always wrong: cloudflared terminates locally and only the local cloudflared is the legitimate caller. Coerce-with-notice is friendlier than hard-erroring on a recoverable mistake.

## Auth and identity

### Module shape

```
src/irc_lens/web/auth.py
  Identity(NamedTuple): principal, nick, raw_jwt_subject
  load_jwks(team_domain) -> JWK set            # cached, refreshed on kid miss
  verify_cf_access_jwt(token, aud, team_domain) -> claims
  derive_nick(server_name, email) -> str       # sanitize + prefix
  build_middleware(config) -> aiohttp.middleware
```

### Middleware flow (CF mode)

1. Skip auth for `/static/*` and the new `/healthz`. Both are unauthenticated and never expose IRC state.
2. Pull JWT from `Cf-Access-Jwt-Assertion` header or `CF_Authorization` cookie. Header wins on conflict. Missing → 401 `{error, hint}`.
3. Verify signature against JWKS; pin audience to `auth.cloudflare.aud` and issuer to `https://<team_domain>`. Failure → 401, hint differentiates expired / bad-signature / wrong-aud.
4. Read identity claims:
   - `email` if present (interactive SSO).
   - Else `common_name` (service tokens), checked against `auth.allowed_service_tokens`.
   - Else 401.
5. If identity is from `email`, check membership in `auth.allowed_emails`. Not present → 403 `{error: "<email> not on allowlist", hint: "add to auth.allowed_emails in <config path>"}`. Defense-in-depth against a CF Access policy that drifted wider than intended.
6. Build `Identity(principal=email_or_common_name, nick=derive_nick(server.name, principal), raw_jwt_subject=claims["sub"])` and stash on `request["identity"]`. The `principal` field holds an email under interactive SSO and a service-token client-id / common-name otherwise; downstream code never branches on which.

### Dev mode

Middleware is still installed, but it short-circuits to `Identity(principal=auth.dev.email, nick=auth.dev.nick, raw_jwt_subject="dev")` on every request. Same downstream contract; handlers always see `request["identity"]`.

### Nick derivation

```
def derive_nick(server_name: str, principal: str) -> str:
    local = principal.split("@", 1)[0].lower()
    sanitized = "".join(c for c in local if c.isalnum() or c == "-")
    if not sanitized:
        raise AuthError("nick derivation produced empty result", ...)
    return f"{server_name}-{sanitized}"
```

AgentIRC's existing `wait_for_welcome` handles the rare nick-collision (432/433) case.

### JWKS caching

Cache the JWK set in process. On a `kid` we don't recognize, refresh once and retry; permanent kid-miss after refresh → 401. Unconditional refresh would let a malformed-`kid` flood drive request-time fetches, so the kid-miss-then-refresh policy is the bound.

### Error rendering

Auth failures use the irc-lens HTTP error shape `{error, hint}`, not the AfiError CLI triple `{code, message, remediation}`. The CLI triple is for `serve` startup errors that exit the process; the HTTP shape is for in-flight requests. Status codes:

| Status | Cause |
| --- | --- |
| 401 | missing or invalid JWT |
| 403 | allowlist denied |
| 500 | nick derivation produced empty result (server-config bug) |
| 502 | JWKS unreachable on first fetch |

### Audit logging

Every authenticated request emits one structured log line on stderr: `auth=ok principal=… nick=… method=… path=… status=…`. Auth denials log `auth=denied principal=… reason=…`. The `principal` field is the email under interactive SSO and the client-id / common-name under service tokens. Logs flow through the existing logger so `--log-json` produces JSON-line output with no extra wiring.

## Per-user Session registry

### Shape change

```
app["sessions"]        : dict[principal, Session]
app["session_locks"]   : dict[principal, asyncio.Lock]
app["session_factory"] : Callable[[nick], Session]
```

The factory closes over `server.host`/`server.port` so handlers don't reach for transport config. The locks dict is populated lazily on first access for a principal.

### Lazy open

```python
async def get_or_open_session(request) -> Session:
    identity = request["identity"]
    sessions = request.app["sessions"]
    if identity.principal in sessions:
        return sessions[identity.principal]
    async with request.app["session_locks"][identity.principal]:
        if identity.principal in sessions:            # double-check
            return sessions[identity.principal]
        s = request.app["session_factory"](identity.nick)
        await s.connect()
        await s.wait_for_welcome()
        sessions[identity.principal] = s
        return s
```

The double-check guards against two concurrent first requests from the same principal racing into Session creation.

### Failure handling

`LensConnectionLost` from the lazy-open path → handler renders 503 `{error: "cannot reach AgentIRC", hint: "verify AgentIRC is up at <host>:<port>"}`. The principal key is only inserted after `wait_for_welcome` returns clean; a failed open leaves no partial state.

### Startup model

- **Dev mode**: existing behavior — open one Session at boot, register under `auth.dev.email`. Preserves today's startup-time fail-fast for the dev workflow.
- **CF mode**: validate config, fetch JWKS once (fail fast if Cloudflare is unreachable), bind aiohttp, return. First IRC connect happens on first authenticated request.

### Shutdown

```python
await asyncio.gather(*(s.disconnect() for s in app["sessions"].values()),
                     return_exceptions=True)
```

`return_exceptions=True` so a failed disconnect on one session doesn't strand the others.

### Concurrency interactions

PR #23's per-`Session` `execute()` lock is unchanged. Each per-user Session holds its own lock; users don't block each other. No new contention.

### Idle TTL

Skipped in v1. AgentIRC connections are cheap. Add a reaper if a teammate ever leaves a tab open for hours.

## Routes

### `GET /`

Replace `request.app["session"]` with `await get_or_open_session(request)`. The first request from a new user blocks on Session connect + welcome (≤ ~10s typical, longer on a bad route). `LensConnectionLost` from the lazy-open renders a small `error.html.j2` page using the same `{error, hint}` text as the JSON path.

### `POST /input`

Same `get_or_open_session` swap. The 4 KiB body cap, JSON/form decode, empty-body, and 503-on-LensConnectionLost branches are unchanged in shape.

New: an Origin/Referer floor for CSRF defense in depth. CF Access already requires the `CF_Authorization` cookie cross-site, but a same-origin XSS in the lens UI shouldn't be able to fire writes from a third-party tab. If `Origin` is present and doesn't match the configured public hostname (or `127.0.0.1:<web.port>` in dev), reject with 403. Absent `Origin` falls through (HTMX submits send Origin; cloudflared probes don't, and that's fine). CSRF-token uplift is tracked as a follow-up issue.

### `GET /events`

Per-user lazy open. JWT is validated at stream open; we don't re-validate per event. CF Access JWTs default to 24 h; a stream that crosses expiry stays open, but the next `POST /input` will 401 and the UI reconnects. Periodic SSE re-auth is not in v1.

### `/static/*`

Auth middleware skips. No change.

### `/healthz` (new)

Returns `{ok: true}`. No auth. No IRC state. Used by cloudflared / external uptime probes only. Opaque on purpose: it does not reveal whether any user has a live session, since that would leak the allowlist.

## Testing

Three tiers.

### Unit

- `tests/test_auth_middleware.py`: JWT verify happy-path, expired, wrong-aud, wrong-issuer, kid-miss-then-refresh, allowlist deny, dev-mode passthrough, service-token branch.
- `tests/test_session_registry.py`: concurrent-first-request lock, double-check skip, connect-failure-clears-key, shutdown-disconnects-all.
- `tests/test_serve_cf_mode.py`: `--nick` rejected; `--bind` non-loopback coerced.
- `tests/test_config_loader.py`: schema validation, missing-required-field renders as the AfiError CLI triple.

### Integration with fake JWKS

A small aiohttp app on a random port serves a JWK we mint in-test. Sign a JWT with the matching private key, hit the lens, assert it lands. Same harness covers kid-miss-then-refresh end-to-end.

### Real Cloudflare round-trip

Marked `@pytest.mark.cloudflare`, gated on the secrets being present, **manual / agentic only** in v1. CI automation is a separate follow-up issue.

Required secrets (loaded from env at test time, not committed):

```
CF_API_TOKEN          # scopes: Account:Cloudflare Tunnel:Edit,
                      #         Account:Access: Apps and Policies:Edit,
                      #         Zone:DNS:Edit on the chosen zone
CF_ACCOUNT_ID
CF_ZONE_ID
CF_TEST_HOSTNAME
CF_TEAM_DOMAIN
```

Two scripts under `scripts/cf-roundtrip/`:

- `setup.sh` — idempotent. For each Cloudflare resource (tunnel `irc-lens-roundtrip`, DNS CNAME, Access app, Access policy, service token), creates if missing and reuses if present. The service-token credentials are cached at `~/.config/irc-lens/cf-roundtrip-token.json` (`chmod 600`, gitignored), since Cloudflare won't return the secret on a re-fetch. Outputs `IRC_LENS_TEST_AUD`, `IRC_LENS_TEST_HOSTNAME`, `IRC_LENS_TEST_CLIENT_ID`, `IRC_LENS_TEST_CLIENT_SECRET`, `IRC_LENS_TEST_TEAM_DOMAIN` to a gitignored `.cf-roundtrip.env`.
- `teardown.sh` — removes the four CF resources and the local cred file.

Round-trip test (`tests/test_cf_roundtrip.py`):

1. Skip unless all required env vars are present.
2. Boot `irc-lens serve --config <test-config>` as a subprocess on `127.0.0.1`.
3. Boot `cloudflared tunnel run irc-lens-roundtrip` as a subprocess.
4. Wait until `https://<CF_TEST_HOSTNAME>/healthz` returns 200 (poll up to ~30 s; DNS may need to settle on first run).
5. Hit `GET /` without service-token headers → expect 401 from CF edge (proves the gate is on).
6. Hit `GET /` with service-token headers → expect 200 (proves the gate accepts and the lens accepts).
7. Hit `POST /input` with service-token headers and a slash-command payload → expect 204.
8. Tear down subprocesses (CF resources stay).

JWKS rotation coverage in the round-trip:

- **Cold-start fetch.** Every test boot starts the lens with an empty JWKS cache; reaching `GET /` with a valid JWT exercises the real fetch from `https://<team_domain>/cdn-cgi/access/certs`. If CF rolled keys overnight, this catches it.
- **Mid-process kid-miss refresh.** A test-only admin path `POST /__test/evict-jwks` drops the in-process JWK cache. Mounted only when `IRC_LENS_TEST_HOOKS=1`; production config never sets the env var, and the route isn't even registered without it.

We can't force CF to rotate during the run, so we test both halves separately and accept that "actual rotation timing" is a property of CF, not of us.

### Test-impact rule

All existing tests run in `auth.mode: dev`. The `seeded_lens_client` fixture (phase 9c) is the canonical site to migrate; it constructs the dev-mode config and an app with a session-factory override. Phase 9b's AgentIRC fixture and Playwright e2e suites must keep passing without rewrite.

## Documentation deliverables

Three new documents under `docs/`. All are domain-neutral; no specific hostname or zone is named.

### `docs/deployment-cloudflare-access.md`

Runbook for "host irc-lens on your own domain". Sections:

1. Prerequisites: a Cloudflare account on the same zone as your chosen host, an IdP (Google SSO, GitHub, etc.), `cloudflared` installed.
2. DNS: add a CNAME under the tunnel.
3. Tunnel: `cloudflared tunnel create`, route the hostname, run as a systemd unit (snippet provided).
4. Access application: create the app, attach an IdP, set the allowlist policy by email (matching `auth.allowed_emails`).
5. Wire the audience tag and team domain into `~/.config/irc-lens/config.yaml`.
6. Boot order (AgentIRC → `irc-lens serve` → `cloudflared`); reverse on shutdown.
7. Verifying: open the URL, confirm SSO redirect, confirm the lens renders, confirm `/healthz` returns `{ok: true}`.

### `docs/auth.md`

Protocol-level reference, so the runbook stays "do this" not "why".

- Identity model (per-user Session, derived nick, allowlist).
- JWT validation rules (header vs. cookie precedence, JWKS caching, kid-miss refresh).
- Dev mode shim and how it parities prod.
- Failure modes and HTTP status codes.

### `docs/security-checklist.md`

Pre-launch yes/no list:

- AgentIRC bound to localhost only?
- Lens bound to localhost only (CF mode auto-coerces; verify)?
- `auth.mode: cloudflare-access` in the active config?
- `auth.cloudflare.aud` matches the actual Access app's AUD?
- `auth.allowed_emails` is the minimum needed?
- IdP enforces MFA on the listed accounts?
- Cloudflare Access policy mirrors the same email list?
- Tunnel's origin URL is `http://localhost:<web.port>`, not `0.0.0.0`?
- `cloudflared` and `irc-lens` run as non-root?
- Logs are persisted somewhere auditable (systemd journal is fine)?
- `/healthz` returns `{ok: true}` and nothing else?

### Updates to existing docs

- `docs/architecture.md`: deployment-modes subsection, updated session-registry diagram.
- `docs/cli.md`: `--config`, `irc-lens config init`, the `--nick`-rejected-in-CF rule.
- `README.md`: short pointer to the runbook.

## Rollout

Five PRs, in order. Each is independently reviewable.

1. **Config loader + `irc-lens config init`** — no behavior change. CLI flags continue to work; the loader sees an empty/no config.
2. **Per-user Session registry under dev mode** — `app["session"]` becomes a registry-of-one keyed by the dev principal. Identity middleware lands but synthesizes only the dev identity. Every existing test must keep passing.
3. **Auth middleware + JWT verification** — `auth.mode: cloudflare-access` becomes selectable. New unit + fake-JWKS integration tests cover it. Dev tests untouched.
4. **CLI/HTTP cleanups** — `--nick` rejection, `--bind` coercion, `/healthz`, Origin floor on `POST /input`.
5. **Docs + security checklist + round-trip scripts** — written against the working code; round-trip test added under `@pytest.mark.cloudflare`.

Steps 1, 3, 4, 5 are small. Step 2 is the largest. Manual verification on the operator's real domain happens after step 5.

## Follow-up issues

To be opened after this spec lands:

1. **CSRF tokens for `POST /input`** — replace the v1 Origin/Referer floor with per-render CSRF tokens.
2. **Automate CF round-trip on GitHub Actions** — track the open questions: secret-scope hygiene, rate limits, runner cost, cleanup-on-failure, and the workflow-edit-by-PR threat model.
3. **Group-based authorization** — consume the `groups` claim when a teammate joins.

## Compatibility notes

- Existing memory: HTTP error shape stays `{error, hint}`, not the AfiError CLI triple. (PR #7 ratification preserved.)
- Existing memory: AFI exit-code policy applies to `serve` startup errors, not in-flight HTTP responses. (Code 1 = user-supplied wrong; code 2 = environment failed to deliver.)
- AgentIRC server-name nick prefix is enforced today via `wait_for_welcome` (`spark-foo` style). Derivation matches that contract.
