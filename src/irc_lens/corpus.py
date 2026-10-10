"""Guest Q&A: record sbx-ask's answers with the guest.

Owner decision (d8): the sandbox IRCd keeps nothing on disk, so the guest
store is the one durable record of guest chat. Each answer sbx-ask posts in a
guest's private room is stored as a ``kind="answer"`` input of that guest,
next to their ``kind="message"`` questions, and is erased with them on
deletion. Nothing of it is kept after a deletion (d9).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


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
