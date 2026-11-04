# Build Plan — chat edge gate and spark2 fallback

slug: `chat-edge-gate-and-spark2-fallback` · status: `exported` · from frame: `chat-edge-gate-and-spark2-fallback`

> chat.culture.dev sits behind a Cloudflare Worker that gates traffic at the edge, so anonymous traffic never reaches our machines and a real outage page shows when both sparks are down; when spark fails, signed-in people land on spark2 instead of a 500, after signing in again if needed

## Tasks

### t1 — Record the reliability work as delivered: stale pidfile fix (culture#520, 14.13.2) and /readyz (irc-lens#91, 0.19.0)

- instruction: Verification only, no code. Cite the merged PRs and test names. If a check fails, file a deviation.
- covers: c2, h4, c3, h5, c4, h6
- acceptance:
  - spark, spark2, thor and orin report culture 14.13.2, and spark reports irc-lens 0.19.0
  - Public /readyz returns 200 with Cache-Control no-store
  - Stopping a test IRCd makes /readyz return 503 in tests/`test_readyz.py`

### t2 — POST spike: prove a browser POST through a Worker on an in-zone test hostname reaches the lens with Origin accepted

- instruction: Extend the earlier scratch spike with a POST and a same-zone route. Owner runs the deploy, since auto mode blocks it. The result chooses t6's approach, so finish this first.
- acceptance:
  - A POST through the Worker to a test route returns the lens's normal response, not the Origin-denied 403
  - The result records whether cf.resolveOverride keeps Host chat.culture.dev through the tunnel, or whether the lens needs a configured public origin
  - Visitor IP seen by the lens equals the client's IP, not a Cloudflare address, so `client_ip` rate limits still work
  - The test Worker and hostname are deleted afterwards

### t3 — culture: `culture console install --healthcheck` verb that installs the health ping timer on any host

- instruction: Follow the existing `verb == "install"` flow in console.py and `culture_core`/persistence.py for unit generation. Keep the check logic in one small function that's testable without systemd.
- covers: c17, h1, h2, c27, h18, c29
- acceptance:
  - Files: `culture_core`/cli/console.py, a new `culture_core`/cli/shared/healthcheck.py, tests/`test_console_healthcheck.py`, docs/reference/cli/console.md
  - Generated unit reads the URL from grant, and the site address from the lens config, with no hardcoded host
  - Ping logic checks local /readyz and public /readyz, and reports /fail unless both pass
  - Tests use real local HTTP listeners for: both pass, local fails, public fails, URL missing
  - The all-backends rule isn't touched

### t4 — culture: log every server link outcome and find why spark's outbound links don't form (#519)

- instruction: Start in agentirc ircd.py `connect_to_peer` and `server_link.py` handle, where the task's result is never awaited. agentirc is a sibling repo: the fix may land there first, then a pin bump in culture. See also #510.
- acceptance:
  - Every link attempt logs success, handshake failure or rejection, with no secrets
  - Root cause of #519 is written on the issue
  - spark holds an established link to thor and orin after a server restart, shown by ss and a link-state query

### t5 — culture: standby rejoins the mesh after a reboot without a person unlocking its keyring (#509)

- instruction: Read #509 first. Moving link secrets from the login keyring to grant, as agent secrets already are, may be simpler than unlocking the keyring. Propose the approach to the owner before building: it touches secret storage.
- depends on: t4
- covers: c19, h13
- acceptance:
  - After a spark2 reboot with no human action, its server holds a link to at least one peer
  - Link secrets are never written in plain text to a world-readable file, or printed
  - Docs describe how it's set up and how to rotate it

### t6 — irc-lens: trust the public origin and the Worker's credential, so browser POSTs work and direct requests are refused

- instruction: Take the approach t2's result chose. Default off, so this can ship and deploy before the Worker exists.
- depends on: t2
- covers: c8, h10, c28
- acceptance:
  - Files: src/`irc_lens`/config.py, web/auth.py, web/routes.py (`_origin_ok`), tests/`test_worker_trust.py`, docs/deployment-cloudflare-access.md
  - A POST with Origin equal to the configured public origin passes through the Worker path; any other Origin still gets 403
  - A request without the Worker's credential is refused when `auth.require_edge_credential` is on, and behaviour is unchanged when it's off
  - The credential comparison is constant-time, and the secret comes from an env var

### t7 — irc-lens: standby mode, for approved users only with account changes and guest mode off

- instruction: List write routes from the stores in `access_store.py` and `guest_store.py`, and block at one middleware rather than per route, so a new route can't be missed silently.
- covers: c20, h14
- acceptance:
  - Files: src/`irc_lens`/config.py, a new web/standby.py, tests/`test_standby.py`, docs/deployment-cloudflare-access.md
  - With `standby.enabled`, a guest sign-in gets the outage message and no guest is created
  - Every route that writes accounts, grants, nicknames, consents or agent credentials refuses with a clear message; a test enumerates them
  - Approved users sign in and send messages normally, as the server-prefixed nick
  - The UI shows a banner that says agents and spark media are unavailable

