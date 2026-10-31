"""A personal agent's lens-held IRC connection, filtered on the receive side.

Every authenticated agent (``web/agent_auth.py``) gets its own IRC
connection, held by the lens, opened lazily on its first tool call:

Where it lives
    In the session registry under ``agent:<credential key>``
    (:func:`irc_lens.web.agent_auth.agent_registry_principal`), apart from
    its human's browser sessions, so a browser logout (irc-lens#70) never
    tears it down and it never takes a guest seat. Re-issuing the credential,
    a failed human re-check and the agent sweep close it.

Which backend, which grants
    An approved human's agent connects to the mesh with exactly its human's
    grants (``Session.set_access(store, <human email>)``; the agent has no
    rows of its own). A guest's agent connects to the sandbox and joins only
    its own human's private room (``#g-<id>`` from the guest store) --
    never the all-rooms guest view.

Its nick
    :func:`irc_lens.agent_nick.agent_nick_for` of its human's nick, in the
    reserved ``<server>-pa_-`` namespace no human nick can enter. The nick
    stored with the credential is re-derived from lens state (the human's
    current nick) before every open; a mismatch refuses the connection
    rather than trusting the stored text. The IRCd's nick uniqueness keeps
    anyone else from holding it while the agent is connected.

Owner-only reach (o2/o13)
    Filtering runs on the *receive* side of this connection, so it covers
    every sender -- other lens users, federated peers and raw IRC clients
    that never passed through the lens. A DM, or a channel line that names
    the agent, from anyone but the agent's human is withheld (counted,
    audited as :data:`WITHHELD_ACTION`, logged without nick or email) and
    never reaches an MCP read. The connection keeps no unfiltered buffer:
    the transport's message buffer is replaced by a sink, and reads come
    only from the filtered inbox (no server-side HISTORY is replayed to an
    agent).

    "The human" is lens state, never nick text: a sender is the owner only
    when the human's *own lens-held session* is live in the registry and
    holds that nick (which the human's nick from the stores must equal). A
    namesake signed in under the same derived nick, or a raw client that
    grabbed the nick while the human was away, is not the owner. Every
    delivered line carries ``owner: true|false``.

The send path (o15)
    Agent sends go through :mod:`irc_lens.web.input_gate` -- the same
    ``/delete`` refusal, per-human rate limit, sandbox allowlist and input
    log as the human's web ``/input`` -- and then through
    ``Session.execute``, the browser's own dispatch, with its grant checks.
    Message text is always chat: it is never re-parsed as a slash command.
    Targets are validated before anything reaches IRC; an agent may DM only
    its own human.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from irc_lens._errors import EXIT_USER_ERROR, AfiError
from irc_lens.agent_nick import agent_nick_for, is_agent_nick, server_name_for
from irc_lens.commands import CommandType, ParsedCommand
from irc_lens.session import LensConnectionLost
from irc_lens.web import app_session, input_gate
from irc_lens.web.identity import TIER_APPROVED, derive_nick
from irc_lens.web.sessions import (
    BACKEND_MESH,
    BACKEND_SANDBOX,
    GUEST_PRINCIPAL_PREFIX,
)

logger = logging.getLogger(__name__)

#: Audit action for a line withheld from an agent (a cross-human delivery).
WITHHELD_ACTION = "agent.delivery.withheld"

#: Attribute on the registry's Session holding its :class:`AgentSession`.
STATE_ATTR = "agent_state"

#: Inbox bound (lines kept for MCP reads, oldest dropped first).
INBOX_MAX = 500
#: Longest message an agent may send (the web console's body cap).
TEXT_MAX = 4000
#: At most one audit row per (reason, target) per this many seconds.
AUDIT_EVERY_S = 60.0

_NICK_CHARS = r"A-Za-z0-9_\-\[\]\\`^{}|"
# A channel name refuses whitespace (``\s``: Unicode, so it already covers
# \t-\r and \x1c-\x1f), ',', ':' and the remaining C0 controls + DEL. The
# control ranges skip what ``\s`` matches so the class has no duplicates.
_CHANNEL_RE = re.compile(r"#[^\s,:\x00-\x08\x0e-\x1b\x7f]{1,63}")
_NICK_RE = re.compile(rf"[{_NICK_CHARS}]{{1,64}}")

#: Refusal texts shared by several tools.
_LOST_ERROR = "connection to IRC lost"
_LOST_HINT = "call the tool again"
_CHANNEL_HINT = "a #channel name"

#: The ``target`` of a delivered direct message.
DM_TARGET = "DM"

# IRC formatting: colour (\x03 fg[,bg]), hex colour (\x04 rrggbb[,rrggbb]),
# then the single-byte toggles (bold, italic, underline, strike, mono,
# reverse, reset).
_IRC_FORMAT_RE = re.compile(
    r"\x03(?:\d{1,2}(?:,\d{1,2})?)?"
    r"|\x04(?:[0-9A-Fa-f]{6}(?:,[0-9A-Fa-f]{6})?)?"
    r"|[\x02\x0f\x11\x16\x1d\x1e\x1f]"
)


def visible_text(text: str) -> str:
    """*text* as a reader sees it: IRC formatting codes and Unicode format
    characters (zero-width space/joiner, word joiner, bidi marks, ...)
    removed, so they cannot hide a name from a text match."""
    stripped = _IRC_FORMAT_RE.sub("", text or "")
    return "".join(ch for ch in stripped if unicodedata.category(ch) != "Cf")


@dataclass(frozen=True)
class AgentLine:
    seq: int
    target: str
    nick: str
    text: str
    kind: str
    owner: bool
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "nick": self.nick,
            "text": self.text,
            "kind": self.kind,
            "owner": self.owner,
            "ts": self.ts,
        }


class _NoBuffer:
    """Stands in for the transport's message buffer: keeps nothing.

    An agent's connection must hold no unfiltered copy of what it received.
    """

    def add(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def read(self, *_args: Any, **_kwargs: Any) -> list:
        return []

    def known_nicks(self) -> set[str]:
        return set()


def _refused(message: str, hint: str = "") -> AfiError:
    return AfiError(code=EXIT_USER_ERROR, message=message, remediation=hint)


def valid_channel(name: str) -> bool:
    return bool(_CHANNEL_RE.fullmatch(name or ""))


def valid_nick(name: str) -> bool:
    return bool(_NICK_RE.fullmatch(name or ""))


def one_line(text: str) -> str:
    """Collapse *text* to one IRC line: no CR/LF/NUL ever reaches the wire."""
    return " ".join(str(text or "").replace("\x00", " ").split())


def human_for_agent_nick(store: Any, nick: str) -> str | None:
    """The credential key (email, or ``guest:<email>``) of *nick*'s human.

    Read from the lens's credential rows -- never inferred from the nick's
    text. None for a nick no credential was ever issued for.
    """
    if store is None or not nick:
        return None
    try:
        return store.agent_key_for_nick(nick)
    except Exception:  # noqa: BLE001 - unreadable store => unknown
        return None


@dataclass(frozen=True)
class HumanState:
    """The agent's human, from lens state (stores + config)."""

    registry_principal: str  # the human's browser key in the registry
    backend: str
    nick: str  # the human's own nick
    room: str = ""  # a guest's private sandbox room


