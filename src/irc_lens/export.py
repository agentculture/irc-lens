"""PII-stripped export of guest inputs (guest mode, obligation o14).

``export_redacted`` reads the ``inputs`` table of a :class:`GuestStore` and
returns transcripts (JSONL or Markdown) that carry no email, IP, nick or
nick-to-person mapping. Only guests whose most recent consent opted in to
training use (``consents.train = 1``, d9) are included; everyone else's
inputs are left out entirely.

Design:

* Each guest is replaced by ``guest-N``. N is assigned by a per-export
  random shuffle (``secrets``); nothing derived from the identity is kept
  and no mapping is returned or written, so the pseudonym cannot be mapped
  back. Pseudonyms are stable *within* one export and deliberately differ
  between exports (no cross-export linkage).
* Timestamps are coarsened to the UTC date.
* Free text is scrubbed: known identifiers of the guests in the store
  (full email, email-local-part tokens, nick, IP) are replaced first
  (nicks become the guest's pseudonym, so "sbx-alice said hi" stays
  readable), then pattern rules remove emails, IPv4/IPv6, phone numbers
  and self-introduced personal names.

Known limitations (heuristic, best effort, not a guarantee):

* Names are only caught when they are a known guest token (email-local-part
  parts / nick, >= 3 chars) or follow an introduction cue ("my name is X",
  "I'm X", "I am X", "call me X", "this is X", "name's X", titles such as
  "Dr./Mr./Ms. X", sign-offs such as "thanks, X"). Names of third parties
  mentioned in plain prose ("ask Priya") are NOT detected, nor are
  lowercase names after a cue.
* Cue-based matching over-scrubs (e.g. "I'm Happy" -> "I'm [name]").
* Phone detection is any digit run of 7-15 digits with common separators;
  numbers written out in words, or obfuscated emails ("a at b dot com"),
  are not detected. Street addresses, employers, and other quasi-identifiers
  are out of scope.
* Dates and long numeric IDs can be over-scrubbed as phones.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Iterable

from irc_lens.guest_store import GuestStore

# Emails and IPv6 addresses: match maximal runs of one simple character class
# (linear, no backtracking), then validate each candidate in Python.
_EMAIL_RUN = re.compile(r"[\w.+@-]+")
_HEX_RUN = re.compile(r"[0-9A-Fa-f:.]+")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\w)")
_TRAIL = ".-"  # sentence punctuation that may cling to a candidate


def _is_email(token: str) -> bool:
    local, at, domain = token.partition("@")
    labels = domain.split(".")
    return (
        bool(at and local)
        and "@" not in domain
        and len(labels) >= 2
        and all(labels)
    )


def _email_sub(m: re.Match[str]) -> str:
    text = m.group(0)
    core = text.rstrip(_TRAIL)
    return "[email]" + text[len(core) :] if _is_email(core) else text


def _ipv6_sub(m: re.Match[str]) -> str:
    """``[ip]`` for a real IPv6 address standing alone (not part of a word)."""
    text, src = m.group(0), m.string
    before = src[m.start() - 1] if m.start() else ""
    after = src[m.end()] if m.end() < len(src) else ""
    if text.count(":") < 2 or before.isalnum() or before == "_" or after.isalnum() or after == "_":
        return text
    core = text.rstrip(".")  # a sentence-ending dot is not part of the address
    try:
        ipaddress.IPv6Address(core)
    except ValueError:
        return text
    return "[ip]" + text[len(core) :]


_PHONE = re.compile(r"(?<![\w])\+?\(?\d[\d\s().-]{5,}\d(?![\w])")
_NAME = r"[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,2}"
_NAME_CUES = [
    re.compile(
        r"(?i:\b(?:my name is|my name's|name's|name is|i am|i'm|im|call me|this is|it's|it is)\s+)"
        rf"({_NAME})"
    ),
    re.compile(rf"\b(?:Dr|Mr|Mrs|Ms|Miss|Prof)\.?\s+({_NAME})"),
    re.compile(
        r"(?i:\b(?:thanks|thank you|regards|cheers|sincerely|best|bye)\b,?\s+)"
        rf"({_NAME})"
    ),
]
_ANY_SBX_NICK = re.compile(r"\bsbx-[\w-]+", re.IGNORECASE)
_SPLIT = re.compile(r"[._+\-\s]+")
_MIN_TOKEN = 3


def _phone_sub(m: re.Match[str]) -> str:
    digits = sum(c.isdigit() for c in m.group(0))
    return "[phone]" if 7 <= digits <= 15 else m.group(0)


def _escape_ci(token: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w]){re.escape(token)}(?![\w])", re.IGNORECASE)


def scrub_text(
    text: str,
    *,
    known_tokens: Iterable[str] = (),
    replacements: dict[str, str] | None = None,
) -> str:
    """Scrub PII from *text*.

    *replacements* maps exact strings (case-insensitive) to a replacement
    (used for nicks -> pseudonym); *known_tokens* are replaced by ``[name]``.
    """
    for key in sorted(replacements or {}, key=len, reverse=True):
        text = _escape_ci(key).sub((replacements or {})[key], text)
    text = _EMAIL_RUN.sub(_email_sub, text)
    text = _ANY_SBX_NICK.sub("[nick]", text)
    text = _HEX_RUN.sub(_ipv6_sub, text)
    text = _IPV4.sub("[ip]", text)
    text = _PHONE.sub(_phone_sub, text)
    for pat in _NAME_CUES:
        text = pat.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + "[name]", text)
    for token in sorted(set(known_tokens), key=len, reverse=True):
        if len(token) >= _MIN_TOKEN:
            text = _escape_ci(token).sub("[name]", text)
    return text


def _identifier_tokens(email: str) -> set[str]:
    local = email.split("@", 1)[0]
    tokens = {t for t in _SPLIT.split(local) if len(t) >= _MIN_TOKEN}
    tokens.add(local)
    return tokens


def _read(store: GuestStore) -> tuple[list[tuple[str, str, str]], list[tuple]]:
    with closing(sqlite3.connect(store.path)) as con:
        guests = [tuple(r) for r in con.execute("SELECT email, nick, ip FROM guests")]
        inputs = [
            tuple(r)
            for r in con.execute(
                "SELECT email, kind, payload, ts FROM inputs ORDER BY ts, rowid"
            )
        ]
    return guests, inputs


def export_redacted(store: GuestStore, *, fmt: str = "jsonl") -> str:
    """Return a redacted transcript of all recorded guest inputs."""
    if fmt not in ("jsonl", "md"):
        raise ValueError(f"unknown format {fmt!r} (use jsonl or md)")
    guests, all_inputs = _read(store)
    # d9: only guests whose latest consent opted in to training use.
    opted_in = store.training_emails()
    inputs = [i for i in all_inputs if i[0] in opted_in]
    emails = {i[0] for i in inputs}
    order = sorted(emails)
    # CSPRNG (secrets.SystemRandom): pseudonym order must not be guessable.
    secrets.SystemRandom().shuffle(order)  # NOSONAR S2245
    pseudo = {e: f"guest-{n}" for n, e in enumerate(order, 1)}

    replacements: dict[str, str] = {}
    tokens: set[str] = set()
    # Every guest's identifiers are scrubbed (an opted-in guest may mention
    # anyone); only opted-in guests get a pseudonym.
    for email, nick, ip in guests:
        replacements[nick] = pseudo.get(email, "[nick]")
        replacements[email] = "[email]"
        if ip:
            replacements[ip] = "[ip]"
        tokens |= _identifier_tokens(email)
    for email in emails | {i[0] for i in all_inputs}:
        tokens |= _identifier_tokens(email)
    # nick/email replacements must not be re-scrubbed as names
    rows = []
    for email, kind, payload, ts in inputs:
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append(
            {
                "guest": pseudo[email],
                "kind": scrub_text(
                    kind, known_tokens=tokens, replacements=replacements
                ),
                "date": date,
                "text": scrub_text(
                    payload, known_tokens=tokens, replacements=replacements
                ),
            }
        )
    if fmt == "jsonl":
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    out = ["# Guest transcripts (redacted)\n"]
    current = None
    for r in sorted(rows, key=lambda r: int(r["guest"].split("-")[1])):
        if r["guest"] != current:
            current = r["guest"]
            out.append(f"\n## {current}\n")
        out.append(f"- {r['date']} [{r['kind']}] {r['text']}")
    return "\n".join(out) + "\n"
