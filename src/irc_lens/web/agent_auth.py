"""Personal-agent credentials and the MCP endpoint's authenticator.

Every human -- an approved user or a guest -- may hold one agent
credential: an opaque bearer token their personal agent sends to the
lens's single MCP endpoint (``web.mcp_path``, mounted by
:func:`irc_lens.web.mcp_http.mount_mcp`). Installed by ``make_app`` only
while ``auth.email_permissions.enabled`` is on (the credentials live in the
access store).

Issuing (``GET``/``POST /agent-credential``)
    A signed-in human of any tier issues their credential from the app. The
    token is shown once, in that one ``no-store`` response; the access store
    keeps only its SHA-256 (``AccessStore.issue_agent_credential``). Issuing
    again revokes the old credential in the same transaction and closes the
    old agent's live connection (o1). The credential is bound to its human
    under a *credential key*: the email for an approved user,
    ``guest:<email>`` for a guest (the registry's guest prefix), so a
    guest's credential can never resolve to an approved user's rows even
    when the two share an address. The agent's nick
    (:func:`irc_lens.agent_nick.agent_nick_for`) is stored with it.

Authenticating (every MCP request)
    :func:`make_authenticator` reads only the ``Authorization: Bearer``
    header. A missing, malformed, unknown, expired or revoked credential is
    refused (401) before any tool runs (o8). A live credential is then
    re-checked against its human on *every* request (o16): an approved
    user must still be approved in the access store; a guest must still
    exist in the guest store and not be banned (by email, by the caller's
    IP, or by the IP recorded at entry). Nothing is cached. A failing
    re-check also closes the agent's live connection.

    The principal handed to tools (:class:`AgentPrincipal`) is built only
    from the store row and the human's current state -- never from the
    query string, other headers or tool arguments (o11). It is not an
    :class:`~irc_lens.web.identity.Identity` and never passes the
    admin-only gate (``routes.admin_only_denial``).

Agent connections
    An agent's lens-held IRC connection (task t13) lives in the session
    registry under :func:`agent_registry_principal` (``agent:<key>``), apart
    from its human's browser sessions, and never counts as a guest seat.
    :func:`close_agent_connections` closes it; :func:`sweep_once` (on the ban
    sweeper's cadence) closes any whose credential was revoked or expired
    out of band (CLI, admin removal) or whose human no longer passes.

Plain emails and tokens are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from irc_lens.agent_nick import agent_nick_for
from irc_lens.web import app_session
from irc_lens.web.auth import allows_anonymous
from irc_lens.web.identity import TIER_ANONYMOUS, TIER_APPROVED, TIER_GUEST
from irc_lens.web.mcp_http import MCP_ROUTES, mount_mcp
from irc_lens.web.render import render_fragment, static_url
from irc_lens.web.sessions import BACKENDS, GUEST_PRINCIPAL_PREFIX

logger = logging.getLogger(__name__)

#: Where a signed-in human views and issues their agent credential.
PAGE_PATH = "/agent-credential"

#: Credential-key prefix for a guest's credential (same as the registry's).
GUEST_KEY_PREFIX = GUEST_PRINCIPAL_PREFIX

#: Registry-principal prefix for agent connections (t13): ``agent:<key>``.
AGENT_PRINCIPAL_PREFIX = "agent:"

#: The background task closing agents whose credential or human lapsed.
SWEEP_TASK = web.AppKey("agent_sweep_task", asyncio.Task)

# ``secrets.token_urlsafe`` output; anything else is not one of ours.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,128}")

_HTML = "text/html"

__all__ = [
    "AGENT_PRINCIPAL_PREFIX",
    "GUEST_KEY_PREFIX",
    "MCP_ROUTES",
    "PAGE_PATH",
    "AgentPrincipal",
    "agent_nick_for",
    "agent_registry_principal",
    "authenticate_token",
    "build_agent_front",
    "close_agent_connections",
    "describe_principal",
    "install",
    "make_authenticator",
    "sweep_once",
]


@dataclass(frozen=True)
class AgentPrincipal:
    """The authenticated caller of one MCP request.

    ``credential_key`` is the store key (email, or ``guest:<email>``),
    ``email`` the human's address, ``tier`` the human's tier
    (``approved``/``guest``) and ``nick`` the agent's stored nick. ``token``
    is the bearer itself, kept only for the duration of the request so
    tools can call ``access.agent_can_*``; it is excluded from ``repr``.
    """

    credential_key: str
    email: str
    tier: str
    nick: str
    token: str = field(repr=False, compare=False, default="")

    #: Agents are never browser identities: the admin-only gate refuses them.
    is_admin = False
    is_approved = False

    @property
    def registry_principal(self) -> str:
        return agent_registry_principal(self.credential_key)


def agent_registry_principal(credential_key: str) -> str:
    """The session-registry principal of the agent bound to *credential_key*."""
    return AGENT_PRINCIPAL_PREFIX + credential_key


def describe_principal(principal: Any) -> dict[str, str]:
    """What an agent may learn about itself: its nick and its human's tier."""
    if not isinstance(principal, AgentPrincipal):
        return {}
    return {"nick": principal.nick, "tier": principal.tier}


