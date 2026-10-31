"""No public host name or address literal in ``src/`` or ``docs/specs/``.

The lens's public endpoint is deployment configuration (``web.mcp_path``,
``auth.app_signin.base_url``); code and specs must never hardcode where it
lives. The scan is generic on purpose: it does not name any particular host,
it flags every host-looking literal that is not on a small allow-list of
reserved example names, loopback addresses, third-party API endpoints the code
must call, and code identifiers that merely look like hosts.

Pre-existing literals from before the email-keyed-permissions work sit in
``LEGACY_BASELINE`` (file -> number of offending matches). That table is a
ratchet, not an exemption: a legacy file may not gain a new literal, no other
file may hold any, and a baseline entry that no longer matches fails the test
so the table can only shrink. Removing the legacy literals is the follow-up.

Also checks ``docs/permissions.md`` against the code it describes.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = (ROOT / "src", ROOT / "docs" / "specs", ROOT / "docs" / "permissions.md")

_HOST_RE = re.compile(
    r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|org|net|dev|io|ai|cloud))(?![\w-])",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")

#: Reserved example names (RFC 2606) and their subdomains.
_RESERVED = ("example.com", "example.org", "example.net")
#: Third-party endpoints the code must call, and names used as test fixtures
#: or code identifiers (``auth.dev`` is the config key path, not a host).
_ALLOWED_EXACT = {
    "challenges.cloudflare.com",  # Turnstile widget script origin
    "api.resend.com",  # mail provider API
    "auth.dev",  # config key path: auth.mode dev
    "trusted.com",  # fixtures for the markdown link allow-list
    "evil.org",
    "trusted.com.evil.org",
}
_ALLOWED_IPV4_PREFIXES = ("127.", "0.0.0.0", "192.0.2.", "198.51.100.", "203.0.113.")

#: Pre-existing offenders (relative path -> match count). Ratchet only.
LEGACY_BASELINE: dict[str, int] = {
    "docs/specs/2026-07-07-residents-presence-page.md": 1,
    "docs/specs/2026-10-10-app-native-sign-in.md": 3,
    "src/irc_lens/config.py": 4,
    "src/irc_lens/mail.py": 10,
    "src/irc_lens/static/lens.css": 1,
    "src/irc_lens/static/mesh.js": 1,
    "src/irc_lens/templates/delete.html.j2": 1,
    "src/irc_lens/templates/entry.html.j2": 2,
    "src/irc_lens/web/entry.py": 3,
}


def _files() -> list[Path]:
    out: list[Path] = []
    for base in SCANNED:
        if base.is_file():
            out.append(base)
            continue
        out.extend(p for p in sorted(base.rglob("*")) if p.is_file())
    return out


def _is_allowed_host(host: str) -> bool:
    host = host.lower()
    if host in _ALLOWED_EXACT:
        return True
    return any(host == r or host.endswith("." + r) for r in _RESERVED)


def _offences(text: str) -> list[str]:
    found = [
        m.group(1) for m in _HOST_RE.finditer(text) if not _is_allowed_host(m.group(1))
    ]
    found += [
        m.group(1)
        for m in _IPV4_RE.finditer(text)
        if not m.group(1).startswith(_ALLOWED_IPV4_PREFIXES)
        and all(int(o) <= 255 for o in m.group(1).split("."))
    ]
    return found


def _scan() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary asset
        n = len(_offences(text))
        if n:
            counts[str(path.relative_to(ROOT))] = n
    return counts


def test_scan_sees_the_trees() -> None:
    names = {str(p.relative_to(ROOT)) for p in _files()}
    assert any(n.startswith("src/irc_lens/") for n in names)
    assert any(n.startswith("docs/specs/") for n in names)


def test_scanner_flags_hosts_and_addresses_and_spares_reserved_names() -> None:
    assert _offences("see https://chat.some-lens.dev/mcp") == ["chat.some-lens.dev"]
    assert _offences("connect to 203.1.2.3 now") == ["203.1.2.3"]
    assert _offences("https://lens.example.com 127.0.0.1 0.0.0.0 localhost") == []
    assert _offences("irc_lens.web.app request.app") == []


def test_no_public_host_literals_beyond_the_legacy_baseline() -> None:
    counts = _scan()
    new = {f: n for f, n in counts.items() if n > LEGACY_BASELINE.get(f, 0)}
    assert not new, f"host/address literals found (read them from config): {new}"
    stale = {f: n for f, n in LEGACY_BASELINE.items() if counts.get(f, 0) < n}
    assert not stale, f"legacy baseline is stale, shrink it: {stale}"


def test_feature_files_hold_no_literal_at_all() -> None:
    """The files written for this feature are never in the baseline."""
    feature = (
        "docs/specs/2026-10-30-email-keyed-permissions-one-sponsored-agent.md",
        "docs/permissions.md",
        "src/irc_lens/access.py",
        "src/irc_lens/access_store.py",
        "src/irc_lens/agent_nick.py",
        "src/irc_lens/web/admin.py",
        "src/irc_lens/web/agent_auth.py",
        "src/irc_lens/web/agent_sessions.py",
        "src/irc_lens/web/input_gate.py",
        "src/irc_lens/web/mcp_http.py",
        "src/irc_lens/cli/_commands/access.py",
    )
    counts = _scan()
    assert not {f: counts[f] for f in feature if f in counts}
    assert not set(feature) & set(LEGACY_BASELINE)


# -- docs/permissions.md ---------------------------------------------------------

_DOC = ROOT / "docs" / "permissions.md"

_REQUIRED_HEADINGS = (
    "Roles and levels",
    "Grants",
    "Invites",
    "Agent credential",
    "MCP endpoint",
    "Owner-only delivery",
    "Extensions",
    "Switches",
    "Audit and metrics",
    "Before and after",
    "For admins",
    "For approved users",
    "For guests",
    "For agent authors",
)


def _doc() -> str:
    assert _DOC.is_file(), "docs/permissions.md is missing"
    return _DOC.read_text(encoding="utf-8")


def test_permissions_doc_covers_every_topic_and_audience() -> None:
    headings = [
        line.lstrip("#").strip() for line in _doc().splitlines() if line.startswith("#")
    ]
    for wanted in _REQUIRED_HEADINGS:
        assert any(wanted.lower() in h.lower() for h in headings), wanted


def test_permissions_doc_code_references_exist() -> None:
    refs = set(re.findall(r"`((?:src|tests|docs)/[\w./-]+\.(?:py|md|j2))`", _doc()))
    assert len(refs) >= 10
    missing = sorted(r for r in refs if not (ROOT / r).exists())
    assert not missing, missing


def test_permissions_doc_before_after_covers_the_surveyed_findings() -> None:
    text = _doc().lower()
    section = text.split("before and after", 1)[1]
    for needle in (
        "binary approval",
        "one guest room",
        "stdio",
        "no ircd",
        "web/identity.py",
        "web/sessions.py",
        "tools.py",
    ):
        assert needle in section, needle


def test_permissions_doc_links_the_sibling_docs() -> None:
    text = _doc()
    assert "(auth.md" in text
    assert "(deployment-permissions.md" in text
