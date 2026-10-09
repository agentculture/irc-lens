"""Guest Q&A: answers stored with the guest; anonymized corpus kept on
deletion (d8).

Owner decision: the sandbox IRCd keeps nothing on disk; the guest store holds
each guest's questions and sbx-ask's answers (linked, erased on deletion); on
deletion, Q&A pairs that cannot reasonably identify the guest are kept in a
separate corpus with no email, IP, nick or room and a day-only date.
"""

from __future__ import annotations

import sqlite3

from irc_lens.corpus import anonymized_pairs, answer_recorder
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


def test_anonymized_pairs_strip_identity_and_drop_flagged(tmp_path) -> None:
    s = _store(tmp_path)
    s.record_input(EMAIL, kind="message", payload="I'm Dana Levi, mail dana.levi@example.com - what is culture?")
    s.record_input(EMAIL, kind="answer", payload="Hi dana, culture is an IRC mesh")
    s.record_input(EMAIL, kind="answer", payload="where agents collaborate.")
    s.record_input(EMAIL, kind="message", payload="send me porn")
    s.record_input(
        EMAIL, kind="answer", payload="I can't help with that. This request has been flagged to the owner."
    )
    s.record_input(EMAIL, kind="message", payload="call me at +1 415 555 0100, I am sbx-dana")
    pairs = anonymized_pairs(s, EMAIL)
    assert len(pairs) == 2  # the NSFW-declined pair is dropped
    q, a, day = pairs[0]["question"], pairs[0]["answer"], pairs[0]["date"]
    assert "Dana" not in q and "dana" not in q.lower() and "@" not in q
    assert "dana" not in a.lower()
    assert a.endswith("where agents collaborate.")
    assert len(day) == 10 and day.count("-") == 2  # YYYY-MM-DD only
    assert "415" not in pairs[1]["question"] and "sbx-dana" not in pairs[1]["question"]
    assert pairs[1]["answer"] == ""
    assert set(pairs[0]) == {"question", "answer", "date"}


def test_corpus_survives_deletion_without_identifiers(tmp_path) -> None:
    s = _store(tmp_path)
    s.record_input(EMAIL, kind="message", payload="what is culture?")
    s.record_input(EMAIL, kind="answer", payload="an IRC mesh")
    s.keep_corpus(anonymized_pairs(s, EMAIL))
    s.delete_guest_inputs(EMAIL)
    rows = s.list_corpus()
    assert [(r["question"], r["answer"]) for r in rows] == [("what is culture?", "an IRC mesh")]
    assert len(rows[0]["id"]) >= 16
    with sqlite3.connect(s.path) as con:
        dump = "\n".join(con.iterdump())
    assert dump.count(EMAIL) == 1  # the deletions record only
    assert NICK not in dump and "203.0.113.9" not in dump
