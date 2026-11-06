# Build Plan — Harden public sign-in

slug: `harden-public-sign-in` · status: `exported` · from frame: `harden-public-sign-in`

> The chat console's public sign-in can't be used to burn the mail quota, mail strangers or raise false alarms, and it warns when its bot check is off

## Tasks

### t1 — Make delivery alerts mean an outage

- instruction: Own src/`irc_lens`/alerts.py, mail.py and tests/`test_alerts.py`. A 403 can be a sender problem or Cloudflare's agent block, so keep it alerting as its own kind and say both in the text. Update the alert wording: it can fire for notice, set-password and delete mail too. The receiving Worker doesn't allow-list kinds.
- covers: c5, h10
- acceptance:
  - A 422 or similar recipient error raises no alert and doesn't take the hourly slot; 401, 403, 429, 5xx and network failures alert under classified kinds
  - A failed send logs its HTTP status and never the address, code or provider body
  - Failed sends are counted in /owner/metrics

### t2 — Refuse reserved addresses in the guest paths

- instruction: Check at web/entry.py:658 and :989, not inside `_plausible_email`, which admin.py and others share. Keep the dummy verify, attempt counting, pending cookie and pad floor.
- covers: c4, h9, c11, h4
- acceptance:
  - example.com, example.org, example.net, .invalid, .test and .localhost never reach the mail adapter
  - Status, body apart from the echoed address, cookie presence and timing floor match a normal guest submission
  - Replaying the Nov 4 submission calls no adapter and raises no alert

### t3 — Add the persistent mail budget