def human_state(app: web.Application, principal: Any) -> HumanState | None:
    """Blocking: the human behind *principal*, or None if unknown."""
    config = app["config"]
    if principal.tier == TIER_APPROVED:
        try:
            nick = derive_nick(config.server_name, principal.email)
        except ValueError:
            return None
        return HumanState(principal.email, BACKEND_MESH, nick)
    store = app.get("guest_store")
    if store is None or not config.guest_enabled:
        return None
    rows = store.get_guest(principal.email)
    if not rows:
        return None
    room = config.guest_room_prefix + store.room_id(principal.email)
    return HumanState(
        GUEST_PRINCIPAL_PREFIX + principal.email, BACKEND_SANDBOX, rows[-1][1], room
    )


class AgentSession:
    """Receive-side filter, inbox and gated send path of one agent."""

    def __init__(
        self,
        app: web.Application,
        session: Any,
        *,
        credential_key: str,
        email: str,
        tier: str,
        human: HumanState,
    ) -> None:
        self.app = app
        self.session = session
        self.credential_key = credential_key
        self.email = email
        self.tier = tier
        self.human = human
        self.withheld = 0
        self._inbox: deque[AgentLine] = deque(maxlen=INBOX_MAX)
        self._seq = 0
        self._seen: dict[str, int] = {}
        self._last_audit: dict[tuple[str, str], float] = {}

    # -- set-up -------------------------------------------------------------

    def attach(self) -> None:
        """Install the filter before the connection opens (``prepare`` hook)."""
        sink = _NoBuffer()
        self.session.buffer = sink
        self.session._transport.buffer = sink  # pylint: disable=protected-access
        transport = self.session._transport  # pylint: disable=protected-access
        transport.add_listener("PRIVMSG", self._on_message)
        transport.add_listener("NOTICE", self._on_message)
        setattr(self.session, STATE_ATTR, self)

    async def join_own_room(self, session: Any) -> None:
        """Sandbox ``init``: a guest's agent joins its human's room only."""
        await session.join(self.human.room)
        session.set_current_channel(self.human.room)

    # -- ownership (lens state) -----------------------------------------------

    def _human_session(self) -> Any:
        registry = self.app["registry"]
        wanted = self.human.registry_principal.casefold()
        for (principal, backend), session in zip(registry.keys(), registry.values()):
            if backend == self.human.backend and principal.casefold() == wanted:
                return session
        return None

    def owner_nick(self) -> str | None:
        """The nick the human's own live lens-held connection holds, or None."""
        session = self._human_session()
        if session is None or not getattr(session, "healthy", False):
            return None
        if not getattr(session, "connected", False):
            return None
        nick = getattr(session, "nick", "") or ""
        if nick.casefold() != self.human.nick.casefold():
            return None
        return nick

    def is_owner(self, sender: str) -> bool:
        owner = self.owner_nick()
        return owner is not None and sender.casefold() == owner.casefold()

    def _names_me(self, text: str) -> bool:
        nick = self.session.nick
        names = {nick, nick.split("-", 1)[-1]}
        text = visible_text(text)
        for name in names:
            pattern = rf"(?<![{_NICK_CHARS}])@?{re.escape(name)}(?![{_NICK_CHARS}])"
            if re.search(pattern, text, flags=re.IGNORECASE):
                return True
        return False

    # -- receive side ---------------------------------------------------------

    def classify(
        self, sender: str, target: str, text: str
    ) -> tuple[AgentLine | None, str | None]:
        """``(line to deliver, None)``, ``(None, withheld reason)`` or both None.

        Pure decision over one received PRIVMSG/NOTICE.
        """
        me = self.session.nick.casefold()
        if not sender or sender.casefold() == me or sender.startswith("system-"):
            return None, None
        kind = "chat"
        if text.startswith("\x01") and text.endswith("\x01"):
            if not text.startswith("\x01ACTION "):
                return None, None  # CTCP VERSION/PING: protocol, not chat
            text, kind = text[len("\x01ACTION ") : -1], "action"
        owner = self.is_owner(sender)
        if target.startswith("#"):
            if not self.session.may("read", target, audit=False):
                return None, None  # not (or no longer) granted: never shown
            if not owner and self._names_me(text):
                return None, "addressed"
            label = target
        else:
            if not owner:
                return None, "dm"
            label = DM_TARGET
        self._seq += 1
        return (
            AgentLine(self._seq, label, sender, text, kind, owner, time.time()),
            None,
        )

    async def _on_message(self, msg: Any) -> None:
        prefix = getattr(msg, "prefix", None) or ""
        if "!" not in prefix or len(msg.params) < 2:
            return  # server-originated (no user prefix) or malformed
        sender = prefix.split("!", 1)[0]
        target, text = msg.params[0], msg.params[1]
        if msg.command == "NOTICE" and text.startswith("\x01"):
            return
        line, reason = self.classify(sender, target, text)
        if line is not None:
            self._inbox.append(line)
        elif reason is not None:
            self.withheld += 1
            logger.info("agent_delivery=withheld reason=%s", reason)
            await self._audit(reason, target if target.startswith("#") else "dm")

    async def _audit(self, reason: str, target: str) -> None:
        now = time.monotonic()
        key = (reason, target)
        if now - self._last_audit.get(key, -AUDIT_EVERY_S) < AUDIT_EVERY_S:
            return
        self._last_audit[key] = now
        store = self.app.get(app_session.ACCESS_STORE)
        if store is None:
            return

        def write() -> None:
            store.record_audit(
                WITHHELD_ACTION, f"{reason}:{target}", actor=self.credential_key
            )

        try:
            await asyncio.to_thread(write)
        except Exception:  # noqa: BLE001 - audit is best effort
            logger.exception("agent_delivery audit failed")

    def read(self, target: str = "", limit: int = 50) -> dict[str, Any]:
        """Unread delivered lines (all targets, or one), oldest first."""
        limit = max(1, min(INBOX_MAX, int(limit)))
        unread = [
            ln
            for ln in self._inbox
            if ln.seq > self._seen.get(ln.target, 0)
            and (not target or ln.target == target)
        ]
        for ln in unread:
            self._seen[ln.target] = max(self._seen.get(ln.target, 0), ln.seq)
        visible = [
            ln
            for ln in unread
            if ln.target == DM_TARGET
            or self.session.may("read", ln.target, audit=False)
        ]
        return {
            "lines": [ln.to_dict() for ln in visible[-limit:]],
            "withheld": self.withheld,
        }

    # -- send side ----------------------------------------------------------

    def _actor(self, ip: str) -> input_gate.InputActor:
        return input_gate.InputActor(
            tier=self.tier,
            email=self.email,
            backend=self.human.backend,
            ip=ip or "unknown",
            via_agent=True,
        )

    def _gate(self, actor: input_gate.InputActor, parsed: ParsedCommand, text: str):
        refusal = input_gate.check(self.app, actor, parsed, text)
        if refusal is not None:
            raise _refused(refusal.error, refusal.hint)

    async def _execute(self, parsed: ParsedCommand) -> None:
        """Run *parsed* through ``Session.execute``; its error event -> raise."""
        sub = self.session.event_bus.subscribe()
        try:
            await self.session.execute(parsed)
            events = sub.drain_nowait()
        except LensConnectionLost as exc:
            await self._drop()
            raise _refused(_LOST_ERROR, _LOST_HINT) from exc
        finally:
            sub.close()
        for event in events:
            if event.name == "error":
                message = json.loads(event.data).get("message", "refused")
                raise _refused(message)

    async def _drop(self) -> None:
        from irc_lens.web.agent_auth import close_agent_connections

        await close_agent_connections(self.app, self.credential_key)

    async def join(self, channel: str, *, ip: str) -> dict[str, Any]:
        if not valid_channel(channel):
            raise _refused(f"invalid channel: {channel!r}", _CHANNEL_HINT)
        parsed = ParsedCommand(type=CommandType.JOIN, args=[channel])
        self._gate(self._actor(ip), parsed, f"/join {channel}")
        await self._execute(parsed)
        return {"joined": channel}

    async def part(self, channel: str, *, ip: str) -> dict[str, Any]:
        if not valid_channel(channel):
            raise _refused(f"invalid channel: {channel!r}", _CHANNEL_HINT)
        parsed = ParsedCommand(type=CommandType.PART, args=[channel])
        self._gate(self._actor(ip), parsed, f"/part {channel}")
        await self._execute(parsed)
        return {"parted": channel}

    async def send(self, target: str, text: str, *, ip: str) -> dict[str, Any]:
        text = one_line(text)
        if not text:
            raise _refused("nothing to send", "pass non-empty text")
        if len(text) > TEXT_MAX:
            raise _refused("message too long", f"at most {TEXT_MAX} characters")
        target = (target or self.session.current_channel or "").strip()
        if target.startswith("#"):
            if not valid_channel(target):
                raise _refused(f"invalid channel: {target!r}", _CHANNEL_HINT)
            # The sandbox allowlist admits only chat in the current room (a
            # guest's own); on the mesh every channel send is a /send, which
            # Session checks against the human's write grant.
            if (
                self.human.backend == BACKEND_SANDBOX
                and target == self.session.current_channel
            ):
                parsed = ParsedCommand(type=CommandType.CHAT, text=text)
            else:
                parsed = ParsedCommand(type=CommandType.SEND, args=[target], text=text)
        elif valid_nick(target):
            parsed = ParsedCommand(type=CommandType.SEND, args=[target], text=text)
        else:
            raise _refused(f"invalid target: {target!r}", "a #channel or your human")
        actor = self._actor(ip)
        self._gate(actor, parsed, text)
        if target.startswith("#"):
            await self._execute(parsed)
        else:
            await self._dm_owner(target, text)
        input_gate.record(self.app, actor, parsed, text)
        return {"sent": target}

    async def _dm_owner(self, target: str, text: str) -> None:
        """A direct message to the agent's own human -- and nobody else."""
        if target.casefold() != self.human.nick.casefold():
            raise _refused(f"not permitted: {target}", "an agent may DM only its human")
        try:
            await self.session.send_raw(f"PRIVMSG {self.human.nick} :{text}")
        except LensConnectionLost as exc:
            await self._drop()
            raise _refused(_LOST_ERROR, _LOST_HINT) from exc

    async def channels(self) -> dict[str, Any]:
        session = self.session
        joined = sorted(
            c for c in session.joined_channels if session.may("list", c, audit=False)
        )
        out: dict[str, Any] = {"joined": joined, "current": session.current_channel}
        if self.human.backend == BACKEND_MESH:
            try:
                out["listed"] = await session.list_channels()
            except LensConnectionLost as exc:
                await self._drop()
                raise _refused(_LOST_ERROR, _LOST_HINT) from exc
        return out


