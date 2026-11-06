# Harden public sign-in

> The chat console's public sign-in can't be used to burn the mail quota, mail strangers or raise false alarms, and it warns when its bot check is off

## Audience

- The operators of a public irc-lens console, who get the delivery alerts and pay for the mail quota, and the guests and approved users who rely on sign-in mail arriving.

## Before → After

- Before: On 2026-11-04 a scripted client submitted the sign-in form with a reserved address. The console called the mail provider, the send failed, and operators got an alert saying codes weren't being delivered, while delivery was fine. Nothing logged the status.
- After: Scripted traffic can't spend more guest mail than the budget, a bad address raises no alert, every failed send logs its HTTP status, and a console with no bot check says so at startup and in doctor.

## Why it matters

- Guest mail is the one thing an anonymous visitor can make the console spend: enough rotating addresses can use up the quota, and that also stops approved users' sign-in codes. False alarms teach operators to ignore the alert that matters.

## Requirements

- A persistent global budget caps guest code mail per hour and per day, counted before the send is scheduled because sends run in background tasks (web/entry.py:552,569), covering the guest-code paths at entry.py:668 and :989. It reuses the attempts table helpers (`guest_store.py`:565-592); retention there is one day (`ATTEMPT_RETENTION_S`), so a daily window needs margin added.
  - honesty: The budget survives a restart, is counted before the send is scheduled, and its daily window can't lapse early from attempts-table retention.
- Reserved domains are refused before any provider call, keeping the same status, page, pending cookie, pad floor, attempt counting and dummy verify as a normal submission (entry.py:653-678), so a refusal reveals nothing.
  - honesty: Status, page, cookie and pad floor match a normal guest submission, and the attempt is still counted, shown by a test comparing the two responses.
- Provider failures are classified so recipient errors don't alert, each failure logs its HTTP status but never the address or body, and failed sends are counted in metrics. classify (alerts.py:48) separates only 429 today; MailSendError.status exists; failures log the type alone (entry.py:549,571).
  - honesty: No log line or alert contains an address, code or provider body, and a real outage's alert isn't suppressed by an earlier recipient error.
- With guest mode on and no bot check configured, the console warns at startup and in doctor, and a setting can refuse guest codes in that state. `make_bot_verifier` silently returns NoopVerifier (entry.py:193-200), and doctor (cli/`_meta.py`:317) has no such check.
  - honesty: With guest mode on and keys absent, startup logs one warning and doctor reports it; with keys set, neither does; and approved sign-in works in every mode.
- Clef is a layer in the console's threat detection, across public traffic and not only sign-in: fixed rules watch counts of sign-in activity, mail spend, probed paths, 401 and 403 responses, and when a window crosses a trigger the console sends the decider (Clef, general domain) counts only and raises an operator alert with its threat verdict and the numbers. Clef detects and never enforces; the bot check, limits and budget do the blocking.
  - honesty: Sign-in never waits on or fails because of the decider: with the decider down, sign-in is unchanged and the numbers still reach the operator, marked as not assessed.
  - honesty: Verdicts are checked against labelled windows, including a real group of guests behind one IP, before the alert is trusted, and its misses are recorded.
  - honesty: A budget hit or a burst always alerts, whatever the decider says, so a wrong 'genuine' can't hide abuse.
  - honesty: The threat detector's decider call receives derived counts only, never addresses, IPs, user agents, paths or any other visitor-supplied string; only the message validator in c30 sends a guest's line.
- All outgoing mail has a total ceiling below the provider quota, and set-password and delete-confirm mail get an hourly per-address limit. Today web/setpw.py `post_password` and web/deletion.py `post_request` allow 10 mails per minute per address with no bot check, to anyone who knows an approved or guest address.
  - honesty: With guest budget unused, a test driving set-password or delete requests for one known address can't exceed its hourly limit, and total sends can't exceed the ceiling.
