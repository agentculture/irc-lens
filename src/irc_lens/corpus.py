"""Guest Q&A: record sbx-ask's answers with the guest; anonymize on deletion.

Owner decision (d8): the sandbox IRCd keeps nothing on disk, so the guest
store is the one durable record of guest chat. Each answer sbx-ask posts in a
guest's private room is stored as a ``kind="answer"`` input of that guest,
next to their ``kind="message"`` questions, and is erased with them on
deletion. Before a deletion, :func:`anonymized_pairs` turns the guest's
questions and answers into Q&A pairs that cannot reasonably identify them —
email, IP, nick and room dropped, PII scrubbed, day-only date, NSFW-declined
pairs left out — which the store keeps in its ``corpus`` table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from irc_lens.export import _identifier_tokens, scrub_text

logger = logging.getLogger(__name__)

#: sbx-ask's NSFW refusal (culture_core/sandbox/agent.py DECLINE_NSFW).
_FLAGGED_MARK = "flagged to the owner"


def answer_recorder(
    store_getter: Callable[[], Any],
    email: str,
    *,
    room: str,
    own_nick: str,
    agent_nick: str,
) -> Callable[[Any], None]:
    """A PRIVMSG listener that stores the agent's answers in *room*."""
    prefix = f"{own_nick}: "

    def record(msg: Any) -> None:
        if msg.command != "PRIVMSG" or len(msg.params) < 2:
            return
        sender = (msg.prefix or "").split("!", 1)[0]
        if sender.lower() != agent_nick.lower() or msg.params[0] != room:
            return
        text = msg.params[1]
        if text.startswith(prefix):
            text = text[len(prefix) :]
        store = store_getter()
        if store is None or not text:
            return
        try:
            store.record_input(email, kind="answer", payload=text)
        except Exception:  # noqa: BLE001 — never break the read loop
            logger.exception("recording an agent answer failed")

    return record


def anonymized_pairs(store: Any, email: str) -> list[dict]:
    """The guest's Q&A as ``{"question", "answer", "date"}`` dicts with every
    identifier removed (see the module docstring)."""
    nicks = {nick for _e, nick, _ip in store.get_guest(email)}
    ips = {ip for _e, _n, ip in store.get_guest(email) if ip}
    tokens = set(_identifier_tokens(email))
    replacements = {email: "[email]", **dict.fromkeys(ips, "[ip]")}
    for nick in nicks:
        replacements[nick] = "[nick]"
        bare = nick.split("-", 1)[-1]
        if len(bare) >= 3:
            tokens.add(bare)

    def clean(text: str) -> str:
        return scrub_text(text, known_tokens=tokens, replacements=replacements).strip()

    pairs: list[dict] = []
    for kind, payload, ts in store.inputs_for(email):
        if kind == "message":
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            pairs.append({"question": payload, "answer": "", "date": day})
        elif kind == "answer" and pairs:
            sep = " " if pairs[-1]["answer"] else ""
            pairs[-1]["answer"] += sep + payload
    return [
        {"question": clean(p["question"]), "answer": clean(p["answer"]), "date": p["date"]}
        for p in pairs
        if _FLAGGED_MARK not in p["answer"]
    ]
