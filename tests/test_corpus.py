"""Guest Q&A: sbx-ask's answers are stored with the guest (d8).

Owner decision: the sandbox IRCd keeps nothing on disk; the guest store holds
each guest's questions and sbx-ask's answers, linked to the guest and erased
with them on deletion. Nothing is kept after a deletion (d9).
"""

from __future__ import annotations

import sqlite3

from irc_lens.corpus import answer_recorder
from irc_lens.guest_store import GuestStore
from irc_lens.irc.message import Message

EMAIL = "dana.levi@example.com"
NICK = "sbx-dana"


def _store(tmp_path) -> GuestStore:
    s = GuestStore(tmp_path / "g.db")
    s.record_guest(EMAIL, NICK, "203.0.113.9")
    return s


def test_answers_in_the_guests_room_are_recorded_with_the_guest(tmp_path) -> None:
    s = _store(tmp_path)
    rec = answer_recorder(lambda: s, EMAIL, room="#g-abc", own_nick=NICK, agent_nick="sbx-ask")
    rec(Message(prefix="sbx-ask!a@h", command="PRIVMSG", params=["#g-abc", f"{NICK}: part one"]))
    rec(Message(prefix="sbx-ask!a@h", command="PRIVMSG", params=["#g-abc", f"{NICK}: part two"]))
    rec(Message(prefix="sbx-kim!a@h", command="PRIVMSG", params=["#g-abc", "not the agent"]))
    rec(Message(prefix="sbx-ask!a@h", command="PRIVMSG", params=["#g-zzz", "other room"]))
    with sqlite3.connect(s.path) as con:
        rows = con.execute("SELECT email, kind, payload FROM inputs").fetchall()
    assert rows == [(EMAIL, "answer", "part one"), (EMAIL, "answer", "part two")]
