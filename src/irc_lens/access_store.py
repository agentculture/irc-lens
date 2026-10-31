"""SQLite store for email-keyed permissions and the sponsored agent.

Lives in the same sqlite file as :mod:`irc_lens.guest_store` and adds its
own tables through an additive, idempotent migration (``CREATE TABLE IF NOT
EXISTS`` only): an existing guest database opens unchanged and no existing
table or row is touched.

Tables:

* ``users(email, role, state, nickname)`` -- one row per known human.
  ``nickname`` is the name the person chose (or an admin set), unique
  without case, NULL until chosen; never derived from the email. Added to
  older stores by :func:`_migrate`.
* ``grants(email, resource_kind, resource, level)`` -- generic resource
  grants. ``resource_kind`` is ``'channel'`` today; a later
  ``'library_tag'`` kind needs no schema change.
* ``agent_credentials(token_hash, email, nick, created, expires, revoked)``
  -- the one sponsored agent credential per human. Tokens are returned in
  plaintext exactly once and stored only as a SHA-256 hexdigest.
* ``agent_extensions(email, until, set_by)`` -- time-boxed extensions.
* ``audit(ts, actor_hash, action, target)`` -- append-only mutation log,
  keyed by :func:`irc_lens.guest_store.email_hash`, never a plain email.
* ``admin_imports(email_hash)`` -- ledger making the one-time admin import
  idempotent (a later demotion is not undone by re-running it). It also
  holds the :data:`INITIALISED_MARK` sentinel row once the first enforced
  start succeeded, however many addresses that start imported.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar

from irc_lens import nickname as nickname_rules
from irc_lens.guest_store import email_hash

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "system"

#: Sentinel ``admin_imports`` row: "the first enforced start completed".
#: Not a hex digest, so it can never equal an imported address's hash.
INITIALISED_MARK = "store-initialised"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY, role TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants (
    email TEXT NOT NULL, resource_kind TEXT NOT NULL, resource TEXT NOT NULL,
    level TEXT NOT NULL, PRIMARY KEY (email, resource_kind, resource));
CREATE TABLE IF NOT EXISTS agent_credentials (
    token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, nick TEXT NOT NULL,
    created INTEGER NOT NULL, expires INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS agent_credentials_email_idx
    ON agent_credentials (email, revoked);
CREATE TABLE IF NOT EXISTS agent_extensions (
    email TEXT PRIMARY KEY, until INTEGER NOT NULL, set_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    ts INTEGER NOT NULL, actor_hash TEXT NOT NULL, action TEXT NOT NULL,
    target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_imports (email_hash TEXT PRIMARY KEY);
"""

_USER_FIELDS = ("email", "role", "state", "nickname")
_USER_SELECT = "SELECT email, role, state, nickname FROM users"


def _migrate(con: sqlite3.Connection) -> None:
    """Additive, idempotent upgrades of an existing store."""
    cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    if "nickname" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN nickname TEXT")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS users_nickname_idx "
        "ON users (nickname COLLATE NOCASE)"
    )


