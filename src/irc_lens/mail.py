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

from irc_lens._errors import EXIT_ENV_ERROR, AfiError
from irc_lens.config import LensConfig

#: POST target of Resend's transactional email API.
RESEND_API_URL = "https://api.resend.com/emails"

_SUBJECT = "Your Culture chat.culture.dev guest token"


def render_token_email(token: str) -> tuple[str, str]:
    """Render the single fixed guest-token template.

    Returns ``(subject, text_body)``. The body shape is identical for
    every token and every address; only the token line varies. The token
    itself is embedded in the body (it *is* the credential the guest
    types in) but is never written to any log by this module.
    """
    if not isinstance(token, str) or not token:
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message="guest token must be a non-empty string",
            remediation="generate the token before rendering its email",
        )
    body = (
        "Hello,\n\n"
        "You asked for a guest seat on chat.culture.dev. Use the token "
        "below to enter:\n\n"
        f"    {token}\n\n"
        "The token expires after a while; after that you can request a "
        "fresh one from the same page.\n\n"
        "If you did not ask for a guest seat, you can ignore this email "
        "— nothing else needs doing.\n\n"
        "The Culture team\n"
    )
    return _SUBJECT, body


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
