# chat edge gate and spark2 fallback

> the public chat host sits behind a Cloudflare Worker that gates traffic at the edge, so anonymous traffic never reaches our machines and a real outage page shows when both sparks are down; when spark fails, signed-in people land on spark2 instead of a 500, after signing in again if needed

## Audience

- The owner as operator, and approved people who use the public chat host as their mesh console

## Before → After

- After: When spark fails, people reach spark2 after signing in again if needed, as `spark2-<nick>`; when both are down they see an outage page; the owner is told by email either way

## Why it matters

- On 2026-11-03 signed-in users got 500s from LensConnectionLost for up to 41h after the Nov 2 00:44 reboot, while anonymous GET / still returned 200, and nothing alerted anyone

## Requirements

- culture server start ignores a pidfile whose PID is not a culture process, so a reboot cannot wedge the IRCd; this ships before any failover work
  - honesty: With a pidfile pointing at another user's thread, culture server start succeeds; a test covers it
- The lens exposes a readiness check that fails when its IRCd is unreachable; failover and alerting key on it, not on / or /healthz
  - honesty: With the IRCd stopped the readiness check returns non-200, and with it running returns 200
- A Worker on the public chat host picks the origin, pins a session to it, fails over on origin failure, and serves a static outage page when both are down
  - honesty: Stopping both origins yields the static outage page, and the Worker stays within Free limits at current traffic
- Origin hostnames accept only the Worker's credential and the lens trusts a verifiable identity from it, so anonymous traffic never reaches our machines
  - honesty: A direct request to an origin hostname without the Worker's credential is refused before reaching the lens
- spark2 is provisioned with irc-lens config, cloudflared, its own secrets and a running IRCd, using the supported install verbs
  - honesty: culture console install and culture server install produce running units on spark2, reachable only through its tunnel
- The service sends periodic pings to the external health check service using the grant secret `CULTURE_HEALTHCHECK_URL`, so an outage alerts even when our machines cannot send anything
  - honesty: Pings are sent only after a real IRCd readiness check passes, so a healthy landing page with a dead IRCd stops the pings
  - honesty: The check also alerts when the pinger's host is off, because silence alone turns it down
- The owner receives an hourly email while any health check is down, until it recovers
  - honesty: With the check down for over an hour, at least 2 reminder emails arrive at the owner's address
- After a reboot the standby rejoins the mesh without a person unlocking its keyring, because a locked keyring leaves spark2 with no link credentials
  - honesty: After a spark2 reboot with no human action, it links to at least one peer
- spark2's standby lens holds a copy of accounts and permissions from spark's guests.db, one-way and refreshed on a schedule, because an empty store would refuse every approved person including the admin
  - honesty: After a copy, an approved person can sign in on spark2 and holds the same role and grants
- The health ping ships as part of the culture app: a CLI verb installs the timer on any host, reads the check URL from grant and the site address from the lens config, replacing the hand-written script and units on spark
  - honesty: On a host with only the culture install and the grant secret, the CLI verb alone produces a timer that pings the external health check service, reports /fail when /readyz fails, and spark runs on it with the hand-written files removed
- Browser POSTs work through the Worker: `_origin_ok` compares Origin to the request host, so a Worker fetching an origin hostname with a different Host gets every browser POST refused
  - honesty: A real browser signs in and sends a message through the Worker, on both origins
- Health pings check the primary's own local /readyz as well as the public one, so a failover can't hide that spark is down: after failover the public /readyz would answer 200 from spark2
  - honesty: With spark's IRCd stopped and traffic on spark2, the check goes down and hourly reminders arrive
- Failover trips only after several consecutive failed /readyz checks, and returning to spark is a deliberate step, so a routine restart can't bounce everyone onto a different nick
  - honesty: Restarting the console or IRCd during a deploy moves no one to spark2
- The Worker can be removed in one step: deleting its route restores direct access through the existing tunnel, and that path stays available until the Worker has proven itself
  - honesty: A rehearsed rollback restores signed-in service within 5 minutes
- The guests.db copy to spark2 stays private: it holds emails, password and token hashes, so it travels only over the tailnet or LAN, stays mode 0600 on spark2, and has the same retention as the original
  - honesty: After a copy, the file on spark2 is mode 0600 and no copy path crosses the public internet

