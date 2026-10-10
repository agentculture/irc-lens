"""Guest token email sender (task t6).

One fixed template, one sender, every address: :func:`render_token_email`
returns the same subject/body shape for every guest token, so a recipient
can never tell from the envelope whether their request was approved or
queued (obligation o5 — the approval decision is carried by the token's
lifetime, not by the mail).

Provider adapters implement :class:`MailAdapter` (a ``send(to, subject,
body)`` protocol). v10 ships one real adapter (:class:`ResendAdapter`,
posting JSON to Resend's transactional API with stdlib ``urllib`` — no
new dependency), a :class:`RecordingAdapter` for tests, and a
:class:`NoneAdapter` that raises a clear error when mail is configured
off. :func:`make_adapter` selects between them from
``LensConfig.guest_mail_provider``.

Credentials come from the environment injected via grant: the API key is
read from ``os.environ[cfg.guest_mail_api_key_env]`` at send time, never
stored on the adapter, and neither the key nor the token is ever logged.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

from irc_lens import __version__
from irc_lens._errors import EXIT_ENV_ERROR, AfiError
from irc_lens.config import LensConfig
from irc_lens.guest_store import DEFAULT_TOKEN_TTL

#: POST target of Resend's transactional email API.
RESEND_API_URL = "https://api.resend.com/emails"

#: Resend sits behind Cloudflare, which rejects urllib's default
#: ``Python-urllib/3.x`` agent (error 1010, HTTP 403) — name ourselves.
USER_AGENT = f"irc-lens/{__version__}"

_SUBJECT = "Your chat.culture.dev code"
_DELETE_SUBJECT = "Your chat.culture.dev deletion code"

#: ``purpose`` values accepted by :func:`render_token_email`.
PURPOSE_GUEST = "guest"
PURPOSE_DELETE = "delete"
PURPOSE_SIGNIN = "signin"

_SIGNIN_SUBJECT = "Your chat.culture.dev sign-in code"
_SETPW_SUBJECT = "Set your chat.culture.dev password"

#: Default lifetime of a sign-in code (10 minutes) and a set-password link.
SIGNIN_TOKEN_TTL = 600
SETPW_LINK_TTL = 1800


def _human_duration(seconds: int) -> str:
    """``900`` -> ``15 minutes``; ``60`` -> ``1 minute``; sub-minute in seconds."""
    if seconds >= 60 and seconds % 60 == 0:
        n, unit = seconds // 60, "minute"
    else:
        n, unit = seconds, "second"
    return f"{n} {unit}{'' if n == 1 else 's'}"


def render_token_email(
    token: str, ttl_s: int | None = None, *, purpose: str = PURPOSE_GUEST
) -> tuple[str, str]:
    """Render the fixed token template for *purpose* (guest seat or deletion).

    Returns ``(subject, text_body)``. For a given *purpose* the body shape is
    identical for every token and every address; only the code line varies.
    *ttl_s* is the real lifetime the store issues tokens with (the same for
    every address). The code itself is embedded in the body (it *is* the
    credential the guest types in) but is never written to any log by this
    module.
    """
    if ttl_s is None:
        ttl_s = SIGNIN_TOKEN_TTL if purpose == PURPOSE_SIGNIN else DEFAULT_TOKEN_TTL
    if purpose not in (PURPOSE_GUEST, PURPOSE_DELETE, PURPOSE_SIGNIN):
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"unknown token email purpose: {purpose!r}",
            remediation="use 'guest', 'delete' or 'signin'",
        )
    if not isinstance(token, str) or not token:
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message="guest token must be a non-empty string",
            remediation="generate the token before rendering its email",
        )
    if purpose == PURPOSE_SIGNIN:
        return _SIGNIN_SUBJECT, _signin_body(token, ttl_s)
    body = (
        "Hello,\n\n"
        "You asked for a guest seat on chat.culture.dev. Enter the code "
        "below in the Code field to continue:\n\n"
        f"    {token}\n\n"
        f"The code works once and expires in {_human_duration(ttl_s)}; "
        "after that you can request a fresh one from the same page.\n\n"
        "If you did not ask for a guest seat, you can ignore this email "
        "— nothing else needs doing.\n\n"
        "The Culture team\n"
    )
    if purpose == PURPOSE_DELETE:
        return _DELETE_SUBJECT, _delete_body(token, ttl_s)
    return _SUBJECT, body


def _signin_body(token: str, ttl_s: int) -> str:
    return (
        "Hello,\n\n"
        "The correct password was just entered for this address on "
        "chat.culture.dev. Enter the code below in the Code field to "
        "finish signing in:\n\n"
        f"    {token}\n\n"
        f"The code works once and expires in {_human_duration(ttl_s)}.\n\n"
        "If this wasn't you, someone may know your password: use "
        "'Set or reset password' on the sign-in page to change it.\n\n"
        "The Culture team\n"
    )


def _is_allowed_base(base_url: object) -> bool:
    if not isinstance(base_url, str):
        return False
    if base_url.startswith("https://"):
        return len(base_url) > len("https://") and not base_url.startswith("https:///")
    for prefix in ("http://127.0.0.1", "http://localhost"):
        if base_url.startswith(prefix) and base_url[len(prefix) :][:1] in (
            "",
            ":",
            "/",
        ):
            return True
    return False


def render_link_email(
    base_url: str, token: str, ttl_s: int = SETPW_LINK_TTL
) -> tuple[str, str]:
    """Render the set-password email: ``(subject, text_body)``.

    The body carries ``<base_url>/password/<token>``; it is identical for
    every address apart from the link. The link is the credential and is
    never written to any log by this module.
    """
    if not _is_allowed_base(base_url):
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message="set-password base URL must be https (or loopback http)",
            remediation="pass the public https base URL of the lens",
        )
    if not isinstance(token, str) or not token:
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message="set-password token must be a non-empty string",
            remediation="generate the token before rendering its email",
        )
    link = f"{base_url.rstrip('/')}/password/{token}"
    body = (
        "Hello,\n\n"
        "Someone asked to set or reset the password for this address on "
        "chat.culture.dev. Open the link below to choose a new one:\n\n"
        f"    {link}\n\n"
        f"The link works once and expires in {_human_duration(ttl_s)}.\n\n"
        "If you did not ask for this, you can ignore this email "
        "— your password stays the same.\n\n"
        "The Culture team\n"
    )
    return _SETPW_SUBJECT, body


def _delete_body(token: str, ttl_s: int) -> str:
    return (
        "Hello,\n\n"
        "You asked to delete your guest data on chat.culture.dev. Enter the "
        "code below in the Code field on the deletion page to confirm:\n\n"
        f"    {token}\n\n"
        "Deleting is permanent: your guest account, your conversations and "
        "your room are erased and can't be recovered.\n\n"
        f"The code works once and expires in {_human_duration(ttl_s)}; "
        "after that you can request a fresh one from the same page.\n\n"
        "If you did not ask to delete anything, you can ignore this email "
        "— nothing will be deleted.\n\n"
        "The Culture team\n"
    )


@runtime_checkable
class MailAdapter(Protocol):
    """A provider that delivers one plain-text email."""

    def send(self, to: str, subject: str, body: str) -> None:
        """Deliver *body* to *to*; raise on failure."""
        ...


class RecordingAdapter:
    """Test double: records every send, touches no network."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


