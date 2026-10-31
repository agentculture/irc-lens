"""Approved users choose their nickname (email permissions on).

Other people in a shared channel see an approved user's IRC nick, so it is
built from a nickname the person chose, ``<server>-<nickname>``, and never
from their email address (``identity.nick_for_user``). Until an approved
user (admin or member) has one, they get no IRC session:

* :func:`nickname_gate` (a middleware after the identity middleware) sends
  every request of such a user to ``GET /nickname``: page loads get a 303,
  htmx requests ``HX-Redirect``, anything else a 409. Sign-in, sign-out,
  password and static paths stay reachable.
* ``GET /nickname`` shows the form (the entry card's nickname pattern,
  CSP-clean: no script, no inline style); ``POST /nickname`` stores the
  choice (:meth:`AccessStore.set_nickname`) and 303s to the console.

A person chooses once; afterwards only an admin renames them (``/admin`` or
``irc-lens users nick``). The form refuses what the store refuses (the guest
character rules, an address, reserved names, the ``pa_`` agent namespace,
another user's nickname) and, live, a nick a resident agent or anyone on the
mesh holds right now: the culture residents list when configured, and the
rosters of the lens's own open mesh connections.

Installed only while the access store is (``auth.email_permissions``); with
the feature off nothing here exists and nicks stay as before.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from aiohttp import web

from irc_lens import nickname as rules
from irc_lens.access_store import AccessStore, NicknameError
from irc_lens.agent_nick import agent_nick_for, server_name_for
from irc_lens.web import app_session
from irc_lens.web.identity import TIER_APPROVED, Identity, nick_for_user
from irc_lens.web.render import render_fragment, static_url
from irc_lens.web.residents import fetch_residents, resolve_residents_url
from irc_lens.web.sessions import (
    AGENT_PRINCIPAL_PREFIX,
    BACKEND_MESH,
    BACKENDS,
    GUEST_PRINCIPAL_PREFIX,
)

logger = logging.getLogger(__name__)

PATH = "/nickname"

ERR_INVALID = "Invalid nickname"
ERR_TAKEN = "Nickname taken"
ERR_RESERVED = "That nickname is reserved"
ERR_CHOOSE_FIRST = "choose a nickname first"
_HINT_CHOOSE_FIRST = f"open {PATH} and pick the nickname others will see"

_ERRORS = {
    rules.REFUSAL_INVALID: ERR_INVALID,
    rules.REFUSAL_RESERVED: ERR_RESERVED,
    rules.REFUSAL_TAKEN: ERR_TAKEN,
}


def error_text(reason: str) -> str:
    """The form error for a refusal reason (``invalid`` for anything else)."""
    return _ERRORS.get(reason, ERR_INVALID)


#: Reachable without a nickname: the form itself, sign-in and sign-out.
_EXEMPT_PATHS = frozenset({PATH, "/logout", "/login", "/healthz", "/guest/logout"})
_EXEMPT_PREFIXES = ("/entry", "/password", "/static/", "/media/")


def needs_nickname(request: web.Request) -> bool:
    """True iff the request is an approved user's who has not chosen one."""
    identity: Identity | None = request.get("identity")
    return bool(
        identity is not None
        and identity.is_approved
        and not identity.nick
        and request.app.get(app_session.ACCESS_STORE) is not None
    )


def _exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


def _choose_first(request: web.Request) -> web.Response:
    if request.headers.get("HX-Request"):
        return web.Response(status=204, headers={"HX-Redirect": PATH})
    if request.method == "GET":
        return web.Response(
            status=303, headers={"Location": PATH, "Cache-Control": "no-store"}
        )
    resp = web.json_response(
        {"error": ERR_CHOOSE_FIRST, "hint": _HINT_CHOOSE_FIRST}, status=409
    )
    resp.headers["HX-Redirect"] = PATH
    return resp


@web.middleware
async def nickname_gate(request: web.Request, handler):
    """Hold an approved user without a nickname at ``/nickname``."""
    if needs_nickname(request) and not _exempt(request.path):
        return _choose_first(request)
    return await handler(request)


