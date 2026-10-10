# Build Plan — app-native sign-in

slug: `app-native-sign-in` · status: `exported` · from frame: `app-native-sign-in`

> chat.culture.dev approved users sign in inside the app with a password and an emailed code, and a wrong password looks exactly like a right one

## Tasks

### t1 — Store: approved sessions table, signin/setpw token purposes, revoke-all, retention sweep

- instruction: Touch only src/`irc_lens`/`guest_store.py`, src/`irc_lens`/web/retention.py and new tests (tests/`test_app_sessions_store.py`). Follow the existing `_migrate`() pattern for the new table (CREATE TABLE IF NOT EXISTS) so old stores upgrade in place. Hash session ids with hashlib.sha256 like `email_hash`().
- covers: c34, h25
- acceptance:
  - `guest_store.py` gains a sessions table (sha256 id hash, email, created, `last_seen`) with create/get/touch/delete/`delete_for_email`; the raw session id is never stored (test reads every cell)
  - `issue_token`/`verify_token` accept purposes 'signin' (10 min) and 'setpw' (30 min); a token of one purpose never verifies for another
  - sweep() deletes sessions idle over 7 days or older than 30 days, and used or expired signin/setpw tokens older than a day; existing guest-store tests pass unchanged

### t2 — Config and metrics for app sign-in, guest limit and delivery alerts

- instruction: Touch only src/`irc_lens`/config.py, src/`irc_lens`/metrics.py, docs/guest-mode-config.md and tests (tests/`test_config_app_signin.py`). Mirror how `guest_mode` keys are parsed today (`_positive_int`). Do not wire behavior; later tasks consume these fields.
- covers: c32, c33
- acceptance:
  - config.py parses auth.`app_signin`.enabled (default true), `guest_mode`.`max_guests` (default 1, positive int), `guest_mode`.`idle_close_s` default 900, and `guest_mode`.mail.`alert_url` + `alert_secret_env`; invalid values fail load with a clear error
  - metrics.py exposes counters `signin_codes_sent`, `sessions_started`, `sessions_ended`, `guest_busy` and `delivery_alerts` on the owner metrics endpoint

### t3 — Mail templates for sign-in and set-password

- instruction: Touch only src/`irc_lens`/mail.py and tests/`test_mail.py` (+ a new test file if cleaner). Keep the `PURPOSE_`\* constants pattern added in 0.12.2. Never log the code or link.
- covers: c13, h9
- acceptance:
  - `render_token_email` accepts purpose 'signin': subject 'Your chat.culture.dev sign-in code', body says the correct password was just entered and to change it via the set-password link if it wasn't you; identical across addresses apart from the code
  - `render_link_email` (or purpose 'setpw') renders a set-password email carrying a link https://<`public_base_url`>/password/<token>, 30-minute expiry stated, identical across addresses apart from the link; guest and delete templates unchanged

### t4 — cultureflare: delivery-alert Worker sending through Cloudflare Email Routing