- instruction: New src/`irc_lens`/`mail_budget.py` using `guest_store` attempts helpers, with config keys in config.py and docs/guest-mode-config.md. Count before the send task is scheduled, since sends run in the background. Attempts retention is exactly one day, so give the daily window margin. Keep sqlite calls off the event loop (see #72). Defaults 50 a day and 20 an hour; live values stay in private config.
- depends on: t2
- covers: c2, h7, c3, h8, c21, h21, c12, h5
- acceptance:
  - Past the hourly or daily budget no guest code reaches the adapter, and the count survives a restart
  - One IP alone can't use the whole hourly budget, while other IPs still get codes
  - With the guest budget at 0, an approved sign-in code still sends
  - Setting the budget in config to lift it restores guest mail after a restart, and the steps are documented

### t4 — Limit set-password and delete mail, and cap total sends

- instruction: Own web/setpw.py and web/deletion.py, and add the total ceiling at the shared send seam in mail.py `send_with_alert`, so every path is counted. Approved sign-in codes pass only the total ceiling, which sits below the provider quota.
- depends on: t3
- covers: c20, h20
- acceptance:
  - Repeat set-password or delete requests for one address stop reaching the adapter at the hourly per-address limit, with the page unchanged
  - Total sends never exceed the ceiling, whichever path sent them

### t5 — Warn on a missing bot check, and keep trusted browsers working

- instruction: Own `make_bot_verifier` in web/entry.py, serve startup, and a doctor check. `require_bot_check` defaults off in this task's first release; turning it on is a rollout step. Trusted browsers skip verify only for approved sign-in, per decision c26. Read `_post_signin_app` closely: correct = human and `pw_ok` today.
- depends on: t3
- covers: c6, h11, c8, h12
- acceptance:
  - Guest mode on with no keys gives one startup warning and a doctor finding; with keys set, neither
  - With verify failing, a trusted approved browser signs in and an untrusted one doesn't
  - With `require_bot_check` on and no keys, guest codes are refused and approved sign-in works
  - Existing approved sign-in tests pass unchanged

### t6 — Count threat signals cheaply on the request path

- instruction: New src/`irc_lens`/threat.py and one middleware. Use `match_info` to tell an unrouted path from a refused real route: anonymous requests get 401 either way. Counters only, fixed size, with distinct-source counts capped and hashed in memory, reset each 15-minute window. Reading user agents to classify is fine; no raw strings are kept.
- covers: c25, h25, c19, h18
- acceptance:
  - An unrouted path counts as a probe, and a 401 on a real route doesn't
  - 10000 requests in a window leave counter state at a fixed size, with no database write or network call on the request path

### t7 — Alert from fixed rules

- instruction: New rules module beside threat.py. Starting triggers: refused responses at 50 or more, entry posts at 10 or more, any reserved address, any failed send, any budget hit. All are config values. Urgency comes from the rules, not Clef. Check thresholds against counters again before trusting them.
- depends on: t1, t3, t6
- covers: h17
- acceptance:
  - A window crossing any trigger raises one alert with its counts, within the existing hourly limit per kind
  - A budget-hit alert says real guests may be getting no mail
  - A lone guest's normal window raises nothing

### t8 — Add Clef as an optional, bounded verdict

- instruction: New src/`irc_lens`/decider.py, off by default, with URL in config and credential from an env var. Runs off the request path. At most 1 call per window and 24 a day, one in flight, 5 second timeout, no retries or queue. Build the request from counters so that no visitor string can reach it. The verdict is labelled advisory.
- depends on: t7
- covers: c18, h14, h15, h19, c23, h23, h35
- acceptance:
  - The request body holds derived counts only, enforced by a test on the built request
  - With the decider down or hanging, sign-in latency is unchanged and the alert goes out marked not assessed
  - With no decider configured, startup is clean and all other layers work
  - The call caps hold in a test with a fake clock and fake decider

### t9 — Test Clef on labelled windows before trusting its verdict

- instruction: A script over a labelled set of counts: real windows from counters once collected, plus made-up ones, including a real group behind one IP and a background scanner. Keep the windows held out from any prompt tuning. Measure the decider host's load per call and lower the 24-a-day default if needed. Record misses. The advisory label stays until the owner lifts it.
- depends on: t8
- covers: h16
- acceptance:
  - Results list each window, the label, the verdict and the misses, with the count of real versus made-up windows
  - No mail-abuse window is missed before the advisory label can be lifted
  - Load per call on the decider host is measured and recorded

### t12 — Cap pending guest seats per address and publish the guest limits

- instruction: Own the reservation path: `try_reserve_guest` in web/sessions.py and its caller in web/entry.py, using the client address entry.py already resolves. The cap is a config value, default 1 pending reservation per address. Show the guest limit, the message rate and the new cap in docs/guest-mode-config.md beside the other published limits.
- depends on: t5
- covers: c31, h32
- acceptance:
  - A test opening reservations from one address past the cap gets the busy response while a second address still gets a seat
  - docs/guest-mode-config.md lists guest limit, message rate and the per-address reservation cap

### t13 — Answer every guest session-open failure with a shaped 503

- instruction: Own the `_resolve_session` call sites in web/routes.py: `get_index`, `post_input`, `get_presence` and the event stream. Catch unexpected exceptions after the existing GuestBusy, GuestSignedOff and ConnectionError handlers, log them with logger.exception, and return the warming-up 503 (page for GET /, JSON with HX-Redirect for input). Keep mesh sessions' current behavior except that a bare 500 becomes a shaped 503.
- covers: c32, h33
- acceptance:
  - A test that makes session opening raise an unexpected error gets a shaped 503 for GET / and POST /input, not a 500
  - The unexpected error is logged with its traceback

### t14 — File the audit items this plan leaves out

- instruction: When the owner approves the split: open follow-up issues for the agentirc webhook secret and read-loop bound, lens per-principal media eviction and MCP resource metadata, and the thor 0.0.0.0 bind. List HSTS and the Cloudflare Access settings as owner steps in t11. Keep live configuration details out of public issue text.
- covers: c33, h34
- acceptance:
  - Every audit item named in c33 has a linked issue or an owner step in t11

### t15 — Validate guest chat lines with Clef before the demo agents see them

- instruction: New src/`irc_lens`/web/`message_check.py`, hooked into `post_input` for guest principals on the sandbox backend only, after the existing policy checks and before session.execute. Reuse t8's decider HTTP client. Config under `guest_mode`.sandbox.validator: url, credential env var name, `prompt_file`, timeout 5 s, `daily_cap`, and mode flag or block (default flag until t16 is accepted). The prompt file is YAML holding the state preamble, question and criteria; read it on change, never log its text, and keep it out of the repo and docs (document its shape with a placeholder only). Send a choice question whose keys are allow and block; anything else counts as an error. On block: refuse with a short message, count it in t6's counters, and store the line with `guest_store`.`record_input` as kind blocked so `delete_guest_inputs` erases it. On error, timeout or cap: send the line and count it unchecked.
- depends on: t8, t7, t13
- covers: c30, h26, h27, h28, h30, h31
- acceptance:
  - A blocked line never reaches the sandbox IRC server, tested against a real test server
  - With the decider hanging, the line goes through after 5 s marked unchecked and the event loop stays responsive
  - Injection lines asking Clef to allow or to answer in prose still map only to allow or block
  - Approved users' lines and mesh lines never reach the decider
  - No prompt text appears in logs, alerts or test fixtures; editing the prompt file changes the next request with no restart
  - Deleting the guest erases their blocked lines; alerts carry counts only
  - Past `daily_cap`, lines go through unchecked and the cap hit is counted

### t16 — Measure the message validator on labelled lines before it blocks

- instruction: A script under scripts/ that runs a labelled line set through the configured validator and prints false blocks and misses per label. Keep the labelled set outside the repo next to the prompt file, since it holds abusive text: at least 40 lines across benign, abusive and injection attempts, held out from prompt tuning. Record the rates in the delivery evidence and put them to the owner. Blocking turns on only after the owner accepts them.
- depends on: t15
- covers: h29
- acceptance:
  - The script reports false blocks and misses for a set of at least 40 labelled lines with three labels
  - The validator stays in flag mode until the owner has accepted the measured rates

### t10 — Prove the whole stack end to end, and document it

- instruction: A real guest store with only the mail adapter and decider faked. Write docs/security-checklist.md and guest-mode-config.md for operators. Add the operator view in /owner/metrics.
- depends on: t4, t5, t8, t12, t15
- covers: c1, h13, c9, h2, c10, h3, c13, h6
- acceptance:
  - 200 rotating-address submissions from one IP: no more than the hourly budget reach the adapter, no reserved addresses reach it, no recipient errors alert, and an approved code still sends at budget 0
  - Each behaviour in the after-state has a test that fails on the pre-hardening code
  - An operator can see the warning, status logs and counters without opening guests.db

### t11 — Roll out on the live console, with a rehearsed rollback

- instruction: With the owner, after the chat edge work is live. Set Turnstile keys in grant and enable them. Pass a real browser test directly and through the Worker, checking siteverify's visitor IP. Only then turn on `require_bot_check`. Set budget values in private config. Rehearse lifting the budget and turning the bot check off.
- depends on: t10, t16
- covers: c22, h22
- acceptance:
  - Sign-in passes in a real browser on every path, through the Worker, with Turnstile on
  - Rollback restores guest mail by config in a timed rehearsal
  - Live values and bot check state aren't written to the public repo

## Risks

- [unknown_nonblocking] Fallback task t6 changes `client_ip` in web/entry.py and auth.py, and this plan builds on it. Start after it merges to avoid a conflict. (task t3)
- [unknown_nonblocking] Turnstile may not work through the Worker, because the visitor IP across the Worker-to-tunnel hop is unverified. (task t11)
- [unknown_nonblocking] HTTP 403 is ambiguous, so its alert text has to cover both causes. (task t1)
- [follow_up] Bounces from rotating addresses may hurt the sending reputation even under budget; the provider's policy wasn't read.
- [unknown_nonblocking] web/entry.py is shared by the reserved-address, budget and bot check tasks, which are ordered to avoid conflicts. (task t5)
