"""Tests for docs/deployment-permissions.md and scripts/verify_mesh_ircd.py.

Plan task t3 (email-keyed-permissions-one-sponsored-agent): the mesh IRCd must be
reachable only by the lens, the mesh's own agents and its federation links. The
production change (unit/bind or firewall) is applied by the owner during cutover;
this task ships the doc plus a verification script that attempts a direct IRC
connect from a non-allowed source and exits non-zero if it succeeds (o10).

Standard library only: no AgentIRC server, no network beyond loopback sockets
opened inside the tests.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "deployment-permissions.md"
SCRIPT = REPO_ROOT / "scripts" / "verify_mesh_ircd.py"

# An IPv4 literal or a dotted hostname with a public TLD would name a specific
# host, which the spec forbids in committed artifacts.
_HOST_LITERAL = re.compile(
    r"(?:\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])+\.(?:com|org|net|io|dev)\b)",
    re.IGNORECASE,
)


def _doc_text() -> str:
    assert DOC.is_file(), f"missing {DOC}"
    return DOC.read_text(encoding="utf-8")


def test_doc_documents_restriction_without_naming_a_host() -> None:
    """The doc explains how the mesh IRCd is restricted (bind address or
    firewall) without naming a specific host."""
    text = _doc_text().lower()
    assert "bind" in text, "doc must cover the bind-address restriction"
    assert "firewall" in text, "doc must cover the firewall restriction"
    assert not _HOST_LITERAL.search(
        DOC.read_text(encoding="utf-8")
    ), "doc must not name a specific host or address"


def test_doc_lists_who_must_still_connect() -> None:
    """The doc lists every party that must keep reaching the mesh IRCd:
    the lens, the mesh's own agents, and federation links."""
    text = _doc_text().lower()
    assert "lens" in text
    assert "agent" in text
    assert "link" in text or "federation" in text


def test_doc_lists_the_real_federation_link_schema() -> None:
    """Before asserting that no link config exists, the real key names were read
    from the culture server config schema (closes lapse l1); the doc records
    them and lists the configured remote peers by name only."""
    text = _doc_text()
    # culture_core/mesh_config.py: MeshLinkConfig(name, host, port, trust),
    # nested under `server:` in ~/.culture/mesh.yaml as a `links:` list.
    assert "links:" in text, "doc must name the real `links:` config key"
    assert "trust" in text, "doc must document the per-link `trust` key"
    assert "mesh.yaml" in text
    # Configured peers (read from the real mesh.yaml) are listed by name.
    for peer in ("thor", "orin"):
        assert peer in text.lower(), f"doc must list configured peer {peer}"


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_verify_script_exits_nonzero_when_connect_succeeds() -> None:
    """o10: a direct connect from a non-allowed source that SUCCEEDS means the
    restriction is broken, so the script must exit non-zero."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        proc = _run_script("--host", "127.0.0.1", "--port", str(port), "--timeout", "5")
    finally:
        listener.close()
    assert proc.returncode != 0, (
        f"script exited 0 although a non-allowed source connected "
        f"(stdout={proc.stdout!r} stderr={proc.stderr!r})"
    )


def test_verify_script_exits_zero_when_connect_is_refused() -> None:
    """A refused connection proves the restriction holds for this source, so
    the script must exit zero."""
    # Bind then close to pick a port that is very likely free.
    tmp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tmp.bind(("127.0.0.1", 0))
    port = tmp.getsockname()[1]
    tmp.close()
    proc = _run_script("--host", "127.0.0.1", "--port", str(port), "--timeout", "5")
    assert proc.returncode == 0, (
        f"script exited {proc.returncode} although the connect was refused "
        f"(stdout={proc.stdout!r} stderr={proc.stderr!r})"
    )


def test_verify_script_has_no_default_host_or_port() -> None:
    """Spec c23: no host/address literals, so --host and --port are required."""
    assert "127.0.0.1" not in SCRIPT.read_text(encoding="utf-8")
    for args in ((), ("--host", "x.invalid"), ("--port", "6667")):
        proc = _run_script(*args)
        assert proc.returncode != 0, f"args {args} must be rejected"
