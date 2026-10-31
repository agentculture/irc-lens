"""SQLite store for email-keyed permissions and the sponsored agent.

Lives in the same sqlite file as :mod:`irc_lens.guest_store` and adds its
own tables through an additive, idempotent migration (``CREATE TABLE IF NOT
EXISTS`` only): an existing guest database opens unchanged and no existing
table or row is touched.

Tables:

* ``users(email, role, state)`` -- one row per known human.
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
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator

from irc_lens.guest_store import email_hash

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


#: Backstop lifetime of a cached read, in seconds. The cache is normally
#: invalidated sooner, by the database's change counter (see _stamp).
READ_CACHE_TTL_S = 5.0


def _norm(email: str) -> str:
    return email.strip().lower()


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
        # Hot-path read cache (finding 6): the grant check runs on every
        # received channel line, so get_user/grants_for answer from memory
        # while the database is unchanged. Keyed by query; each entry keeps
        # the change stamp it was read under and expires after
        # READ_CACHE_TTL_S at the latest.
        self._read_cache: dict[tuple, tuple[bytes, float, object]] = {}

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

    def _cached(self, key: tuple, load: Callable[[], object]) -> object:
        stamp = self._stamp()
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

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: transactions are explicit (see _tx).
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
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

    def _audit(
        self, con: sqlite3.Connection, actor: str | None, action: str, target: str
    ) -> None:
        actor_hash = email_hash(actor) if actor else SYSTEM_ACTOR
        con.execute(
            "INSERT INTO audit (ts, actor_hash, action, target) VALUES (?,?,?,?)",
            (self._now(), actor_hash, action, target),
        )

    # -- users & roles ---------------------------------------------------
    def set_user(self, email: str, *, role: str, state: str, actor: str | None) -> None:
        email = _norm(email)
        with self._tx() as con:
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
            lambda: self._all(
                "SELECT email, role, state FROM users WHERE email=?", (email,)
            ),
        )
        if not rows:
            return None
        return dict(zip(("email", "role", "state"), rows[0]))

    def list_users(self) -> list[dict[str, str]]:
        """Every known user, ordered by email."""
        rows = self._all("SELECT email, role, state FROM users ORDER BY email")
        return [dict(zip(("email", "role", "state"), r)) for r in rows]

    def admin_count(self) -> int:
        """How many users are approved admins right now."""
        return self._all(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND state='approved'"
        )[0][0]

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
            lambda: self._all(sql + " ORDER BY resource_kind, resource", args),
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
        """The credential key whose agent was issued *nick* (newest row).

        Lens state, not nick text: revoked and expired rows still answer,
        so a flag raised against an agent after its credential lapsed still
        reaches its human.
        """
        rows = self._all(
            "SELECT email FROM agent_credentials WHERE nick = ? COLLATE NOCASE "
            "ORDER BY created DESC, rowid DESC LIMIT 1",
            ((nick or "").strip(),),
        )
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
    def set_extension(self, email: str, *, until: int, set_by: str) -> None:
        email = _norm(email)
        with self._tx() as con:
            con.execute(
                "INSERT INTO agent_extensions (email, until, set_by) VALUES (?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET until=excluded.until, "
                "set_by=excluded.set_by",
                (email, until, _norm(set_by)),
            )
            self._audit(con, set_by, "extension.set", f"{email_hash(email)}:{until}")

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
    def audit_rows(self) -> list[tuple[int, str, str, str]]:
        return self._all(
            "SELECT ts, actor_hash, action, target FROM audit ORDER BY rowid"
        )
