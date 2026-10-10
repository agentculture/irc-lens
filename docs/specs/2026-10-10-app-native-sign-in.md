# app-native sign-in

> chat.culture.dev approved users sign in inside the app with a password and an emailed code, and a wrong password looks exactly like a right one

## Audience

- Approved users of chat.culture.dev (the owner and one collaborator), and anyone trying to guess their passwords

## Before → After

- Before: A correct password answers 303 to /login (Cloudflare Access) and a wrong one 401 'Email or password is wrong', so an attacker spreading guesses over many IPs learns the moment a guess is right; the 5-per-15-minutes per-email limit lets an attacker lock the owner out; the collaborator has no password and relies on the Access email PIN
- After: Email, then password, then always the same code screen; a sign-in code is emailed only when the email is approved and the password is correct; the right code in the same browser starts a server-side, revocable app session that reaches the real mesh; Cloudflare Access is not on the normal path

## Why it matters

- Guessing passwords teaches an attacker nothing, a stolen password alone is not enough (password plus mailbox, where Access checked only the mailbox), and a correct guess emails the owner

## Requirements

- POST /entry/signin answers identically (same status, same body apart from the echoed email, padded to the same floor) for an unknown email, an approved email with a wrong password, an approved email with the right password, and a rate-limited attempt; the code email is sent off the request path so it adds no timing difference
  - instruction: tests/`test_app_signin.py`: parametrize unknown / wrong / right / limited; compare bodies with the email masked; assert RecordingAdapter got exactly 1 mail only in the right case
  - honesty: a test posts the 4 cases and asserts equal status, equal body after replacing the email, and that each took at least the floor; the mail send runs in a background task
- Sign-in codes are single-use, expire after 10 minutes, carry purpose 'signin', and work only together with the pending-sign-in cookie set by the code screen (same browser); code entry is limited to 5 tries per 15 minutes per email and per IP; a wrong, expired, reused or rate-limited code all show the one error
  - honesty: a code from browser A fails in browser B, a used code fails the second time, an 11-minute-old code fails, and the 6th try in 15 minutes fails even with the right code
- Approved sessions are server-side: a sessions table holds sha256 of the session id, the email, created and last-seen times; the cookie `lens_session` is HttpOnly, Secure, SameSite=Lax; 7 days idle or 30 days total ends it; logout ends it; setting a password ends every session for that email; the email is re-checked against `allowed_emails` on every request
  - honesty: the database never holds a raw session id; a session idle 7 days or older than 30 days is refused; logout and password change make the old cookie fail; removing the email from `allowed_emails` makes the next request anonymous
- The sign-in email says the correct password was just entered and tells the reader to change it if it wasn't them
  - honesty: the rendered sign-in email contains the change-your-password line and the email is identical for every approved address apart from the code
- Rate limits: password checks stay limited per IP (5 per 15 minutes; over the limit, no check, same screen); per email there is no lockout, only a cap of 5 sign-in codes sent per 15 minutes
  - honesty: the 6th password attempt from one IP in 15 minutes is not checked yet shows the same screen; an approved email under a distributed attack still gets codes when the right password is entered, up to 5 per 15 minutes
- Set or reset password: a link on the entry card asks for an email and always shows the same 'check your email' screen; only an approved email gets a single-use link (purpose 'setpw', 30-minute expiry) to a page that sets an argon2id password of at least 12 characters and ends all of that email's sessions
  - honesty: the set-password screen is identical for approved and unknown emails; a link works once, expires in 30 minutes, rejects passwords under 12 characters, and setting a password ends that email's sessions
- During the trial a verified Cloudflare Access JWT still yields the approved tier, so /login keeps working as break-glass
  - honesty: with a valid Access JWT the approved tier is granted exactly as in 0.12.2, so /login still works
- Guest limit: while `guest_mode`.`max_guests` (default 1) guests are active, a new visitor who picks Guest mode sees 'The sandbox is busy. Try again in a few minutes.' before any code is emailed, and code verification re-checks the limit so two guests can't slip in together; a slot frees when the guest deletes their data, or after 15 minutes with no action (idle close 900 s); an approved user's Guest view does not count
  - honesty: with one guest active, a second visitor gets the busy message and 0 codes are emailed; two guests verifying codes at the same moment yield exactly one active guest; after the first guest's 15 idle minutes or deletion, the next visitor gets in; approved users in Guest view are never refused