class NoneAdapter:
    """Raised-adapter for ``provider: none`` (the default).

    Guest mode is off by default and mail is optional; rather than fail
    silently, sending through this adapter raises a clear error pointing
    at the config keys to set.
    """

    def send(self, to: str, subject: str, body: str) -> None:
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message="no mail provider configured for guest tokens",
            remediation=(
                "set `guest_mode.mail.provider:` to a supported provider "
                "(e.g. `resend`) and `guest_mode.mail.from:` to the sender "
                "address in your lens config"
            ),
        )


class ResendAdapter:
    """Transactional-API adapter posting JSON to Resend.

    The API key is read from ``os.environ[api_key_env]`` at each send
    (credentials arrive via grant-injected env, per the task instruction)
    and goes only into the ``Authorization`` header — never into the
    request body, never into any log line.
    """

    def __init__(self, from_address: str, api_key_env: str) -> None:
        self._from = from_address
        self._api_key_env = api_key_env

    def build_request(
        self, api_key: str, to: str, subject: str, body: str
    ) -> urllib.request.Request:
        """Build the Resend POST without sending it.

        Kept separate from :meth:`send` so tests exercise the exact
        wire shape (URL, headers, JSON payload) with zero network.
        """
        payload = json.dumps(
            {"from": self._from, "to": [to], "subject": subject, "text": body}
        ).encode("utf-8")
        return urllib.request.Request(
            RESEND_API_URL,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": USER_AGENT,
            },
        )

    def send(self, to: str, subject: str, body: str) -> None:
        try:
            api_key = os.environ[self._api_key_env]
        except KeyError:
            raise AfiError(
                code=EXIT_ENV_ERROR,
                message=f"mail API key env var {self._api_key_env!r} is not set",
                remediation=(
                    f"export {self._api_key_env} (or inject it via grant) "
                    "before starting the lens with guest mail enabled"
                ),
            ) from None
        req = self.build_request(api_key, to, subject, body)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            # Re-raise without the response body: it may echo the
            # request back, which would put the token in logs.
            raise AfiError(
                code=EXIT_ENV_ERROR,
                message=f"mail provider rejected the send (HTTP {exc.code})",
                remediation="check `guest_mode.mail.*` and the provider account",
            ) from exc
        except urllib.error.URLError as exc:
            raise AfiError(
                code=EXIT_ENV_ERROR,
                message=f"mail provider unreachable ({exc.reason})",
                remediation="check network connectivity to the mail provider",
            ) from exc


def make_adapter(cfg: LensConfig) -> MailAdapter:
    """Select the provider adapter from ``cfg.guest_mail_provider``."""
    provider = cfg.guest_mail_provider.lower()
    if provider == "none":
        return NoneAdapter()
    if provider == "resend":
        return ResendAdapter(cfg.guest_mail_from, cfg.guest_mail_api_key_env)
    raise AfiError(
        code=EXIT_ENV_ERROR,
        message=f"unknown guest mail provider {cfg.guest_mail_provider!r}",
        remediation=("set `guest_mode.mail.provider:` to one of: none, resend"),
    )
