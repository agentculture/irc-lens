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

from irc_lens.web.sessions import (
    BACKEND_SANDBOX,
    GUEST_PRINCIPAL_PREFIX,
    SessionRegistry,
)

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_S = 30.0
SWEEP_TASK = web.AppKey("ban_sweep_task", asyncio.Task)


async def sweep_once(registry: SessionRegistry, store) -> list[str]:
    """Close every active guest session whose guest is banned; return emails."""
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
    return dropped


async def _loop(app: web.Application, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep_once(app["registry"], app["guest_store"])
        except Exception:  # noqa: BLE001 -- the sweeper must never die
            logger.exception("ban sweep failed")
        try:
            # Same cadence: close sandbox sessions nobody has had open for
            # guest_idle_close_s (closed tabs send no goodbye) (d7).
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
