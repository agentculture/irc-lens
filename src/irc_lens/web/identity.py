"""Authenticated identity carried per-request through the web layer.

`Identity.principal` is the email under interactive SSO and the
service-token client-id / common-name otherwise. Downstream code
never branches on which.
"""
from __future__ import annotations

from typing import NamedTuple


class Identity(NamedTuple):
    principal: str
    nick: str
    raw_jwt_subject: str


def derive_nick(server_name: str, principal: str) -> str:
    """Return ``<server_name>-<sanitized-local-part>``.

    Sanitization: lowercase the local part (the bit before ``@``, or the
    whole string if no ``@``), then drop everything outside ASCII
    ``[a-z0-9-]``. ``str.isalnum()`` is Unicode-aware and would let
    non-ASCII letters/digits through (``ö``, ``ñ``, ``١``); AgentIRC
    rejects those, so we filter to ASCII explicitly to keep the
    contract documented above.

    Raises:
        ValueError: when the sanitized local part is empty.
    """
    local = principal.split("@", 1)[0].lower()
    sanitized = "".join(c for c in local if (c.isascii() and c.isalnum()) or c == "-")
    if not sanitized:
        raise ValueError(
            f"nick derivation produced empty result for principal={principal!r}"
        )
    return f"{server_name}-{sanitized}"
