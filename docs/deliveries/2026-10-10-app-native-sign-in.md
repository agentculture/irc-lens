# Delivery Summary — app-native sign-in

plan: `app-native-sign-in` · run: `complete` · date: `2026-10-10`
baseline: `devague summary skeleton`

## Intent

chat.culture.dev approved users sign in inside the app with a password and
an emailed code, and a wrong password looks exactly like a right one. The
run executed the converged plan `app-native-sign-in` (11 tasks, spec
`docs/specs/2026-10-10-app-native-sign-in.md`): email, then password, then
always the same code screen; a code is mailed only for an approved email
with the right password; the code starts a server-side, revocable app
session; set or reset password by emailed link; one guest at a time with a
15-minute no-action sign-off; and a delivery alert to approved users,
through a Cloudflare Worker, when Resend fails. Cloudflare Access stays on
`/login` as break-glass. It went live on 2026-10-10 at about 23:08 +0300
(irc-lens 0.13.0) and was patched to 0.13.1 the same night.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Store: approved sessions table, signin/setpw token purposes,
  revoke-all, retention sweep
- `t2` — Config and metrics for app sign-in, guest limit and delivery alerts
- `t3` — Mail templates for sign-in and set-password
- `t4` — cultureflare: delivery-alert Worker sending through Cloudflare
  Email Routing
- `t5` — App session: cookie, middleware tier, revocation of live sessions,
  CSRF coverage
- `t6` — Set or reset password by emailed link
- `t7` — Delivery alert client: one alert per failure kind per hour
- `t8` — Oracle-free sign-in: password -> code screen -> emailed code -> app
  session
- `t9` — Guest limit and action-based idle sign-off
- `t10` — Log hygiene, docs, version bump
- `t11` — Staging browser pass and live cutover

## Actual Delivery

