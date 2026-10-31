"""In-app admin: add or invite users, set roles and grants, approve agents.

``GET /admin`` shows the access store (users, roles, channel grants, agent
extensions); the POST routes change it. Installed only while
``auth.email_permissions.enabled`` is on. Every route is admin-only: the
role is read from the access store on each request (``app_session`` /
``auth``), so a promotion, demotion, removal or new grant applies on the
very next action with no restart. Members, guests, anonymous visitors and
agents all get 403.

* ``POST /admin/users`` -- ``mode=add`` approves the address now;
  ``mode=invite`` stores it as ``invited`` and mails the existing
  set-password link (``setpw``); ``mode=role`` (the users table's role
  form) changes only the role of an existing user and keeps their state. An invited user becomes ``approved`` only
  once they finish an app sign-in (``app_session.promote_on_signin``).
* ``POST /admin/users/remove`` -- marks the user removed and revokes their
  agent credential and sessions.
* ``POST /admin/grants`` -- set a channel grant (``read``/``write``/``ask``)
  or ``revoke`` it.
* ``POST /admin/extension`` -- approve an *n*-hour agent extension; *n* is
  typed per approval and has no fixed maximum (``clear`` removes it). The
  address may be a known user or a guest in the guest store.

The last approved admin can be neither demoted nor removed. Pages are
server-rendered and CSP-clean: no inline script or style.
"""

from __future__ import annotations

import asyncio
import logging
import math

from aiohttp import web

from irc_lens.access_store import AccessStore
from irc_lens.web import app_session, setpw
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.entry import (
    ENTRY_PAGE_MARKER,
    ENTRY_STATE,
    _norm_email,
    _plausible_email,
)
from irc_lens.web.identity import Identity
from irc_lens.web.render import render_fragment, static_url

logger = logging.getLogger(__name__)

ROLES = ("member", "admin")
LEVELS = ("read", "write", "ask")
CHANNEL_KIND = "channel"
SECONDS_PER_HOUR = 3600
#: Largest stored timestamp (sqlite INTEGER is 64-bit); a technical bound
#: only -- the admin's hours have no policy maximum.
_MAX_UNTIL = 2**62
MAX_CHANNEL_LEN = 200
_NO_SUCH_USER = "No such user."

_DONE = {
    "user": "User saved.",
    "invite": "Invitation sent.",
    "remove": "User removed.",
    "grant": "Grant saved.",
    "revoke": "Grant revoked.",
    "extension": "Agent extension approved.",
    "clear": "Agent extension cleared.",
}


def _store(request: web.Request) -> AccessStore:
    return request.app[app_session.ACCESS_STORE]


def _admin(request: web.Request) -> Identity:
    """The requesting admin; 403 for everyone else (o18)."""
    identity: Identity | None = request.get("identity")
    if identity is None or not identity.is_admin:
        raise web.HTTPForbidden(
            text='{"error": "admin only", "hint": "ask an admin"}',
            content_type="application/json",
        )
    return identity


def _page(
    request: web.Request, *, status: int = 200, error: str = "", done: str = ""
) -> web.Response:
    store = _store(request)
    users = []
    for user in store.list_users():
        email = user["email"]
        users.append(
            {
                **user,
                "grants": [
                    {"channel": res, "level": lvl}
                    for _kind, res, lvl in store.grants_for(email, CHANNEL_KIND)
                ],
                "extension_until": store.extension_until(email),
                "credentials": store.active_credential_count(email),
            }
        )
    html = render_fragment(
        "admin.html.j2",
        users=users,
        roles=ROLES,
        levels=LEVELS,
        error=error,
        done=_DONE.get(done, ""),
        now=store._now(),  # pylint: disable=protected-access
        css_url=static_url("entry.css"),
        admin_css_url=static_url("admin.css"),
    )
    resp = web.Response(text=html, content_type="text/html", status=status)
    resp.headers["Cache-Control"] = "no-store"
    resp[ENTRY_PAGE_MARKER] = True
    return resp


def _refuse(request: web.Request, message: str) -> web.Response:
    return _page(request, status=400, error=message)


def _see_other(done: str) -> web.Response:
    return web.Response(status=303, headers={"Location": f"/admin?done={done}"})


def _would_orphan_admins(store: AccessStore, email: str, role: str, state: str) -> bool:
    """True if applying (role, state) to *email* leaves no approved admin."""
    current = store.get_user(email)
    is_admin_now = bool(
        current and current["role"] == "admin" and current["state"] == "approved"
    )
    stays = role == "admin" and state == "approved"
    return is_admin_now and not stays and store.admin_count() <= 1


@allows_anonymous
async def get_admin(request: web.Request) -> web.Response:
    _admin(request)
    done = request.query.get("done", "")
    return await asyncio.to_thread(_page, request, done=done)


def _invite_mail(request: web.Request, email: str) -> bool:
    """Mail the set-password link in the background; False if mail is unavailable.

    Synchronous: it only schedules the send on the running loop (it is called
    from a handler, so one exists) and never waits for it.
    """
    state = request.app.get(ENTRY_STATE)
    tasks = request.app.get(setpw.MAIL_TASKS)
    if state is None or tasks is None:
        return False
    task = asyncio.create_task(
        setpw._send_link(state, email)
    )  # pylint: disable=protected-access
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


