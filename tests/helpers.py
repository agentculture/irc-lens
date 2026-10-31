"""Shared test helpers — nothing pytest-specific lives here.

Both ``test_web_skeleton.py`` and ``test_web_events.py`` need to build
an aiohttp ``Application`` against an unconnected ``Session``. The
``LensConfig`` and the registry-pre-seed dance are identical between
them; centralizing the pair here keeps the two test files in lockstep
when Phase 3 reshapes the wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from irc_lens.config import LensConfig
from irc_lens.web import make_app

if TYPE_CHECKING:
    from aiohttp import web

    from irc_lens.session import Session


DEV_CONFIG = LensConfig(
    auth_mode="dev",
    dev_nick="lens-test",
    dev_email="dev@local",
    cf_aud=None,
    cf_team_domain=None,
    allowed_emails=(),
    allowed_service_tokens=(),
    server_name="testsrv",
    server_host="127.0.0.1",
    server_port=6667,
    web_bind="127.0.0.1",
    web_port=0,
    media_enabled=True,
    media_dir="/tmp/irc-lens-test-media",
    media_max_file_bytes=10485760,
    media_max_store_bytes=268435456,
    media_public_base_url="",
    media_remote_embeds="click",
    media_trusted_hosts=(),
    culture_residents_url=None,
    culture_overview_name=None,
)


def make_app_for(session: "Session") -> "web.Application":
    """Build an app pre-seeded with *session* so ``get_or_open`` returns
    it without re-running ``connect()``."""
    app = make_app(DEV_CONFIG, lambda _nick: session)
    app["registry"].register(DEV_CONFIG.dev_email, session)
    return app


#: Nicknames the permission suites give their approved test users (q14).
#: Chosen by hand and unrelated to the addresses, so a test can also check
#: that nothing derived from an email reaches anyone else.
TEST_NICKNAMES = {
    "alice@example.com": "aurora",
    "root@example.com": "chief",
    "bob@example.org": "basil",
    "bob@example.net": "brook",
    "ann@example.org": "amber",
    "ben@example.org": "birch",
    "carol@example.net": "cedar",
    "pat@example.com": "pepper",
    "sam@example.org": "sage",
    "pending@example.org": "pip",
    "nobody@example.org": "nemo",
}


def nickname_of(email: str) -> str:
    """The nickname :data:`TEST_NICKNAMES` gives *email*."""
    return TEST_NICKNAMES[email.strip().lower()]


def mesh_nick_of(email: str, server: str = "testsrv") -> str:
    """The IRC nick an approved test user gets: ``<server>-<nickname>``."""
    return f"{server}-{nickname_of(email)}"