def build_agent_front():
    """The agentfront ``App`` served at the MCP endpoint.

    Deliberately *not* ``irc_lens.cli.build_app()``: those live tools are the
    identity-less dev path (fixed config nick) and must never be reachable
    from the endpoint (o14). Task t13 adds the agent's real tools here,
    each running under the request's :class:`AgentPrincipal`.
    """
    from agentfront import current_caller
    from agentfront.app import App

    from irc_lens import __version__

    front = App(
        name="irc-lens-agent",
        version=__version__,
        description="Personal-agent endpoint of the irc-lens console.",
    )

    @front.tool
    def whoami() -> dict:
        """Report this agent's nick and its human's tier."""
        return describe_principal(current_caller())

    return front


# -- the human behind a credential ---------------------------------------------


def _split_key(credential_key: str) -> tuple[str, str]:
    """``(email, tier)`` of a credential key."""
    if credential_key.startswith(GUEST_KEY_PREFIX):
        return credential_key[len(GUEST_KEY_PREFIX) :], TIER_GUEST
    return credential_key, TIER_APPROVED


def _human_ok(app: web.Application, credential_key: str, ip: str | None) -> bool:
    """Is the credential's human still approved / present and unbanned?

    Re-read from the stores on every call (no cache). Fails closed.
    """
    email, tier = _split_key(credential_key)
    if not email:
        return False
    if tier == TIER_APPROVED:
        return app_session.approved_user(app, email) is not None
    store = app.get("guest_store")
    if not app["config"].guest_enabled or store is None:
        return False
    rows = store.get_guest(email)
    if not rows:
        return False  # deleted
    if store.is_banned(email, rows[-1][2]):
        return False  # banned by email or by the IP recorded at entry
    return not (ip and store.is_banned(None, ip))


def _resolve(app: web.Application, token: str, ip: str) -> tuple[str, Any]:
    """Blocking part of authentication; returns ``(outcome, value)``."""
    store = app.get(app_session.ACCESS_STORE)
    if store is None:
        return "no-store", None
    found = store.verify_agent_credential(token)
    if found is None:
        return "unknown", None  # missing, expired, revoked or replaced
    credential_key, nick = found
    if not _human_ok(app, credential_key, ip):
        return "human", credential_key
    email, tier = _split_key(credential_key)
    return "ok", AgentPrincipal(
        credential_key=credential_key, email=email, tier=tier, nick=nick, token=token
    )