## Honesty conditions

- A simulated spark failure with Worker and spark2 in place returns the app, not a 500, to a signed-in test account
- The dates and durations match the journal: first refusal Nov 2 00:44, restored Nov 3 17:58
- Two requests from one session always reach the same instance while that instance is healthy
- spark and spark2 connect through different tunnel UUIDs
- Every behaviour in this spec is observable by one of these two groups
- Each clause is exercised in the failover drill: sign-in again, spark2 nick, outage page, email
- Both timings are measured in a drill, not estimated

## Success signals

- An IRCd outage behind a healthy landing page alerts the owner within 10 minutes, against about 41 hours unnoticed on 2026-11-03; a simulated spark failure serves signed-in people from spark2 within 5 minutes

## Scope / boundaries

- No per-request round-robin between instances: one person's session stays on one instance until that instance fails
- Tunnel replicas are not the fallback mechanism: each host gets its own tunnel and the Worker chooses between them

## Non-goals

- This does not survive a home power or internet outage on the machines; only the Worker's static outage page does

## Assumptions

- A single source of truth for sign-in, permissions and agent credentials must exist across both instances; separate guests.db files would sign people out on failover
- On failover a person's nick changes from `spark-<nick>` to `spark2-<nick>` unless the design changes that
- spark2 needs no guest sandbox or model path, because guest mode is off on standby; a second sbx would only be needed if guests are later served there
- Moved history is partial: a person failing over may see gaps because history is per-server and backfill replays only recent in-memory events
- During failover people see rooms but no spark agents, media uploaded on spark is missing, and personal agent credentials issued for spark- nicks don't work; standby means keeping humans talking, not full service
- Rate limits keep working through the Worker because same-zone subrequests keep the visitor's IP in CF-Connecting-IP, per Cloudflare docs; if the origin isn't same-zone they collapse to one IP and one visitor could lock out all sign-ins

## Scope exploration

- `s1` — `spark journal + console log, 2026-11-03`: stale pidfile server-spark.pid held a PID reused as a thread of another user's process (xdg-document-portal); sbx and spark2 the same
  - seeds: `c2`
- `s2` — `culture_core/cli/server.py:564 + pidfile.py:100`: `_check_already_running` calls `is_process_alive` only; kill(pid,0) PermissionError counts as alive; `is_culture_process` exists at pidfile.py:75 but is not called. No open issue for this; overview/`renderer_web.py`:139-154 can signal an unrelated PID
  - seeds: `c3`
- `s3` — `irc-lens web/routes.py:1079-1088, session.py:429-446`: /healthz is a constant ok with no IRC check; connect() has one host, no retry; auto-reconnect is deliberately off, dead sessions return 503 on input
  - seeds: `c4`
- `s4` — `irc-lens web/sessions.py:195-205, web/routes.py:746-756,975-980`: IRC sessions and SSE buses are in-memory per process, so a send on A never reaches a stream on B; affinity is assumed, unstated, and no stickiness code exists
  - seeds: `c5`
- `s5` — `Cloudflare tunnel docs + tunnel API GET`: chat tunnel has one connector today; docs say replicas give no steering or stickiness and route to the closest one, and session affinity needs a separate tunnel per host
  - seeds: `c6`
- `s6` — `the site's Cloudflare zone + account via CF API GET`: Free Website plan; Load Balancing API 403; existing Workers learn-api and lens-delivery-alert, 2 KV, 1 D1, no routes; docs: 10ms CPU, 100k req/day then error 1027, no wall-clock limit while client connected
  - seeds: `c7`
- `s7` — `lens config auth.mode + web/auth.py:399`: auth.mode is cloudflare-access with `app_signin` enabled; no Worker-trust mode exists. Worker fetch through Access service token on a tunnel hostname is unverified and needs a spike
  - seeds: `c8`
- `s8` — `irc-lens access_store.py:236-267, app_session.py:84-103,190-213, guest_store.py:60-101`: sessions, users, grants, `agent_credentials`, guests, consents all in one local rollback-journal SQLite file with a per-process read cache; unsafe over NFS; cookie is an opaque id so no key to share, but the row must exist
  - seeds: `c9`