- Delivery alert: when a Resend send fails (HTTP error, including quota exhaustion, or a network error), the lens posts at most one alert per hour per failure kind to a Cloudflare Worker over HTTPS with a shared secret; the Worker emails each approved user through Cloudflare Email Routing; the alert names what failed, says sign-in and guest codes are not being delivered, and points to /login; it never contains a guest's email address or a code
  - honesty: a forced Resend failure sends exactly one alert per failure kind within an hour, the alert body holds no guest email address or code, and the Worker rejects a request without the shared secret
- The CSRF guard that today demands same-origin proof for cookie-bearing POSTs without an Origin header (csrf.py: only when `lens_guest` is present) also applies to the `lens_session` and pending-sign-in cookies
  - honesty: a cross-site POST carrying `lens_session` or the pending cookie, with no Origin and Sec-Fetch-Site cross-site, gets 403 before any handler runs
- Guest idle sign-off counts actions, not open tabs: 15 minutes with no message or command sent closes the guest's session even if the tab stays open (today's `reap_idle` only closes sessions with no open event stream)
  - honesty: a guest with the tab open who sends nothing for 15 minutes is signed off and frees the slot; a guest who keeps chatting is not
- Ending an approved session (logout, password change, expiry, removal from `allowed_emails`) also closes that user's open IRC session and event stream, as the ban sweeper does for guests, so an open tab doesn't keep working
  - honesty: after logout or password change, the user's already-open tab stops receiving events and its next POST is refused within one sweep interval
- Opening a set-password link (GET) never uses it up; only submitting the new password (POST) consumes the token, so mail scanners that prefetch links can't burn it
  - honesty: a GET of a set-password link leaves the token usable; only the POST that sets the password consumes it
- A new random session id is minted at each successful code entry and the pending-sign-in cookie is cleared; a session id from before sign-in is never promoted
  - honesty: the session cookie after sign-in differs from any cookie the browser held before, and the pending-sign-in cookie is gone
- A config switch (auth.`app_signin`.enabled, default on) turns app sign-in off and restores the 0.12.2 behavior (correct password -> /login), as the rollback if something goes wrong at cutover
  - honesty: with auth.`app_signin`.enabled false, a correct password answers 303 /login exactly as 0.12.2 did
- Owner metrics count sign-in codes sent, sessions started and ended, guest-busy refusals and delivery alerts; no log line contains a code, a session id or a password
  - honesty: metrics expose the five counters; a grep of logs from a full test run finds no code, session id or password value
- The retention sweep deletes expired sessions and used or expired sign-in and set-password tokens after a day, the same as guest codes
  - honesty: a sweep at day+1 leaves no expired session row and no used or expired signin/setpw token

## Honesty conditions

- an approved user reaches the real mesh at chat.culture.dev with password plus emailed code, without passing through Cloudflare Access
- the only approved users are the emails in auth.`allowed_emails`; nothing else grants the approved tier besides a verified Access JWT during the trial
- the before-state is what 0.12.2 does: `post_signin` returns 303 /login on success and 401 otherwise, with `_over_limit` counting per email and per IP
- after the change no response of /entry/signin depends on whether the password was correct; only the mailbox learns it
- a correct password with no access to the mailbox never yields a session
- outside the guest limit and idle sign-off, guest-mode tests pass unchanged; the PR changes no Cloudflare tunnel or cache configuration
- each success signal maps to a named test or a recorded browser/live check in the delivery record

## Success signals

- Tests show the 4 sign-in cases give byte-identical responses apart from the email with 0 codes sent for 3 of them; a correct password without the code gives 0 access; a real-browser pass on staging signs the owner in with password plus code in under 2 minutes; the live owner sign-in works with Cloudflare Access bypassed

## Scope / boundaries

- irc-lens plus one small Cloudflare email Worker for delivery alerts; the guest flow changes only by the one-guest limit and the 15-minute idle sign-off; the Cloudflare tunnel and CDN cache rules are unchanged; approved users stay defined by `allowed_emails` in config (no sign-up, no account creation)

## Non-goals

- No TOTP or WebAuthn, and no code-only (passwordless) sign-in in this change

## Assumptions

- Cloudflare Email Routing can send only to verified destination addresses, so every approved user's address must be verified as a destination before alerts reach them

## Scope exploration