def _page(
    request: web.Request, *, status: int = 200, nickname: str = "", error: str = ""
) -> web.Response:
    from irc_lens.web.entry import ENTRY_PAGE_MARKER

    config = request.app["config"]
    html = render_fragment(
        "entry.html.j2",
        step="nickname",
        nickname=nickname,
        error=error,
        nick_prefix=rules.mesh_nick(config.server_name, ""),
        nick_max=rules.NICK_MAX,
        site_key=None,
        css_url=static_url("entry.css"),
    )
    resp = web.Response(text=html, content_type="text/html", status=status)
    resp.headers["Cache-Control"] = "no-store"
    # Same-origin Referrer so the form POST carries a real Origin.
    resp[ENTRY_PAGE_MARKER] = True
    return resp


def _requester(request: web.Request) -> Identity:
    """The approved person asking; 403 for anyone else."""
    identity: Identity | None = request.get("identity")
    if identity is None or not identity.is_approved:
        raise web.HTTPForbidden(
            text='{"error": "approved sign-in required", "hint": "sign in first"}',
            content_type="application/json",
        )
    return identity


def _done() -> web.Response:
    return web.Response(
        status=303, headers={"Location": "/", "Cache-Control": "no-store"}
    )


async def get_nickname(request: web.Request) -> web.Response:
    """The nickname form (a person who already has one goes to the console)."""
    if not needs_nickname(request):
        _requester(request)
        return _done()
    return _page(request)


def _roster_nicks(session: Any) -> set[str]:
    nicks = {
        str(getattr(entry, "nick", "")) for entry in getattr(session, "roster", ())
    }
    nicks.add(str(getattr(session, "nick", "")))
    return {n.casefold() for n in nicks if n}


async def _resident_nicks(config: Any) -> set[str]:
    url = await asyncio.to_thread(
        resolve_residents_url,
        getattr(config, "culture_residents_url", None),
        getattr(config, "culture_overview_name", None),
    )
    if url is None:
        return set()
    result = await fetch_residents(url)
    residents = (result.payload or {}).get("residents") or []
    return {str(r.get("nick") or "").casefold() for r in residents} - {""}


async def nick_in_use(app: web.Application, nick: str) -> bool:
    """Is *nick* a resident agent's, or held on the mesh right now?

    Best effort and read-only: the culture residents list (when the lens is
    configured to fetch it) and every open mesh connection's own nick and
    roster. The IRCd's nick uniqueness stays the final word.
    """
    wanted = nick.casefold()
    registry = app["registry"]
    for (_principal, backend), session in zip(registry.keys(), registry.values()):
        if backend == BACKEND_MESH and wanted in _roster_nicks(session):
            return True
    return wanted in await _resident_nicks(app["config"])


def _store(request: web.Request) -> AccessStore:
    return request.app[app_session.ACCESS_STORE]


async def post_nickname(request: web.Request) -> web.Response:
    """Store the person's chosen nickname, then open the console."""
    if not needs_nickname(request):
        _requester(request)
        return _done()  # chosen already: only an admin renames
    identity = _requester(request)
    form = await request.post()
    raw = str(form.get("nickname") or "")[:200]
    refusal = rules.approved_refusal(raw)
    if refusal is not None:
        return _page(request, status=400, nickname=raw, error=error_text(refusal))
    chosen = rules.sanitize_nickname(raw) or ""
    server = request.app["config"].server_name
    if await nick_in_use(request.app, rules.mesh_nick(server, chosen)):
        return _page(request, status=400, nickname=raw, error=ERR_TAKEN)
    try:
        await asyncio.to_thread(
            _store(request).set_nickname,
            identity.principal,
            chosen,
            actor=identity.principal,
        )
    except NicknameError as exc:
        return _page(request, status=400, nickname=raw, error=error_text(exc.reason))
    logger.info("approved user chose a nickname")
    return _done()


# -- renames: reconnect under the new nick -----------------------------------------


async def close_user_connections(app: web.Application, email: str) -> int:
    """Close *email*'s own IRC sessions and their agent's connection.

    Called after an admin renames them: the next request reopens the
    browser's session under the new nick, and the agent's credential (issued
    under the old nick) is refused until it is re-issued. The app session
    (the sign-in) is kept. Returns how many connections were closed.
    """
    from irc_lens.web.agent_auth import close_agent_connections

    registry = app["registry"]
    links = app.get(app_session.LINKS)
    closed = 0
    for backend in sorted(BACKENDS):
        if registry.has(email, backend):
            await registry.close(email, backend)
            closed += 1
        if links is not None:
            links.pop((email, backend), None)
    return closed + await close_agent_connections(app, email)


