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
    An agent's lens-held IRC connection (``web/agent_sessions.py``) lives in
    the session registry under :func:`agent_registry_principal`
    (``agent:<key>``), apart from its human's browser sessions, and never
    counts as a guest seat.
    :func:`close_agent_connections` closes it; :func:`sweep_once` (on the ban
    sweeper's cadence) closes any whose credential was revoked or expired
    out of band (CLI, admin removal) or whose human no longer passes.

Switches (``auth.email_permissions``)
    ``agents_enabled: false`` is the kill switch: every MCP call is refused
    before any credential is looked at, issuing is refused, and the sweeper
    closes every live agent connection. Permissions switched off after the
    access store was initialised resolve to the same (see
    :func:`irc_lens.config.resolve_access`). Every refusal -- bad or missing
    bearer, lapsed human, kill switch -- appends an ``agent.refuse`` audit row
    (reason only, actor hashed) and is counted in ``/owner/metrics``.

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
from irc_lens.web.sessions import AGENT_PRINCIPAL_PREFIX as _REGISTRY_AGENT_PREFIX
from irc_lens.web.sessions import BACKENDS, GUEST_PRINCIPAL_PREFIX

logger = logging.getLogger(__name__)

#: Where a signed-in human views and issues their agent credential.
PAGE_PATH = "/agent-credential"

#: Credential-key prefix for a guest's credential (same as the registry's).
GUEST_KEY_PREFIX = GUEST_PRINCIPAL_PREFIX

#: Registry-principal prefix for agent connections (t13): ``agent:<key>``.
AGENT_PRINCIPAL_PREFIX = _REGISTRY_AGENT_PREFIX

#: Audit action recorded for every refused agent call.
REFUSE_ACTION = "agent.refuse"

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
    "REFUSE_ACTION",
    "AgentPrincipal",
    "agent_nick_for",
    "agents_off",
    "audit_refusal",
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
    #: The caller's address (``CF-Connecting-IP`` or the peer), for the
    #: per-IP leg of the shared input gate's rate limit (t13). Not a scope.
    ip: str = field(repr=False, compare=False, default="")

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


def build_agent_front(app: web.Application | None = None):
    """The agentfront ``App`` served at the MCP endpoint.

    Deliberately *not* ``irc_lens.cli.build_app()``: those live tools are the
    identity-less dev path (fixed config nick) and must never be reachable
    from the endpoint (o14). Every tool here resolves its caller through
    ``agentfront.current_caller()`` -- the :class:`AgentPrincipal` the
    authenticator built for this request -- and acts only through that
    agent's own lens-held session (``web/agent_sessions.py``): grant checks,
    the shared input gate and the receive-side owner filter. A call without
    an authenticated agent principal (or without the lens *app*) is refused
    before anything touches IRC.
    """
    from agentfront import current_caller
    from agentfront.app import App

    from irc_lens import __version__
    from irc_lens.web import agent_sessions

    front = App(
        name="irc-lens-agent",
        version=__version__,
        description="Personal-agent endpoint of the irc-lens console.",
    )

    async def _agent() -> tuple[AgentPrincipal, Any]:
        principal = current_caller()
        if not isinstance(principal, AgentPrincipal) or app is None:
            raise _no_caller()
        return principal, await agent_sessions.agent_session_for(app, principal)

    @front.tool
    async def whoami() -> dict:
        """Report this agent's nick, its human's tier and its joined channels."""
        principal, agent = await _agent()
        return {
            **describe_principal(principal),
            "joined": sorted(agent.session.joined_channels),
        }

    @front.tool
    async def read(channel: str = "", limit: int = 50) -> dict:
        """Unread lines delivered to this agent, each marked owner / not-owner.

        Only lines that reached the agent's own connection, after the
        owner-only filter: DMs and lines naming the agent from anyone but its
        human are withheld (``withheld`` counts them). ``channel`` limits the
        read to one #channel (or ``DM``).
        """
        _principal, agent = await _agent()
        return agent.read(channel, limit)

    @front.tool
    async def send(target: str = "", text: str = "") -> dict:
        """Send one line to a #channel (default: the current one) or to your human.

        Passes the same gate as the human's own input (allowlist, rate
        limit, input log) and the human's channel grants. Text is always
        chat, never a command.
        """
        principal, agent = await _agent()
        return await agent.send(target, text, ip=principal.ip)

    @front.tool
    async def channels() -> dict:
        """Channels this agent has joined (and, on the mesh, may list)."""
        _principal, agent = await _agent()
        return await agent.channels()

    @front.tool
    async def join(channel: str = "") -> dict:
        """Join a #channel your human is granted (not available to guests)."""
        principal, agent = await _agent()
        return await agent.join(channel, ip=principal.ip)

    @front.tool
    async def part(channel: str = "") -> dict:
        """Leave a #channel (not available to guests)."""
        principal, agent = await _agent()
        return await agent.part(channel, ip=principal.ip)

    return front


def _no_caller():
    from irc_lens._errors import EXIT_USER_ERROR, AfiError

    return AfiError(
        code=EXIT_USER_ERROR,
        message="no authenticated agent",
        remediation="call the MCP endpoint with your agent credential",
    )


# -- kill switch + refusal audit -------------------------------------------------


def agents_off(app: web.Application) -> bool:
    """Is the agent kill switch thrown? Read live from config; fails closed."""
    config = app.get("config")
    return config is None or not getattr(config, "agents_enabled", True)


def _write_refusal(app: web.Application, reason: str, actor: str | None) -> None:
    store = app.get(app_session.ACCESS_STORE)
    if store is None:
        return
    with store._tx() as con:  # pylint: disable=protected-access
        store._audit(  # pylint: disable=protected-access
            con, actor, REFUSE_ACTION, reason
        )


async def audit_refusal(
    app: web.Application, reason: str, actor: str | None = None
) -> None:
    """Best-effort ``agent.refuse`` audit row; the refusal stands regardless.

    *actor* is an email (hashed by the store) or None; *reason* is a fixed
    word, never request data.
    """
    try:
        await asyncio.to_thread(_write_refusal, app, reason, actor)
    except Exception:  # noqa: BLE001 - audit must never turn a refusal into a pass
        logger.warning("agent refusal audit failed reason=%s", reason)


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
        credential_key=credential_key,
        email=email,
        tier=tier,
        nick=nick,
        token=token,
        ip=ip,
    )


