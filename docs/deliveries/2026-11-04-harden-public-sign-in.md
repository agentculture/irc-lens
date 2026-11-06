# Delivery Summary — Harden public sign-in

plan: `harden-public-sign-in` · run: `partial` · date: `2026-11-04`
baseline: `devague summary skeleton`

## Intent

> The chat console's public sign-in can't be used to burn the mail quota, mail strangers or raise false alarms, and it warns when its bot check is off

The plan (16 tasks, 7 waves) also adds the owner's 2026-11-06 decisions: Clef validates guest chat lines (c27-c30), and three red-team audit items (c31-c33). The workforce built 15 tasks and merged them as irc-lens#100 (0.23.0). The live rollout, `t11`, is partly done: some of its steps are the owner's.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Make delivery alerts mean an outage
- `t2` — Refuse reserved addresses in the guest paths
- `t3` — Add the persistent mail budget
- `t4` — Limit set-password and delete mail, and cap total sends
- `t5` — Warn on a missing bot check, and keep trusted browsers working
- `t6` — Count threat signals cheaply on the request path
- `t7` — Alert from fixed rules
- `t8` — Add Clef as an optional, bounded verdict
- `t9` — Test Clef on labelled windows before trusting its verdict
- `t10` — Prove the whole stack end to end, and document it
- `t11` — Roll out on the live console, with a rehearsed rollback
- `t12` — Cap pending guest seats per address and publish the guest limits
- `t13` — Answer every guest session-open failure with a shaped 503
- `t14` — File the audit items this plan leaves out
- `t15` — Validate guest chat lines with Clef before the demo agents see them
- `t16` — Measure the message validator on labelled lines before it blocks

## Actual Delivery

| Plan task | Status | What actually landed |
| --- | --- | --- |
| `t1` | delivered | Alerts only for outage statuses (401, 403, 429, 5xx, network), each under its own kind; recipient errors log their status and count in `failed_sends` (Codex; counter split from `delivery_alerts` by Claude) |
| `t2` | delivered | Reserved test domains refused in both guest paths, with the same page; autouse conftest switch for tests at `example.com` (pi edit, Claude tests) |
| `t3` | delivered | `mail_budget.py`: 50 a day, 20 an hour, 5 an hour per address, persisted in the guest store; 25-hour attempt retention (Codex; per-address cap made configurable by Claude) |
| `t4` | delivered | 5 per address per hour for set-password and deletion mail, beside the per-IP limit; one all-mail ceiling, 90 a day by default (Codex; made configurable by Claude) |
| `t5` | delivered | Startup warning, doctor `guest bot check` warning, `guest_mode.require_bot_check` (default off), trusted-browser bypass for approved sign-in (Claude) |
| `t6` | delivered | `threat.py`: 15-minute in-memory windows of counts and keyed source hashes, capped at 256 (Codex) |
| `t7` | delivered | `rules.py`: fixed triggers in `threat_rules:`, checked every 5 s off the request path, one counts-only alert per window (Codex; docs and a test fix by Claude) |
| `t8` | delivered | `decider.py`: bounded Clef client and counts-only advisory verdict on rule alerts, at most 1 per window and 24 a day (Codex; response shape checked against the live gateway by Claude) |
| `t9` | delivered | `scripts/clef_windows.py` and `docs/clef-threat-verdicts.md`: 45 labelled windows (35 real) against live Clef, misses recorded, orin load measured (Claude) |
| `t10` | delivered | A 200-address whole-stack test, a `/owner/metrics` threat and bot-check view, and `docs/security-checklist.md` (Codex; an over-broad assertion fixed by Claude) |
| `t11` | partial | Done 2026-11-06: 0.23.0 installed on spark, budgets and `threat_rules` in the private config, Clef verdict and validator (flag mode) enabled with the key from grant, console restarted, live tests passed. Turnstile: a widget was created through the Cloudflare API by the owner's run of `cf_turnstile.py`, its keys went into grant and the console, a real browser passed it, server-side verify passed, and `require_bot_check: true` is on. HSTS is on for the `culture.dev` zone: 6 months, no subdomains, no preload, set by the owner's run of `cf_hsts.sh`. Not done: a test through the edge Worker (not deployed), a rehearsed rollback, and switching the validator to block mode (needs owner acceptance) |
| `t12` | delivered | `guest_mode.max_pending_per_address` (default 1), guest limits published together (Codex; call sites folded into helpers by Claude) |
| `t13` | delivered | Shaped warming-up 503 for any guest session-open failure on `GET /`, `POST /input` and the event stream (pi edit, Claude test) |
| `t14` | delivered | agentirc#69 (webhook secret), irc-lens#99 (read-loop bound, per-principal eviction, MCP resource metadata), comment on culture#508 (bind) |
| `t15` | delivered | `web/message_check.py`: allow/block choice question, prompt outside the repo, fail open on error, timeout or cap, flag and block modes (Claude) |
| `t16` | delivered | `scripts/clef_lines.py`; first run with the draft prompt on 48 held-out lines: 1/20 benign blocked, 12/12 abusive blocked, 13/16 injection blocked (Claude). The rates are recorded; the owner has not accepted them yet |

