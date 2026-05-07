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


def test_identity_is_namedtuple_like() -> None:
    i = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="sub-123")
    assert i.principal == "alice@example.com"
    assert i.nick == "spark-alice"
    assert i.raw_jwt_subject == "sub-123"