async def authenticate_token(
    app: web.Application, token: str, *, ip: str
) -> AgentPrincipal | None:
    """The principal for *token*, or None; re-checks the human every call.

    A live credential whose human no longer passes also closes the agent's
    connection (c39/h33).
    """
    if agents_off(app):
        logger.info("agent_auth=refused reason=agents-off")
        await audit_refusal(app, "agents-off")
        return None
    if not _TOKEN_RE.fullmatch(token or ""):
        await audit_refusal(app, "malformed")
        return None
    try:
        outcome, value = await asyncio.to_thread(_resolve, app, token, ip)
    except Exception:  # noqa: BLE001 - unreadable store => refuse
        logger.error("agent_auth=refused reason=store-error")
        return None
    if outcome == "ok":
        return value
    logger.info("agent_auth=refused reason=%s", outcome)
    await audit_refusal(
        app, outcome, _split_key(value)[0] if outcome == "human" else None
    )
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
        if agents_off(app):  # kill switch: refuse before reading anything
            return await authenticate_token(app, "", ip="")
        token = _bearer(req.headers.get("authorization"))
        if token is None:
            logger.info("agent_auth=refused reason=no-bearer")
            await audit_refusal(app, "no-bearer")
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
        not agents_off(app)
        and store is not None
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
    if agents_off(request.app):
        return _deny(
            403,
            "agent access is switched off",
            "an administrator has disabled personal agents",
        )
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
        app, build_agent_front(app), app["config"], authenticate=make_authenticator(app)
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
