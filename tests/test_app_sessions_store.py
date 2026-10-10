"""Approved-user app sessions, signin/setpw tokens and their retention."""

from __future__ import annotations

import sqlite3

from irc_lens.guest_store import (
    SESSION_IDLE_S,
    SESSION_MAX_S,
    GuestStore,
)

DAY = 86400


def _store(tmp_path, t0=1_000_000):
    clock = [t0]
    return GuestStore(tmp_path / "g.db", clock=lambda: clock[0]), clock


def _all_cells(path) -> list:
    con = sqlite3.connect(path)
    try:
        cells = []
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            for row in con.execute(f"SELECT * FROM {t}"):
                cells.extend(row)
        return cells
    finally:
        con.close()


def test_limits_constants() -> None:
    assert SESSION_IDLE_S == 7 * DAY
    assert SESSION_MAX_S == 30 * DAY


def test_session_roundtrip_and_raw_id_never_stored(tmp_path) -> None:
    s, clock = _store(tmp_path)
    raw = s.create_session("a@x.org")
    assert s.get_session(raw) == ("a@x.org", clock[0], clock[0])
    assert s.get_session("nope") is None
    for cell in _all_cells(s.path):
        assert raw not in str(cell)
    assert raw != ""
    assert len(raw) >= 32


def test_touch_and_delete(tmp_path) -> None:
    s, clock = _store(tmp_path)
    raw = s.create_session("a@x.org")
    s.touch_session(raw, clock[0] + 100)
    assert s.get_session(raw)[2] == clock[0] + 100
    s.delete_session(raw)
    assert s.get_session(raw) is None


def test_delete_sessions_for_email(tmp_path) -> None:
    s, _ = _store(tmp_path)
    a1, a2 = s.create_session("a@x.org"), s.create_session("a@x.org")
    b = s.create_session("b@x.org")
    assert s.delete_sessions_for_email("a@x.org") == 2
    assert s.get_session(a1) is None
    assert s.get_session(a2) is None
    assert s.get_session(b) is not None
    assert s.delete_sessions_for_email("a@x.org") == 0


def test_get_session_enforces_idle_and_absolute_limits(tmp_path) -> None:
    s, clock = _store(tmp_path)
    raw = s.create_session("a@x.org")
    clock[0] += SESSION_IDLE_S - 1
    assert s.get_session(raw) is not None
    clock[0] += 2
    assert s.get_session(raw) is None  # idle
    raw2 = s.create_session("a@x.org")
    for _ in range(6):  # keep it active, but past 30 days absolute
        clock[0] += 6 * DAY
        s.touch_session(raw2, clock[0])
    assert s.get_session(raw2) is None


def test_token_purposes_signin_setpw_ttls(tmp_path) -> None:
    s, clock = _store(tmp_path)
    t = s.issue_token("a@x.org", purpose="signin")
    clock[0] += 599
    assert s.verify_token("a@x.org", t, purpose="signin")
    t = s.issue_token("a@x.org", purpose="signin")
    clock[0] += 600
    assert not s.verify_token("a@x.org", t, purpose="signin")
    p = s.issue_token("a@x.org", purpose="setpw")
    clock[0] += 1799
    assert s.verify_token("a@x.org", p, purpose="setpw")
    p = s.issue_token("a@x.org", purpose="setpw")
    clock[0] += 1800
    assert not s.verify_token("a@x.org", p, purpose="setpw")


def test_token_of_one_purpose_never_verifies_for_another(tmp_path) -> None:
    s, _ = _store(tmp_path)
    for a, b in [("signin", "setpw"), ("setpw", "signin"), ("signin", "guest")]:
        t = s.issue_token("a@x.org", purpose=a)
        assert not s.verify_token("a@x.org", t, purpose=b)
        assert s.verify_token("a@x.org", t, purpose=a)


def test_sweep_removes_expired_sessions_and_old_tokens(tmp_path) -> None:
    s, clock = _store(tmp_path)
    t0 = clock[0]
    idle = s.create_session("idle@x.org")
    old = s.create_session("old@x.org")
    clock[0] = t0 + 25 * DAY
    fresh = s.create_session("fresh@x.org")
    clock[0] = t0
    used = s.issue_token("a@x.org", purpose="signin")
    assert s.verify_token("a@x.org", used, purpose="signin")
    s.issue_token("a@x.org", purpose="setpw")  # expired, never used
    s.touch_session(old, t0 + 29 * DAY)
    s.touch_session(fresh, t0 + 29 * DAY)
    s.touch_session(idle, t0 + 20 * DAY)
    now = t0 + 30 * DAY + 1
    counts = s.sweep(now=now)
    assert counts["sessions"] == 2  # idle >7d and created >30d
    assert s.get_session(fresh) is not None
    assert counts_remaining(s) == 1
    assert s._all("SELECT COUNT(*) FROM tokens")[0][0] == 0


def counts_remaining(s) -> int:
    return s._all("SELECT COUNT(*) FROM sessions")[0][0]


def test_sweep_keeps_live_sessions_and_recent_tokens(tmp_path) -> None:
    s, clock = _store(tmp_path)
    raw = s.create_session("a@x.org")
    s.issue_token("a@x.org", purpose="signin")
    counts = s.sweep(now=clock[0] + 3600)
    assert counts["sessions"] == 0
    assert counts["tokens"] == 0
    assert s.get_session(raw) is not None


def test_sweep_at_day_plus_one_leaves_no_expired_rows(tmp_path) -> None:
    s, clock = _store(tmp_path)
    t0 = clock[0]
    s.create_session("a@x.org")
    used = s.issue_token("a@x.org", purpose="setpw")
    s.verify_token("a@x.org", used, purpose="setpw")
    s.issue_token("a@x.org", purpose="signin")
    s.sweep(now=t0 + 8 * DAY)  # session idle 8d
    assert s._all("SELECT COUNT(*) FROM sessions")[0][0] == 0
    assert s._all("SELECT COUNT(*) FROM tokens")[0][0] == 0


def test_old_store_upgrades_in_place(tmp_path) -> None:
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE passwords (email TEXT PRIMARY KEY, hash TEXT NOT NULL)")
    con.commit()
    con.close()
    s = GuestStore(path)
    assert s.get_session(s.create_session("a@x.org")) is not None
