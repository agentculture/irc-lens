"""Current Terms/Privacy versions and guest consent currency (guest mode).

``current_legal_versions(cfg)`` fetches ``cfg.guest_legal_version_url``, a
JSON document ``{"terms": str, "privacy": str, "effective": str}``, with a
short timeout and an in-process cache (``CACHE_TTL_S``). When a refresh
fails, the last good document is served (stale beats locking every guest
out over a blip); with nothing cached it raises
:class:`LegalVersionsUnavailable` -- callers must fail closed, never record
consent against an unknown version.

``consent_is_current(store, email, versions)`` is True only when the
guest's most recent consent record matches both current versions. The
sandbox routing task gates guest routes with these two functions; the
entry card records consent with the versions fetched here.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from irc_lens.config import LensConfig
    from irc_lens.guest_store import GuestStore

CACHE_TTL_S = 300.0
FETCH_TIMEOUT_S = 3.0

_monotonic = time.monotonic
# url -> (fetched_at, versions)
_cache: dict[str, tuple[float, dict[str, str]]] = {}


class LegalVersionsUnavailable(Exception):
    """No valid legal-version document could be obtained (fail closed)."""


def clear_cache() -> None:
    _cache.clear()


def _parse(doc: object) -> dict[str, str]:
    if not isinstance(doc, dict):
        raise LegalVersionsUnavailable("legal version document is not an object")
    terms, privacy = doc.get("terms"), doc.get("privacy")
    if not isinstance(terms, str) or not terms:
        raise LegalVersionsUnavailable("legal version document lacks 'terms'")
    if not isinstance(privacy, str) or not privacy:
        raise LegalVersionsUnavailable("legal version document lacks 'privacy'")
    effective = doc.get("effective", "")
    return {
        "terms": terms,
        "privacy": privacy,
        "effective": effective if isinstance(effective, str) else "",
    }


async def _fetch(url: str) -> dict[str, str]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
            ) as resp:
                resp.raise_for_status()
                doc = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise LegalVersionsUnavailable(
            f"could not fetch legal versions ({type(exc).__name__})"
        ) from exc
    return _parse(doc)


async def current_legal_versions(cfg: "LensConfig") -> dict:
    """Return ``{"terms", "privacy", "effective"}`` for the configured URL."""
    url = cfg.guest_legal_version_url
    hit = _cache.get(url)
    if hit is not None and _monotonic() - hit[0] < CACHE_TTL_S:
        return dict(hit[1])
    # No lock: a concurrent miss just fetches twice (idempotent), and a
    # module-level asyncio.Lock would bind to whichever loop first contends.
    try:
        versions = await _fetch(url)
    except LegalVersionsUnavailable:
        if hit is not None:
            return dict(hit[1])
        raise
    _cache[url] = (_monotonic(), versions)
    return dict(versions)


def consent_is_current(store: "GuestStore", email: str, versions: dict) -> bool:
    """True iff *email*'s latest consent matches the current versions."""
    consents = store.get_consents(email)
    if not consents:
        return False
    _email, _ip, tos_version, privacy_version, _ts = consents[-1]
    return tos_version == versions.get("terms") and privacy_version == versions.get(
        "privacy"
    )
