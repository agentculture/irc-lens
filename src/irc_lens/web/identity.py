"""Authenticated identity carried per-request through the web layer.

`Identity.principal` is the email under interactive SSO and the
service-token client-id / common-name otherwise. Downstream code
never branches on which.

Every Identity carries a ``tier`` (guest mode, docs/auth.md "Tiers"):

* ``approved`` — a verified Cloudflare Access JWT (``Cf-Access-Jwt-Assertion``
  header or ``CF_Authorization`` cookie) whose email is on
  ``auth.allowed_emails`` (or whose service-token common name is on
  ``auth.allowed_service_tokens``), or the single local ``auth.mode: dev``
  identity. The only tier that may reach the real mesh.
* ``guest`` — reserved for the signed guest-session cookie (a later task);
  never produced by the auth middleware itself. Sandbox only.
* ``anonymous`` — everything else when ``guest_mode.enabled``: no JWT, an
  unverifiable JWT, or a verified JWT for a principal not on the allowlist.

The field defaults to ``anonymous`` so an Identity built without an
explicit tier fails closed — it can never be mistaken for ``approved``.
"""

from __future__ import annotations

from typing import NamedTuple

TIER_APPROVED = "approved"
TIER_GUEST = "guest"
TIER_ANONYMOUS = "anonymous"
TIERS = frozenset({TIER_APPROVED, TIER_GUEST, TIER_ANONYMOUS})


class Identity(NamedTuple):
    principal: str
    nick: str
    raw_jwt_subject: str
    tier: str = TIER_ANONYMOUS

    @property
    def is_approved(self) -> bool:
        """True only for the real-mesh tier."""
        return self.tier == TIER_APPROVED


# The one anonymous identity: no principal, no nick. Carrying nothing from a
# rejected JWT keeps an unapproved email from leaking into downstream code.
ANONYMOUS_IDENTITY = Identity(
    principal="", nick="", raw_jwt_subject="", tier=TIER_ANONYMOUS
)


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