async def authenticate_token(
    app: web.Application, token: str, *, ip: str
) -> AgentPrincipal | None:
    """The principal for *token*, or None; re-checks the human every call.

    A live credential whose human no longer passes also closes the agent's
    connection (c39/h33).
    """
    if not _TOKEN_RE.fullmatch(token or ""):
        return None
    try:
        outcome, value = await asyncio.to_thread(_resolve, app, token, ip)
    except Exception:  # noqa: BLE001 - unreadable store => refuse
        logger.exception("agent_auth=refused reason=store-error")
        return None
    if outcome == "ok":
        return value
    logger.info("agent_auth=refused reason=%s", outcome)
    if outcome == "human":
        await close_agent_connections(app, value)
    return None


def _bearer(header: str | None) -> str | None:
    """The token from ``Authorization: Bearer <token>``, else None."""
    scheme, _, value = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not _TOKEN_RE.fullmatch(value):
        return None
    return value


def make_authenticator(app: web.Application):
    """The ``mount_mcp`` authenticator: bearer credential -> AgentPrincipal.

    Only the ``Authorization`` header is read (plus the caller's address for
    the guest IP ban). Query strings, cookies and every other header are
    ignored, so no request parameter can pick or widen the scope.
    """

    async def authenticate(req) -> AgentPrincipal | None:
        token = _bearer(req.headers.get("authorization"))
        if token is None:
            logger.info("agent_auth=refused reason=no-bearer")
            return None
        ip = (req.headers.get("cf-connecting-ip") or "").strip()
        if not ip and req.client:
            ip = str(req.client[0])
        return await authenticate_token(app, token, ip=ip)

    return authenticate


# -- agent connections -----------------------------------------------------------


async def close_agent_connections(app: web.Application, credential_key: str) -> int:
    """Close every live connection of the agent bound to *credential_key*."""
    registry = app.get("registry")
    if registry is None:
        return 0
    principal = agent_registry_principal(credential_key)
    closed = 0
    for backend in sorted(BACKENDS):
        if registry.has(principal, backend):
            await registry.close(principal, backend)
            closed += 1
    return closed


def _still_live(app: web.Application, credential_key: str) -> bool:
    store = app.get(app_session.ACCESS_STORE)
    return (
        store is not None
        and store.active_credential_count(credential_key) > 0
        and _human_ok(app, credential_key, None)
    )


async def sweep_once(app: web.Application) -> list[str]:
    """Close agents whose credential or human lapsed; return their principals.

    Catches what no request will: a credential revoked or expired from
    another process (CLI) or by an admin, and a human removed, banned or
    deleted while their agent sits idle.
    """
    registry = app.get("registry")
    if registry is None:
        return []
    closed: list[str] = []
    for principal, backend in registry.keys():  # a snapshot (list)
        if not principal.startswith(AGENT_PRINCIPAL_PREFIX):
            continue
        key = principal[len(AGENT_PRINCIPAL_PREFIX) :]
        if await asyncio.to_thread(_still_live, app, key):
            continue
        await registry.close(principal, backend)
        if principal not in closed:
            closed.append(principal)
    if closed:
        logger.info("agent_sweep closed=%d", len(closed))
    return closed