- The guest budget gets an hourly per-IP cap beside it, and exhausting the budget is treated as a denial of guest sign-in, not only as protection: 20 scripted posts an hour would stop real guests, who still see the normal code page because entry.py keeps responses identical.
  - honesty: A test shows one IP can't use the whole hourly budget, and the budget-hit alert says real guests may be getting no mail.
- Operators can lift or reset the budget and turn off required bot check through config, with no code change, and the rollback steps are documented.
  - honesty: A rehearsal in a test environment restores guest mail with a config change and a restart.
- Clef detection is optional and off by default: decider URL and credential come from config and an env var, and a deployment with no gateway, such as a standby host or an outside user of irc-lens, runs every other layer unchanged.
  - honesty: With no decider configured, startup is clean, rules still alert, and alerts say not assessed.
- Probe counting uses route matching in middleware, not status codes: anonymous requests to real and nonexistent paths both get 401, because auth runs first (auth.py:97 reads `match_info`), so status can't separate a probe from a mistake.
  - honesty: A test shows a nonexistent path counts as a probe and a real route's 401 doesn't.
- The message validator is a second Clef layer beside the counts-only detector in c18: it sends one guest line per call, takes its prompt and question from a config file outside the repo that the operator can change without a release, and turns the answer into allow or block only.
  - honesty: With the validator configured, a guest line reaches the sandbox server only after an allow verdict, a decider error, or the 5 second timeout; a blocked line never reaches it.
  - honesty: The prompt and question live only in the operator's config file: no prompt text is in the repo, the exported spec, or the logs, and editing the file changes the next call with no restart.
  - honesty: A line that tells Clef to allow it, or asks for prose, still yields only allow or block, because the request is a choice question with fixed keys; tested with injection lines.
  - honesty: Before blocking goes live, at least 40 labelled guest lines (benign, abusive, and injection attempts) are run through the configured prompt; benign lines blocked and abusive lines allowed are recorded, and the owner accepts the rates.
  - honesty: Load stays bounded: one call per guest line, lines already capped by the guest message rate and guest limit, one call in flight per guest, a 5 second timeout, and a daily call cap in config after which lines go through marked unchecked.
  - honesty: Blocked lines are kept with the guest's flags and erased by guest deletion; alerts carry counts only.
- Red-team audit RT-01: a single address can't hold guest seats with pending reservations: a per-IP cap on open reservations, with the guest limit and message rate shown beside the other published limits (RT-08).
  - honesty: A test opening reservations from one address past the cap gets the busy response, while a second address still gets a seat.
- Red-team audit RT-14: any failure while opening or reopening a guest session answers with the shaped 503 warming-up or busy response, never a bare 500.
  - honesty: A test that makes session opening raise an unexpected error gets a shaped 503, not a 500, and the error is logged.

## Honesty conditions

- Every behaviour in c10 is shown by automated tests against a real guest store, with only the mail adapter faked.
- No approved-mail path checks or increments the guest budget, and a test proves it with the budget exhausted.
- The approved sign-in code path has no behaviour change apart from budget exemption, shown by the existing sign-in tests passing unchanged.
- Operators can read the new warning, status logs and counters without reading guests.db.
- Each clause maps to at least one test that fails on main today.
- The scenario replays from the same input in a test: a reserved address no longer reaches the adapter or raises an alert.
- Approved mail really is exempt: with the budget used up, the approved sign-in path still calls the adapter.
- The test uses the real limit and budget code with a fake clock, not a mocked limiter.
- A test driving 10000 requests in a window with a fake clock and fake decider sees at most 1 Clef call, a fixed-size counter state, and no added database writes on the request path.
- With the decider hanging, request latency is unchanged and the event loop isn't blocked, because the call runs off the request path with the timeout enforced.
- Thresholds are re-checked on real counters before alerts are trusted, since 7 days is a short sample.
- Each excluded audit item is either an owner step listed in the rollout task or a follow-up issue filed when this plan is approved.

