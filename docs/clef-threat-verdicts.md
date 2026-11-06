# Clef threat verdicts: evaluation

When a fixed threat rule fires, the alert can carry an advisory verdict from
the Clef decider (`threat_rules.decider_url`): `genuine`, `abusive` or
`unclear`, judged from the window's counts only. The verdict never changes
whether an alert is sent. It stays labelled advisory until the owner lifts the
label, and that needs an evaluation with no missed mail-abuse window.

## Running an evaluation

`scripts/clef_windows.py` rebuilds real 15-minute windows from the console's
access log, then sends labelled windows to the decider with the same counts-only
request the alert path builds:

```bash
journalctl --user -u <console unit> --since -7days -o short-iso \
  | python scripts/clef_windows.py from-access-log > windows.jsonl
# label each window: genuine, abusive, mail_abuse, or unknown (not scored)
GATEWAY_API_KEY=... python scripts/clef_windows.py evaluate windows.jsonl
```

Unrouted paths are told apart by resolving them against the lens's own routes,
built from the console's config. The access log has no visitor address and no
line for a successful send, so real windows carry `distinct_sources` and
`mail_sends` of 0; made-up windows cover those signals. Keep the labelled
windows out of any prompt tuning, and outside the repo.

A miss is an `abusive` or `mail_abuse` window judged `genuine`. A false alarm
is a `genuine` window judged `abusive`. The script exits 1 when a `mail_abuse`
window is missed.

## Results, 2026-11-06

45 windows: 35 real (7 days of the live console's log), 10 made-up. Real windows
were labelled only where the cause is known: 4 `abusive` (three background
scanners of about 300 unrouted paths each, and the authorized red-team probe of
culture#524), 17 `genuine` (quiet windows), and 14 `unknown`.

| Windows | Label | Verdict |
| --- | --- | --- |
| 4 real | abusive | 4 abusive |
| 17 real | genuine | 3 genuine, 14 unclear |
| 14 real | unknown | 13 abusive, 1 unclear (not scored) |
| 2 made-up | abusive (scanners) | 2 abusive |
| 4 made-up | mail_abuse | 3 abusive, **1 genuine (miss)** |
| 4 made-up | genuine | 2 genuine, 1 unclear, **1 abusive (false alarm)** |

- **Miss:** a reserved-address probe (10 entry posts, 10 reserved addresses, one
  source) was judged `genuine` (0.35). The fixed rule still alerts on any
  reserved address, so the alert goes out; only the advisory verdict is wrong.
- **False alarm:** eight real guests behind one address (8 entry posts and 8
  sends from one source) were judged `abusive` (0.34).
- Clef rarely calls a quiet real window `genuine`: 14 of 17 came back
  `unclear`.

**Decision:** a mail-abuse window was missed, so the verdict stays labelled
advisory.

## Load on the decider host

The decider runs on orin's shared GPU lane. During the run, 45 sequential
calls took 32.6 s (median 575 ms, max 1833 ms per call). The GPU was busy in
131 of 197 samples at a 250 ms interval, at 87% on average while busy, with no
memory growth. At the default cap of 24 calls a day that's about 15 seconds of
GPU time a day, so the cap stays at 24.
