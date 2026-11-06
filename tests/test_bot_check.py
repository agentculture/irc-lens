"""t5: warn when guests face no bot check, refuse guest codes on request, and
let trusted browsers sign in approved users whatever the widget says."""

from __future__ import annotations

import dataclasses
import logging

from aiohttp import web

from irc_lens.cli._meta import bot_check_doctor
from irc_lens.config import load_config
from irc_lens.web import app_session, entry

from test_app_signin import ALICE, PW, UNKNOWN, Env, env  # noqa: F401 -- fixture
from test_entry import Harness, _request_token, h, legal_server  # noqa: F401 -- fixtures

KEYS = {entry.TURNSTILE_SITE_KEY_ENV: "site", entry.TURNSTILE_SECRET_ENV: "secret"}


def _sent_to(env: Env, email: str) -> int:
    return sum(1 for to, _s, _b in env.mail.sent if to == email)


async def test_trusted_browser_signs_in_with_the_bot_check_failing(env: Env) -> None:
    env.state.verifier.ok = False
    device = env.store.add_trusted_device(ALICE)
    await env.signin(ALICE, PW, cookie=f"{app_session.DEVICE_COOKIE_NAME}={device}")
    await env.drain()
    assert _sent_to(env, ALICE) == 1

    await env.signin(ALICE, PW, ip="203.0.113.9")
    await env.drain()
    assert _sent_to(env, ALICE) == 1


async def test_required_bot_check_without_keys_refuses_guests_only(env: Env) -> None:
    env.state.config = dataclasses.replace(env.state.config, guest_require_bot_check=True)
    env.state.verifier = entry.NoopVerifier()
    resp = await env.signin(UNKNOWN, "")
    assert resp.status == 200  # the same page either way
    await env.signin(ALICE, PW)
    await env.drain()
    assert _sent_to(env, UNKNOWN) == 0
    assert _sent_to(env, ALICE) == 1


async def test_required_bot_check_without_keys_refuses_guest_start(h: Harness) -> None:
    h.state.config = dataclasses.replace(h.state.config, guest_require_bot_check=True)
    h.state.verifier = entry.NoopVerifier()
    resp = await _request_token(h, email="someone@culture-guest.net")
    assert resp.status == 400
    assert h.mail.sent == []


def _install(tmp_path, monkeypatch, *, guest: bool, keys: dict) -> None:
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    for name, value in keys.items():
        monkeypatch.setenv(name, value)
    entry.install(web.Application(), load_config(_lens_config(tmp_path, guest)))


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_startup_warns_once_without_keys(tmp_path, monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="irc_lens.web.entry")
    _install(tmp_path, monkeypatch, guest=True, keys={})
    assert _warnings(caplog).count(entry.NO_BOT_CHECK_WARNING) == 1


def test_startup_is_quiet_with_keys_or_guest_mode_off(
    tmp_path, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.WARNING, logger="irc_lens.web.entry")
    _install(tmp_path, monkeypatch, guest=True, keys=KEYS)
    _install(tmp_path, monkeypatch, guest=False, keys={})
    assert entry.NO_BOT_CHECK_WARNING not in _warnings(caplog)


def _lens_config(tmp_path, guest: bool):
    path = tmp_path / "lens.yaml"
    path.write_text(
        "auth:\n  mode: dev\n  dev:\n    nick: lens\n    email: dev@local\n"
        f"server:\n  name: spark\nguest_mode:\n  enabled: {str(guest).lower()}\n"
    )
    return path


def test_doctor_warns_only_for_guest_mode_without_keys(tmp_path) -> None:
    on = _lens_config(tmp_path, guest=True)
    warned = bot_check_doctor(on, env={})
    assert (warned.status, warned.remediation) == ("warn", entry.NO_BOT_CHECK_WARNING)
    assert bot_check_doctor(on, env=KEYS).status == "ok"
    assert bot_check_doctor(_lens_config(tmp_path, guest=False), env={}).status == "ok"