## Mid-work Decisions

No `devague deviate` records were filed for this plan. The decisions below were made directly; the ones that changed behaviour are filed as deltas `b1`-`b6`.

- **Owner decisions captured on the frame, not as deviations (2026-11-06):** Clef validates guest chat lines (`c27`), a rejected line is blocked and flagged (`c28`), and a down or slow Clef lets the line through (`c29`). The plan was extended with `t12`-`t16` before the fan-out began.
- **Reserved domains in tests:** most tests sign guests in at `example.com`, so `t2` added an autouse conftest switch and a `reserved_domains` marker instead of rewriting 359 test addresses.
- **Mail budget per-address cap:** Codex's default let one address use 19 of 20 hourly codes. Changed to a configurable `ip_hourly_budget` of 5.
- **Mail ceiling:** made configurable (`total_daily_ceiling`, default 90).
- **Independent review fixes:**
  - the ceiling keeps a quarter for approved sign-in mail and alerts when it refuses;
  - the guest budget is taken in the background task;
  - reserved-domain edge cases are refused;
  - set-password and deletion keep their per-IP limit;
  - blocked and flagged lines stay out of the training export.
- **`/code-review` fixes:**
  - a flagged line is stored once;
  - each guest gets a share of the validator cap;
  - only outages feed the failed-send rule;
  - the validator has its own thread pool;
  - a blocked line answers 422.
- **Qodo fixes:**
  - malformed Clef answers fail open;
  - a timed-out call can't overlap the same guest's next one;
  - unmailed codes are revoked;
  - `clef_windows` counts only not-found routes as probes;
  - a neutral docs path.
- **Qodo items not changed:**
  - `require_bot_check` stays off by default until `t11`'s edge test;
  - a budget transaction across two instances isn't needed, since one console serves guests;
  - the ceiling resetting on restart is documented.
- **Rollout tuning:** `threat_rules.entry_posts` is 20 on the live console instead of 10, because one guest's sign-in is about 4 entry posts.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
| --- | --- | --- |
| `t3` | The budget is taken in the background send task, not before scheduling, and an unmailed code is revoked (`b1`). This keeps request-path work equal for every address. Contract `c2` says "counted before the send is scheduled"; it is still counted before the send itself. | acceptable |
| `t4` | The ceiling keeps a quarter for approved sign-in mail, and a refusal alerts (`b2`). It is per process and resets on restart, which falls short of "total sends can't exceed the ceiling" across restarts. | risky |
| `t1` | Recipient errors and ceiling refusals don't feed the failed-send threat rule (`b3`); the plan didn't specify this. | acceptable |
| `t15` | Additions beyond the plan: a per-guest share of the cap, an own thread pool, no overlapping calls, 422 for a block, flagged lines stored once (`b4`). | acceptable |
| `t7` | The live `entry_posts` trigger is 20, not the plan's starting 10 (`b5`). | acceptable |
| `t11` | The rollout is partial: Turnstile and `require_bot_check` are live, but they were tested only on the direct origin, not through the Worker. The rollback isn't rehearsed. HSTS is on, and Access needed no change. The validator stays in flag mode. | needs-follow-up |
| `t16` | The rates are measured but not yet accepted by the owner, so `h29` is unmet and blocking is not live. | needs-follow-up |

