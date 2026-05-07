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

from irc_lens._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError

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


# Reused remediation hint — extracted so SonarCloud S1192 (duplicate-literal)
# stays quiet and so the wording stays consistent across every shape error.
_HINT_CONFIG_INIT = "run `irc-lens config init` to see a working example"


def _err(message: str, hint: str) -> AfiError:
    return AfiError(code=EXIT_USER_ERROR, message=message, remediation=hint)


def _err_env(message: str, hint: str) -> AfiError:
    return AfiError(code=EXIT_ENV_ERROR, message=message, remediation=hint)


def _coerce_port(value: object, where: str) -> int:
    """Coerce *value* to a valid TCP port integer (1..65535).

    Accepts: int (non-bool), float only if it is a whole number, str
    that parses as an integer.  Rejects bool, fractional floats, and
    out-of-range values.
    """
    _bad = _err(
        f"{where} must be an integer between 1 and 65535, got {value!r}",
        f"set `{where}:` to a port number like 6667 or 8765",
    )
    if isinstance(value, bool):
        raise _bad
    if isinstance(value, int):
        port = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise _bad
        port = int(value)
    elif isinstance(value, str):
        try:
            port = int(value.strip())
        except ValueError:
            raise _bad from None
    else:
        raise _bad
    if not (1 <= port <= 65535):
        raise _bad
    return port


def _require(d: dict, key: str, where: str) -> object:
    if key not in d:
        raise _err(
            f"missing required key {where}.{key}" if where else f"missing required key {key}",
            f"add `{key}:` under `{where}:` in the config file" if where else f"add `{key}:` to the config file",
        )
    return d[key]


def _require_str_list(value: object, where: str, hint: str) -> list[str]:
    """Assert *value* is a list of strings; raise with a single shared message.

    The two-step shape check (list, then per-element string) is needed for
    SonarCloud S5864 (type narrowing) but produced duplicated message+hint
    literals when each step had its own ``raise``. Centralizing here keeps
    the narrowing while collapsing the duplication.
    """
    msg = f"{where} must be a list of strings"
    if not isinstance(value, list):
        raise _err(msg, hint)
    if not all(isinstance(x, str) for x in value):
        raise _err(msg, hint)
    return value


# ---------------------------------------------------------------------------
# Section validators (extracted to keep load_config's cognitive complexity low)
# ---------------------------------------------------------------------------

def _load_dev_fields(auth: dict) -> tuple[str, str]:
    """Return (dev_nick, dev_email) from the auth section in dev mode."""
    dev = auth.get("dev")
    if dev is None:
        raise _err(
            "missing required keys auth.dev.nick and auth.dev.email",
            "add `dev:` section under `auth:` with `nick:` and `email:` fields",
        )
    if not isinstance(dev, dict):
        raise _err(
            "auth.dev must be a mapping",
            _HINT_CONFIG_INIT,
        )
    dev_nick = str(_require(dev, "nick", "auth.dev"))
    dev_email = str(_require(dev, "email", "auth.dev"))
    return dev_nick, dev_email


def _load_cf_fields(
    auth: dict,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    """Return (cf_aud, cf_team_domain, allowed_emails, allowed_service_tokens)."""
    cf = _require(auth, "cloudflare", "auth")
    if not isinstance(cf, dict):
        raise _err(
            "auth.cloudflare must be a mapping",
            _HINT_CONFIG_INIT,
        )
    cf_aud = str(_require(cf, "aud", "auth.cloudflare"))
    cf_team_domain = str(_require(cf, "team_domain", "auth.cloudflare"))

    emails_raw = _require_str_list(
        _require(auth, "allowed_emails", "auth"),
        "auth.allowed_emails",
        "set `auth.allowed_emails: [you@example.com]`",
    )
    if len(emails_raw) == 0 and not auth.get("allowed_service_tokens"):
        raise _err(
            "auth.allowed_emails is empty and no service tokens configured",
            "add at least one email to auth.allowed_emails or one entry to "
            "auth.allowed_service_tokens",
        )
    allowed_emails = tuple(emails_raw)

    ts_raw = _require_str_list(
        auth.get("allowed_service_tokens", []),
        "auth.allowed_service_tokens",
        "set `auth.allowed_service_tokens: []` or add client-ids",
    )
    allowed_service_tokens = tuple(ts_raw)

    return cf_aud, cf_team_domain, allowed_emails, allowed_service_tokens


def _validate_auth_section(raw: dict) -> dict:
    """Validate the top-level `auth:` key and return it as a dict."""
    auth = _require(raw, "auth", "")
    if not isinstance(auth, dict):
        raise _err(
            "auth: must be a mapping",
            _HINT_CONFIG_INIT,
        )
    mode = _require(auth, "mode", "auth")
    if mode not in _AUTH_MODES:
        raise _err(
            f"auth.mode must be one of {_AUTH_MODES}, got {mode!r}",
            "set `auth.mode:` to either `dev` or `cloudflare-access`",
        )
    return auth


def _validate_server_section(raw: dict) -> tuple[str, str, int]:
    """Return (server_name, server_host, server_port)."""
    server = _require(raw, "server", "")
    if not isinstance(server, dict):
        raise _err(
            "server: must be a mapping",
            _HINT_CONFIG_INIT,
        )
    server_name = str(_require(server, "name", "server"))
    server_host = str(server.get("host", "127.0.0.1"))
    server_port = _coerce_port(server.get("port", 6667), "server.port")
    return server_name, server_host, server_port


def _validate_web_section(raw: dict) -> tuple[str, int]:
    """Return (web_bind, web_port)."""
    web_raw = raw.get("web")
    if web_raw is None:
        web: dict = {}
    elif not isinstance(web_raw, dict):
        raise _err(
            "web: must be a mapping",
            _HINT_CONFIG_INIT,
        )
    else:
        web = web_raw
    web_bind = str(web.get("bind", "127.0.0.1"))
    web_port = _coerce_port(web.get("port", 8765), "web.port")
    return web_bind, web_port


# ---------------------------------------------------------------------------
# Public loader — flat orchestrator, no nested conditionals beyond auth.mode
# ---------------------------------------------------------------------------

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
            f"fix the YAML syntax; {_HINT_CONFIG_INIT}",
        ) from exc
    except OSError as exc:
        raise _err_env(
            f"could not read config at {path}: {exc}",
            "check file permissions or pick a different --config <path>",
        ) from exc
    if not isinstance(raw, dict):
        raise _err(
            f"config at {path} must be a YAML mapping at the top level",
            "wrap the file's contents in `auth:` and `server:` keys",
        )

    auth = _validate_auth_section(raw)
    mode = auth["mode"]

    dev_nick: str | None = None
    dev_email: str | None = None
    cf_aud: str | None = None
    cf_team_domain: str | None = None
    allowed_emails: tuple[str, ...] = ()
    allowed_service_tokens: tuple[str, ...] = ()

    if mode == "dev":
        dev_nick, dev_email = _load_dev_fields(auth)
    else:
        cf_aud, cf_team_domain, allowed_emails, allowed_service_tokens = _load_cf_fields(auth)

    server_name, server_host, server_port = _validate_server_section(raw)
    web_bind, web_port = _validate_web_section(raw)

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
