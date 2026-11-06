#!/usr/bin/env python3
"""Check Clef's threat verdicts against labelled 15-minute windows (t9, h16).

Two subcommands:

``from-access-log``
    Rebuild real windows from the console's aiohttp access log on stdin (for
    example ``journalctl --user -u <console unit> -o short-iso``). Unrouted
    paths are told apart by resolving them against the lens's own router. The
    log has no visitor address and no successful-send line, so those counts
    stay 0. Prints one JSON window per line, unlabelled: the operator labels
    them (``genuine``, ``abusive`` or ``mail_abuse``) before ``evaluate``.

``evaluate``
    Send each labelled window to the decider, counts only (the same request
    the live alert path builds), and print each window's label, verdict,
    confidence and latency, the misses, and the count of real versus made-up
    windows. A miss is an ``abusive`` or ``mail_abuse`` window judged
    ``genuine``; a false alarm is a ``genuine`` window judged ``abusive``.
    Exit 1 when any ``mail_abuse`` window is missed, since the advisory label
    can't be lifted then.

Window file (JSON lines)::

    {"name": "...", "source": "real" | "made-up", "label": "genuine",
     "counts": {"requests": 3, "entry_posts": 1, ...}, "distinct_sources": 1}

Keep the labelled windows out of any prompt tuning.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from irc_lens import decider, threat

ABUSE_LABELS = frozenset({"abusive", "mail_abuse"})
_ACCESS = re.compile(
    r"aiohttp\.access: \S+ \[(?P<ts>[^\]]+)\] \"(?P<method>[A-Z]+) (?P<path>\S+) "
    r"HTTP/[\d.]+\" (?P<status>\d{3}) "
)
_FAILED_SEND = re.compile(r"mail send failed|MailSendError")
_STAMP = "%d/%b/%Y:%H:%M:%S %z"


def _router(config_path: str | None):
    """The lens's own router, to tell an unrouted path from a refused route.

    Built from the console's config so its routes match production; the guest
    store and media paths point at a temporary directory, so the live
    database is never opened.
    """
    import dataclasses
    import tempfile

    from aiohttp.test_utils import make_mocked_request

    from irc_lens.config import default_config_path, load_config
    from irc_lens.web import make_app

    scratch = tempfile.mkdtemp(prefix="clef-windows-")
    config = dataclasses.replace(
        load_config(Path(config_path) if config_path else default_config_path()),
        guest_store_path=f"{scratch}/guests.db",
        media_dir=f"{scratch}/media",
    )
    app = make_app(config, lambda nick: None)
    return app.router, make_mocked_request


def _window_start(when: datetime) -> datetime:
    minute = when.minute - when.minute % 15
    return when.replace(minute=minute, second=0, microsecond=0)


def from_access_log(lines, min_requests: int, config_path: str | None) -> list[dict]:
    import asyncio

    router, mocked = _router(config_path)
    windows: dict[datetime, Counter] = defaultdict(Counter)
    resolved: dict[tuple[str, str], bool] = {}
    loop = asyncio.new_event_loop()
    for line in lines:
        match = _ACCESS.search(line)
        if match is None:
            if _FAILED_SEND.search(line):
                stamp = line.split(" ", 1)[0]
                when = datetime.fromisoformat(stamp).astimezone(timezone.utc)
                windows[_window_start(when)][threat.Signal.FAILED_SENDS.value] += 1
            continue
        when = datetime.strptime(match["ts"], _STAMP).astimezone(timezone.utc)
        method, path = match["method"], match["path"].split("?", 1)[0]
        key = (method, path)
        if key not in resolved:
            info = loop.run_until_complete(router.resolve(mocked(method, path)))
            # Not found only, like the live middleware: a wrong method on a
            # real route is not a probe.
            resolved[key] = isinstance(
                getattr(info, "http_exception", None), web.HTTPNotFound
            )
        counts = windows[_window_start(when)]
        counts[threat.Signal.REQUESTS.value] += 1
        counts[threat.Signal.PROBES.value] += int(resolved[key] and method != "OPTIONS")
        counts[threat.Signal.REFUSED_RESPONSES.value] += int(match["status"] in ("401", "403"))
        counts[threat.Signal.ENTRY_POSTS.value] += int(
            method == "POST" and path.startswith("/entry/")
        )
    loop.close()
    return [
        {
            "name": start.strftime("%Y-%m-%dT%H:%MZ"),
            "source": "real",
            "label": "",
            "counts": dict(counts),
            "distinct_sources": 0,
        }
        for start, counts in sorted(windows.items())
        if counts[threat.Signal.REQUESTS.value] >= min_requests
    ]


def _snapshot(window: dict) -> threat.ThreatSnapshot:
    counts = {signal: int(window["counts"].get(signal.value, 0)) for signal in threat.Signal}
    return threat.ThreatSnapshot(
        window_started_at=0.0,
        counts=counts,
        distinct_sources=int(window.get("distinct_sources", 0)),
        source_count_capped=False,
    )


def _ask(url: str, credential: str, window: dict) -> tuple[decider.Verdict | None, float]:
    request = decider.build_threat_request(_snapshot(window))
    started = time.perf_counter()
    try:
        payload = decider.http_post(url, credential, request, decider.TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- recorded as no verdict
        payload = None
    elapsed = time.perf_counter() - started
    verdict = decider.validated_verdict(
        payload, decider.THREAT_QUESTION_KEY, decider.THREAT_CHOICES
    )
    return verdict, elapsed


def _outcome(label: str, choice: str | None) -> str:
    if label in ABUSE_LABELS and choice == "genuine":
        return "MISS"
    if label == "genuine" and choice == "abusive":
        return "false alarm"
    if choice is None:
        return "no verdict"
    return ""


def evaluate(windows: list[dict], url: str, credential: str) -> int:
    rows, misses, mail_misses = [], 0, 0
    print(f"{'window':34} {'source':8} {'label':11} {'verdict':9} {'conf':>5} {'ms':>6}  note")
    for window in windows:
        verdict, elapsed = _ask(url, credential, window)
        choice = verdict.choice if verdict else None
        note = _outcome(window["label"], choice)
        misses += note == "MISS"
        mail_misses += note == "MISS" and window["label"] == "mail_abuse"
        rows.append(elapsed)
        conf = f"{verdict.confidence:.2f}" if verdict else "-"
        print(
            f"{window['name'][:34]:34} {window['source']:8} {window['label']:11} "
            f"{choice or '-':9} {conf:>5} {elapsed * 1000:6.0f}  {note}"
        )
    sources = Counter(window["source"] for window in windows)
    print(
        f"\nwindows: {len(windows)} ({sources['real']} real, {sources['made-up']} made-up); "
        f"misses: {misses} (mail abuse: {mail_misses}); "
        f"latency median {sorted(rows)[len(rows) // 2] * 1000:.0f} ms, "
        f"max {max(rows) * 1000:.0f} ms"
    )
    return 1 if mail_misses else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("from-access-log")
    extract.add_argument("--min-requests", type=int, default=10)
    extract.add_argument("--config", help="lens config (default: the usual path)")
    run = sub.add_parser("evaluate")
    run.add_argument("windows", help="labelled windows, JSON lines")
    run.add_argument("--url", default="http://127.0.0.1:8001/v1/systemone")
    run.add_argument("--credential-env", default="GATEWAY_API_KEY")
    args = parser.parse_args(argv)
    if args.command == "from-access-log":
        for window in from_access_log(sys.stdin, args.min_requests, args.config):
            print(json.dumps(window))
        return 0
    credential = os.environ.get(args.credential_env, "")
    if not credential:
        parser.error(f"set {args.credential_env} to the decider credential")
    with open(args.windows, encoding="utf-8") as handle:
        windows = [json.loads(line) for line in handle if line.strip()]
    return evaluate(windows, args.url, credential)


if __name__ == "__main__":
    sys.exit(main())
