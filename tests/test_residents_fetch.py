"""Tests for the residents fetch layer (plan task t2).

Covers ``resolve_residents_url``'s port-file discovery and
``fetch_residents``'s classification of every outcome culture's
``GET /residents.json`` can produce — the load-bearing mapping from
spec c11/c13/h10. Uses the in-tree ``FakeCultureServer``
(``tests/_culture_server.py``, the ``tests/_jwks_server.py`` idiom) so
the suite has no network egress.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from irc_lens.web.residents import (
    RESIDENT_KINDS,
    fetch_residents,
    resolve_residents_url,
)

from _culture_server import FakeCultureServer

# ---------------------------------------------------------------------------
# resolve_residents_url
# ---------------------------------------------------------------------------


def test_explicit_url_wins_even_with_overview_name(tmp_path: Path) -> None:
    # No port file exists for "myoverview" at all — proves the explicit
    # URL short-circuits before any file access is attempted.
    result = resolve_residents_url(
        "http://example.invalid/residents.json", "myoverview", pids_dir=tmp_path
    )
    assert result == "http://example.invalid/residents.json"


def test_port_file_happy_path(tmp_path: Path) -> None:
    (tmp_path / "overview-myoverview.port").write_text("54321\n")
    result = resolve_residents_url(None, "myoverview", pids_dir=tmp_path)
    assert result == "http://127.0.0.1:54321/residents.json"


def test_missing_port_file_returns_none(tmp_path: Path) -> None:
    result = resolve_residents_url(None, "no-such-overview", pids_dir=tmp_path)
    assert result is None


def test_garbled_port_file_returns_none(tmp_path: Path) -> None:
    (tmp_path / "overview-myoverview.port").write_text("not-a-port\n")
    result = resolve_residents_url(None, "myoverview", pids_dir=tmp_path)
    assert result is None


def test_both_args_none_returns_none(tmp_path: Path) -> None:
    assert resolve_residents_url(None, None, pids_dir=tmp_path) is None


def test_default_pids_dir_expands_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``pids_dir`` given -> the default must expanduser
    ``~/.culture/pids``, not a literal ``~`` path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    pids_dir = tmp_path / ".culture" / "pids"
    pids_dir.mkdir(parents=True)
    (pids_dir / "overview-myoverview.port").write_text("9999")
    result = resolve_residents_url(None, "myoverview")
    assert result == "http://127.0.0.1:9999/residents.json"


# ---------------------------------------------------------------------------
# fetch_residents
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def culture_server() -> AsyncIterator[FakeCultureServer]:
    """An in-tree FakeCultureServer bound to a random loopback port. See
    `tests/_culture_server.py` for the full surface."""
    server = FakeCultureServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_supported_payload_is_returned(culture_server: FakeCultureServer) -> None:
    culture_server.serve_supported(residents=[{"nick": "spark-claude"}])
    result = await fetch_residents(culture_server.residents_url)
    assert result.kind == "supported"
    assert result.payload is not None
    assert result.payload["supported"] is True
    assert result.payload["residents"] == [{"nick": "spark-claude"}]


async def test_supported_false_classifies_unsupported(
    culture_server: FakeCultureServer,
) -> None:
    culture_server.serve_unsupported()
    result = await fetch_residents(culture_server.residents_url)
    assert result.kind == "unsupported"
    assert result.payload is None


async def test_503_structured_body_classifies_unreachable(
    culture_server: FakeCultureServer,
) -> None:
    culture_server.serve_unreachable()
    result = await fetch_residents(culture_server.residents_url)
    assert result.kind == "unreachable"
    assert result.payload is None


async def test_503_garbled_body_still_classifies_unreachable(
    culture_server: FakeCultureServer,
) -> None:
    """Culture's contract: 503 always means unreachable/stalled, even if
    the body isn't parseable JSON — the status code is trusted over
    the body."""
    culture_server.serve_unreachable_garbled()
    result = await fetch_residents(culture_server.residents_url)
    assert result.kind == "unreachable"
    assert result.payload is None


async def test_500_classifies_unavailable(culture_server: FakeCultureServer) -> None:
    culture_server.serve_internal_error()
    result = await fetch_residents(culture_server.residents_url)
    assert result.kind == "unavailable"
    assert result.payload is None


async def test_non_json_200_body_classifies_unavailable(
    culture_server: FakeCultureServer,
) -> None:
    culture_server.serve_non_json()
    result = await fetch_residents(culture_server.residents_url)
    assert result.kind == "unavailable"
    assert result.payload is None


async def test_200_json_missing_supported_key_classifies_unavailable(
    culture_server: FakeCultureServer,
) -> None:
    culture_server.serve_missing_supported_key()
    result = await fetch_residents(culture_server.residents_url)
    assert result.kind == "unavailable"
    assert result.payload is None


async def test_connection_refused_classifies_unavailable(
    culture_server: FakeCultureServer,
) -> None:
    url = culture_server.residents_url
    await culture_server.stop()
    result = await fetch_residents(url)
    assert result.kind == "unavailable"
    assert result.payload is None


async def test_timeout_classifies_unavailable(
    culture_server: FakeCultureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force a short client timeout (so the test doesn't wait out the
    real 5s) against a handler that stalls past it.

    ``irc_lens.web.residents`` does ``import aiohttp`` and reads
    ``aiohttp.ClientTimeout`` off the same module object this test
    imports — capture the *original* class before patching, or the
    replacement lambda would call itself recursively.
    """
    original_timeout = aiohttp.ClientTimeout
    monkeypatch.setattr(
        aiohttp, "ClientTimeout", lambda **_kwargs: original_timeout(total=0.05)
    )
    culture_server.serve_stalling(delay_seconds=1.0)
    result = await fetch_residents(culture_server.residents_url)
    assert result.kind == "unavailable"
    assert result.payload is None


def test_resident_kinds_are_the_four_documented_values() -> None:
    assert RESIDENT_KINDS == (
        "supported",
        "unsupported",
        "unreachable",
        "unavailable",
    )