class NicknameError(ValueError):
    """A nickname the store refuses; :attr:`reason` says why.

    ``invalid``, ``reserved`` or ``taken`` (see :mod:`irc_lens.nickname`), or
    ``no-user`` for an address the store does not know. The message never
    names the email.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"nickname refused: {reason}")
        self.reason = reason


#: Backstop lifetime of a cached read, in seconds. The cache is normally
#: invalidated sooner, by the database's change counter (see _stamp).
READ_CACHE_TTL_S = 5.0

#: Busy timeout of the hot-path reads (the grant check on every received
#: line), in seconds. A writer holding the database locked longer than this
#: makes the check fail closed (deny, logged) instead of stalling the event
#: loop for the 30 s write timeout.
READ_BUSY_TIMEOUT_S = 0.25
#: Busy timeout of writes and of the reads off the hot path.
WRITE_BUSY_TIMEOUT_S = 30.0


ROLE_ADMIN = "admin"
STATE_APPROVED = "approved"
_ADMIN_COUNT_SQL = "SELECT COUNT(*) FROM users WHERE role='admin' AND state='approved'"


class LastAdminError(ValueError):
    """A change would leave the store without an approved admin."""


def _norm(email: str) -> str:
    return email.strip().lower()


def _checked_email(email: str) -> str:
    email = _norm(email)
    if ":" in email:
        # 'guest:<email>' is the guest credential key; refuse lookalikes.
        raise ValueError("an email address may not contain ':'")
    return email


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AccessStore:
    """Email-keyed permissions state. Each call uses a short connection."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            _migrate(con)
        # Hot-path read cache (finding 6): the grant check runs on every
        # received channel line, so get_user/grants_for answer from memory
        # while the database is unchanged. Keyed by query; each entry keeps
        # the change stamp it was read under and expires after
        # READ_CACHE_TTL_S at the latest.
        self._read_cache: dict[tuple, tuple[bytes, float, object]] = {}
        # Per-thread pinned change stamp (see pinned_stamp).
        self._pin = threading.local()

    def change_stamp(self) -> bytes | None:
        """The database's change counter now: equal stamps mean nothing was
        committed in between. None when it cannot be read (assume changed)."""
        pinned = getattr(self._pin, "stamp", None)
        return pinned if pinned is not None else self._stamp()

    @contextmanager
    def pinned_stamp(self) -> Iterator[bytes | None]:
        """Read the change counter once and reuse it for every cached read
        made by this thread inside the block (one file read per received
        line, however many checks the line needs). Nested blocks keep the
        outer stamp. Other threads are unaffected."""
        if getattr(self._pin, "stamp", None) is not None:
            yield self._pin.stamp
            return
        self._pin.stamp = self._stamp()
        try:
            yield self._pin.stamp
        finally:
            self._pin.stamp = None

    def _stamp(self) -> bytes | None:
        """The database file's change counter (header bytes 24-27).

        SQLite bumps it on every committed write in rollback-journal mode
        (this store's mode), from any connection or process, so a changed
        stamp means "something was written". None (no cache) on any error.
        """
        try:
            with open(self.path, "rb") as fh:
                header = fh.read(28)
        except OSError:
            return None
        return header[24:28] if len(header) == 28 else None

    def _cached(self, key: tuple, load: Callable[[], _T]) -> _T:
        stamp = self.change_stamp()
        now = time.monotonic()
        hit = self._read_cache.get(key)
        if stamp is not None and hit is not None:
            hit_stamp, expires, value = hit
            if hit_stamp == stamp and now < expires:
                return value
        value = load()  # errors propagate: callers fail closed, nothing cached
        if stamp is not None:
            if len(self._read_cache) > 4096:
                self._read_cache.clear()
            self._read_cache[key] = (stamp, now + READ_CACHE_TTL_S, value)
        return value

    def _now(self) -> int:
        return int(self._clock())

    def _connect(self, timeout: float = WRITE_BUSY_TIMEOUT_S) -> sqlite3.Connection:
        # isolation_level=None: transactions are explicit (see _tx).
        con = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        con.execute("PRAGMA secure_delete = ON")
        return con

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; BEGIN IMMEDIATE serialises concurrent writers."""
        self._read_cache.clear()
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    def _all(self, sql: str, args: tuple = ()) -> list[tuple]:
        with closing(self._connect()) as con:
            return [tuple(r) for r in con.execute(sql, args)]

    def _hot_all(self, sql: str, args: tuple = ()) -> list[tuple]:
        """A hot-path read with a short busy timeout; a locked database
        raises (callers fail closed) and is logged, never waited out."""
        try:
            with closing(self._connect(timeout=READ_BUSY_TIMEOUT_S)) as con:
                return [tuple(r) for r in con.execute(sql, args)]
        except sqlite3.OperationalError:
            logger.warning(
                "access store read timed out after %.2fs (locked); denying",
                READ_BUSY_TIMEOUT_S,
            )
            raise

    def _audit(
        self, con: sqlite3.Connection, actor: str | None, action: str, target: str
    ) -> None:
        actor_hash = email_hash(actor) if actor else SYSTEM_ACTOR
        con.execute(
            "INSERT INTO audit (ts, actor_hash, action, target) VALUES (?,?,?,?)",
            (self._now(), actor_hash, action, target),
        )

    def record_audit(self, action: str, target: str, *, actor: str | None = None) -> None:
        """Append one audit row (the actor is hashed; *target* carries no email).

        Its own write transaction: an audit row never changes access, so unlike
        the mutating methods it leaves the hot-path read cache intact.
        """
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                self._audit(con, actor, action, target)
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    # -- users & roles ---------------------------------------------------
    def set_user(self, email: str, *, role: str, state: str, actor: str | None) -> None:
        email = _checked_email(email)
        with self._tx() as con:
            self._upsert_user(con, email, role, state, actor)

    def set_user_keeping_admin(
        self, email: str, *, role: str, state: str, actor: str | None
    ) -> None:
        """Set *email*'s role and state unless that leaves no approved admin.

        The check and the write are one ``BEGIN IMMEDIATE`` transaction, so
        two concurrent demotions can never both pass it. Raises
        :class:`LastAdminError` (and changes nothing) when *email* is the
        last approved admin and would stop being one. The one guard behind
        both ``/admin`` and the ``irc-lens users`` verbs.
        """
        email = _checked_email(email)
        with self._tx() as con:
            row = con.execute(
                "SELECT role, state FROM users WHERE email=?", (email,)
            ).fetchone()
            was_admin = row is not None and tuple(row) == (ROLE_ADMIN, STATE_APPROVED)
            stays = (role, state) == (ROLE_ADMIN, STATE_APPROVED)
            if was_admin and not stays and self._admins_in(con) <= 1:
                raise LastAdminError("at least one approved admin must remain")
            self._upsert_user(con, email, role, state, actor)

    @staticmethod
    def _admins_in(con: sqlite3.Connection) -> int:
        return con.execute(_ADMIN_COUNT_SQL).fetchone()[0]

    def _upsert_user(
        self,
        con: sqlite3.Connection,
        email: str,
        role: str,
        state: str,
        actor: str | None,
    ) -> None:
        con.execute(
            "INSERT INTO users (email, role, state) VALUES (?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET role=excluded.role, state=excluded.state",
            (email, role, state),
        )
        self._audit(con, actor, "user.set", f"{email_hash(email)}:{role}:{state}")

    def get_user(self, email: str) -> dict[str, str] | None:
        email = _norm(email)
        rows = self._cached(
            ("user", email),
            lambda: self._hot_all(_USER_SELECT + " WHERE email=?", (email,)),
        )
        if not rows:
            return None
        return dict(zip(_USER_FIELDS, rows[0]))

    def list_users(self) -> list[dict[str, str]]:
        """Every known user, ordered by email."""
        rows = self._all(_USER_SELECT + " ORDER BY email")
        return [dict(zip(_USER_FIELDS, r)) for r in rows]

    def set_nickname(self, email: str, raw: str, *, actor: str | None) -> str:
        """Give *email* the nickname *raw*; return the stored form.

        The guest character rules apply (:mod:`irc_lens.nickname`), plus no
        reserved name, nothing in the agent namespace, and no other user's
        nickname (case-insensitive; the unique index backs the check).
        Raises :class:`NicknameError`. The audit row's target is the
        nickname, never the address: ``nickname.set`` for a first choice,
        ``nickname.rename`` for a change; re-setting the same one is a no-op.
        """
        email = _norm(email)
        refusal = nickname_rules.approved_refusal(raw)
        if refusal is not None:
            raise NicknameError(refusal)
        nick = nickname_rules.sanitize_nickname(raw) or ""
        with self._tx() as con:
            row = con.execute(
                "SELECT nickname FROM users WHERE email=?", (email,)
            ).fetchone()
            if row is None:
                raise NicknameError("no-user")
            if row[0] == nick:
                return nick
            other = con.execute(
                "SELECT 1 FROM users WHERE nickname=? COLLATE NOCASE AND email<>?",
                (nick, email),
            ).fetchone()
            if other is not None:
                raise NicknameError(nickname_rules.REFUSAL_TAKEN)
            con.execute("UPDATE users SET nickname=? WHERE email=?", (nick, email))
            action = "nickname.set" if row[0] is None else "nickname.rename"
            self._audit(con, actor, action, nick)
        return nick

    def admin_count(self) -> int:
        """How many users are approved admins right now."""
        return self._all(_ADMIN_COUNT_SQL)[0][0]

    # -- grants ------------------------------------------------------------
    def set_grant(
        self,
        email: str,
        resource_kind: str,
        resource: str,
        level: str,
        *,
        actor: str | None,
    ) -> None:
        email = _norm(email)
        with self._tx() as con:
            con.execute(
                "INSERT INTO grants (email, resource_kind, resource, level) "
                "VALUES (?,?,?,?) ON CONFLICT(email, resource_kind, resource) "
                "DO UPDATE SET level=excluded.level",
                (email, resource_kind, resource, level),
            )
            self._audit(
                con,
                actor,
                "grant.set",
                f"{email_hash(email)}:{resource_kind}:{resource}:{level}",
            )

    def remove_grant(
        self, email: str, resource_kind: str, resource: str, *, actor: str | None
    ) -> int:
        """Delete the grant on exactly *resource*; return the rows removed.

        0 means there was no such grant (the resource is matched exactly, so
        ``#ops`` never removes ``#Ops``); nothing changed and nothing is
        audited, so callers can say "no such grant" instead of claiming a
        revocation.
        """
        email = _norm(email)
        with self._tx() as con:
            n = con.execute(
                "DELETE FROM grants WHERE email=? AND resource_kind=? AND resource=?",
                (email, resource_kind, resource),
            ).rowcount
            if n:
                self._audit(
                    con,
                    actor,
                    "grant.remove",
                    f"{email_hash(email)}:{resource_kind}:{resource}",
                )
        return n

    def grants_for(
        self, email: str, resource_kind: str | None = None
    ) -> list[tuple[str, str, str]]:
        sql = "SELECT resource_kind, resource, level FROM grants WHERE email=?"
        args: tuple = (_norm(email),)
        if resource_kind is not None:
            sql += " AND resource_kind=?"
            args += (resource_kind,)
        rows = self._cached(
            ("grants",) + args,
            lambda: self._hot_all(sql + " ORDER BY resource_kind, resource", args),
        )
        return list(rows)

    # -- agent credentials -------------------------------------------------
    def issue_agent_credential(
        self, email: str, nick: str, *, ttl: int, actor: str | None
    ) -> str:
        """Issue a credential, revoking any prior one atomically (o1).

        Returns the plaintext token once; only its hash is stored.
        """
        email = _norm(email)
        token = secrets.token_urlsafe(32)
        now = self._now()
        with self._tx() as con:
            con.execute(
                "UPDATE agent_credentials SET revoked=1 WHERE email=? AND revoked=0",
                (email,),
            )
            con.execute(
                "INSERT INTO agent_credentials "
                "(token_hash, email, nick, created, expires, revoked) "
                "VALUES (?,?,?,?,?,0)",
                (_hash_token(token), email, nick, now, now + ttl),
            )
            self._audit(con, actor, "credential.issue", f"{email_hash(email)}:{nick}")
        return token

    def revoke_agent_credential(self, email: str, *, actor: str | None) -> int:
        email = _norm(email)
        with self._tx() as con:
            n = con.execute(
                "UPDATE agent_credentials SET revoked=1 WHERE email=? AND revoked=0",
                (email,),
            ).rowcount
            self._audit(con, actor, "credential.revoke", email_hash(email))
        return n

    def verify_agent_credential(self, token: str) -> tuple[str, str] | None:
        """``(email, nick)`` for a live (unrevoked, unexpired) token, else None."""
        rows = self._all(
            "SELECT email, nick FROM agent_credentials "
            "WHERE token_hash=? AND revoked=0 AND expires>?",
            (_hash_token(token), self._now()),
        )
        return rows[0] if rows else None

    def agent_key_for_nick(self, nick: str) -> str | None:
        """The credential key whose agent was issued *nick*.

        Lens state, not nick text: revoked and expired rows still answer,
        so a flag raised against an agent after its credential lapsed still
        reaches its human. Two humans can derive the same agent nick (a
        ``derive_nick`` collision); when rows for *nick* belong to more than
        one key the answer is None, so a flag is never attributed to the
        wrong human. That case is logged without any address.
        """
        rows = self._all(
            "SELECT DISTINCT email FROM agent_credentials "
            "WHERE nick = ? COLLATE NOCASE",
            ((nick or "").strip(),),
        )
        if len(rows) > 1:
            logger.warning(
                "agent nick is ambiguous: issued to %d credential keys; "
                "not mapping it to a human",
                len(rows),
            )
            return None
        return rows[0][0] if rows else None

    def guest_credential_emails(self) -> list[str]:
        """Emails of every guest holding any credential row (any state)."""
        rows = self._all(
            "SELECT DISTINCT email FROM agent_credentials WHERE email LIKE 'guest:%'"
        )
        return [r[0][len("guest:") :] for r in rows]

    def purge_agent_credentials(self, key: str, *, actor: str | None) -> int:
        """Delete every credential row of *key* (a ban or a deletion, o9).

        Unlike :meth:`revoke_agent_credential` the rows are removed, not
        flagged. The key's own extension goes with them. Returns the rows
        removed; the audit row carries only the hashed key.
        """
        key = _norm(key)
        with self._tx() as con:
            n = con.execute(
                "DELETE FROM agent_credentials WHERE email=?", (key,)
            ).rowcount
            con.execute("DELETE FROM agent_extensions WHERE email=?", (key,))
            self._audit(con, actor, "credential.purge", email_hash(key))
        return n

    def active_credential_count(self, email: str) -> int:
        return self._all(
            "SELECT COUNT(*) FROM agent_credentials "
            "WHERE email=? AND revoked=0 AND expires>?",
            (_norm(email), self._now()),
        )[0][0]

    # -- extensions ----------------------------------------------------------
    def set_extension(self, email: str, *, until: int, actor: str | None) -> None:
        """Set *email*'s agent extension to end at *until* (unix seconds).

        *actor* is the admin who set it (hashed in the audit row, stored as
        ``set_by``); None means the operator CLI, recorded as the plain
        :data:`SYSTEM_ACTOR` marker in both places.
        """
        email = _norm(email)
        set_by = _norm(actor) if actor else SYSTEM_ACTOR
        with self._tx() as con:
            con.execute(
                "INSERT INTO agent_extensions (email, until, set_by) VALUES (?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET until=excluded.until, "
                "set_by=excluded.set_by",
                (email, until, set_by),
            )
            self._audit(con, actor, "extension.set", f"{email_hash(email)}:{until}")

    def clear_extension(self, email: str, *, actor: str | None) -> None:
        email = _norm(email)
        with self._tx() as con:
            con.execute("DELETE FROM agent_extensions WHERE email=?", (email,))
            self._audit(con, actor, "extension.clear", email_hash(email))

    def now(self) -> int:
        """The store's clock (unix seconds), injectable for tests."""
        return self._now()

    def extension_live(self, *emails: str) -> bool:
        """True iff any of *emails* has an admin extension ending in the future."""
        for email in emails:
            until = self.extension_until(email)
            if until is not None and until > self._now():
                return True
        return False

    def extension_until(self, email: str) -> int | None:
        rows = self._all(
            "SELECT until FROM agent_extensions WHERE email=?", (_norm(email),)
        )
        return rows[0][0] if rows else None

    # -- one-time admin import (o21) -----------------------------------------
    def import_admins(self, emails: Iterable[str]) -> int:
        """Mark each not-yet-imported email approved+admin; return how many.

        Each address is imported at most once ever (ledger of hashes), so a
        re-run -- or a re-run after a deliberate demotion -- changes nothing.
        """
        count = 0
        with self._tx() as con:
            for raw in emails:
                email = _norm(raw)
                if not email:
                    continue
                h = email_hash(email)
                if (
                    con.execute(
                        "INSERT OR IGNORE INTO admin_imports (email_hash) VALUES (?)",
                        (h,),
                    ).rowcount
                    == 0
                ):
                    continue
                con.execute(
                    "INSERT INTO users (email, role, state) VALUES (?, 'admin', 'approved') "
                    "ON CONFLICT(email) DO UPDATE SET role='admin', state='approved'",
                    (email,),
                )
                self._audit(con, None, "admin.import", h)
                count += 1
        return count

    def mark_initialised(self) -> None:
        """Record that the first enforced start completed (idempotent).

        Without it, a first start that imported no address (an empty
        ``auth.allowed_emails``, the admin created by the CLI) left the
        ledger empty: every restart re-imported the list as admins, and
        switching the feature off fell back to the legacy allowlist.
        """
        with self._tx() as con:
            con.execute(
                "INSERT OR IGNORE INTO admin_imports (email_hash) VALUES (?)",
                (INITIALISED_MARK,),
            )

    # -- audit ---------------------------------------------------------------
    def audit_action_counts(self) -> dict[str, int]:
        """``{action: rows}`` over the audit table: one GROUP BY query."""
        rows = self._all("SELECT action, COUNT(*) FROM audit GROUP BY action")
        return dict(rows)

    def audit_rows(self) -> list[tuple[int, str, str, str]]:
        return self._all(
            "SELECT ts, actor_hash, action, target FROM audit ORDER BY rowid"
        )
