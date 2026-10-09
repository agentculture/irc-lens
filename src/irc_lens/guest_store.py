"""SQLite store for guest mode: guests, consent, tokens, bans, passwords.

Security properties:

* Tokens are random, returned in plaintext exactly once by
  :meth:`GuestStore.issue_token`, and stored only as a SHA-256 hexdigest.
  They are single use and expire after ``ttl`` seconds (default 900).
* Approved-user passwords are stored only as argon2id hashes
  (``argon2-cffi``); no plaintext or reversible form is persisted.
* ``secure_delete`` is on, so deleted guest inputs are overwritten.

The clock is injectable (``clock=`` callable returning epoch seconds) so
TTL and rate-limit behaviour is testable without sleeping.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

DEFAULT_TOKEN_TTL = 900

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    email TEXT PRIMARY KEY, room_id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS guests (
    email TEXT NOT NULL, nick TEXT NOT NULL, ip TEXT NOT NULL,
    created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS consents (
    email TEXT NOT NULL, ip TEXT NOT NULL, tos_version TEXT NOT NULL,
    privacy_version TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, purpose TEXT NOT NULL,
    issued INTEGER NOT NULL, ttl INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bans (
    email TEXT, ip TEXT, reason TEXT, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inputs (
    email TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS deletions (
    email TEXT NOT NULL, ts INTEGER NOT NULL, removed INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS passwords (
    email TEXT PRIMARY KEY, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flags (
    email TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
    kind TEXT NOT NULL, key TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_idx ON attempts (kind, key, ts);
"""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class GuestStore:
    """SQLite-backed guest-mode state. Each call uses a short connection."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._hasher = PasswordHasher()  # argon2id by default
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con, con:
            con.executescript(_SCHEMA)

    def _now(self) -> int:
        return int(self._clock())

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.execute("PRAGMA secure_delete = ON")
        return con

    def _run(self, sql: str, args: tuple = ()) -> int:
        with closing(self._connect()) as con, con:
            return con.execute(sql, args).rowcount

    def _all(self, sql: str, args: tuple = ()) -> list[tuple]:
        with closing(self._connect()) as con:
            return [tuple(r) for r in con.execute(sql, args)]

    # -- guests & consent ------------------------------------------------
    def record_guest(self, email: str, nick: str, ip: str) -> None:
        self._run(
            "INSERT INTO guests (email, nick, ip, created) VALUES (?,?,?,?)",
            (email, nick, ip, self._now()),
        )

    def get_guest(self, email: str) -> list[tuple[str, str, str]]:
        return self._all(
            "SELECT email, nick, ip FROM guests WHERE email=? ORDER BY rowid", (email,)
        )

    def room_id(self, email: str) -> str:
        """The guest's private room id (``#g-<id>``), created on first use.

        Random, not derived from the nickname: once a guest is deleted the
        id is forgotten, so nobody re-using the nickname inherits the room
        or its history (d7).
        """
        with closing(self._connect()) as con, con:
            row = con.execute("SELECT room_id FROM rooms WHERE email=?", (email,)).fetchone()
            if row:
                return row[0]
            rid = secrets.token_hex(5)
            con.execute("INSERT INTO rooms (email, room_id) VALUES (?,?)", (email, rid))
            return rid

    def peek_room(self, email: str) -> str | None:
        """The guest's room id if one was ever assigned (no side effect)."""
        rows = self._all("SELECT room_id FROM rooms WHERE email=?", (email,))
        return rows[0][0] if rows else None

    def list_rooms(self) -> list[tuple[str, str]]:
        """``(email, room_id)`` for every guest that still has a room."""
        return self._all("SELECT email, room_id FROM rooms ORDER BY rowid")

    def list_guests(self) -> list[tuple[str, str, str]]:
        return self._all("SELECT email, nick, ip FROM guests ORDER BY rowid")

    def record_consent(
        self, email: str, ip: str, *, tos_version: str, privacy_version: str
    ) -> None:
        self._run(
            "INSERT INTO consents (email, ip, tos_version, privacy_version, ts) VALUES (?,?,?,?,?)",
            (email, ip, tos_version, privacy_version, self._now()),
        )

    def get_consents(self, email: str) -> list[tuple[str, str, str, str, int]]:
        return self._all(
            "SELECT email, ip, tos_version, privacy_version, ts FROM consents "
            "WHERE email=? ORDER BY rowid",
            (email,),
        )

    # -- tokens (single use, hashed, TTL) --------------------------------
    def issue_token(
        self, email: str, *, purpose: str, ttl: int = DEFAULT_TOKEN_TTL
    ) -> str:
        token = secrets.token_urlsafe(32)
        self._run(
            "INSERT INTO tokens (token_hash, email, purpose, issued, ttl) VALUES (?,?,?,?,?)",
            (_hash_token(token), email, purpose, self._now(), int(ttl)),
        )
        return token

    def verify_token(self, email: str, token: str, *, purpose: str) -> bool:
        """True exactly once for a live token matching email and purpose."""
        now = self._now()
        # Atomic: the UPDATE only matches an unused, unexpired, matching row.
        n = self._run(
            "UPDATE tokens SET used=1 WHERE token_hash=? AND email=? AND purpose=? "
            "AND used=0 AND issued + ttl > ?",
            (_hash_token(token), email, purpose, now),
        )
        return n == 1

    # -- attempt counting (rate limits for the entry task) ---------------
    def record_attempt(self, kind: str, key: str) -> None:
        self._run(
            "INSERT INTO attempts (kind, key, ts) VALUES (?,?,?)",
            (kind, key, self._now()),
        )

    def count_attempts(self, kind: str, key: str, window: int) -> int:
        rows = self._all(
            "SELECT COUNT(*) FROM attempts WHERE kind=? AND key=? AND ts > ?",
            (kind, key, self._now() - int(window)),
        )
        return rows[0][0]

    def rate_limited(self, kind: str, key: str, *, limit: int, window: int) -> bool:
        return self.count_attempts(kind, key, window) >= limit

    # -- bans & flags ----------------------------------------------------
    def ban(
        self,
        *,
        email: str | None = None,
        ip: str | None = None,
        reason: str | None = None,
    ) -> None:
        if email is None and ip is None:
            raise ValueError("ban requires an email or an ip")
        self._run(
            "INSERT INTO bans (email, ip, reason, ts) VALUES (?,?,?,?)",
            (email, ip, reason, self._now()),
        )

    def is_banned(self, email: str | None, ip: str | None) -> bool:
        rows = self._all(
            "SELECT 1 FROM bans WHERE (email IS NOT NULL AND email=?) "
            "OR (ip IS NOT NULL AND ip=?) LIMIT 1",
            (email, ip),
        )
        return bool(rows)

    def list_bans(self) -> list[tuple[str | None, str | None, str | None, int]]:
        return self._all("SELECT email, ip, reason, ts FROM bans ORDER BY rowid")

    def unban(self, *, email: str | None = None, ip: str | None = None) -> int:
        """Lift bans matching *email* and/or *ip*; returns rows removed."""
        if email is None and ip is None:
            raise ValueError("unban requires an email or an ip")
        return self._run(
            "DELETE FROM bans WHERE (email IS NOT NULL AND email=?) "
            "OR (ip IS NOT NULL AND ip=?)",
            (email, ip),
        )

    def record_flag(
        self, email: str, *, kind: str = "nsfw", detail: str | None = None
    ) -> None:
        self._run(
            "INSERT INTO flags (email, kind, detail, ts) VALUES (?,?,?,?)",
            (email, kind, detail, self._now()),
        )

    def list_flags(
        self, email: str | None = None
    ) -> list[tuple[str, str, str | None, int]]:
        sql = "SELECT email, kind, detail, ts FROM flags"
        if email is None:
            return self._all(sql + " ORDER BY rowid")
        return self._all(sql + " WHERE email=? ORDER BY rowid", (email,))

    def list_flags_with_id(
        self,
    ) -> list[tuple[int, str, str, str | None, int]]:
        """Flags as ``(id, email, kind, detail, ts)``; ids are stable rowids."""
        return self._all(
            "SELECT rowid, email, kind, detail, ts FROM flags ORDER BY rowid"
        )

    # -- guest inputs & deletion -----------------------------------------
    def record_input(self, email: str, *, kind: str, payload: str) -> None:
        self._run(
            "INSERT INTO inputs (email, kind, payload, ts) VALUES (?,?,?,?)",
            (email, kind, payload, self._now()),
        )

    def delete_guest_inputs(self, email: str) -> int:
        """Erase the guest's profile, inputs, consents, tokens, flags and
        rate-limit counters; log the deletion.

        Only the ``deletions`` record keeps the email. Bans are kept on
        purpose: erasing them would let a banned guest evade the ban by
        requesting deletion. The return value counts guest rows and inputs.
        """
        with closing(self._connect()) as con, con:
            removed = con.execute("DELETE FROM guests WHERE email=?", (email,)).rowcount
            removed += con.execute(
                "DELETE FROM inputs WHERE email=?", (email,)
            ).rowcount
            con.execute("DELETE FROM consents WHERE email=?", (email,))
            con.execute("DELETE FROM tokens WHERE email=?", (email,))
            con.execute("DELETE FROM flags WHERE email=?", (email,))
            con.execute("DELETE FROM rooms WHERE email=?", (email,))
            con.execute("DELETE FROM attempts WHERE key = ?", (f"e:{email}",))
            con.execute(
                "INSERT INTO deletions (email, ts, removed) VALUES (?,?,?)",
                (email, self._now(), removed),
            )
        return removed

    # -- approved-user passwords (argon2id only) --------------------------
    def set_password(self, email: str, password: str) -> None:
        self._run(
            "INSERT INTO passwords (email, hash) VALUES (?,?) "
            "ON CONFLICT(email) DO UPDATE SET hash=excluded.hash",
            (email, self._hasher.hash(password)),
        )

    def check_password(self, email: str, password: str) -> bool:
        rows = self._all("SELECT hash FROM passwords WHERE email=?", (email,))
        if not rows:
            return False
        try:
            return self._hasher.verify(rows[0][0], password)
        except (VerificationError, InvalidHashError):
            return False
