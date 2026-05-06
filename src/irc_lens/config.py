"""YAML config loader and schema for irc-lens.

One file, two top-level sections (`auth` and `server`), optional `web`.
The loader returns a frozen :class:`LensConfig` dataclass; missing
required keys raise :class:`AfiError` with an `error:`/`hint:` shape
the dispatcher renders without leaking a traceback.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError

_AUTH_MODES = ("dev", "cloudflare-access")


@dataclass(frozen=True)
class LensConfig:
    auth_mode: str
    dev_nick: str | None
    dev_email: str | None
    cf_aud: str | None
    cf_team_domain: str | None
    allowed_emails: tuple[str, ...]
    allowed_service_tokens: tuple[str, ...]
    server_name: str
    server_host: str
    server_port: int
    web_bind: str
    web_port: int


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "irc-lens" / "config.yaml"


def _err(message: str, hint: str) -> AfiError:
    return AfiError(code=EXIT_USER_ERROR, message=message, remediation=hint)


def _coerce_port(value: object, where: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _err(
            f"{where} must be an integer, got {value!r}",
            f"set `{where}:` to a port number like 6667 or 8765",
        ) from None


def _require(d: dict, key: str, where: str) -> object:
    if key not in d:
        raise _err(
            f"missing required key {where}.{key}" if where else f"missing required key {key}",
            f"add `{key}:` under `{where}:` in the config file" if where else f"add `{key}:` to the config file",
        )
    return d[key]


def load_config(path: Path) -> LensConfig:
    if not path.exists():
        raise _err(
            f"no config at {path}",
            "run 'irc-lens config init' to drop a starter, or pass --config <path>",
        )
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise _err(
            f"could not parse YAML in {path}: {exc}",
            "fix the YAML syntax; see docs/cli.md for a working example",
        ) from exc
    if not isinstance(raw, dict):
        raise _err(
            f"config at {path} must be a YAML mapping at the top level",
            "wrap the file's contents in `auth:` and `server:` keys",
        )

    auth = _require(raw, "auth", "")
    if not isinstance(auth, dict):
        raise _err("auth: must be a mapping", "see docs/cli.md")
    mode = _require(auth, "mode", "auth")
    if mode not in _AUTH_MODES:
        raise _err(
            f"auth.mode must be one of {_AUTH_MODES}, got {mode!r}",
            "set `auth.mode:` to either `dev` or `cloudflare-access`",
        )

    dev_nick = dev_email = None
    cf_aud = cf_team_domain = None
    allowed_emails: tuple[str, ...] = ()
    allowed_service_tokens: tuple[str, ...] = ()

    if mode == "dev":
        dev = auth.get("dev")
        if dev is None:
            raise _err(
                "missing required keys auth.dev.nick and auth.dev.email",
                "add `dev:` section under `auth:` with `nick:` and `email:` fields",
            )
        if not isinstance(dev, dict):
            raise _err("auth.dev must be a mapping", "see docs/cli.md")
        dev_nick = str(_require(dev, "nick", "auth.dev"))
        dev_email = str(_require(dev, "email", "auth.dev"))
    else:
        cf = _require(auth, "cloudflare", "auth")
        if not isinstance(cf, dict):
            raise _err("auth.cloudflare must be a mapping", "see docs/cli.md")
        cf_aud = str(_require(cf, "aud", "auth.cloudflare"))
        cf_team_domain = str(_require(cf, "team_domain", "auth.cloudflare"))
        emails_raw = _require(auth, "allowed_emails", "auth")
        if not isinstance(emails_raw, list) or not all(isinstance(x, str) for x in emails_raw):
            raise _err(
                "auth.allowed_emails must be a list of strings",
                "set `auth.allowed_emails: [you@example.com]`",
            )
        if len(emails_raw) == 0 and not auth.get("allowed_service_tokens"):
            raise _err(
                "auth.allowed_emails is empty and no service tokens configured",
                "add at least one email to auth.allowed_emails or one entry to "
                "auth.allowed_service_tokens",
            )
        allowed_emails = tuple(emails_raw)
        ts_raw = auth.get("allowed_service_tokens", [])
        if not isinstance(ts_raw, list) or not all(isinstance(x, str) for x in ts_raw):
            raise _err(
                "auth.allowed_service_tokens must be a list of strings",
                "set `auth.allowed_service_tokens: []` or add client-ids",
            )
        allowed_service_tokens = tuple(ts_raw)

    server = _require(raw, "server", "")
    if not isinstance(server, dict):
        raise _err("server: must be a mapping", "see docs/cli.md")
    server_name = str(_require(server, "name", "server"))
    server_host = str(server.get("host", "127.0.0.1"))
    server_port = _coerce_port(server.get("port", 6667), "server.port")

    web = raw.get("web", {}) or {}
    if not isinstance(web, dict):
        raise _err("web: must be a mapping", "see docs/cli.md")
    web_bind = str(web.get("bind", "127.0.0.1"))
    web_port = _coerce_port(web.get("port", 8765), "web.port")

    return LensConfig(
        auth_mode=mode,
        dev_nick=dev_nick,
        dev_email=dev_email,
        cf_aud=cf_aud,
        cf_team_domain=cf_team_domain,
        allowed_emails=allowed_emails,
        allowed_service_tokens=allowed_service_tokens,
        server_name=server_name,
        server_host=server_host,
        server_port=server_port,
        web_bind=web_bind,
        web_port=web_port,
    )