def _target_state(mode: str, existing: dict[str, str] | None) -> str | None:
    """The state *mode* gives a user (None: a role change for no such user)."""
    if mode == "role":
        # A role change keeps the user's state: changing a removed or
        # invited user's role must never approve them (only "Add now" does).
        return None if existing is None else existing["state"]
    if mode == "add":
        return "approved"
    # Never downgrade someone who is already approved.
    return "approved" if existing and existing["state"] == "approved" else "invited"


@allows_anonymous
async def post_user(request: web.Request) -> web.Response:
    admin = _admin(request)
    store = _store(request)
    form = await request.post()
    email = _norm_email(form.get("email"))
    role = str(form.get("role") or "member")
    mode = str(form.get("mode") or "add")
    if not _plausible_email(email):
        return _refuse(request, "Enter a valid email address.")
    if role not in ROLES:
        return _refuse(request, "Unknown role.")
    if mode not in ("add", "invite", "role"):
        return _refuse(request, "Unknown action.")
    state = _target_state(mode, store.get_user(email))
    if state is None:
        return _refuse(request, _NO_SUCH_USER)
    if _would_orphan_admins(store, email, role, state):
        return _refuse(request, "At least one approved admin must remain.")
    store.set_user(email, role=role, state=state, actor=admin.principal)
    if mode == "invite":
        if not _invite_mail(request, email):
            return _refuse(request, "Mail is not available; the user was saved.")
        logger.info("admin invited a user")
        return _see_other("invite")
    logger.info("admin saved a user role=%s", role)
    return _see_other("user")


@allows_anonymous
async def post_remove(request: web.Request) -> web.Response:
    admin = _admin(request)
    store = _store(request)
    form = await request.post()
    email = _norm_email(form.get("email"))
    user = store.get_user(email)
    if user is None:
        return _refuse(request, _NO_SUCH_USER)
    if _would_orphan_admins(store, email, user["role"], "removed"):
        return _refuse(request, "At least one approved admin must remain.")
    store.set_user(email, role=user["role"], state="removed", actor=admin.principal)
    store.revoke_agent_credential(email, actor=admin.principal)
    await app_session.end_sessions_for_email(request.app, email)
    return _see_other("remove")


@allows_anonymous
async def post_grant(request: web.Request) -> web.Response:
    admin = _admin(request)
    store = _store(request)
    form = await request.post()
    email = _norm_email(form.get("email"))
    channel = str(form.get("channel") or "").strip()
    level = str(form.get("level") or "")
    if store.get_user(email) is None:
        return _refuse(request, _NO_SUCH_USER)
    if (
        not channel.startswith("#")
        or len(channel) < 2
        or len(channel) > MAX_CHANNEL_LEN
        or any(c in channel for c in " ,\x07\r\n\0")
    ):
        return _refuse(request, "Enter a channel such as #general.")
    if level == "revoke":
        if not store.remove_grant(email, CHANNEL_KIND, channel, actor=admin.principal):
            return _refuse(
                request, "No such grant (channel names match exactly, case included)."
            )
        return _see_other("revoke")
    if level not in LEVELS:
        return _refuse(request, "Unknown level.")
    store.set_grant(email, CHANNEL_KIND, channel, level, actor=admin.principal)
    return _see_other("grant")


def _is_guest(request: web.Request, email: str) -> bool:
    """Is *email* a guest the guest store knows? Fails closed (False).

    A guest has no users row; their agent extension is keyed by the bare
    address, which is what ``sessions._agent_extended`` reads.
    """
    guests = request.app.get("guest_store")
    if guests is None or not email:
        return False
    try:
        return bool(guests.get_guest(email))
    except Exception:  # noqa: BLE001 - an unreadable store knows nobody
        return False


@allows_anonymous
async def post_extension(request: web.Request) -> web.Response:
    admin = _admin(request)
    store = _store(request)
    form = await request.post()
    email = _norm_email(form.get("email"))
    raw = str(form.get("hours") or "").strip().lower()
    if store.get_user(email) is None and not _is_guest(request, email):
        return _refuse(request, _NO_SUCH_USER)
    if raw == "clear":
        store.clear_extension(email, actor=admin.principal)
        return _see_other("clear")
    try:
        hours = float(raw)
    except ValueError:
        return _refuse(request, "Enter the number of hours.")
    if not math.isfinite(hours) or hours <= 0:
        return _refuse(request, "Hours must be a number above zero.")
    until = store._now() + math.ceil(
        hours * SECONDS_PER_HOUR
    )  # pylint: disable=protected-access
    if until >= _MAX_UNTIL:
        return _refuse(request, "That many hours is too large to store.")
    store.set_extension(email, until=until, actor=admin.principal)
    return _see_other("extension")


def install(app: web.Application) -> None:
    app.router.add_get("/admin", get_admin)
    app.router.add_post("/admin/users", post_user)
    app.router.add_post("/admin/users/remove", post_remove)
    app.router.add_post("/admin/grants", post_grant)
    app.router.add_post("/admin/extension", post_extension)