def _human_nick(app: web.Application, email: str, user: Any) -> str:
    """*email*'s current mesh nick from its store row; ``""`` if none."""
    try:
        return nick_for_user(app["config"].server_name, email, user)
    except ValueError:
        return ""


def _expected_nick(app: web.Application, principal: str, backend: str) -> str | None:
    """The nick the registry entry (*principal*, *backend*) should hold now.

    None for entries this check does not govern (guests and their agents,
    whose nicknames never change, and people no longer approved, whom the
    other sweeps close); ``""`` when the person has no nickname any more.
    An agent's nick is derived on the configured mesh server name
    (``agent_nick.server_name_for``), never read off the human's nick.
    """
    from irc_lens.web.routes import sandbox_nick_for

    agent = principal.startswith(AGENT_PRINCIPAL_PREFIX)
    email = principal[len(AGENT_PRINCIPAL_PREFIX) :] if agent else principal
    if email.startswith(GUEST_PRINCIPAL_PREFIX):
        return None
    user = app_session.approved_user(app, email)
    if user is None:
        return None
    nick = _human_nick(app, email, user)
    if not nick:
        return ""
    config = app["config"]
    if agent:
        try:
            return agent_nick_for(nick, server_name_for(config, guest=False))
        except ValueError:
            return ""
    if backend == BACKEND_MESH:
        return nick
    human = Identity(email, nick, "", tier=TIER_APPROVED)
    return sandbox_nick_for(config, human)


def _stale(app: web.Application, principal: str, backend: str, session: Any) -> bool:
    current = getattr(session, "nick", None)
    if not isinstance(current, str):
        return False  # not a connection with a nick (nothing to compare)
    expected = _expected_nick(app, principal, backend)
    if expected is None:
        return False
    return current.casefold() != expected.casefold()


def _stale_keys(
    app: web.Application, entries: list[tuple[tuple[str, str], Any]]
) -> list[tuple[str, str]]:
    """Blocking: the registry keys held under a retired nick.

    One pass under one pinned change stamp (one counter read for every
    entry). The store reads use the short hot-path busy timeout; a locked
    store closes nothing this round (logged) and the next sweep retries. No
    permission rides on this sweep: every line is still checked, fail
    closed, by ``Session.may``.
    """
    store: AccessStore = app[app_session.ACCESS_STORE]
    try:
        with store.pinned_stamp():
            return [key for key, session in entries if _stale(app, *key, session)]
    except sqlite3.Error:
        logger.warning("nickname_sweep skipped: access store unreadable")
        return []


async def sweep_once(app: web.Application) -> list[tuple[str, str]]:
    """Close connections held under a nick the store no longer gives.

    Catches a rename made in another process (``irc-lens users nick``) for
    people with no request in flight: their browser reconnects under the new
    nick on its next request, and their agent must re-issue its credential.
    Runs on the agent sweep's interval; read-only on the store.
    """
    if app.get(app_session.ACCESS_STORE) is None:
        return []
    registry = app["registry"]
    entries = list(zip(registry.keys(), registry.values()))
    stale = await asyncio.to_thread(_stale_keys, app, entries)
    links = app.get(app_session.LINKS)
    for key in stale:
        await registry.close(*key)
        if links is not None:
            links.pop(key, None)
    if stale:
        logger.info("nickname_sweep closed=%d", len(stale))
    return stale


def install(app: web.Application) -> None:
    """Add ``GET``/``POST /nickname`` (the gate is in ``make_app``'s chain)."""
    app.router.add_get(PATH, get_nickname)
    app.router.add_post(PATH, post_nickname)


__all__ = [
    "ERR_CHOOSE_FIRST",
    "ERR_INVALID",
    "ERR_RESERVED",
    "ERR_TAKEN",
    "PATH",
    "close_user_connections",
    "error_text",
    "install",
    "needs_nickname",
    "nick_in_use",
    "nickname_gate",
    "sweep_once",
]
