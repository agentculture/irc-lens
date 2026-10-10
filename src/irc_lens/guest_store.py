"""SQLite store for guest mode: guests, consent, tokens, bans, passwords.

Security properties:

* Tokens are random, returned in plaintext exactly once by
  :meth:`GuestStore.issue_token`, and stored only as a SHA-256 hexdigest.
  They are single use and expire after ``ttl`` seconds (default 900).
* Approved-user passwords are stored only as argon2id hashes
  (``argon2-cffi``); no plaintext or reversible form is persisted.
* ``secure_delete`` is on, so deleted guest inputs are overwritten.
* A deletion keeps nothing of the guest's conversation (d9); the deletion
  log stores only :func:`email_hash`, never the email.
* :meth:`GuestStore.sweep` enforces retention: inactive guests are erased
  and tokens, rate-limit attempts and old bans expire.

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
#: Per-purpose token lifetimes for the app-native sign-in flow.
TOKEN_TTLS = {"signin": 600, "setpw": 1800}
#: App sessions expire after this much idle time or this much total age.
SESSION_IDLE_S = 7 * 86400
SESSION_MAX_S = 30 * 86400
#: Retention (d9): guests inactive this long are erased by :meth:`GuestStore.sweep`.
DEFAULT_INACTIVE_DAYS = 90
_DAY_S = 86400
TOKEN_RETENTION_S = _DAY_S
ATTEMPT_RETENTION_S = _DAY_S
BAN_RETENTION_S = 365 * _DAY_S
#: Trusted browsers (``lens_device``) last one year from the trusting sign-in.
TRUSTED_DEVICE_S = 365 * _DAY_S
#: Untrusted sign-in budget per email (r6/c38): wrong password submissions
#: only (code entries, c41, and correct passwords, c42, are not counted).
#: Normal: 3 per 15 minutes. Once it
#: has been exhausted the email is strict -- 2 per 30 minutes -- until 24
#: hours pass with no blocked attempt.
SIGNIN_BUDGET = (3, 900)
SIGNIN_BUDGET_STRICT = (2, 1800)
SIGNIN_BUDGET_QUIET_S = _DAY_S
SIGNIN_BUDGET_KIND = "signin-email"

# d9 removed the anonymized ``corpus`` table (deletion keeps nothing).
_SCHEMA = """
DROP TABLE IF EXISTS corpus;
CREATE TABLE IF NOT EXISTS rooms (
    email TEXT PRIMARY KEY, room_id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS guests (
    email TEXT NOT NULL, nick TEXT NOT NULL, ip TEXT NOT NULL,
    created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS consents (
    email TEXT NOT NULL, ip TEXT NOT NULL, tos_version TEXT NOT NULL,
    privacy_version TEXT NOT NULL, ts INTEGER NOT NULL,
    train INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, purpose TEXT NOT NULL,
    issued INTEGER NOT NULL, ttl INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
-- Binds a sign-in code to the browser that entered the password: both
-- columns are sha256 digests (pending sign-in cookie value, code).
CREATE TABLE IF NOT EXISTS signin_pending (
    pending_hash TEXT PRIMARY KEY, token_hash TEXT NOT NULL);
-- sessions.id_hash is sha256(raw session id); the raw id lives only in the cookie.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY, email TEXT NOT NULL,
    created INTEGER NOT NULL, last_seen INTEGER NOT NULL);
-- Trusted browsers: device_hash is sha256(raw lens_device id); one browser
-- may be trusted for several emails, each row exempts it for that email only.
CREATE TABLE IF NOT EXISTS trusted_devices (
    device_hash TEXT NOT NULL, email TEXT NOT NULL,
    created INTEGER NOT NULL, last_used INTEGER NOT NULL,
    PRIMARY KEY (device_hash, email));
-- Escalation state of the untrusted sign-in budget: the last blocked attempt.
CREATE TABLE IF NOT EXISTS signin_budget (
    email TEXT PRIMARY KEY, last_blocked INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bans (
    email TEXT, ip TEXT, reason TEXT, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inputs (
    email TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    ts INTEGER NOT NULL);
-- deletions.email holds email_hash(email), never the address (d9).
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


def session_id_hash(raw_id: str) -> str:
    """sha256 hexdigest of a raw app-session id (the ``sessions.id_hash``)."""
    return _hash_token(raw_id)


def email_hash(email: str) -> str:
    """SHA-256 hexdigest of the lower-cased email: the deletion log's key.

    Lets the owner answer "was this address deleted?" for an address they
    already know, without the log itself holding any address.
    """
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _migrate(con: sqlite3.Connection) -> None:
    """Bring an existing store up to the current schema (idempotent)."""
    cols = {row[1] for row in con.execute("PRAGMA table_info(consents)")}
    if "train" not in cols:
        con.execute(
            "ALTER TABLE consents ADD COLUMN train INTEGER NOT NULL DEFAULT 0"
        )
    # Deletion records written before d9 held the plaintext email.
    plain = con.execute(
        "SELECT rowid, email FROM deletions WHERE email LIKE '%@%'"
    ).fetchall()
    con.executemany(
        "UPDATE deletions SET email=? WHERE rowid=?",
        [(email_hash(e), rowid) for rowid, e in plain],
    )


# The latest consent row of each email (newest ts, then newest rowid).
_LATEST_CONSENT = (
    "SELECT rowid FROM consents c2 WHERE c2.email = consents.email "
    "ORDER BY c2.ts DESC, c2.rowid DESC LIMIT 1"
)


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
            _migrate(con)

    def _now(self) -> int:
        return int(self._clock())

    def now(self) -> int:
        """The store's clock (unix seconds), injectable for tests."""
        return self._now()

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
        self,
        email: str,
        ip: str,
        *,
        tos_version: str,
        privacy_version: str,
        train: bool = False,
    ) -> None:
        """Record Terms/Privacy consent; *train* is the separate, optional
        opt-in to training use of the guest's conversations (d9)."""
        self._run(
            "INSERT INTO consents (email, ip, tos_version, privacy_version, ts, train) "
            "VALUES (?,?,?,?,?,?)",
            (email, ip, tos_version, privacy_version, self._now(), int(bool(train))),
        )

    def get_consents(self, email: str) -> list[tuple[str, str, str, str, int]]:
        return self._all(
            "SELECT email, ip, tos_version, privacy_version, ts FROM consents "
            "WHERE email=? ORDER BY rowid",
            (email,),
        )

    def training_opt_in(self, email: str) -> bool:
        """True when the guest's most recent consent row opted in to training."""
        rows = self._all(
            "SELECT train FROM consents WHERE email=? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (email,),
        )
        return bool(rows and rows[0][0])

    def training_emails(self) -> set[str]:
        """Every email whose most recent consent row has ``train=1``."""
        rows = self._all(
            f"SELECT email FROM consents WHERE rowid = ({_LATEST_CONSENT}) AND train=1"
        )
        return {r[0] for r in rows}

    def set_training(self, email: str, on: bool) -> int:
        """Set the training opt-in on the guest's latest consent row.

        For the owner (withdrawal arrives by email). Returns rows updated:
        0 when the email has no consent on record.
        """
        return self._run(
            "UPDATE consents SET train=? WHERE rowid = ("
            "SELECT rowid FROM consents WHERE email=? ORDER BY ts DESC, rowid DESC LIMIT 1)",
            (int(bool(on)), email),
        )

    # -- tokens (single use, hashed, TTL) --------------------------------
    def issue_token(
        self, email: str, *, purpose: str, ttl: int | None = None
    ) -> str:
        if ttl is None:
            ttl = TOKEN_TTLS.get(purpose, DEFAULT_TOKEN_TTL)
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

    def peek_token(self, token: str, *, purpose: str) -> str | None:
        """Email of a live (unused, unexpired) *purpose* token; never consumes.

        For links that carry only the token (``/password/<token>``): a GET
        that a mail scanner may prefetch must leave the token usable.
        """
        rows = self._all(
            "SELECT email FROM tokens WHERE token_hash=? AND purpose=? "
            "AND used=0 AND issued + ttl > ?",
            (_hash_token(token), purpose, self._now()),
        )
        return rows[0][0] if rows else None

    def consume_token(self, token: str, *, purpose: str) -> str | None:
        """Use a live *purpose* token up; return its email exactly once."""
        now = self._now()
        with closing(self._connect()) as con, con:
            row = con.execute(
                "SELECT email FROM tokens WHERE token_hash=? AND purpose=? "
                "AND used=0 AND issued + ttl > ?",
                (_hash_token(token), purpose, now),
            ).fetchone()
            if row is None:
                return None
            # Atomic: only one caller's UPDATE matches the still-unused row.
            n = con.execute(
                "UPDATE tokens SET used=1 WHERE token_hash=? AND purpose=? "
                "AND used=0 AND issued + ttl > ?",
                (_hash_token(token), purpose, now),
            ).rowcount
        return row[0] if n == 1 else None

    def bind_signin(self, pending: str, token: str) -> None:
        """Tie sign-in *token* to the pending sign-in cookie value *pending*.

        Only sha256 digests of both are stored.
        """
        self._run(
            "INSERT OR REPLACE INTO signin_pending (pending_hash, token_hash) "
            "VALUES (?,?)",
            (_hash_token(pending), _hash_token(token)),
        )

    def verify_signin_token(self, email: str, token: str, pending: str) -> bool:
        """True exactly once for a live ``signin`` token bound to *pending*.

        Like :meth:`verify_token` (single use, TTL, email) plus the binding
        made by :meth:`bind_signin`; one atomic UPDATE, so a code from
        another browser, a reused code and an expired code all fail alike.
        """
        n = self._run(
            "UPDATE tokens SET used=1 WHERE token_hash=? AND email=? "
            "AND purpose='signin' AND used=0 AND issued + ttl > ? "
            "AND token_hash IN (SELECT token_hash FROM signin_pending "
            "WHERE pending_hash=?)",
            (_hash_token(token), email, self._now(), _hash_token(pending)),
        )
        return n == 1

    # -- approved-user app sessions (id stored only as sha256) -------------
    def create_session(self, email: str) -> str:
        """Start a session; return the raw id (never stored, only its hash)."""
        raw = secrets.token_urlsafe(32)
        now = self._now()
        self._run(
            "INSERT INTO sessions (id_hash, email, created, last_seen) VALUES (?,?,?,?)",
            (_hash_token(raw), email, now, now),
        )
        return raw

    def get_session(self, raw_id: str) -> tuple[str, int, int] | None:
        """``(email, created, last_seen)`` for a live session, else None.

        Idle over :data:`SESSION_IDLE_S` or older than :data:`SESSION_MAX_S`
        counts as gone (the sweep removes the row later).
        """
        now = self._now()
        rows = self._all(
            "SELECT email, created, last_seen FROM sessions WHERE id_hash=? "
            "AND last_seen >= ? AND created >= ?",
            (_hash_token(raw_id), now - SESSION_IDLE_S, now - SESSION_MAX_S),
        )
        return rows[0] if rows else None

    def session_email_by_hash(self, id_hash: str) -> str | None:
        """Email of the live session whose sha256 id is *id_hash*, else None.

        Same idle/max-age rules as :meth:`get_session`; used by the web
        app's revocation sweep, which only ever holds the hash.
        """
        now = self._now()
        rows = self._all(
            "SELECT email FROM sessions WHERE id_hash=? "
            "AND last_seen >= ? AND created >= ?",
            (id_hash, now - SESSION_IDLE_S, now - SESSION_MAX_S),
        )
        return rows[0][0] if rows else None

    def touch_session(self, raw_id: str, now: int | None = None) -> None:
        self._run(
            "UPDATE sessions SET last_seen=? WHERE id_hash=?",
            (self._now() if now is None else int(now), _hash_token(raw_id)),
        )

    def delete_session(self, raw_id: str) -> int:
        return self._run("DELETE FROM sessions WHERE id_hash=?", (_hash_token(raw_id),))

    def delete_sessions_for_email(self, email: str) -> int:
        """Revoke every session of *email*; returns how many were removed."""
        return self._run("DELETE FROM sessions WHERE email=?", (email,))

    # -- trusted browsers (id stored only as sha256) ------------------------
    def add_trusted_device(self, email: str, previous_raw: str | None = None) -> str:
        """Trust a fresh browser id for *email*; return the raw id.

        A fresh random id is minted every time (a cookie the browser held
        before is never promoted). Trust rows of *previous_raw* (this
        browser's earlier ``lens_device``, e.g. for another email) move to
        the new id and keep their original year (moving never extends trust).
        """
        raw = secrets.token_urlsafe(32)
        new_hash, now = _hash_token(raw), self._now()
        with closing(self._connect()) as con, con:
            if previous_raw:
                con.execute(
                    "UPDATE trusted_devices SET device_hash=? "
                    "WHERE device_hash=? AND email<>? AND created > ?",
                    (new_hash, _hash_token(previous_raw), email,
                     now - TRUSTED_DEVICE_S),
                )
                con.execute(
                    "DELETE FROM trusted_devices WHERE device_hash=?",
                    (_hash_token(previous_raw),),
                )
            con.execute(
                "INSERT OR REPLACE INTO trusted_devices "
                "(device_hash, email, created, last_used) VALUES (?,?,?,?)",
                (new_hash, email, now, now),
            )
        return raw

    def is_trusted_device(self, raw: str | None, email: str) -> bool:
        """True iff browser id *raw* is trusted for *email* (and under a year old)."""
        if not raw:
            return False
        rows = self._all(
            "SELECT 1 FROM trusted_devices WHERE device_hash=? AND email=? "
            "AND created > ?",
            (_hash_token(raw), email, self._now() - TRUSTED_DEVICE_S),
        )
        return bool(rows)

    def touch_trusted_device(self, raw: str, email: str) -> None:
        self._run(
            "UPDATE trusted_devices SET last_used=? WHERE device_hash=? AND email=?",
            (self._now(), _hash_token(raw), email),
        )

    def delete_trusted_devices_for_email(self, email: str) -> int:
        """Revoke every trusted browser of *email*; returns rows removed."""
        return self._run("DELETE FROM trusted_devices WHERE email=?", (email,))

    # -- untrusted sign-in budget per email (r6/c38) ---------------------
    def signin_budget_take(self, email: str) -> bool:
        """Count one untrusted sign-in attempt for *email*; False if blocked.

        Only attempts that pass are counted. Normal budget
        :data:`SIGNIN_BUDGET`; while the last blocked attempt is less than
        :data:`SIGNIN_BUDGET_QUIET_S` old the strict
        :data:`SIGNIN_BUDGET_STRICT` applies. A blocked attempt (re)starts
        the strict period. One transaction, so concurrent takes can't both
        slip past the last slot.
        """
        now, key = self._now(), f"e:{email}"
        with closing(self._connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT last_blocked FROM signin_budget WHERE email=?", (email,)
            ).fetchone()
            strict = row is not None and now - row[0] < SIGNIN_BUDGET_QUIET_S
            limit, window = SIGNIN_BUDGET_STRICT if strict else SIGNIN_BUDGET
            (used,) = con.execute(
                "SELECT COUNT(*) FROM attempts WHERE kind=? AND key=? AND ts > ?",
                (SIGNIN_BUDGET_KIND, key, now - window),
            ).fetchone()
            if used >= limit:
                con.execute(
                    "INSERT INTO signin_budget (email, last_blocked) VALUES (?,?) "
                    "ON CONFLICT(email) DO UPDATE "
                    "SET last_blocked=excluded.last_blocked",
                    (email, now),
                )
                return False
            con.execute(
                "INSERT INTO attempts (kind, key, ts) VALUES (?,?,?)",
                (SIGNIN_BUDGET_KIND, key, now),
            )
            return True

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

    def unrecord_attempt(self, kind: str, key: str) -> None:
        """Take back the most recent *kind*/*key* attempt (one row).

        Used when an attempt turns out not to count: a correct password on
        app sign-in (c42). Counting first and refunding after keeps the
        limit check atomic.
        """
        self._run(
            "DELETE FROM attempts WHERE rowid = (SELECT MAX(rowid) FROM attempts "
            "WHERE kind=? AND key=?)",
            (kind, key),
        )

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

    def inputs_for(self, email: str) -> list[tuple[str, str, int]]:
        """``(kind, payload, ts)`` for one guest, oldest first."""
        return self._all(
            "SELECT kind, payload, ts FROM inputs WHERE email=? ORDER BY ts, rowid",
            (email,),
        )

    def delete_guest_inputs(self, email: str) -> int:
        """Erase the guest's profile, inputs, consents, tokens, flags, room
        and rate-limit counters; log the deletion.

        Nothing of the guest's conversation is kept (d9). The ``deletions``
        record holds only :func:`email_hash`, never the address. Bans are
        kept on purpose: erasing them would let a banned guest evade the ban
        by requesting deletion. The return value counts guest rows and inputs.
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
                (email_hash(email), self._now(), removed),
            )
        return removed

    # -- retention (d9) ----------------------------------------------------
    def inactive_guests(self, cutoff: int) -> list[str]:
        """Emails whose last activity (max of guests.created, inputs.ts and
        consents.ts) is older than *cutoff*. A guest-linked row with no
        activity at all (e.g. an orphaned room) counts as inactive."""
        rows = self._all(
            "SELECT email, MAX(ts) FROM ("
            " SELECT email, created AS ts FROM guests"
            " UNION ALL SELECT email, ts FROM inputs"
            " UNION ALL SELECT email, ts FROM consents"
            " UNION ALL SELECT email, NULL FROM rooms"
            ") GROUP BY email HAVING MAX(ts) IS NULL OR MAX(ts) < ? ORDER BY email",
            (int(cutoff),),
        )
        return [r[0] for r in rows]

    def sweep(
        self,
        now: int | None = None,
        *,
        inactive_days: int = DEFAULT_INACTIVE_DAYS,
        erase: Callable[[str], int] | None = None,
    ) -> dict:
        """Apply the retention policy once; return per-category counts.

        * ``guests``: every guest inactive for more than *inactive_days* is
          erased through *erase* (default :meth:`delete_guest_inputs`; the
          web app passes the full user-deletion path, which also clears
          uploads and flag-log lines) and logged by hash.
        * ``flags``: flag rows of those guests (erased with them).
        * ``tokens`` issued more than a day ago (so used or expired
          guest, signin and setpw tokens are gone a day later).
        * ``sessions`` idle over 7 days or older than 30 days.
        * ``attempts`` (rate-limit counters) older than a day.
        * ``bans`` older than 365 days.
        * ``trusted_devices`` trusted more than 365 days ago.
        * ``signin_budget`` rows whose last block is over 24 hours old.
        """
        now = self._now() if now is None else int(now)
        erase = erase or self.delete_guest_inputs
        expired = self.inactive_guests(now - int(inactive_days) * _DAY_S)
        counts = {
            "guests": 0,
            "flags": 0,
            "tokens": 0,
            "sessions": 0,
            "attempts": 0,
            "bans": 0,
            "trusted_devices": 0,
            "signin_budget": 0,
        }
        for email in expired:
            counts["flags"] += self._all(
                "SELECT COUNT(*) FROM flags WHERE email=?", (email,)
            )[0][0]
            erase(email)
            counts["guests"] += 1
        with closing(self._connect()) as con, con:
            counts["tokens"] = con.execute(
                "DELETE FROM tokens WHERE issued < ?", (now - TOKEN_RETENTION_S,)
            ).rowcount
            con.execute(
                "DELETE FROM signin_pending WHERE token_hash NOT IN "
                "(SELECT token_hash FROM tokens)"
            )
            counts["sessions"] = con.execute(
                "DELETE FROM sessions WHERE last_seen < ? OR created < ?",
                (now - SESSION_IDLE_S, now - SESSION_MAX_S),
            ).rowcount
            counts["attempts"] = con.execute(
                "DELETE FROM attempts WHERE ts < ?", (now - ATTEMPT_RETENTION_S,)
            ).rowcount
            counts["bans"] = con.execute(
                "DELETE FROM bans WHERE ts < ?", (now - BAN_RETENTION_S,)
            ).rowcount
            counts["trusted_devices"] = con.execute(
                "DELETE FROM trusted_devices WHERE created <= ?",
                (now - TRUSTED_DEVICE_S,),
            ).rowcount
            counts["signin_budget"] = con.execute(
                "DELETE FROM signin_budget WHERE last_blocked <= ?",
                (now - SIGNIN_BUDGET_QUIET_S,),
            ).rowcount
        return counts

    # -- approved-user passwords (argon2id only) --------------------------
    def set_password(self, email: str, password: str) -> None:
        """Set *email*'s password and revoke every trusted browser of it (c37)."""
        hashed = self._hasher.hash(password)
        with closing(self._connect()) as con, con:
            con.execute(
                "INSERT INTO passwords (email, hash) VALUES (?,?) "
                "ON CONFLICT(email) DO UPDATE SET hash=excluded.hash",
                (email, hashed),
            )
            con.execute("DELETE FROM trusted_devices WHERE email=?", (email,))

    def check_password(self, email: str, password: str) -> bool:
        rows = self._all("SELECT hash FROM passwords WHERE email=?", (email,))
        if not rows:
            return False
        try:
            return self._hasher.verify(rows[0][0], password)
        except (VerificationError, InvalidHashError):
            return False