- `s9` — `agentirc client.py:469-476, irc-lens web/nickname.py:67-69, agent_nick.py, access_store.py:64-66`: NICK must start with the server name, hard-coded with no config switch; lens prefixes from server.name; grants key on email and survive, `agent_credentials` store the full prefixed nick and would not; no identity links spark-ori to spark2-ori
  - seeds: `c10`
- `s10` — `docs/guest-sandbox.md:12-38 + ~/.culture/sandbox/*.yaml`: sbx is loopback, standalone, no-persist so a second copy is cheap but relocation of its agents is undocumented; both agents use gateway localhost:8001 on spark; models cortex-spark2 and worker-spark span both hosts
  - seeds: `c11`
- `s11` — `spark2 over ssh; docs/durable-mesh.md:21-26, culture_core/cli/console.py:696`: spark2 has culture 14.11.1 (spark 14.13.0), grant, no cloudflared, no ~/.config/irc-lens; culture console install exists; server crash-looping on the stale pidfile
  - seeds: `c12`
- `s12` — `ip route on spark and spark2`: both hosts use the same home router as default gateway, so they share a LAN and uplink; correction: an older note that spark2's gateway is spark is stale
  - seeds: `c13`
- `s13` — `docs/reference/server/architecture.md:120,166,181-183, agentirc ircd.py:59, server_link.py:975-1008`: HistoryStore is local SQLite; event log is a 10000-entry in-memory deque; whether remote-origin messages land in local history.db is unknown
  - seeds: `c14`
