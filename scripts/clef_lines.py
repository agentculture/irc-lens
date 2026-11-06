#!/usr/bin/env python3
"""Measure the guest message validator on labelled lines (t16, h29).

Runs each labelled line through the validator exactly as the console would
(the operator's prompt file, the fixed allow/block question) and prints, per
label, how many lines Clef allowed and blocked, then the false blocks (benign
lines blocked) and misses (abusive or injection lines allowed). Blocking goes
live only after the owner accepts these rates; until then the validator runs
in flag mode.

Labelled set (JSON lines), kept outside the repo next to the prompt file
because it holds abusive text, and held out from prompt tuning::

    {"line": "what is the all-backends rule?", "label": "benign"}
    {"line": "...", "label": "abusive"}
    {"line": "...", "label": "injection"}

Usage::

    GATEWAY_API_KEY=... python scripts/clef_lines.py lines.jsonl \\
        --prompt-file ~/.config/irc-lens/clef-prompt.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter

from irc_lens.web import message_check

LABELS = ("benign", "abusive", "injection")
MIN_LINES = 40


def _note(label: str, outcome: str) -> str:
    if label == "benign" and outcome == message_check.OUTCOME_BLOCK:
        return "FALSE BLOCK"
    if label != "benign" and outcome == message_check.OUTCOME_ALLOW:
        return "MISS"
    if outcome == message_check.OUTCOME_UNCHECKED:
        return "no verdict"
    return ""


async def measure(lines: list[dict], validator: message_check.MessageValidator) -> int:
    tally: Counter = Counter()
    notes: Counter = Counter()
    for index, item in enumerate(lines):
        started = time.perf_counter()
        # One principal per line: the per-guest lock never serialises the run.
        outcome = await validator.check(f"measure-{index}", item["line"])
        elapsed = (time.perf_counter() - started) * 1000
        note = _note(item["label"], outcome)
        tally[(item["label"], outcome)] += 1
        notes[note] += 1
        shown = item["line"] if len(item["line"]) <= 60 else item["line"][:57] + "..."
        print(f"{item['label']:9} {outcome:9} {elapsed:5.0f} ms  {note:11} {shown}")
    print()
    for label in LABELS:
        total = sum(n for (lab, _o), n in tally.items() if lab == label)
        blocked = tally[(label, message_check.OUTCOME_BLOCK)]
        unchecked = tally[(label, message_check.OUTCOME_UNCHECKED)]
        print(f"{label:9} {total:3} lines: {blocked} blocked, {total - blocked - unchecked} allowed, {unchecked} no verdict")
    print(
        f"\nfalse blocks: {notes['FALSE BLOCK']}, misses: {notes['MISS']}, "
        f"no verdict: {notes['no verdict']} (of {len(lines)} lines)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("lines", help="labelled lines, JSON lines")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8001/v1/systemone")
    parser.add_argument("--credential-env", default="GATEWAY_API_KEY")
    args = parser.parse_args(argv)
    credential = os.environ.get(args.credential_env, "")
    if not credential:
        parser.error(f"set {args.credential_env} to the decider credential")
    with open(args.lines, encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    labels = Counter(item["label"] for item in lines)
    if len(lines) < MIN_LINES or set(labels) != set(LABELS):
        parser.error(f"need at least {MIN_LINES} lines covering all of {', '.join(LABELS)}")
    validator = message_check.MessageValidator(
        url=args.url,
        credential=credential,
        prompt=message_check.PromptFile(args.prompt_file),
        mode=message_check.MODE_BLOCK,
        daily_cap=len(lines),
    )
    return asyncio.run(measure(lines, validator))


if __name__ == "__main__":
    sys.exit(main())