## Success signals

- In a test driving 200 guest submissions with rotating addresses from one IP, no more than the configured hourly budget reach the mail adapter, 0 reserved-domain addresses reach it, 0 recipient errors raise an alert, and an approved user's sign-in code still sends with the budget at 0.

## Scope / boundaries

- Approved users' sign-in code, new-browser notice and set-password mail stay outside the budget (entry.py:663,732; setpw.py:196-205), so guests can never lock an owner out of signing in.
- No change to the approved sign-in flow or its alerts; irc-lens#69 owns those.
- Threat detection stays cheap and can't be driven by attackers. Counting per request is in memory, with no database write or network call. The detector's Clef call runs at most once per 15-minute window and at most 24 times a day, only after a rule triggers, with one call in flight and a 5 second timeout. The message validator in c30 has its own bounds.
- Not in this frame, from the red-team audit: HSTS and Cloudflare Access settings (owner's Cloudflare changes), the 0.0.0.0 IRC bind on thor (ops), webhook secret and read-loop bound (agentirc), per-principal media eviction and MCP resource metadata (later lens hygiene). Dependency currency (RT-02) was already met on spark on 2026-11-06.

## Non-goals

- No IP bans or blocklists: carrier NAT makes them blunt, per irc-lens#59.

## Assumptions

- Default guest budget is 50 mails a day and 20 an hour: half the average daily allowance of the operator's monthly plan at the time. Each deployment sets its own values in config, and live values stay out of the repo.
- Starting triggers from 7 days of spark's access log: entry posts peak at 5 per 15 minutes, refused responses at 311 with only 2 windows at 20 or more, and all requests have p99 29. So refused responses at 50 or more plus entry posts at 10 or more, plus any reserved address or failed send, would have raised few alerts. The Nov 4 probe had 6 posts, so only the reserved-address rule catches it.

## Scope exploration

- `s1` — `web/entry.py + guest_store.py mail paths`: Guest mail is limited only per email and per IP at 10 per 60 s (`TOKEN_REQUEST_WINDOW_S`). No global cap exists. Attempts rows are kept a day and are attacker-keyed, so unbounded row growth is slow but real.
  - seeds: `c2`
- `s2` — `web/setpw.py and approved sign-in`: Approved-only mail shares the token counter at entry.py:633, so a budget must not count approved emails there.
  - seeds: `c3`
- `s3` — `_plausible_email and its callers`: Needs @, local part, dot in domain, no space or colon. Also used by deletion.py:151 and setpw.py:196, which need a known account, and by admin.py:184, where an operator adds a person: the reserved-domain check belongs in the guest paths at entry.py:658 and :989, not inside this shared helper.
  - seeds: `c4`
- `s4` — `alerts.py, mail.py, cultureflare delivery-alert template`: The template doesn't allow-list kinds, so a new kind won't break it; whether the deployed Worker matches wasn't checked. Alert text says codes are undeliverable even when a notice or delete mail failed. Throttle state is in memory and resets on restart.
  - seeds: `c5`
- `s5` — `Turnstile wiring`: Env `IRC_LENS_TURNSTILE_SITE_KEY` and `_SECRET`; the widget renders only on password and guest steps. /delete/request and /password never call the verifier (deletion.py, setpw.py).
  - seeds: `c6`
- `s6` — `issue irc-lens#59`: Shared NAT is a known cost of per-IP limits, so the global budget does the heavy lifting.
  - seeds: `c7`
- `s7` — `issue irc-lens#69`: Covers approved sign-in alerts and blocks; not repeated here.
  - seeds: `c8`
- `s8` — `edge Worker (t8, unmerged)`: Forwards every path to the active origin with no filter, so path blocks and /entry limits fit in handleRequest later. The lens reads neither edge header yet; t6 adds that. `client_ip` is defined twice (entry.py:291, auth.py:102), which t6 must keep in step. Turnstile through the Worker is unverified.
- `s9` — `access log, metrics, privacy`: aiohttp default access log with no format set (serve.py:480); visitor IP needs a custom logger. Metrics live at /owner/metrics, approved only, and reset on restart. The privacy text is on the public site, outside this repo, and wasn't read.
- `s10` — `challenge pass / adjacent-systems lens: web/setpw.py, web/deletion.py`: These paths bypass c2 and c3 exempts set-password, so a known address can drain the quota and flood that inbox. c17's claim of 1500 left for approved users doesn't hold until this is fixed.
  - seeds: `c20`
- `s11` — `challenge pass / failure-modes lens: web/entry.py response path`: The budget itself is a cheap denial lever, and over-budget guests are told a code is coming.
  - seeds: `c21`
- `s12` — `challenge pass / recovery lens: guest_store.py attempts, web/retention.py`: No reset path exists for a persistent budget; attempts rows otherwise age out over a day.
  - seeds: `c22`
- `s13` — `challenge pass / adjacent-systems lens: assess script, lobes gateway`: The decider is reached through a local gateway with its own key, which irc-lens doesn't depend on today.
  - seeds: `c23`
- `s14` — `challenge pass / cheap probe: 7 days of access log, counts only`: Measured baseline per 15 minutes; no addresses or IPs read.
  - seeds: `c24`
- `s15` — `challenge pass / unstated-assumptions lens: web/auth.py and access log`: The Nov 4 log shows a made-up path returning 401, the same as real routes.
  - seeds: `c25`
- `s16` — `challenge pass / failure-modes lens: cultureflare delivery-alert template`: Clean: alerts use Cloudflare's `send_email` binding, not Resend, so a used-up quota doesn't silence them. The template was read; the deployed Worker wasn't.
- `s17` — `challenge pass / lifecycle lens: web/standby.py`: Clean: standby already refuses guest and account paths. Budget and counters are per host and not replicated; each host alerts on its own.
- `s18` — `challenge pass / security lens: web/entry.py verify and approved sign-in`: correct = human and `pw_ok`, and TurnstileVerifier fails closed, so with keys set an approved user can't sign in during a Cloudflare siteverify outage. That conflicts with h11. Routed to a user decision.

## Decisions

- Guest mail budget: the daily cap is a fraction of the Resend plan's quota and the hourly cap is 40% of the daily, fixed only once the quota is read.
- Bot check: warn now, set keys, then block on a missing check only after a real browser test passes through the Worker.
- No visitor IPs in the access log.
- With Turnstile on, already-trusted approved browsers skip the verify, so approved access survives a Cloudflare outage. New browsers stay blocked until verify recovers.
- Owner decision 2026-11-06: Clef validates each guest chat line before the demo agents see it. Approved users' lines and mesh lines are never sent.
- Owner decision 2026-11-06: a line Clef rejects is blocked and flagged: it never reaches the agents, the guest sees a short refusal, and it is logged and counted for the alert rules.
- Owner decision 2026-11-06: when Clef is down or takes longer than 5 seconds, the line goes through marked unchecked, so the demo keeps working.

## Open parks

- [unknown_nonblocking] HTTP 403 can mean our sender config or Cloudflare's agent block (mail.py `USER_AGENT` comment), so 403 can't be classified on status alone
- [unknown_nonblocking] Whether Turnstile works through the edge Worker: siteverify gets `client_ip`, and CF-Connecting-IP across the Worker-to-tunnel hop is unverified
- [unknown_nonblocking] The Resend plan's daily and monthly quota, and the fraction to use, are still to come from the owner; the build can't fix budget defaults without them
- [unknown_nonblocking] The measured load of a Clef call on orin's shared lane and whether 24 calls a day needs lowering; two calls here took about 2.5 s each but load wasn't measured
- [unknown_nonblocking] Bounces from rotating addresses may hurt the sending reputation even under budget; src has no bounce or complaint handling, and Resend's policy wasn't read
