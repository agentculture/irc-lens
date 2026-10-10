"""Guest-data retention sweep (owner deviation d9).

While guest mode is on, the lens applies :meth:`GuestStore.sweep` once at
startup and then every hour: guests inactive for ``guest_mode.retention_days``
(default 90) are erased through the same path as a self-service deletion
(:func:`irc_lens.web.deletion.erase_guest_data`: uploads, flag-log lines,
every store row, a hashed deletion record), and tokens, rate-limit attempts
and bans older than their retention windows expire. The sqlite work runs in
a worker thread; the task is cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiohttp import web

from irc_lens.web.deletion import erase_guest_data

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_INTERVAL_S = 3600.0
RETENTION_TASK = web.AppKey("guest_retention_task", asyncio.Task)


def _config(app: web.Application):
    # The entry state's config is the one the deletion routes read, so the
    # sweep and a self-service deletion always agree (flag_log etc.).
    from irc_lens.web.entry import ENTRY_STATE

    state = app.get(ENTRY_STATE)
    return state.config if state is not None else app["config"]


async def sweep_once(app: web.Application, *, now: int | None = None) -> dict:
    """Run one retention sweep over the app's guest store; return counts."""
    store = app["guest_store"]
    cfg = _config(app)
    media = app.get("media_store")

    def erase(email: str) -> int:
        return erase_guest_data(store, cfg, email, media)

    counts = await asyncio.to_thread(
        store.sweep, now, inactive_days=cfg.guest_retention_days, erase=erase
    )
    if any(counts.values()):
        logger.info("guest retention sweep: %s", counts)
    return counts


async def _loop(app: web.Application, interval: float) -> None:
    while True:
        try:
            await sweep_once(app)
        except Exception:  # noqa: BLE001 -- the sweeper must never die
            logger.exception("guest retention sweep failed")
        await asyncio.sleep(interval)


def install(
    app: web.Application, interval: float = DEFAULT_RETENTION_INTERVAL_S
) -> None:
    """Sweep at startup and every *interval* seconds (guest mode only)."""

    async def start(app: web.Application) -> None:  # NOSONAR S7503 - aiohttp on_startup
        app[RETENTION_TASK] = asyncio.create_task(_loop(app, interval))

    async def stop(app: web.Application) -> None:
        task = app.get(RETENTION_TASK)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
