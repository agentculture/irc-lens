"""Identity NamedTuple + derive_nick rules."""
from __future__ import annotations

import pytest

from irc_lens.web.identity import Identity, derive_nick


@pytest.mark.parametrize("server,principal,expected", [
    ("spark", "ori.nachum@gmail.com", "spark-orinachum"),
    ("spark", "Ori.Nachum@Gmail.com", "spark-orinachum"),
    ("spark", "alice+irc@example.com", "spark-aliceirc"),
    ("spark", "bob_smith@example.com", "spark-bobsmith"),
    ("spark", "kebab-case@example.com", "spark-kebab-case"),
    ("spark", "svc-token-id", "spark-svc-token-id"),
])
def test_derive_nick_cases(server: str, principal: str, expected: str) -> None:
    assert derive_nick(server, principal) == expected


def test_derive_nick_empty_after_strip_raises() -> None:
    with pytest.raises(ValueError):
        derive_nick("spark", "...@example.com")


@pytest.mark.parametrize("principal,expected", [
    # str.isalnum() is Unicode-aware and would let non-ASCII letters/digits
    # through (Copilot PR #31 review). The documented contract is ASCII-only
    # [a-z0-9-]; AgentIRC rejects non-ASCII nicks, so derivation drops them.
    ("björn@example.com", "spark-bjrn"),               # ö stripped
    ("ñoño@example.com", "spark-oo"),                  # ñ stripped (twice)
    ("user١٢٣@example.com", "spark-user"),             # Arabic-Indic digits stripped
    ("漢字user@example.com", "spark-user"),            # CJK stripped
])
def test_derive_nick_strips_non_ascii(principal: str, expected: str) -> None:
    assert derive_nick("spark", principal) == expected


def test_derive_nick_all_non_ascii_raises() -> None:
    """If sanitization strips everything because the local-part is entirely
    non-ASCII, we must fail closed instead of returning a bare prefix."""
    with pytest.raises(ValueError):
        derive_nick("spark", "漢字@example.com")


def test_identity_is_namedtuple_like() -> None:
    i = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="sub-123")
    assert i.principal == "alice@example.com"
    assert i.nick == "spark-alice"
    assert i.raw_jwt_subject == "sub-123"