async def _guest_consent_current(app: web.Application, email: str) -> bool:
    """Has the guest consented to the *current* Terms and Privacy versions?

    Same seam as the browser's consent gate (``routes._consent_current``);
    fails closed.
    """
    import inspect

    try:
        from irc_lens import legal

        versions = await legal.current_legal_versions(app["config"])
        ok = legal.consent_is_current(app["guest_store"], email, versions)
        if inspect.isawaitable(ok):
            ok = await ok
        return bool(ok)
    except Exception:  # noqa: BLE001 - fail closed
        logger.exception("agent consent check failed; refusing")
        return False


async def _guest_agent_gate(app: web.Application, principal: Any) -> None:
    """Refuse (and close) a guest's agent that may not run right now.

    Checked on every agent request, before any connection is (re)opened:

    * the guest must hold consent for the current legal versions, so a
      legal bump stops an existing agent until the guest re-consents;
    * the guest's agent is present only where its human is: the human's own
      sandbox session must be open, unless a live admin extension covers
      the agent (o19). Without this, any tool call would lazily reopen an
      agent the reaper had just closed with its human.
    """
    if not await _guest_consent_current(app, principal.email):
        logger.info("agent_session=refused reason=consent")
        await _close_agent(app, principal.credential_key)
        raise _refused(
            "your human must accept the current Terms and Privacy Policy",
            "the guest re-consents in the app, then call the tool again",
        )
    registry = app["registry"]
    if not await asyncio.to_thread(registry.guest_agent_may_run, principal.email):
        logger.info("agent_session=refused reason=human-absent")
        await _close_agent(app, principal.credential_key)
        raise _refused(
            "your human is not signed in to the sandbox",
            "a guest's agent runs only while its human is signed in "
            "(or an admin extension is live)",
        )


