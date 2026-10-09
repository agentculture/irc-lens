"""Payload tests for scripts/cf_access_login_path.py (no network)."""

import importlib.util
import pathlib

import pytest

_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cf_access_login_path.py"
)
_spec = importlib.util.spec_from_file_location("cf_access_login_path", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

APP = {
    "id": "app-1",
    "name": "chat.culture.dev",
    "type": "self_hosted",
    "domain": "chat.culture.dev",
    "destinations": [{"type": "public", "uri": "chat.culture.dev"}],
    "session_duration": "24h",
    "http_only_cookie_attribute": True,
    "path_cookie_attribute": None,
    "policies": [
        {"id": "p-allow", "precedence": 1, "include": [{"email": {"email": "x"}}]},
        {"id": "p-svc", "precedence": 2},
    ],
}


def test_narrow_targets_login_path_only():
    body = mod.build_update(APP, "chat.culture.dev", "narrow")
    assert body["destinations"] == [{"type": "public", "uri": "chat.culture.dev/login"}]
    assert body["domain"] == "chat.culture.dev/login"


def test_restore_targets_whole_host():
    body = mod.build_update(APP, "chat.culture.dev", "restore")
    assert body["destinations"] == [{"type": "public", "uri": "chat.culture.dev"}]
    assert body["domain"] == "chat.culture.dev"


@pytest.mark.parametrize("mode", ["narrow", "restore"])
def test_cookie_stays_host_wide_and_lax(mode):
    body = mod.build_update(APP, "chat.culture.dev", mode)
    assert body["path_cookie_attribute"] is False
    assert body["same_site_cookie_attribute"] == "lax"


def test_settings_and_policies_carried_by_reference():
    body = mod.build_update(APP, "chat.culture.dev", "narrow")
    assert body["session_duration"] == "24h"
    assert body["http_only_cookie_attribute"] is True
    assert body["policies"] == [
        {"id": "p-allow", "precedence": 1},
        {"id": "p-svc", "precedence": 2},
    ]


def test_find_app_matches_host_or_host_path():
    narrowed = dict(APP, domain="chat.culture.dev/login")
    other = dict(APP, id="o", domain="chat.example.org")
    assert mod.find_app([other, narrowed], "chat.culture.dev")["id"] == "app-1"
    with pytest.raises(SystemExit):
        mod.find_app([other], "chat.culture.dev")


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        mod.target_destinations("chat.culture.dev", "wide")


def test_dry_run_without_credentials_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("CF_TOK", raising=False)
    monkeypatch.delenv("CF_ACC", raising=False)
    assert mod.main(["--host", "chat.culture.dev"]) == 2