## Evidence

- **Merge commit:** `598f258` (squash of irc-lens#100, 40 commits).
- **Full suite:** at `cfd4c39` (the last PR head), 2079 passed and 1 failed. The failure, `tests/test_guest_admin.py::test_passwd_stdin_sets_argon2id_hash`, is #89 and also fails on main.
- **CI on `cfd4c39`:** `test`, `playwright`, `SonarCloud` (0 open issues), `GitGuardian` and `test-publish` all green.
- **Obligation tests at `598f258` on 2026-11-06:** 84 passed and 0 failed. Evidence records `e1`-`e25` map them to obligations `o1`-`o22`. Examples:
  - `tests/test_mail_budget.py::test_budget_persists_and_lift_after_restart_restores_mail` — pass
  - `tests/test_signin_guest.py::test_200_rotating_addresses_cannot_spend_guest_mail_or_block_approved` — pass
  - `tests/test_signin_guest.py::test_reserved_address_gets_the_code_page_and_no_mail` — pass
  - `tests/test_alerts.py::test_recipient_error_does_not_alert_or_take_outage_slot` — pass
  - `tests/test_decider.py::test_threat_request_contains_only_fixed_questions_and_derived_counts` — pass
  - `tests/test_rules.py::test_hanging_decider_does_not_block_and_timeout_alerts_not_assessed` — pass
  - `tests/test_message_check.py::test_blocked_line_never_reaches_the_sandbox` — pass
  - `tests/test_guest_limit.py::test_pending_reservations_are_capped_per_address` — pass
  - `tests/test_guest_limit.py::test_unexpected_open_failure_is_a_shaped_503` — pass
- **Live, after the 0.23.0 restart (2026-11-06):**
  - `/`, `/readyz`, `/entry` and `/healthz` answer 200, and an unrouted path answers 401.
  - A reserved-address sign-in got 200 with the code page and no send (`e20`).
  - The startup warning about the missing bot check is in the log, and doctor reports `warn` (`e21`).
  - The validator, built from the live config and grant key, allowed a normal question and flagged an injection line (`e23`).
  - The demo agent answered a real question in 11.6 s.
  - The health ping succeeded.
  - All console, IRC server and demo agent units were active.
  - The home page rendered in a real browser.
- **Clef evaluations:** `docs/clef-threat-verdicts.md` covers the threat verdict; the 48-line validator run is in `docs/guest-mode-config.md`.
- **Reviews:**
  - an independent review agent (5 findings fixed);
  - `/code-review` medium (5 fixed; the rest answered on the PR);
  - Qodo (11 comments: 8 fixed, 3 pushed back with reasons, all resolved).
- **Issues and PRs:** irc-lens#94, irc-lens#100, irc-lens#99, agentirc#69, culture#508, culture#524 (red-team authorization).

## Delivery Claims

The evidence records `e1`-`e25` and deltas `b1`-`b6` are filed with origin `llm` and await the owner's confirmation. Until then, confidence is capped at `medium`. No lapse is approved yet; `l1`-`l4` are pending and are not counted as evidence.

| Claim | Confidence | Evidence |
| --- | --- | --- |
| `c2` — persistent guest mail budget, surviving a restart | medium | `e1`, test `tests/test_mail_budget.py::test_budget_persists_and_lift_after_restart_restores_mail` |
| `c3` — approved sign-in mail stays outside the guest budget | medium | `e2`, test `tests/test_app_signin.py::test_exhausted_guest_budget_blocks_adapter_but_not_approved_mail` |
| `c4` — reserved domains refused before any provider call, same page | medium | `e7`, `e20` (live); cookie and timing floor not asserted by a test |
| `c5` — recipient errors don't alert, and the log has the status only | medium | `e8`, `e9`, test `tests/test_alerts.py::test_recipient_error_does_not_alert_or_take_outage_slot` |
| `c6` — startup and doctor warning without a bot check | medium | `e10`, `e21` (live) |
| `c8` — approved sign-in unchanged | medium | `e13`, `tests/test_app_signin.py` |
| `c18` — rule alerts with counts, Clef verdict advisory only | medium | `e14`, `e15`, `e16`, `docs/clef-threat-verdicts.md` |
| `c19` — threat detection is cheap and bounded | medium | `e17`, test `tests/test_threat.py::test_ten_thousand_requests_keep_fixed_bounded_state` |
| `c20` — per-address hourly limit and one all-mail ceiling | medium | `e3`, `e4`; the ceiling resets on restart (drift `t4`) |
| `c21` — one address can't use the whole hourly budget; budget-hit alert wording | medium | `e5`, `e6` |
| `c22` — the budget and the bot-check requirement lift through config | medium | `e12`; the rollback was not rehearsed live |
| `c23` — Clef is optional, and with none configured every other layer works | medium | `e18` |
| `c25` — probe counting by route matching | medium | `e19`, test `tests/test_threat.py::test_unrouted_401_is_a_probe_but_real_route_401_is_not` |
| `c26` — a trusted approved browser skips a failing bot check | medium | `e11`, test `tests/test_bot_check.py::test_trusted_browser_signs_in_with_the_bot_check_failing` |
| `c30` — Clef validates guest lines; a blocked line never reaches the sandbox | medium | `e22`, `e23` (live, flag mode); block mode not live |
| `c31` — pending seats capped per address | medium | `e24`, test `tests/test_guest_limit.py::test_pending_reservations_are_capped_per_address` |
| `c32` — shaped 503 instead of a bare 500 | medium | `e25`, test `tests/test_guest_limit.py::test_unexpected_open_failure_is_a_shaped_503` |
| `c33` — excluded audit items are tracked | medium | agentirc#69, irc-lens#99, culture#508 comment |
| `h29` — owner accepts the validator's measured rates before blocking | unverified | rates measured in `t16`, not yet accepted; not claimed done |
| Turnstile live and `require_bot_check` on (part of `c22` and `t11`) | medium | `e26`, `e27` (live, direct origin); not tested through the edge Worker |
| Clef's threat verdict trusted beyond advisory | unverified | `docs/clef-threat-verdicts.md` records a missed mail-abuse window; it stays advisory |

Lapse ledger evidence:

pending approval (not yet evidence): `l1`, `l2`, `l3`, `l4`

## Remaining Work / Follow-up

- **`t11` Turnstile through the Worker** — the keys are live and `require_bot_check` is on; repeat the browser test through the edge Worker once it is deployed, including siteverify seeing the visitor IP.
- **`t11` rollback rehearsal** — lift the budget and turn the bot check off through config, with the owner.
- **`t16`, `h29`** — the owner reviews the draft prompt (`~/.config/irc-lens/clef-prompt.yaml` on spark) and the measured rates; switch the validator to `mode: block` only after acceptance.
- **A guest past their share of the validator cap** — their lines go through unchecked under `c29`. Whether block mode should refuse them instead is an owner decision.
- **Set-password lockout** — the per-address hourly limit lets anyone delay one known address's set-password link by up to an hour. This is the `c20` tradeoff; exempting trusted browsers is a possible follow-up.
- **The ceiling resets on restart** (drift `t4`) — persist it in the guest store if restarts turn out to matter.
- **Clef's threat verdict** stays advisory; re-run `scripts/clef_windows.py` once production counters exist (`clef-windows.jsonl` on spark).
- **Adjudication** — the owner confirms or rejects evidence `e1`-`e25`, deltas `b1`-`b6`, obligations `o20`-`o22` and lapses `l1`-`l4`.
- **Follow-up issues** — agentirc#69, irc-lens#99, culture#508.
