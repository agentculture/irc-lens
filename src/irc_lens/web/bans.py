"""Ban enforcement for live guest sessions (task t11, obligation o15).

The owner CLI writes bans to the guest store from another process; the
running lens notices by sweeping its active sandbox sessions on an
interval (default 30 s, well under the one-minute contract) and closing
any whose guest is now banned, by email or by the IP recorded at entry.
Requests are also refused individually (``auth._guest_identity`` checks
``is_banned``), so a dropped guest cannot simply reconnect.
"""

from __future__ import annotations

import asyncio
import time
import contextlib
import logging

from aiohttp import web

from irc_lens.web import app_session
from irc_lens.web.sessions import (
    AGENT_PRINCIPAL_PREFIX,
    BACKEND_SANDBOX,
    BACKENDS,
    GUEST_PRINCIPAL_PREFIX,
    SessionRegistry,
)

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_S = 30.0
SWEEP_TASK = web.AppKey("ban_sweep_task", asyncio.Task)


def _guest_banned(store, email: str) -> bool:
    rows = store.get_guest(email)
    ip = rows[-1][2] if rows else None
    return store.is_banned(email, ip)


async def _drop_agent(registry: SessionRegistry, access, email: str) -> None:
    """Disconnect a banned guest's agent and delete its credential rows (o9)."""
    key = GUEST_PRINCIPAL_PREFIX + email
    principal = AGENT_PRINCIPAL_PREFIX + key
    for backend in sorted(BACKENDS):
        if registry.has(principal, backend):
            await registry.close(principal, backend)
    if access is not None:
        await asyncio.to_thread(access.purge_agent_credentials, key, actor=None)


async def _sweep_agents(
    registry: SessionRegistry, store, access, dropped: list[str]
) -> None:
    """Ban sweep for guests' agents, which hold no guest session of their own."""
    emails: set[str] = set()
    for principal, _backend in registry.keys():
        if principal.startswith(AGENT_PRINCIPAL_PREFIX + GUEST_PRINCIPAL_PREFIX):
            emails.add(
                principal[len(AGENT_PRINCIPAL_PREFIX + GUEST_PRINCIPAL_PREFIX) :]
            )
    if access is not None:
        emails.update(await asyncio.to_thread(access.guest_credential_emails))
    for email in sorted(emails):
        if await asyncio.to_thread(_guest_banned, store, email):
            await _drop_agent(registry, access, email)
            if email not in dropped:
                dropped.append(email)


async def sweep_once(registry: SessionRegistry, store, access=None) -> list[str]:
    """Close every active guest session whose guest is banned; return emails.

    With the *access* store, a banned guest's agent is disconnected too and
    its credential rows are removed (o9) -- also when the guest has no open
    session of their own.
    """
    dropped: list[str] = []
    for principal, backend in registry.keys():
        if backend != BACKEND_SANDBOX or not principal.startswith(
            GUEST_PRINCIPAL_PREFIX
        ):
            continue
        email = principal[len(GUEST_PRINCIPAL_PREFIX) :]
        rows = await asyncio.to_thread(store.get_guest, email)
        ip = rows[-1][2] if rows else None
        if await asyncio.to_thread(store.is_banned, email, ip):
            await registry.close(principal, backend)
            dropped.append(email)
    await _sweep_agents(registry, store, access, dropped)
    return dropped


async def _loop(app: web.Application, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep_once(
                app["registry"],
                app["guest_store"],
                access=app.get(app_session.ACCESS_STORE),
            )
        except Exception:  # noqa: BLE001 -- the sweeper must never die
            logger.exception("ban sweep failed")
        try:
            # Same cadence: close IRC sessions opened through an app session
            # that has since ended (logout, password change, expiry, removal
            # from auth.allowed_emails) (c29).
            await app_session.sweep_once(app)
        except Exception:  # noqa: BLE001 -- the sweeper must never die
            logger.exception("app session sweep failed")
        try:
            # Same cadence: sign off guests with no action (message or
            # command) for guest_idle_close_s, open tab or not (c28), and
            # close Guest-view sessions with no open tab that long (d7).
            await app["registry"].reap_idle(
                now=time.monotonic(), idle_s=app["config"].guest_idle_close_s
            )
        except Exception:  # noqa: BLE001 -- the sweeper must never die
            logger.exception("idle session sweep failed")


def install(app: web.Application, interval: float = DEFAULT_SWEEP_INTERVAL_S) -> None:
    """Run the sweeper for the app's lifetime (guest mode only)."""

    async def start(app: web.Application) -> None:  # NOSONAR S7503 - aiohttp on_startup
        app[SWEEP_TASK] = asyncio.create_task(_loop(app, interval))

    async def stop(app: web.Application) -> None:
        task = app.get(SWEEP_TASK)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
