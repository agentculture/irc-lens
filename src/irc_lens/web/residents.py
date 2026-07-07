"""Server-side fetch layer for culture's ``GET /residents.json``.

This module is the one seam through which irc-lens talks to culture's
resource-view endpoint: it discovers the endpoint's URL (an explicit
override, or by reading the ephemeral port ``culture mesh overview
--serve`` writes to its pidfile) and fetches + classifies the result
into one of four outcomes the console can always render as HTTP 200 —
never proxying the upstream status code, and never raising past this
module.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import aiohttp

RESIDENT_KINDS = ("supported", "unsupported", "unreachable", "unavailable")


@dataclass(frozen=True)
class ResidentsResult:
    """A classified outcome of a residents fetch.

    ``kind`` is one of :data:`RESIDENT_KINDS`. ``payload`` carries the
    parsed culture payload only when ``kind == "supported"`` — every
    other kind means the caller renders a static, kind-specific notice
    instead of a table.
    """

    kind: str
    payload: dict | None


def resolve_residents_url(
    residents_url: str | None,
    overview_name: str | None,
    pids_dir: Path | None = None,
) -> str | None:
    """Return the URL to fetch, or ``None`` when nothing resolves.

    An explicit ``residents_url`` always wins and is returned
    unchanged. Otherwise, when ``overview_name`` is set, read the port
    culture's ``mesh overview --serve`` wrote to
    ``{pids_dir or ~/.culture/pids}/overview-{overview_name}.port`` (a
    file containing a single integer) and build
    ``http://127.0.0.1:{port}/residents.json``. A missing port file, an
    unreadable one, or content that isn't a bare integer all yield
    ``None`` — this function never raises, matching the "resource view
    unavailable" degrade path callers fall back to when discovery
    itself comes up empty.
    """
    if residents_url:
        return residents_url
    if not overview_name:
        return None
    base = pids_dir if pids_dir is not None else Path("~/.culture/pids").expanduser()
    port_file = base / f"overview-{overview_name}.port"
    try:
        port = int(port_file.read_text().strip())
    except (OSError, ValueError):
        return None
    return f"http://127.0.0.1:{port}/residents.json"


async def fetch_residents(url: str) -> ResidentsResult:
    """Fetch and classify culture's residents payload. Never raises.

    The mapping is load-bearing (spec c11/c13/h10):

    - HTTP 200, valid JSON, ``supported`` true -> ``"supported"`` with
      the parsed payload
    - HTTP 200, valid JSON, ``supported`` false -> ``"unsupported"``
    - HTTP 503 — even with a malformed/non-JSON body — ->
      ``"unreachable"`` (culture's contract: 503 always means "server
      unreachable or presence stream stalled", so we trust the status
      code over the body)
    - connection refused, timeout, HTTP 500 or any other status,
      non-JSON 200 body, or a 200 JSON body without a boolean
      ``supported`` -> ``"unavailable"``

    Reuses the repo's one existing outbound-HTTP idiom (see
    ``_JWKSCache._refresh`` in ``web/auth.py``): a fresh
    ``aiohttp.ClientSession`` per call and ``ClientTimeout(total=5)``,
    catching both ``aiohttp.ClientError`` and ``asyncio.TimeoutError``
    (the latter is not a subclass of the former).
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                status = resp.status
                body = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return ResidentsResult("unavailable", None)

    if status == 503:
        return ResidentsResult("unreachable", None)
    if status != 200:
        return ResidentsResult("unavailable", None)

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ResidentsResult("unavailable", None)

    if not isinstance(payload, dict) or not isinstance(payload.get("supported"), bool):
        return ResidentsResult("unavailable", None)

    if payload["supported"]:
        # The envelope checks above trust nothing about the body, and the
        # residents array gets the same treatment: an ephemeral-port
        # endpoint can be answered by the wrong process or a
        # version-skewed serializer, and a malformed-but-200 payload must
        # degrade, not raise downstream in the renderer (PR #54 review).
        residents = payload.get("residents")
        if not isinstance(residents, list) or not all(
            isinstance(r, dict) for r in residents
        ):
            return ResidentsResult("unavailable", None)
        return ResidentsResult("supported", payload)
    return ResidentsResult("unsupported", None)