- `s1` — `src/irc_lens/web/entry.py post_signin (L401)`: correct password -> 303 /login, wrong -> 401 `ERR_SIGNIN`: a password-correctness oracle; argon2id + dummy verify + 0.5 s floor + `_over_limit` 5/15min per email and per IP (per-email lets an attacker lock the owner out)
- `s2` — `src/irc_lens/web/auth.py build_cloudflare_middleware`: approved tier comes only from a verified Access JWT + `allowed_emails`; non-approved resolves to anonymous only when `guest_mode` is on; deny-by-default via `allows_anonymous`
- `s3` — `src/irc_lens/web/csrf.py guest cookie`: guest session is a stateless HMAC cookie `lens_guest` (Secure, SameSite=Strict, secret `IRC_LENS_GUEST_COOKIE_SECRET`) - not revocable server-side; Origin check on cookie-bearing POSTs
- `s4` — `src/irc_lens/guest_store.py`: passwords table (argon2id, CLI-set via irc-lens guests passwd), tokens table with purpose (guest/delete), attempts table for rate limits; no sessions table
- `s5` — `src/irc_lens/web/deletion.py + mail.py`: emailed single-use token pattern with purpose and fixed per-purpose template already exists (0.12.2); Resend adapter live
- `s6` — `challenge pass / security lens: src/irc_lens/web/csrf.py csrf_middleware`: same-origin proof without Origin is checked only for the `lens_guest` cookie; a SameSite=Lax `lens_session` would not be covered - seeded the CSRF requirement
- `s7` — `challenge pass / lifecycle lens: src/irc_lens/web/sessions.py reap_idle + bans.py _loop`: `reap_idle` closes sandbox sessions only after `idle_s` with no open SSE tab; an open idle tab is never reaped, so with `max_guests`=1 one forgotten tab holds the only slot forever - seeded the action-based idle requirement
- `s8` — `challenge pass / security lens: src/irc_lens/web/bans.py sweep_once`: bans close live sessions via registry.close; nothing equivalent exists for approved users because Access JWT expiry was enforced per request only - seeded the revocation requirement
- `s9` — `challenge pass / adjacent-systems lens: src/irc_lens/web/entry.py get_login`: /login always 303s to / whatever the tier; with Access path-scoped and `path_cookie` off, the Access cookie still covers / so break-glass keeps working - consistent with c16, no new claim
- `s10` — `challenge pass / concurrency lens: guest slot (sessions.py registry, store calls via asyncio.to_thread)`: slot check and session open are separate awaits; two verifies can interleave - c24 already demands the verify-time re-check; plan must hold an asyncio lock across check+open (plan-side risk)
- `s11` — `challenge pass / adjacent-systems lens: Cloudflare Email Routing Worker (cultureflare)`: not read - no Worker code exists yet; `send_email` destination-verification rule rests on assumption c26; Resend quota error shape unknown (v3)

## Decisions

- Approved-user session lasts 7 days sliding (renewed while used), with a 30-day absolute cap
- Cloudflare Access /login stays as a break-glass way in during a trial; deleting the Access app is a follow-up after the trial
- Approved users without a password set one through an emailed set-password link (folds in irc-lens #66)
- Process: short devague spec, owner approves, then one irc-lens PR built test-first with a real-browser pass and a staged cutover
- A password set before this change that is shorter than 12 characters still signs in; the set-or-reset password flow is how it gets replaced with one of at least 12
- Only 1 guest may use the sandbox at a time for now; a guest with no action for 15 minutes is signed off; the limit rises after the move to AWS
- An email-delivery outage needs no extra sign-in fallback for now (break-glass /login covers the trial)
- When Resend can't send (the 3000-email quota is used up, or Resend returns an error) the approved users get an alert email sent through Cloudflare email

## Open parks

- [unknown_nonblocking] Whether Resend reports quota exhaustion distinctly (status or error name) so the alert can say 'quota used up' instead of a generic failure; any failure alerts either way
- [unknown_nonblocking] Cloudflare Email Routing `send_email` limits and pricing for a Worker were not checked in this pass
- [follow_up] Many IPs each sending 5 argon2id checks per 15 minutes can still cost CPU; whether a global password-check budget is needed

## Resolved vagueness

- [follow_up] Resend outage blocks app sign-in; break-glass /login covers the trial, but after Access is removed a fallback (e.g. server CLI one-time login) may be needed — resolved: Owner: an email-delivery outage needs no extra fallback for now