- instruction: In ../cultureflare (separate repo and PR, follow its CLAUDE.md and existing Worker layout; relates to issue #59). Recipients come from Worker config, not the request. Deploy only after owner approval.
- covers: h17
- acceptance:
  - A Worker accepts POST /alert with a bearer shared secret (constant-time compare); without or with a wrong secret it answers 401 and sends nothing
  - With the secret it sends one plain-text email per configured recipient through the `send_email` binding (verified destinations only), using the posted kind and message; the body it sends contains only the posted fields

### t5 — App session: cookie, middleware tier, revocation of live sessions, CSRF coverage

- instruction: New module src/`irc_lens`/web/`app_session.py` (issue/read/end session, cookie names `lens_session` and `lens_signin`); integrate into auth.py's middleware before the Access JWT path; extend csrf.py; revocation sweep alongside bans.py's loop. Session ids: secrets.`token_urlsafe`(32). Do not touch entry.py.
- depends on: t1, t2
- covers: c12, h8, c16, h12, c6, h2, c27, h18, c29, h20
- acceptance:
  - A valid `lens_session` cookie (HttpOnly, Secure, SameSite=Lax) whose email is still in `allowed_emails` yields the approved tier; idle >7 days, >30 days old, deleted, or email removed -> anonymous
  - A verified Access JWT still yields the approved tier exactly as 0.12.2 (existing auth tests unchanged)
  - POST /logout deletes the session and clears the cookie; a sweep closes registry sessions whose app session ended (logout, revoke, expiry, allowlist removal) within one interval
  - `csrf_middleware` applies the same-origin proof check to `lens_session` and `lens_signin` cookies (cross-site POST without Origin -> 403)

### t6 — Set or reset password by emailed link

- instruction: New module src/`irc_lens`/web/setpw.py + templates/setpw.html.j2, modelled on web/deletion.py. Only a one-line link in templates/entry.html.j2 (coordinate: t8 edits entry.html.j2 later, so keep this change minimal). Mark routes `allows_anonymous`.
- depends on: t1, t3, t5
- covers: c15, h11, c30, h21
- acceptance:
  - GET/POST /password asks for an email and always shows the same check-your-email page; only an allowed email gets a setpw link (rate-limited per email and IP)
  - GET /password/{token} shows the form without consuming the token; POST consumes it, refuses passwords under 12 characters (token stays valid), sets argon2id, and ends every session for that email
  - the entry card's password step links to /password

### t7 — Delivery alert client: one alert per failure kind per hour

- instruction: New module src/`irc_lens`/alerts.py; hook it where entry.py/deletion.py catch mail exceptions via a small helper in mail.py (`send_with_alert`) so callers change one line. Messages: 'Resend quota used up' for 429 else 'Resend send failed (<status or network>)'; both say codes aren't being delivered and to use /login.
- depends on: t2, t3
- covers: c25
- acceptance:
  - When a mail send fails (HTTP error incl. 429/quota, or network error) the lens posts {kind, message} to `guest_mode`.mail.`alert_url` with the bearer secret from `alert_secret_env`, at most once per hour per kind; `delivery_alerts` counter increments
  - The alert payload never contains a recipient address or a code (test inspects the posted body); an alert failure is logged and never raises into the request

### t8 — Oracle-free sign-in: password -> code screen -> emailed code -> app session

- instruction: Edit src/`irc_lens`/web/entry.py and templates/entry.html.j2 (new 'code' step for sign-in reusing the guest code UI). Keep `_dummy_verify` and the floor. Use t5's `app_session` API; send mail with asyncio.`create_task` and t7's `send_with_alert` if merged (else plain send). Code screen copy: 'If your email and password are right, a code is on its way. Nothing after a minute? Go back and re-enter your password.'
- depends on: t1, t2, t3, t5
- covers: c1, h1, c8, h4, c9, h5, c10, h6, c11, h7, c14, h10, c31, h22, c32, h23, c7, h3
- acceptance:
  - POST /entry/signin returns byte-identical responses (email masked) for unknown, wrong, right and rate-limited, each >= the floor; only the right case sends exactly one signin mail, from a background task
  - POST /entry/code with the right code and matching `lens_signin` cookie mints a new `lens_session` and clears `lens_signin`; wrong browser, reuse, >10 minutes, or 6th try in 15 minutes -> the one error
  - per-IP password limit 5/15min (over: no check, same screen); no per-email lockout; at most 5 signin codes sent per email per 15 min
  - stored passwords shorter than 12 characters still sign in; with auth.`app_signin`.enabled false the 0.12.2 behavior (303 /login) is restored and its tests pass

### t9 — Guest limit and action-based idle sign-off

- instruction: Edit src/`irc_lens`/web/entry.py (`post_guest`, verify), src/`irc_lens`/web/sessions.py (track last action; reap on action idle), src/`irc_lens`/web/routes.py (touch last action in `post_input` and on GET / for returning guests). Count active guests from the registry's sandbox sessions whose principal is a guest.
- depends on: t2, t8
- covers: c24, h16, c28, h19
- acceptance:
  - with `max_guests` guests active, `post_guest` shows 'The sandbox is busy. Try again in a few minutes.' and sends no code; code verify re-checks under an asyncio lock so simultaneous verifies leave exactly `max_guests` active; Guest view of approved users never counts
  - a guest session with no POST /input for `idle_close_s` (900) is closed even with an open tab; a returning guest with a valid cookie rejoins if a slot is free, else sees the busy page; no new code is emailed

### t10 — Log hygiene, docs, version bump

- instruction: Docs and one test file; no behavior changes. Confirm the PR diff contains no Cloudflare config changes.
- depends on: t6, t7, t8, t9
- covers: c33, h24, c17, h15
- acceptance:
  - a test runs the full sign-in, set-password and guest flows with log capture and finds no code, session id or password value in any record
  - docs/cli.md and docs/guest-mode-config.md describe app sign-in, set-password, guest limit, idle sign-off, delivery alerts and the rollback switch; CHANGELOG + pyproject bumped (minor); full suite and guest-mode tests pass

### t11 — Staging browser pass and live cutover

- instruction: Main agent only (outward-facing). Reuse scratchpad/staging/ux layout (own sbx ircd on 6678, file mailer). Ask before deploying the Worker and before the live switch.
- depends on: t10, t4
- covers: c19, h14
- acceptance:
  - real-browser pass on an isolated local staging: owner signs in with password + code in under 2 minutes; wrong password shows the identical code screen; set-password, logout, guest busy page and idle sign-off all exercised; screenshots at 375px
  - after merge + owner approval: lens upgraded on spark, alert Worker deployed, owner signs in live with Cloudflare Access bypassed; /login break-glass still works; results recorded in the delivery record

## Risks

- [unknown_nonblocking] t6 and t8 share wave 2 and both edit templates/entry.html.j2 (t6 adds one link); expect a small merge conflict, resolved at merge (task t6)
- [unknown_nonblocking] Guest-slot check and session open are separate awaits; t9 must hold one asyncio lock across check+open or two verifies can both pass (task t9)
- [follow_up] Alert emails reach only verified Email Routing destinations; the collaborator's address must be verified before the Worker can mail them (assumption c26) (task t4)
- [unknown_nonblocking] Cloudflare `send_email` limits/pricing unchecked (frame v4) and Resend quota error shape unknown (frame v3); t7 treats any 429 as quota (task t7)
