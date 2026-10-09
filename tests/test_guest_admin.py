"""Owner admin for guest mode (task t11): passwd, ban/unban, flags, deletion.

Covers acceptance criteria 1-4 and obligations o13 (deletion) / o15 (ban).
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from irc_lens import legal
from irc_lens.cli import main
from irc_lens.guest_store import GuestStore
from irc_lens.mail import RecordingAdapter
from irc_lens.web import bans, csrf, entry, make_app
from irc_lens.web.identity import Identity
from irc_lens.web.sessions import BACKEND_SANDBOX, GUEST_PRINCIPAL_PREFIX
from irc_lens.web.store import MediaStore

from test_entry import VERSIONS, _config  # noqa: F401  (fixtures below reuse it)

EMAIL = "gus@example.net"
NICK = "sbx-gus"
IP = "203.0.113.9"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def store(tmp_path: Path) -> GuestStore:
    s = GuestStore(tmp_path / "guests.db")
    s.record_guest(EMAIL, NICK, IP)
    return s


def run(*argv: str) -> int:
    return main(["guests", *argv])


# -- CLI: passwd / ban / unban / list / flags --------------------------------


def test_passwd_stdin_sets_argon2id_hash(store, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret pass\n"))
    assert (
        run("passwd", "Owner@Example.com", "--stdin", "--store", str(store.path)) == 0
    )
    assert store.check_password("owner@example.com", "s3cret pass")
    assert not store.check_password("owner@example.com", "other")
    with sqlite3.connect(store.path) as con:
        (h,) = con.execute("SELECT hash FROM passwords").fetchone()
    assert h.startswith("$argon2id$")
    err = capsys.readouterr().err
    assert "not in allowed_emails" in err or "could not read the config" in err


def test_passwd_tty_prompts_twice_and_rejects_mismatch(store, monkeypatch):
    answers = iter(["pw-one", "pw-two"])
    monkeypatch.setattr("getpass.getpass", lambda _p="": next(answers))
    assert run("passwd", "o@example.com", "--store", str(store.path)) != 0
    answers = iter(["pw-one", "pw-one"])
    assert run("passwd", "o@example.com", "--store", str(store.path)) == 0
    assert store.check_password("o@example.com", "pw-one")


def test_passwd_has_no_password_argument():
    assert run("passwd", "o@example.com", "hunter2") != 0


def test_ban_unban_email_and_ip_and_list(store, capsys):
    s = str(store.path)
    assert run("ban", EMAIL, "--reason", "spam", "--store", s) == 0
    assert run("ban", "198.51.100.1", "--store", s) == 0
    assert store.is_banned(EMAIL, None)
    assert store.is_banned(None, "198.51.100.1")
    capsys.readouterr()
    assert run("list", "--store", s) == 0
    out = capsys.readouterr().out
    assert f"{NICK}\t{EMAIL}\t{IP} [banned]" in out
    assert "198.51.100.1" in out
    assert "spam" in out
    assert run("unban", EMAIL, "--store", s) == 0
    assert run("unban", "198.51.100.1", "--store", s) == 0
    assert not store.is_banned(EMAIL, None)
    assert not store.is_banned(None, "198.51.100.1")


def test_store_unban_requires_target(store):
    with pytest.raises(ValueError):
        store.unban()
    store.ban(email=EMAIL)
    assert store.unban(email=EMAIL) == 1
    assert store.unban(email=EMAIL) == 0


def test_flags_lists_store_and_log_and_ban_by_flag(store, tmp_path, capsys):
    log = tmp_path / "flags.jsonl"
    log.write_text(
        json.dumps({"ts": 1, "reason": "nsfw", "nick": NICK, "excerpt": "x"})
        + "\nnot json\n"
        + json.dumps({"ts": 2, "reason": "nsfw", "nick": "sbx-ghost", "excerpt": "y"})
        + "\n"
    )
    store.record_flag("other@example.net", detail="store flag")
    s, fl = str(store.path), str(log)
    assert run("flags", "--store", s, "--flag-log", fl) == 0
    out = capsys.readouterr().out
    assert "s1\tstore\tother@example.net" in out
    assert f"j1\tflag-log\t{NICK}" in out
    assert "j3\tflag-log\tsbx-ghost" in out
    # ids are stable across runs
    assert run("flags", "--store", s, "--flag-log", fl) == 0
    assert capsys.readouterr().out == out

    assert run("ban", "--flag", "j1", "--store", s, "--flag-log", fl) == 0
    assert store.is_banned(EMAIL, None)
    assert run("ban", "--flag", "s1", "--store", s, "--flag-log", fl) == 0
    assert store.is_banned("other@example.net", None)
    # unknown nick / unknown id / target+flag are refused
    assert run("ban", "--flag", "j3", "--store", s, "--flag-log", fl) != 0
    assert run("ban", "--flag", "zz", "--store", s, "--flag-log", fl) != 0
    assert run("ban", EMAIL, "--flag", "j1", "--store", s, "--flag-log", fl) != 0


def test_flags_default_log_missing_is_fine(store, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    assert run("flags", "--store", str(store.path)) == 0


# -- web: ban enforcement and deletion ----------------------------------------


class FakeSandboxSession:
    healthy = True

    def __init__(self, nick: str) -> None:
        self.nick = nick
        self.connect = AsyncMock()
        self.wait_for_welcome = AsyncMock()
        self.disconnect = AsyncMock()
        self.join = AsyncMock()
        self.set_current_channel = MagicMock()
        self._transport = MagicMock()


@pytest_asyncio.fixture
async def legal_server():
    async def handler(_r):
        return web.json_response(VERSIONS)

    app = web.Application()
    app.router.add_get("/version.json", handler)
    server = TestServer(app)
    await server.start_server()
    legal.clear_cache()
    try:
        yield str(server.make_url("/version.json"))
    finally:
        legal.clear_cache()
        await server.close()


class Env:
    def __init__(self, client, store, app):
        self.client, self.store, self.app = client, store, app
        self.mail: RecordingAdapter = app[entry.ENTRY_STATE].mailer

    def cookie(self, email=EMAIL) -> dict[str, str]:
        val = csrf.make_cookie_value(email, self.app[csrf.SECRET_KEY], 3600)
        return {"Cookie": f"{csrf.GUEST_COOKIE_NAME}={val}", **SAME_ORIGIN}

    async def open_session(self, email=EMAIL):
        ident = Identity(
            principal=f"{GUEST_PRINCIPAL_PREFIX}{email}",
            nick=NICK,
            raw_jwt_subject="guest",
        )
        return await self.app["registry"].get_or_open(ident, BACKEND_SANDBOX)

    def token(self) -> str:
        return re.search(r"^ {4}(\S+)$", self.mail.sent[-1][2], flags=re.M).group(1)


async def _env(jwks, legal_url, tmp_path, *, interval=3600.0, media=False):
    cfg = _config(jwks, legal_url)
    if media:
        import dataclasses

        cfg = dataclasses.replace(
            cfg, media_enabled=True, media_dir=str(tmp_path / "media")
        )
    store = GuestStore(tmp_path / "guests.db")
    store.record_guest(EMAIL, NICK, IP)
    app = make_app(
        cfg,
        lambda _n: (_ for _ in ()).throw(AssertionError("no mesh")),
        sandbox_session_factory=FakeSandboxSession,
        ban_sweep_interval_s=interval,
    )
    app["guest_store"] = store
    state = app[entry.ENTRY_STATE]
    state.store = store
    state.mailer = RecordingAdapter()
    client = TestClient(TestServer(app))
    await client.start_server()
    return Env(client, store, app)


@pytest_asyncio.fixture
async def env(jwks, legal_server, tmp_path):
    e = await _env(jwks, legal_server, tmp_path, interval=0.05)
    try:
        yield e
    finally:
        await e.client.close()


async def _wait_for(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_banned_email_session_dropped_by_sweeper(env):
    session = await env.open_session()
    reg = env.app["registry"]
    assert reg.has(f"{GUEST_PRINCIPAL_PREFIX}{EMAIL}", BACKEND_SANDBOX)
    # The CLI is another process: it just writes to the same store file.
    assert run("ban", EMAIL, "--store", str(env.store.path)) == 0
    assert await _wait_for(
        lambda: not reg.has(f"{GUEST_PRINCIPAL_PREFIX}{EMAIL}", BACKEND_SANDBOX)
    )
    session.disconnect.assert_awaited()


async def test_banned_ip_session_dropped_and_unrelated_kept(env):
    other = "kim@example.net"
    env.store.record_guest(other, "sbx-kim", "198.51.100.5")
    await env.open_session()
    await env.open_session(other)
    run("ban", IP, "--store", str(env.store.path))
    reg = env.app["registry"]
    assert await _wait_for(lambda: not reg.has(f"guest:{EMAIL}", BACKEND_SANDBOX))
    assert reg.has(f"guest:{other}", BACKEND_SANDBOX)


async def test_sweeper_default_interval_is_under_a_minute():
    assert bans.DEFAULT_SWEEP_INTERVAL_S <= 30


async def test_ban_by_flag_drops_session(env, tmp_path):
    log = tmp_path / "flags.jsonl"
    log.write_text(
        json.dumps({"ts": 1, "reason": "nsfw", "nick": NICK, "excerpt": "e"}) + "\n"
    )
    await env.open_session()
    assert (
        run(
            "ban",
            "--flag",
            "j1",
            "--store",
            str(env.store.path),
            "--flag-log",
            str(log),
        )
        == 0
    )
    reg = env.app["registry"]
    assert await _wait_for(lambda: not reg.has(f"guest:{EMAIL}", BACKEND_SANDBOX))


async def test_banned_email_and_ip_refused_at_entry_and_guest_tier(env):
    env.store.ban(email=EMAIL)
    # verify refused (valid token cannot be redeemed)
    tok = env.store.issue_token(EMAIL, purpose="guest")
    resp = await env.client.post(
        "/entry/verify",
        data={"email": EMAIL, "nickname": "gus", "code": tok},
        allow_redirects=False,
    )
    assert resp.status == 401
    # token request sends no mail
    resp = await env.client.post(
        "/entry/guest/start",
        data={"email": EMAIL, "nickname": "gus", "consent": "on"},
    )
    assert resp.status == 200
    assert env.mail.sent == []
    # an existing cookie no longer yields the guest tier
    resp = await env.client.get("/residents", headers=env.cookie())
    assert resp.status == 401
    # IP ban
    env.store.unban(email=EMAIL)
    env.store.ban(ip="192.0.2.77")
    resp = await env.client.post(
        "/entry/guest/start",
        data={"email": "new@example.net", "nickname": "newbie", "consent": "on"},
        headers={"CF-Connecting-IP": "192.0.2.77"},
    )
    assert resp.status == 200
    assert env.mail.sent == []


# -- deletion ------------------------------------------------------------------


def _all_text(path: Path) -> str:
    with sqlite3.connect(path) as con:
        dump = "\n".join(con.iterdump())
    return dump


async def test_deletion_flow_removes_everything_but_the_record(env):
    s = env.store
    s.record_consent(EMAIL, IP, tos_version="t", privacy_version="p")
    s.record_input(EMAIL, kind="message", payload=f"hello from {NICK}")
    s.record_flag(EMAIL, detail="nsfw")
    await env.open_session()

    resp = await env.client.get("/delete")
    assert resp.status == 200
    page_a = await env.client.post("/delete/request", data={"email": EMAIL})
    body_a = (await page_a.text()).replace(EMAIL, "<E>")
    page_b = await env.client.post(
        "/delete/request", data={"email": "nobody@example.org"}
    )
    body_b = (await page_b.text()).replace("nobody@example.org", "<E>")
    assert page_a.status == page_b.status == 200
    assert body_a == body_b
    assert len(env.mail.sent) == 1  # only the real guest got a (fresh) token
    code = env.token()

    # wrong code deletes nothing
    bad = await env.client.post(
        "/delete/confirm", data={"email": EMAIL, "code": "nope"}
    )
    assert bad.status == 401
    assert s.get_guest(EMAIL)
    # an entry-purpose token cannot delete
    entry_tok = s.issue_token(EMAIL, purpose="guest")
    bad = await env.client.post(
        "/delete/confirm", data={"email": EMAIL, "code": entry_tok}
    )
    assert bad.status == 401
    assert s.get_guest(EMAIL)

    ok = await env.client.post(
        "/delete/confirm", data={"email": EMAIL, "code": code}, headers=env.cookie()
    )
    assert ok.status == 200
    assert "lens_guest" in ok.headers.get("Set-Cookie", "")
    assert not env.app["registry"].has(f"guest:{EMAIL}", BACKEND_SANDBOX)
    dump = _all_text(s.path)
    assert NICK not in dump
    assert dump.count(EMAIL) == 1  # only the deletion record
    assert s.get_guest(EMAIL) == []
    assert s.get_consents(EMAIL) == []
    assert s.list_flags(EMAIL) == []
    with sqlite3.connect(s.path) as con:
        assert con.execute("SELECT email FROM deletions").fetchall() == [(EMAIL,)]
    # single use
    again = await env.client.post(
        "/delete/confirm", data={"email": EMAIL, "code": code}
    )
    assert again.status == 401


async def test_deletion_keeps_ban(env):
    env.store.ban(email=EMAIL)
    tok = env.store.issue_token(EMAIL, purpose="delete")
    r = await env.client.post("/delete/confirm", data={"email": EMAIL, "code": tok})
    assert r.status == 200
    assert env.store.is_banned(EMAIL, None)


async def test_deletion_removes_guest_uploads(jwks, legal_server, tmp_path):
    e = await _env(jwks, legal_server, tmp_path, media=True)
    try:
        media: MediaStore = e.app["media_store"]

        async def chunks():
            yield b"\x89PNG\r\n\x1a\n" + b"0" * 64

        mine = await media.save(EMAIL, "a.png", chunks())
        theirs = await media.save("kim@example.net", "b.png", chunks())
        tok = e.store.issue_token(EMAIL, purpose="delete")
        r = await e.client.post("/delete/confirm", data={"email": EMAIL, "code": tok})
        assert r.status == 200
        assert media.resolve(f"{mine.token}.{mine.ext}") is None
        assert media.resolve(f"{theirs.token}.{theirs.ext}") is not None
    finally:
        await e.client.close()


async def test_delete_routes_404_when_guest_mode_off(jwks, legal_server, tmp_path):
    cfg = _config(jwks, legal_server, guest=False)
    app = make_app(cfg, lambda _n: None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/delete")).status in (401, 403, 404)
        assert (await client.post("/delete/request", data={"email": EMAIL})).status in (
            401,
            403,
            404,
        )
        assert not any(
            r.resource.canonical.startswith("/delete") for r in app.router.routes()
        )
    finally:
        await client.close()


async def test_deletion_purges_flag_log_and_keeps_anonymized_corpus(env, tmp_path):
    """d7/d8: deletion also erases the guest's sbx-ask flag lines; their Q&A
    survives only as anonymized corpus rows. The sandbox IRCd keeps no history
    on disk (culture server --no-persist), so there is nothing there to purge."""
    import dataclasses

    flags = tmp_path / "flags.jsonl"
    flags.write_text(
        json.dumps({"ts": 1, "reason": "nsfw", "nick": NICK, "excerpt": "x"})
        + "\n"
        + json.dumps({"ts": 2, "reason": "nsfw", "nick": "sbx-kim", "excerpt": "y"})
        + "\n"
    )
    env.app[entry.ENTRY_STATE].config = dataclasses.replace(
        env.app["config"], guest_sandbox_flag_log=str(flags)
    )
    env.store.room_id(EMAIL)
    env.store.record_input(EMAIL, kind="message", payload="what is culture?")
    env.store.record_input(EMAIL, kind="answer", payload="an IRC mesh")
    tok = env.store.issue_token(EMAIL, purpose="delete")
    r = await env.client.post("/delete/confirm", data={"email": EMAIL, "code": tok})
    assert r.status == 200
    assert NICK not in flags.read_text()
    assert "sbx-kim" in flags.read_text()
    assert env.store.list_rooms() == []
    assert [(c["question"], c["answer"]) for c in env.store.list_corpus()] == [
        ("what is culture?", "an IRC mesh")
    ]