- `s14` — `grant on spark and spark2`: `CULTURE_HEALTHCHECK_URL` is set on spark (https, host the check service's ping host, value not printed); spark2's grant has no such secret, so a ping from spark2 needs it copied or a separate check URL
- `s15` — `culture-server-spark2 on spark2`: fixed 2026-11-03 16:18 UTC by moving the stale pidfile aside after confirming its PID was a thread of another user's process and 6667 was not listening; server now listens on spark2's tailnet address
- `s16` — `mesh links on spark, thor, orin, spark2`: 2026-11-03 18:2x: all peer ports reachable from spark, but no established server-to-server sockets on spark, thor or orin; spark logged Linking to thor/orin once with no follow-up, thor/orin logged nothing; spark2 login keyring Locked=true, logging No credential found for all peers. Cause on spark side unconfirmed; matches culture#510 no-retry shape
- `s17` — `spark restart 18:22 and agentirc ircd.py:521-532`: restart logged Linking to all three peers including spark2, yet zero peer sockets on spark, thor or orin and nothing in the thor/orin server logs; `connect_to_peer` spawns link.handle as a background task, so a failed handshake may be silent. Restart did not restore federation; cause unconfirmed
- `s18` — `irc-lens web/app_session.py:100-103, access_store.py:59-78`: with gate-only sign-in on the lens, spark2 opens its own guests.db; unknown session ids mean signed out and an empty users table means no approved people, so failover needs at least a copied store. This follows from the owner's gate-first decision and was not explicit in the options offered
  - seeds: `c25`
- `s19` — `Worker spike, Cloudflare's Worker test domain to the public chat host tunnel, 2026-11-03`: PASSED: a throwaway Worker reached the tunnel hostname with an Access service token (login 303 with session cookie) and held GET /events open as text/event-stream for the full 300s, 20 chunks, on the Free plan, with no CPU-limit error; Worker deleted afterwards. NOT tested: a Worker route on the public chat host itself, Access in front of that same hostname, a second tunnel, failover between origins, or streams longer than 5 minutes
- `s20` — `spark ~/.culture/healthcheck-ping.sh and culture-healthcheck units`: the working ping is hand-installed on spark, hardcodes the public chat host, and isn't in any repo; the owner decided it belongs in the app. `culture_core`/cli/console.py already has an install verb, so this is the likely home, to confirm while planning
  - seeds: `c27`
- `s21` — `challenge pass / adjacent-systems lens: irc-lens web/routes.py _origin_ok, web/csrf.py`: Origin must equal the request host. Cloudflare docs: changing the fetch URL hostname changes Host, while cf.resolveOverride keeps Host but needs both hosts in the zone. The spike only sent GETs, so this was untested and every send would have returned 403. Whether the tunnel ingress and resolveOverride combine on this plan is unknown
  - seeds: `c28`
- `s22` — `challenge pass / observability lens: ~/.culture/healthcheck-ping.sh`: the ping script probes the public URL only. It is correct today, but once the Worker fails over, a dead spark IRCd with a live spark host would keep reporting healthy, which silently defeats the owner's hourly email during standby
  - seeds: `c29`
- `s23` — `challenge pass / failure-mode lens: this session's deploy`: restarting the IRCd and console took seconds, and the spec states no threshold or failback rule, so a plain deploy could trigger failover and change everyone's nick
  - seeds: `c30`
- `s24` — `challenge pass / reversibility lens: spec c8 and tunnel config`: c8 locks origins to the Worker's credential, but nothing states how to undo it. Worker bugs, Free-plan error 1027 or an expired service token would be a full outage with no way around
  - seeds: `c31`
- `s25` — `challenge pass / security lens: irc-lens guest_store.py, access_store.py`: the store holds consents, password hashes, token hashes and audit rows, and c25 says only that it is copied
  - seeds: `c32`
- `s26` — `challenge pass / overlooked-actors lens: systemd units on spark, web/store.py, access_store.py:64-66`: all 4 mesh agents and the media directory live only on spark, and `agent_credentials` store the full prefixed nick. Cross-server rooms also need #519 fixed, since mesh links are down today
  - seeds: `c33`
- `s27` — `challenge pass / security lens: irc-lens web/entry.py:291,426`: `client_ip` reads only CF-Connecting-IP and drives sign-in, guest and deletion limits. The documented same-zone behaviour is untested here, as the spike ran from Cloudflare's Worker test domain
  - seeds: `c34`
- `s28` — `challenge pass / adjacent-systems lens: spark2 gateway`: spark2 has nothing on port 8001, and both sandbox agents use gateway localhost:8001, confirming c11: guests on spark2 have no model path until that's built
- `s29` — `challenge pass / operations lens: Worker quota`: clean for now: the console logged 71 requests in 24h against 100k per day. Residual: each request counts, and Worker service token expiry needs an owner
- `s30` — `challenge pass / security lens: cookies and Turnstile`: clean: cookies are Secure and host-only and passed through in the spike. Turnstile wasn't exercised, and its behaviour through a Worker is unexamined
- `s31` — `challenge pass / migration lens: guests.db schema`: tables use additive CREATE IF NOT EXISTS migrations, so a copy from a newer spark to an older spark2 lens is unexamined: both hosts need matching versions, to be a plan risk

## Decisions

- Owner decision 2026-11-03: warm standby on spark2 is enough; active-active sharing of live load is not wanted
- Owner decision 2026-11-03: a person's nick may change from `spark-<nick>` to `spark2-<nick>` during failover
- Owner decision 2026-11-03: hourly email only while a check is down, using the the external health check service reminder setting; silence means healthy; no always-on status mail
- Owner decision 2026-11-03: the Worker is gate-only first (routing, failover, outage page, keeping anonymous traffic off the origin); sign-in stays on the lens; identity in D1 is a later separate phase
- Owner decision 2026-11-03: /readyz covers the main IRC server only, so a guest sandbox failure never triggers failover for everyone; a separate sbx signal can come later
- Owner decision 2026-11-03: standby is approved users only, with no account changes and guest mode off. Guests get the outage message during a failover, and spark2 needs no guest sandbox or model path

## Open parks

- [unknown_nonblocking] Whether remote-origin messages are written to the local history.db (agentirc), which decides how complete history looks after failover
- [unknown_nonblocking] Whether moving email and consent records to Cloudflare D1 requires a Terms/Privacy update (katvan legal pages not read)
- [unknown_nonblocking] Why spark has no established links to thor and orin after its restart (culture#510 suspected, unconfirmed)
- [unknown_nonblocking] Whether cf.resolveOverride keeps the Host header through the tunnel on this plan, or the lens must instead trust a configured public origin; needs a small POST spike before building

## Resolved vagueness

- [unknown_blocking] Whether Worker subrequests can reach tunnel hostnames behind an Access service token and hold SSE through the 10ms CPU limit; needs a spike — resolved: Owner resolved 2026-11-03: the spike proved a Free-plan Worker can reach the tunnel through an Access service token and hold SSE for 5 minutes (scope s19). Route on the public chat host, Access on the same hostname, second tunnel, failover and longer streams become plan acceptance tests.