### t8 — Worker: gate traffic, pin sessions, fail over on repeated /readyz failures, serve the outage page

- instruction: Keep pure logic separate from fetch so it can be tested with node. Store the active origin in KV, written by a cron check, so requests don't each probe. Deploy using cultureflare's scripts, dry-run first. Owner deploys.
- depends on: t2
- covers: c1, c5, h7, c7, h9, c30, h21
- acceptance:
  - Files: deploy/chat-edge/worker.js, deploy/chat-edge/outage.html, deploy/chat-edge/worker.test.mjs, deploy/chat-edge/README.md, all in irc-lens
  - Pure functions, unit tested with no network: pick origin, pin cookie, failure count
  - A session stays on one origin while it's healthy
  - Failover needs N consecutive failed checks, so a restart moves no one; returning to spark needs a deliberate config change
  - Both origins down returns the outage page with status 503
  - Handler does almost no CPU work: it never parses bodies, and streams SSE through unchanged

### t9 — Provision a separate tunnel for spark2 and an origin hostname for each host

- instruction: Use cultureflare verbs where they exist. Tunnel tokens go in grant, never in a file in a repo.
- depends on: t2
- covers: c6, h8
- acceptance:
  - spark and spark2 connect through different tunnel UUIDs, shown by the Cloudflare API
  - Each origin hostname refuses a request lacking the Worker's credential once t6 is on
  - The existing chat.culture.dev tunnel route is untouched until t11's rollback path is proven

### t10 — Copy accounts one way from spark to spark2 on a schedule, privately

- instruction: Run on spark and push to spark2, so standby needs no way into spark. Guest mode is off on standby: prefer leaving guest and consent rows out, which shrinks the private data on spark2.
- depends on: t3
- covers: c25, h17, c32, h23
- acceptance:
  - Files: a new `culture_core`/cli/shared/`console_replica.py` and a `culture console replica` verb in culture, with tests
  - Uses sqlite's backup API, so the copy is consistent while the lens is running
  - Copy travels only over the tailnet or LAN, lands mode 0600 and is replaced atomically
  - A copy from a newer schema to an older lens is refused with a clear message
  - After a copy, an approved person signs in on spark2 with the same role and grants, in a test using two real stores
  - Retention on the copy matches the original: `guest_mode`.`retention_days`, or no guest rows copied

### t11 — Rollback: remove the Worker in one step and rehearse it

- instruction: Rehearse before any origin lockdown is enforced, so the direct path is known to work.
- depends on: t8, t9
- covers: c31, h22
- acceptance:
  - A documented single command removes the Worker route, and traffic returns through the direct tunnel
  - Rehearsal in production restores signed-in service in under 5 minutes, with the time measured
  - The runbook says who rotates the Worker's service token, and when it expires

### t12 — Provision spark2 as standby with the supported install verbs

- instruction: Keep the same versions on both hosts. spark2's bind address stays on its tailnet or loopback.
- depends on: t3, t6, t7, t9, t10, t5
- covers: c12, h11
- acceptance:
  - culture console install and culture server install produce running units on spark2
  - spark2's lens runs in standby mode, reachable only through its tunnel
  - spark2 has its own health check, or a documented reason it doesn't, and its secrets are in its own grant

### t13 — Failover drill in production: measure everything the spec promises

- instruction: Schedule with the owner, because this is a real interruption. Run it with real browser click-through on every path.
- depends on: t11, t12
- covers: h3, c21, h15, c22, h16, c18, h12, h19, h20
- acceptance:
  - Stopping spark's IRCd alerts the owner within 10 minutes, with the time measured
  - Signed-in test account reaches spark2 within 5 minutes and sends a real message in a real browser
  - At least 2 hourly reminder emails arrive while the check is down
  - Stopping both origins shows the outage page
  - Restarting the console during a deploy moves no one
  - History gaps after failover are recorded as observed
  - Results are filed as evidence, including anything that failed

## Risks

- [unknown_nonblocking] History after failover may look incomplete, depending on whether remote-origin messages reach the local history.db
- [unknown_nonblocking] Turnstile through a Worker is unexamined; t13 must exercise a real sign-in with it
- [unknown_nonblocking] Same-zone route, Access on the same hostname, and Worker cron with KV on the Free plan are unverified; t2 and t8 must cover them (task t2)
- [follow_up] Moving link secrets touches secret storage on all four hosts, so t5 needs owner sign-off on its approach (task t5)