async def _close_agent(app: web.Application, credential_key: str) -> None:
    from irc_lens.web.agent_auth import close_agent_connections

    await close_agent_connections(app, credential_key)


def _nick_matches(app: web.Application, nick: str, human: HumanState) -> bool:
    """Is *nick* the agent nick of *human*, on the configured server name?"""
    server = server_name_for(app["config"], guest=human.backend == BACKEND_SANDBOX)
    try:
        expected = agent_nick_for(human.nick, server)
    except ValueError:
        return False
    return is_agent_nick(nick, server) and nick == expected


async def agent_session_for(app: web.Application, principal: Any) -> AgentSession:
    """The (lazily opened) lens-held session of the authenticated *principal*.

    *principal* is the request's
    :class:`~irc_lens.web.agent_auth.AgentPrincipal` -- built from the
    credential row and the human's current state only (o11).
    """
    human = await asyncio.to_thread(human_state, app, principal)
    if human is None or not _nick_matches(app, principal.nick, human):
        logger.info("agent_session=refused reason=nick-mismatch")
        raise _refused(
            "this credential's agent nick no longer matches its human",
            "re-issue the agent credential from the app",
        )
    if human.backend == BACKEND_SANDBOX:
        await _guest_agent_gate(app, principal)
    holder: dict[str, AgentSession] = {}

    def prepare(session: Any) -> None:
        state = AgentSession(
            app,
            session,
            credential_key=principal.credential_key,
            email=principal.email,
            tier=principal.tier,
            human=human,
        )
        state.attach()
        holder["state"] = state

    async def init(session: Any) -> None:
        if human.backend == BACKEND_SANDBOX:
            await holder["state"].join_own_room(session)

    registry = app["registry"]
    try:
        session = await registry.get_or_open_agent(
            principal.registry_principal,
            human.backend,
            principal.nick,
            access_human=principal.email,
            init=init,
            prepare=prepare,
        )
    except OSError as exc:  # LensConnectionLost is a ConnectionError
        raise _refused(
            "could not connect the agent to IRC",
            "retry shortly; if it persists the nick may be in use",
        ) from exc
    state = getattr(session, STATE_ATTR, None)
    if state is None:  # never: every agent session is opened with prepare
        raise _refused("agent session unavailable", "retry")
    return state


__all__ = [
    "DM_TARGET",
    "STATE_ATTR",
    "WITHHELD_ACTION",
    "AgentLine",
    "AgentSession",
    "HumanState",
    "agent_session_for",
    "human_for_agent_nick",
    "human_state",
    "one_line",
    "valid_channel",
    "valid_nick",
    "visible_text",
]