| Plan task | Status | What actually landed |
| --- | --- | --- |
| `t1` | delivered | `sessions` table (sha256 ids, 7-day idle / 30-day cap), `signin`/`setpw` token purposes, `delete_sessions_for_email`, sweep; irc-lens #68 |
| `t2` | delivered | `auth.app_signin.*`, `guest_mode.max_guests`, `idle_close_s`, `mail.alert_url`/`alert_secret_env`; five new counters; #68 |
| `t3` | delivered | sign-in code and set-password link emails (`mail.py`); #68 |
| `t4` | delivered | `delivery-alert.js` Worker + `cf-delivery-alert-deploy.sh` (dry-run default); cultureflare #60 (0.16.0); deployed live as `lens-delivery-alert` |
| `t5` | delivered | `lens_session` cookie, middleware tier before Access, revocation sweep, CSRF proof cookies, Log out button (`d1`); #68 |
| `t6` | delivered | `/password`, `/password/<token>` (GET never consumes, 12-char minimum, ends sessions), `auth.app_signin.base_url` (`d2`), token redaction (`d3`); #68 |
| `t7` | delivered | `alerts.Alerter` (one per kind per hour, never raises); #68, finished-thread pruning added in review |
| `t8` | delivered, then amended | oracle-free password step and code entry (#68); limits replaced by trusted browsers + per-email budget (`d5`); new-browser notice (`d6`); code entries never counted and only wrong passwords counted (`d7`, `d8`) in #73 (0.13.1) |
| `t9` | delivered | `max_guests` with 120 s slot reservation (`d4`), action-based idle sign-off, busy page; #68 |
| `t10` | delivered | `tests/test_log_hygiene.py`, `docs/cli.md`, `docs/guest-mode-config.md`, CHANGELOG, 0.13.0 / 0.13.1 |
| `t11` | delivered | isolated staging browser passes (before #68 and for the trust change); live cutover steps 1–7 on 2026-10-10 (details under Evidence) |

All 11 tasks are accounted for; none dropped or blocked.

## Mid-work Decisions

Approved deviations, quoted from the records:

- `d1` — t5 also adds a minimal 'Log out' button (form POST /logout) to the
  approved user's header — the plan requires POST /logout but names no way
  to reach it from the UI; without a button logout is unusable
- `d2` — t6 adds config key `auth.app_signin.base_url` (falls back to
  `media.public_base_url`) — link host must come from trusted config, never
  the request Host header
- `d3` — t6 adds RedactTokenFilter on the access and auth loggers — the
  token-logging test showed the full set-password token reached the journal;
  c33 forbids codes in logs
- `d4` — t9 checks the guest limit before verifying the code, so a refused
  guest keeps an unused code; a verified slot is reserved for 120 s
- `d5` — r6 decided by owner: trusted browsers (no limit) plus an escalating
  per-email budget for untrusted browsers (3/15 min, then 2/30 min), silent
  when blocked
- `d6` — owner adds a new-browser sign-in notice email (c40) — users should
  learn of sign-ins from browsers they have not trusted
- `d7` — code entries no longer count toward the per-IP limit or the
  per-email budget; no limit on code entries — found live at go-live
- `d8` — correct password submissions are refunded from both limits; only
  wrong ones count — review of #73, finding 2

Approved at closeout (filed late, at validate-delivery):

- `d9` — review fixes on #68 changed behavior outside the plan:
  app sign-in turns itself off when `guest_mode.mail.provider` is `none`;
  `allowed_emails` compared without case on every app path; set-password
  checks the base URL before issuing a token; 429 alerts say "quota used up
  or rate-limited". Filed late, at validate-delivery, because no record had
  been made when the owner confirmed the fixes.

Decisions no record covers:

- The live `idle_close_s` was 600 in the deployed config; set to 900 at
  cutover to match the owner's 15-minute rule (config change only).
- Alert recipients are the owner only for now: the Cloudflare token cannot
  read Email Routing, so the collaborator's address could not be checked as
  a verified destination.
- Owner-run Cloudflare cache rule and Access path narrowing predate this plan
  (guest-mode go-live) and were left unchanged.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
| --- | --- | --- |
| `t5` (`d1`) | the plan requires POST /logout but names no way to reach it from the UI | acceptable |
| `t6` (`d2`) | link host must come from trusted config, never the Host header | acceptable |
| `t6` (`d3`) | the full set-password token reached the journal; c33 forbids it | acceptable |
| `t9` (`d4`) | kinder to the refused guest; reservation closes the check-then-open race | acceptable |
| `t8` (`d5`) | owner decision on r6: the old per-email limit let anyone block an approved email's sign-in | acceptable |
| `t8` (`d6`) | owner request during review of #68 | acceptable |
| `t8` (`d7`) | live go-live: the owner's second browser on the same IP had its correct code silently refused | acceptable |
| `t8` (`d8`) | several new browsers on one home IP could still lock the owner out at the password step | acceptable |
| `t2`/`t6`/`t7`/`t8` (`d9`) | owner-confirmed review fixes, recorded only at closeout | acceptable |
| `t11` | alert recipients limited to the owner; collaborator pending Email Routing verification | needs-follow-up |

## Evidence

- tests: 47 obligation-mapped node ids (57 items with parametrization) ran
  green at merged main `cf6bad6` on 2026-10-10T21:32Z, filed as `e1`–`e19`
  (one per obligation `o1`–`o19`); full suite 1277 passed.
- tests (amended behavior, `b1`–`b4`): `tests/test_trusted_devices.py`
  (incl. `test_second_browser_on_the_same_ip_signs_in`,
  `test_correct_passwords_are_not_counted`),
  `tests/test_app_signin.py::test_wrong_codes_never_lock_out_the_right_one`,
  `tests/test_signin_notice.py`, `tests/test_signin_review_fixes.py` — pass.
- live observations (`e20`–`e23`):
  - owner signed in live with "Trust this browser" (2026-10-10 23:13:37–51
    +0300); the new-browser notice email arrived (owner-confirmed);
  - `/login` break-glass in a private window authenticated through the
    Access JWT path (23:16:26, no `via=app-session`);
  - a test alert through the deployed Worker reached the owner's inbox;
    requests with no or a wrong secret got 401;
  - after the 0.13.1 restart both app sessions and the trusted browser
    survived, and open tabs reconnected via the app session.
- staging: isolated browser passes for sign-in, set-password, guest limit,
  forced 429/500 alerts, trust checkbox and silent brute force.
- CI: irc-lens #68 and #73 — tests, Playwright, SonarCloud (0 open issues
  after the S3776 split), GitGuardian all pass; cultureflare #60 — all pass,
  Sonar 0 issues.
- commits: irc-lens `d660d7d` (#68, 0.13.0) and `cf6bad6` (#73, 0.13.1) on
  main; cultureflare `c1c56c1` (#60).
- lint: `markdownlint-cli2` counts unchanged on the edited docs (pre-existing
  errors only); flake8 clean on new code apart from pre-existing E501.

## Delivery Claims

The owner approved evidence `e1`–`e23`, deltas `b1`–`b5`, deviation `d9`
and lapses `l1`–`l9` on 2026-10-11; the delta for `d9` is `b6` (proposed).
Each approved lapse caps the confidence of the claims it touches at medium,
named in the row: `l3`/`l4`/`l6` (tests not watched failing first), `l7`
(the set-password grader missed the referrer bug), `l8` (trust-year test
gap), `l9` (same-IP multi-browser case untested before go-live), `l1`/`l5`
(alert binding and poster not checked end to end until go-live).

| Claim | Confidence | Evidence |
| --- | --- | --- |
| `c10` the password step answers identically for every case, padded to the floor | high | `tests/test_app_signin.py::test_signin_four_cases_identical_and_floored` · `e1` |
| `c9` a correct password without the emailed code never yields a session | high | `tests/test_app_signin.py::test_correct_password_without_mailbox_never_yields_session` · live `e22` |
| `c11` codes: same browser only, single use, 10 minutes — the claim's "5 tries" limit was deliberately removed (`d7`) | medium | `tests/test_app_signin.py::test_code_is_single_use` · `test_wrong_codes_never_lock_out_the_right_one` · `e3`, delta `b3`; claim text predates `d7` · capped by approved lapse l9 |
| `c12` server-side sessions, sha256 only, 7 d idle / 30 d cap, logout and password change end them | medium | `tests/test_app_session.py::test_db_never_holds_raw_session_id` · `e4` · capped by approved lapse l4 |
| `c13` the sign-in email says the right password was entered and how to change it | medium | `tests/test_mail_signin.py::test_signin_body_says_password_entered_and_how_to_change` · `e5` · capped by approved lapse l3 |
| `c14`/`c38` only wrong password submissions count, per IP (3/15 min) and per email (3/15 min, strict 2/30 min); trusted browsers unlimited; blocks silent | medium | `tests/test_trusted_devices.py::test_correct_passwords_are_not_counted` · `test_strict_mode_two_per_thirty_minutes_after_exhaustion` · `e6`, deltas `b2`, `b4` · capped by approved lapse l9 |
| `c15` set or reset password by emailed link (identical request screen, single use, 30 min, 12-char minimum, ends sessions) | medium | `tests/test_setpw.py::test_set_password_argon2id_consumes_token_and_ends_sessions` · `e7` · capped by approved lapse l7 |
| `c16` an Access JWT still grants the approved tier; `/login` works | high | `tests/test_app_session.py::test_access_jwt_alone_still_approved` · live `e20` |
| `c20` a legacy password under 12 characters still signs in | high | `tests/test_app_signin.py::test_short_legacy_password_still_signs_in` · `e8` |
| `c24` one guest at a time; busy page before any code is mailed | medium | `tests/test_guest_limit.py::test_simultaneous_verifies_admit_exactly_one` · `e10` · capped by approved lapse l6 |
| `c25` a Resend failure posts one alert per kind per hour; the alert reaches approved users | medium | `tests/test_alerts.py::test_rate_limit_per_kind_and_after_an_hour` · `e11` · live `e21` (Worker path proven; a real Resend failure not forced live; collaborator not yet a recipient) · capped by approved lapse l1, l5 |
| `c27` CSRF proof covers `lens_session`, the pending cookie and `lens_device` | high | `tests/test_app_session.py::test_cross_site_post_with_new_cookie_is_403` · `e12` |
| `c28` 15 minutes without an action signs a guest off | medium | `tests/test_guest_limit.py::test_open_tab_with_no_input_is_signed_off` · `e13` (live config set to 900) · capped by approved lapse l6 |
| `c29` ending a session closes its IRC session within one sweep | high | `tests/test_app_session.py::test_sweep_closes_irc_session_when_app_session_ends` · `e14` |
| `c30` GET never consumes a set-password token | medium | `tests/test_setpw.py::test_get_link_twice_leaves_token_usable` · `e15` · capped by approved lapse l7 |
| `c31` a fresh session id at code entry, pending cookie cleared | high | `tests/test_app_signin.py::test_full_signin_reaches_real_mesh` · live `e23` |
| `c32` `auth.app_signin.enabled: false` restores 0.12.2 | high | `tests/test_app_signin.py::test_switch_off_restores_login_redirect` · `e17` |
| `c33` counters exist; no log line holds a code, session id or password | medium | `tests/test_log_hygiene.py::test_no_secret_value_in_any_log_record` · `e18` · capped by approved lapse l3 |
| `c34` the sweep removes expired sessions and old tokens after a day | medium | `tests/test_app_sessions_store.py::test_sweep_at_day_plus_one_leaves_no_expired_rows` · `e19` · capped by approved lapse l4 |
| `c37`/`c39` trusted browsers via an unchecked-by-default checkbox | medium | `tests/test_trusted_devices.py` · delta `b1` · live sign-in with trust ticked · capped by approved lapse l8 |
| `c40` new-browser sign-in notice | high | `tests/test_signin_notice.py` · delta `b5` · notice received live |
| `d9` review fixes (no-provider auto-off, case-insensitive allowlist, base-URL check, 429 wording) | high | `tests/test_signin_review_fixes.py` · `tests/test_alerts.py` · `d9` approved, delta `b6` (proposed) |

Lapse ledger (all approved): `l1` binding shape written from memory (now
backed by live delivery), `l2` weak 'templates unchanged' grader, `l3`–`l6`
tests not watched failing first, `l7` set-password grader blind to browser
referrer policy (fixed, regression test added), `l8` trust-year test gap
(fixed in review), `l9` same-IP multi-browser case untested before go-live
(fixed in #73 with a regression test).

## Remaining Work / Follow-up

- Owner adjudication — confirm or reject delta `b6` (the review fixes, `d9`).
- Alert recipients — add the collaborator after their address is verified
  in Cloudflare Email Routing; redeploy with `cf-delivery-alert-deploy.sh
  --apply`.
- #69 — "It wasn't me" emails and further blocks; also covers draining an
  email's budget from many IPs (review of #73, finding 6, deferred by the
  owner).
- #70 — logout in one browser can close the same user's IRC session in
  another browser.
- #71 — `guest_lock` held across the sandbox IRC connect.
- #72 — synchronous SQLite calls on the event loop.
- #74 — `irc-lens serve` exits 1 on SIGTERM, so every systemd stop reads as
  a failure.
- #75 — an idle-signed-off guest still sees "online" until they send.
- Later — remove the Cloudflare Access app once `/login` break-glass is no
  longer needed.