async def _sweep_loop(app: web.Application, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep_once(app)
        except Exception:  # noqa: BLE001 -- the sweeper must never die
            logger.exception("agent sweep failed")


# -- GET/POST /agent-credential ----------------------------------------------------


def _deny(status: int, error: str, hint: str) -> web.Response:
    return web.json_response({"error": error, "hint": hint}, status=status)


async def _requester(request: web.Request) -> tuple[str, str, str] | web.Response:
    """``(credential_key, human_nick, actor_email)`` or a refusal response.

    Only a human who passed their normal sign-in gets here: an approved
    identity still approved in the access store, or a guest identity (signed
    cookie, present, unbanned) with current consent.
    """
    identity = request.get("identity")
    if identity is None or identity.tier == TIER_ANONYMOUS:
        return _deny(401, "sign-in required", "sign in to issue an agent credential")
    if identity.tier == TIER_GUEST:
        from irc_lens.web import routes

        gated = await routes._guest_consent_gate(  # pylint: disable=protected-access
            request, redirect=False
        )
        if gated is not None:
            return gated
        email = identity.principal.strip().lower()
        return GUEST_KEY_PREFIX + email, identity.nick, email
    email = identity.principal.strip().lower()
    if (
        not identity.is_approved
        or app_session.approved_user(request.app, email) is None
    ):
        return _deny(
            403,
            "agent credentials are for approved people",
            "sign in with an approved email address",
        )
    return email, identity.nick, email


def _endpoint(config) -> str:
    base = (config.app_signin_base_url or "").rstrip("/")
    return f"{base}{config.mcp_path}"


def _page(
    request: web.Request,
    *,
    credential_key: str,
    human_nick: str,
    token: str = "",
    status: int = 200,
) -> web.Response:
    config = request.app["config"]
    store = request.app[app_session.ACCESS_STORE]
    html = render_fragment(
        "agent_credential.html.j2",
        token=token,
        active=store.active_credential_count(credential_key) > 0,
        agent_nick=agent_nick_for(human_nick),
        endpoint=_endpoint(config),
        ttl_days=max(1, config.agent_credential_ttl_s // 86400),
        css_url=static_url("entry.css"),
        admin_css_url=static_url("admin.css"),
    )
    resp = web.Response(text=html, content_type=_HTML, status=status)
    resp.headers["Cache-Control"] = "no-store"
    # Same-origin Referrer so the page's form POST carries a real Origin.
    from irc_lens.web.entry import ENTRY_PAGE_MARKER

    resp[ENTRY_PAGE_MARKER] = True
    return resp


@allows_anonymous
async def get_page(request: web.Request) -> web.Response:
    """Show the requester's agent status and the issue button (no token)."""
    found = await _requester(request)
    if isinstance(found, web.StreamResponse):
        return found
    key, human_nick, _actor = found
    try:
        return await asyncio.to_thread(
            _page, request, credential_key=key, human_nick=human_nick
        )
    except ValueError:
        return _deny(400, "no agent nick for this account", "contact an admin")


@allows_anonymous
async def post_issue(request: web.Request) -> web.Response:
    """Issue (or replace) the requester's credential; show the token once."""
    found = await _requester(request)
    if isinstance(found, web.StreamResponse):
        return found
    key, human_nick, actor = found
    try:
        nick = agent_nick_for(human_nick)
    except ValueError:
        return _deny(400, "no agent nick for this account", "contact an admin")
    store = request.app[app_session.ACCESS_STORE]
    token = await asyncio.to_thread(
        store.issue_agent_credential,
        key,
        nick,
        ttl=request.app["config"].agent_credential_ttl_s,
        actor=actor,
    )
    # The replaced credential is already revoked; drop its agent now (o1).
    await close_agent_connections(request.app, key)
    logger.info("agent credential issued tier=%s", _split_key(key)[1])
    return await asyncio.to_thread(
        _page, request, credential_key=key, human_nick=human_nick, token=token
    )


def install(app: web.Application, *, sweep_interval_s: float) -> None:
    """Add the credential page and mount the authenticated MCP endpoint.

    No-op unless the access store is installed (email permissions on).
    """
    if app.get(app_session.ACCESS_STORE) is None:
        return
    app.router.add_get(PAGE_PATH, get_page)
    app.router.add_post(PAGE_PATH, post_issue)
    mount_mcp(
        app, build_agent_front(), app["config"], authenticate=make_authenticator(app)
    )

    async def start(app: web.Application) -> None:  # NOSONAR S7503 - on_startup
        app[SWEEP_TASK] = asyncio.create_task(_sweep_loop(app, sweep_interval_s))

    async def stop(app: web.Application) -> None:
        task = app.get(SWEEP_TASK)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
