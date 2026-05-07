# Remote-connect via Cloudflare Access — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `irc-lens serve` deployable behind Cloudflare Tunnel + Cloudflare Access, with per-user Sessions keyed by validated identity, while keeping local dev frictionless.

**Architecture:** A single `~/.config/irc-lens/config.yaml` selects `auth.mode: dev | cloudflare-access`. Dev mode preserves today's single-Session behavior with a synthetic identity. CF mode validates Cloudflare Access JWTs, derives per-user nicks (`<server>-<sanitized-local-part>`), and lazily opens one IRC `Session` per principal. New `web/auth.py`, `web/identity.py`, `web/sessions.py` modules; `make_app(session)` becomes `make_app(config)`.

**Tech Stack:** Python 3.11+, aiohttp, PyJWT (new), cryptography (new for JWT verify), PyYAML (already present), pytest / pytest-asyncio / pytest-aiohttp, Playwright (existing e2e), Cloudflare Tunnel + Access (operational), `cloudflared` (operational).

**Spec reference:** `docs/superpowers/specs/2026-05-06-remote-connect-cloudflare-access-design.md`.

**Follow-up issues:** [#27 (CSRF tokens)](https://github.com/agentculture/irc-lens/issues/27), [#28 (CF round-trip GHA automation)](https://github.com/agentculture/irc-lens/issues/28).

---

## File Structure

### New files

| Path | Responsibility |
| --- | --- |
| `src/irc_lens/config.py` | YAML config loader, schema validation, defaults, `LensConfig` dataclass. |
| `src/irc_lens/cli/_commands/config_cmd.py` | `irc-lens config init` and `irc-lens config overview` verbs. |
| `src/irc_lens/web/identity.py` | `Identity` NamedTuple, `derive_nick()` pure function. |
| `src/irc_lens/web/auth.py` | CF Access JWT verification, JWKS fetch + cache, middleware factory. |
| `src/irc_lens/web/sessions.py` | Per-principal Session registry, `get_or_open_session()`, registry shutdown. |
| `src/irc_lens/web/templates/error.html.j2` | Tiny error page for 503s on lazy-open. |
| `tests/test_config_loader.py` | Schema validation + defaults + CLI override precedence. |
| `tests/test_identity.py` | `derive_nick()` cases — sanitization, prefix, empty-after-strip. |
| `tests/test_auth_middleware.py` | JWT verify + middleware happy/sad paths against a fake JWKS server. |
| `tests/test_session_registry.py` | Lazy open, double-check lock, shutdown-disconnects-all. |
| `tests/test_serve_cf_mode.py` | `--nick` rejected, `--bind` coercion notice, JWKS fail-fast on startup. |
| `tests/test_origin_check.py` | `POST /input` Origin/Referer floor. |
| `tests/test_healthz.py` | `/healthz` opaque response, no auth, no IRC state. |
| `tests/test_cf_roundtrip.py` | Real-CF round-trip (skipped unless env set, `@pytest.mark.cloudflare`). |
| `tests/_jwks_server.py` | In-tree fake JWKS aiohttp server fixture (mirrors `_agentirc_server.py` pattern). |
| `scripts/cf-roundtrip/setup.sh` | Idempotent CF tunnel/DNS/Access/service-token bootstrap. |
| `scripts/cf-roundtrip/teardown.sh` | Tear down everything `setup.sh` created. |
| `docs/deployment-cloudflare-access.md` | Domain-neutral runbook. |
| `docs/auth.md` | Auth protocol reference. |
| `docs/security-checklist.md` | Pre-launch yes/no list. |

### Modified files

| Path | Reason |
| --- | --- |
| `pyproject.toml` | Add `pyjwt[crypto]` and bump version. New `cloudflare` pytest marker. |
| `src/irc_lens/web/__init__.py` | Export new symbols if needed. |
| `src/irc_lens/web/app.py` | `make_app(config)` instead of `make_app(session)`. Mounts middleware, registers /healthz. |
| `src/irc_lens/web/routes.py` | Use `get_or_open_session`, add `/healthz`, add Origin/Referer check. |
| `src/irc_lens/cli/_commands/serve.py` | Load config, branch on `auth.mode`, gate `--nick`, coerce `--bind`. |
| `src/irc_lens/cli/__init__.py` | Register the `config` noun group. |
| `tests/conftest.py` | New `dev_config` and config-based `lens_client` fixtures; preserve old fixtures for backward-compat where simple. |
| `docs/architecture.md` | Add deployment-modes subsection. |
| `docs/cli.md` | Document `--config`, `irc-lens config init`, `--nick`-rejected-in-CF rule. |
| `README.md` | Pointer to the runbook. |

### Phasing (commit boundaries)

Each phase below maps to one PR. Tasks are ordered for TDD; commit between tasks where called out.

| Phase | Scope | Approx tasks |
| --- | --- | --- |
| 1 | Config loader + `irc-lens config init` (no behavior change yet) | T1.1–T1.6 |
| 2 | Per-user Session registry under dev mode | T2.1–T2.6 |
| 3 | CF auth middleware + JWT verification | T3.1–T3.7 |
| 4 | CLI/HTTP cleanups (`--nick`/`--bind`/`/healthz`/Origin) | T4.1–T4.5 |
| 5 | Docs + round-trip scripts + CF round-trip test | T5.1–T5.6 |

---

## Phase 1 — Config foundation

### Task 1.1: Add PyJWT to dependencies, register `cloudflare` pytest marker

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit `pyproject.toml` to add `pyjwt[crypto]` and the new marker**

In the `[project] dependencies` block, add `pyjwt[crypto]>=2.8` after `pyyaml>=6.0`:

```toml
dependencies = [
    "aiohttp>=3.9",
    "jinja2>=3.1",
    "pyyaml>=6.0",
    "pyjwt[crypto]>=2.8",
]
```

In the `[tool.pytest.ini_options] markers` block, append the cloudflare marker:

```toml
markers = [
    "playwright: opt-in browser end-to-end tests (requires `playwright install chromium`).",
    "cloudflare: opt-in real-Cloudflare round-trip tests (requires CF_API_TOKEN and friends).",
]
```

Update `addopts` to also exclude `cloudflare` by default:

```toml
addopts = "-m 'not playwright and not cloudflare'"
```

- [ ] **Step 2: Sync the lockfile**

Run: `uv sync --extra dev`
Expected: `uv.lock` updates; `import jwt` works in the venv.

- [ ] **Step 3: Verify import**

Run: `uv run python -c "import jwt; from jwt import PyJWKClient; print(jwt.__version__)"`
Expected: prints a version `>= 2.8`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add pyjwt[crypto] dependency, cloudflare pytest marker"
```

---

### Task 1.2: Write failing tests for `LensConfig` schema validation

**Files:**
- Create: `tests/test_config_loader.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Schema validation, defaults, and override semantics for LensConfig."""
from __future__ import annotations

from pathlib import Path

import pytest

from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import LensConfig, load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_dev_mode_minimal_loads(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, """
auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local
server:
  name: spark
"""))
    assert cfg.auth_mode == "dev"
    assert cfg.dev_nick == "lens"
    assert cfg.dev_email == "dev@local"
    assert cfg.server_name == "spark"
    assert cfg.server_host == "127.0.0.1"   # default
    assert cfg.server_port == 6667          # default
    assert cfg.web_bind == "127.0.0.1"      # default
    assert cfg.web_port == 8765             # default


def test_cf_mode_minimal_loads(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, """
auth:
  mode: cloudflare-access
  cloudflare:
    aud: aud-tag
    team_domain: team.cloudflareaccess.com
  allowed_emails:
    - alice@example.com
server:
  name: spark
"""))
    assert cfg.auth_mode == "cloudflare-access"
    assert cfg.cf_aud == "aud-tag"
    assert cfg.cf_team_domain == "team.cloudflareaccess.com"
    assert cfg.allowed_emails == ("alice@example.com",)
    assert cfg.allowed_service_tokens == ()


def test_cf_mode_missing_aud_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: cloudflare-access
  cloudflare:
    team_domain: team.cloudflareaccess.com
  allowed_emails:
    - alice@example.com
server:
  name: spark
"""))
    assert exc.value.code == EXIT_USER_ERROR
    assert "auth.cloudflare.aud" in exc.value.message


def test_dev_mode_missing_dev_section_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: dev
server:
  name: spark
"""))
    assert "auth.dev.nick" in exc.value.message


def test_unknown_mode_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: oauth-passport
server:
  name: spark
"""))
    assert "auth.mode" in exc.value.message
    assert "dev" in exc.value.remediation
    assert "cloudflare-access" in exc.value.remediation


def test_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(tmp_path / "nope.yaml")
    assert exc.value.code == EXIT_USER_ERROR
    assert "config init" in exc.value.remediation


def test_malformed_yaml_errors(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, "::: not: yaml :::"))
    assert "YAML" in exc.value.message or "parse" in exc.value.message


def test_empty_allowed_emails_errors_in_cf_mode(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as exc:
        load_config(_write(tmp_path, """
auth:
  mode: cloudflare-access
  cloudflare:
    aud: aud-tag
    team_domain: team.cloudflareaccess.com
  allowed_emails: []
server:
  name: spark
"""))
    assert "allowed_emails" in exc.value.message
    assert "empty" in exc.value.message.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: FAIL with `ImportError: cannot import name 'LensConfig' from 'irc_lens.config'`.

- [ ] **Step 3: Commit failing test**

```bash
git add tests/test_config_loader.py
git commit -m "test: failing tests for LensConfig schema validation"
```

---

### Task 1.3: Implement `LensConfig` and `load_config`

**Files:**
- Create: `src/irc_lens/config.py`

- [ ] **Step 1: Write the loader**

```python
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
    # dev fields (set when auth_mode == 'dev'):
    dev_nick: str | None
    dev_email: str | None
    # cloudflare-access fields (set when auth_mode == 'cloudflare-access'):
    cf_aud: str | None
    cf_team_domain: str | None
    allowed_emails: tuple[str, ...]
    allowed_service_tokens: tuple[str, ...]
    # server (always required):
    server_name: str
    server_host: str
    server_port: int
    # web (defaults applied):
    web_bind: str
    web_port: int


def default_config_path() -> Path:
    """Resolve the default config file location.

    Honors ``$XDG_CONFIG_HOME`` per the freedesktop spec; falls back to
    ``~/.config`` so a stock home directory works without setup.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "irc-lens" / "config.yaml"


def _err(message: str, hint: str) -> AfiError:
    return AfiError(code=EXIT_USER_ERROR, message=message, remediation=hint)


def _require(d: dict, key: str, where: str) -> object:
    if key not in d:
        raise _err(
            f"missing required key {where}.{key}",
            f"add `{key}:` under `{where}:` in the config file",
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
        dev = _require(auth, "dev", "auth")
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
    server_port = int(server.get("port", 6667))

    web = raw.get("web", {}) or {}
    if not isinstance(web, dict):
        raise _err("web: must be a mapping", "see docs/cli.md")
    web_bind = str(web.get("bind", "127.0.0.1"))
    web_port = int(web.get("port", 8765))

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
```

- [ ] **Step 2: Run tests, expect green**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add src/irc_lens/config.py
git commit -m "feat(config): YAML loader with schema validation"
```

---

### Task 1.4: Write failing tests for `irc-lens config init`

**Files:**
- Create: `tests/test_config_init.py`

- [ ] **Step 1: Write failing tests**

```python
"""`irc-lens config init` writes a starter dev-mode config."""
from __future__ import annotations

from pathlib import Path

from irc_lens.cli import main


def test_config_init_writes_default_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rc = main(["config", "init"])
    assert rc == 0
    target = tmp_path / "irc-lens" / "config.yaml"
    assert target.exists()
    body = target.read_text()
    assert "auth:" in body
    assert "mode: dev" in body
    assert "server:" in body


def test_config_init_with_explicit_path(tmp_path: Path) -> None:
    target = tmp_path / "custom.yaml"
    rc = main(["config", "init", "--path", str(target)])
    assert rc == 0
    assert target.exists()


def test_config_init_refuses_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "exists.yaml"
    target.write_text("# pre-existing\n")
    rc = main(["config", "init", "--path", str(target)])
    assert rc == 1                                 # AfiError EXIT_USER_ERROR
    assert target.read_text() == "# pre-existing\n"   # unchanged


def test_config_init_force_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "exists.yaml"
    target.write_text("# stale\n")
    rc = main(["config", "init", "--path", str(target), "--force"])
    assert rc == 0
    assert "auth:" in target.read_text()


def test_config_overview_works(tmp_path: Path) -> None:
    rc = main(["config", "overview"])
    assert rc == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_init.py -v`
Expected: FAIL — `argparse: invalid choice: 'config'` or similar.

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_config_init.py
git commit -m "test: failing tests for irc-lens config init"
```

---

### Task 1.5: Implement `irc-lens config init` and `irc-lens config overview`

**Files:**
- Create: `src/irc_lens/cli/_commands/config_cmd.py`
- Modify: `src/irc_lens/cli/__init__.py`

- [ ] **Step 1: Write the command module**

```python
"""`irc-lens config` noun group: init + overview verbs."""
from __future__ import annotations

import argparse
from pathlib import Path

from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.cli._output import emit_diagnostic
from irc_lens.config import default_config_path

_STARTER = """\
# irc-lens — local dev config
# Written by `irc-lens config init`. See docs/cli.md for the full schema.

auth:
  mode: dev
  dev:
    nick: lens
    email: dev@local

server:
  name: spark        # AgentIRC server name (used to derive nicks in CF mode)
  host: 127.0.0.1
  port: 6667

web:
  bind: 127.0.0.1
  port: 8765
"""


def _resolve_target(args: argparse.Namespace) -> Path:
    return Path(args.path) if args.path else default_config_path()


def cmd_config_init(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    if target.exists() and not args.force:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"config already exists at {target}",
            remediation="pass --force to overwrite, or pick a different --path",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_STARTER)
    emit_diagnostic(f"wrote starter config to {target}")
    return 0


def cmd_config_overview(_args: argparse.Namespace) -> int:
    print(
        "irc-lens config — manage the lens config file.\n"
        "\n"
        "verbs:\n"
        "  init       write a starter dev-mode config\n"
        "  overview   this help\n"
        "\n"
        "default path: ~/.config/irc-lens/config.yaml (XDG_CONFIG_HOME respected)\n"
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    cfg = sub.add_parser(
        "config",
        help="Manage the irc-lens config file.",
    )
    cfg_sub = cfg.add_subparsers(dest="config_command")

    init = cfg_sub.add_parser("init", help="Write a starter dev-mode config.")
    init.add_argument("--path", default=None, help="Override the default config path.")
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing file (default refuses).",
    )
    init.set_defaults(func=cmd_config_init)

    overview = cfg_sub.add_parser("overview", help="Help for the config noun.")
    overview.set_defaults(func=cmd_config_overview)

    cfg.set_defaults(func=lambda _args: (cfg.print_help() or 0))
```

- [ ] **Step 2: Wire the noun in `cli/__init__.py`**

In `src/irc_lens/cli/__init__.py`, after the existing `cli_noun` setup (line ~92), import and register:

At the top, add to imports:
```python
from irc_lens.cli._commands import config_cmd as _config_cmd
```

Inside `_build_parser()`, after the `cli_noun.set_defaults(...)` line, add:
```python
_config_cmd.register(sub)
```

- [ ] **Step 3: Run tests, expect green**

Run: `uv run pytest tests/test_config_init.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 4: Smoke-test the CLI**

Run: `uv run irc-lens config init --path /tmp/lens-smoke.yaml && cat /tmp/lens-smoke.yaml | head -5`
Expected: `wrote starter config to /tmp/lens-smoke.yaml`, then the first comment line of the starter.

- [ ] **Step 5: Commit**

```bash
git add src/irc_lens/cli/_commands/config_cmd.py src/irc_lens/cli/__init__.py
git commit -m "feat(cli): irc-lens config init + overview verbs"
```

---

### Task 1.6: `cli verify` `overview` rubric — confirm new noun passes

**Files:**
- (no edits — verification step)

- [ ] **Step 1: Confirm `irc-lens overview` lists the new noun**

Run: `uv run irc-lens overview`
Expected: output mentions `config` alongside `cli`.

If the existing `overview` walks subparsers automatically, this is free. If not, the existing overview implementation needs the `config` noun added; check `src/irc_lens/cli/_commands/overview.py` and update similarly to how `cli` was registered.

- [ ] **Step 2: Confirm `irc-lens overview --json` is parseable**

Run: `uv run irc-lens overview --json | python -m json.tool > /dev/null`
Expected: exit 0, no parse error.

- [ ] **Step 3: Confirm `irc-lens config overview` works**

Run: `uv run irc-lens config overview`
Expected: exit 0, prints the config-noun help.

- [ ] **Step 4: If any rubric item failed, fix the gap and commit**

If `overview.py` needed an update, commit it with `chore(cli): include config noun in overview rubric`.

---

## Phase 2 — Per-user Session registry under dev mode

### Task 2.1: Write failing tests for `Identity` and `derive_nick`

**Files:**
- Create: `tests/test_identity.py`

- [ ] **Step 1: Write failing tests**

```python
"""Identity NamedTuple + derive_nick rules."""
from __future__ import annotations

import pytest

from irc_lens.web.identity import Identity, derive_nick


@pytest.mark.parametrize("server,principal,expected", [
    ("spark", "ori.nachum@gmail.com", "spark-orinachum"),
    ("spark", "Ori.Nachum@Gmail.com", "spark-orinachum"),     # case-fold
    ("spark", "alice+irc@example.com", "spark-aliceirc"),     # strip +
    ("spark", "bob_smith@example.com", "spark-bobsmith"),     # strip _
    ("spark", "kebab-case@example.com", "spark-kebab-case"),  # keep -
    ("spark", "svc-token-id", "spark-svc-token-id"),          # no @ ok (service token CN)
])
def test_derive_nick_cases(server: str, principal: str, expected: str) -> None:
    assert derive_nick(server, principal) == expected


def test_derive_nick_empty_after_strip_raises() -> None:
    with pytest.raises(ValueError):
        derive_nick("spark", "...@example.com")


def test_identity_is_namedtuple_like() -> None:
    i = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="sub-123")
    assert i.principal == "alice@example.com"
    assert i.nick == "spark-alice"
    assert i.raw_jwt_subject == "sub-123"
```

- [ ] **Step 2: Run, expect fail**

Run: `uv run pytest tests/test_identity.py -v`
Expected: ImportError on `irc_lens.web.identity`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_identity.py
git commit -m "test: failing tests for derive_nick and Identity"
```

---

### Task 2.2: Implement `Identity` and `derive_nick`

**Files:**
- Create: `src/irc_lens/web/identity.py`

- [ ] **Step 1: Write the module**

```python
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
    whole string if no ``@``), then drop everything outside ``[a-z0-9-]``.
    AgentIRC accepts ``-`` in nicks but rejects ``.``, ``_``, ``+``, etc.

    Raises:
        ValueError: when the sanitized local part is empty.
    """
    local = principal.split("@", 1)[0].lower()
    sanitized = "".join(c for c in local if c.isalnum() or c == "-")
    if not sanitized:
        raise ValueError(
            f"nick derivation produced empty result for principal={principal!r}"
        )
    return f"{server_name}-{sanitized}"
```

- [ ] **Step 2: Run tests, expect green**

Run: `uv run pytest tests/test_identity.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add src/irc_lens/web/identity.py
git commit -m "feat(web): Identity NamedTuple + derive_nick"
```

---

### Task 2.3: Write failing tests for the per-principal Session registry

**Files:**
- Create: `tests/test_session_registry.py`

- [ ] **Step 1: Write failing tests**

```python
"""Per-principal Session registry: lazy open, double-check, shutdown-all."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from irc_lens.web.identity import Identity
from irc_lens.web.sessions import (
    SessionRegistry,
    disconnect_all,
)


def _fake_session_factory() -> tuple[Callable[[str], MagicMock], list[MagicMock]]:
    created: list[MagicMock] = []

    def factory(nick: str) -> MagicMock:
        s = MagicMock()
        s.connect = AsyncMock()
        s.wait_for_welcome = AsyncMock()
        s.disconnect = AsyncMock()
        s.nick = nick
        created.append(s)
        return s

    return factory, created


@pytest.mark.asyncio
async def test_first_request_opens_session() -> None:
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    s = await reg.get_or_open(ident)

    assert s is created[0]
    s.connect.assert_awaited_once()
    s.wait_for_welcome.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_request_reuses_session() -> None:
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    a = await reg.get_or_open(ident)
    b = await reg.get_or_open(ident)

    assert a is b
    assert len(created) == 1


@pytest.mark.asyncio
async def test_concurrent_first_requests_share_one_session() -> None:
    """Two coroutines reaching get_or_open at once must not both build a Session."""
    factory, created = _fake_session_factory()

    # Make connect() yield to the loop so the race window is real.
    async def slow_connect() -> None:
        await asyncio.sleep(0.01)

    reg = SessionRegistry(factory=factory)
    # Patch the factory to return a session whose connect awaits.
    base_factory = factory

    def slow_factory(nick: str) -> MagicMock:
        s = base_factory(nick)
        s.connect = AsyncMock(side_effect=slow_connect)
        return s

    reg = SessionRegistry(factory=slow_factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    a, b = await asyncio.gather(reg.get_or_open(ident), reg.get_or_open(ident))

    assert a is b
    # Two created mocks would mean the lock failed.
    assert len(created) == 1


@pytest.mark.asyncio
async def test_failed_open_does_not_register() -> None:
    factory, created = _fake_session_factory()

    class Boom(Exception):
        pass

    def bad_factory(nick: str) -> MagicMock:
        s = factory(nick)
        s.connect = AsyncMock(side_effect=Boom("nope"))
        return s

    reg = SessionRegistry(factory=bad_factory)
    ident = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")

    with pytest.raises(Boom):
        await reg.get_or_open(ident)

    # Second attempt must build a fresh Session, not reuse a half-open one.
    with pytest.raises(Boom):
        await reg.get_or_open(ident)
    assert len(created) == 2


@pytest.mark.asyncio
async def test_disconnect_all_calls_each_session() -> None:
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    a = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")
    b = Identity(principal="bob@example.com", nick="spark-bob", raw_jwt_subject="s")
    await reg.get_or_open(a)
    await reg.get_or_open(b)

    await disconnect_all(reg)

    for s in created:
        s.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_all_swallows_individual_failures() -> None:
    factory, created = _fake_session_factory()
    reg = SessionRegistry(factory=factory)
    a = Identity(principal="alice@example.com", nick="spark-alice", raw_jwt_subject="s")
    b = Identity(principal="bob@example.com", nick="spark-bob", raw_jwt_subject="s")
    await reg.get_or_open(a)
    await reg.get_or_open(b)
    created[0].disconnect = AsyncMock(side_effect=RuntimeError("first fails"))

    # Must not raise — both should be attempted.
    await disconnect_all(reg)
    created[1].disconnect.assert_awaited_once()
```

- [ ] **Step 2: Run, expect fail**

Run: `uv run pytest tests/test_session_registry.py -v`
Expected: ImportError on `irc_lens.web.sessions`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_session_registry.py
git commit -m "test: failing tests for per-principal Session registry"
```

---

### Task 2.4: Implement `SessionRegistry` and `disconnect_all`

**Files:**
- Create: `src/irc_lens/web/sessions.py`

- [ ] **Step 1: Write the module**

```python
"""Per-principal Session registry.

A registry hands out one :class:`Session` per authenticated principal,
opening it lazily on first request. Concurrent first-requests for the
same principal share one Session via a per-key lock + double-check.

Failed opens are *not* registered, so a transient AgentIRC outage
doesn't poison the cache for that principal.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from irc_lens.web.identity import Identity

if TYPE_CHECKING:
    from irc_lens.session import Session

SessionFactory = Callable[[str], "Session"]


class SessionRegistry:
    """Maps principal → Session, lazy-opening as needed."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory
        self._sessions: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def __contains__(self, principal: str) -> bool:
        return principal in self._sessions

    def values(self) -> list[Any]:
        return list(self._sessions.values())

    async def get_or_open(self, identity: Identity) -> Any:
        if identity.principal in self._sessions:
            return self._sessions[identity.principal]
        async with self._locks[identity.principal]:
            if identity.principal in self._sessions:           # double-check
                return self._sessions[identity.principal]
            session = self._factory(identity.nick)
            await session.connect()
            await session.wait_for_welcome()
            self._sessions[identity.principal] = session
            return session


async def disconnect_all(registry: SessionRegistry) -> None:
    """Disconnect every registered Session, swallowing individual failures."""
    sessions = registry.values()
    if not sessions:
        return
    await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
```

- [ ] **Step 2: Run tests, expect green**

Run: `uv run pytest tests/test_session_registry.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add src/irc_lens/web/sessions.py
git commit -m "feat(web): per-principal SessionRegistry with double-check lazy open"
```

---

### Task 2.5: Refactor `make_app` to take `LensConfig`, install dev-mode middleware, wire registry

**Files:**
- Modify: `src/irc_lens/web/app.py`
- Modify: `src/irc_lens/web/routes.py`
- Modify: `tests/conftest.py`

This is the largest single edit. We change `make_app(session)` → `make_app(config, session_factory)`. Existing routes that read `app["session"]` switch to `await get_or_open_session(request)`. In Phase 2 the middleware always synthesizes the dev identity; CF mode lands in Phase 3.

- [ ] **Step 1: Update `tests/conftest.py` for the new signature first (TDD: tests own the contract)**

Replace the body of `_serve_lens` and add a `dev_config` fixture:

```python
"""Test fixtures shared across the suite.

Phase 2 changes the lens app signature: `make_app(config, session_factory)`
instead of `make_app(session)`. Tests build a dev-mode config and a
session-factory closure that returns the live test Session.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.session import Session
from irc_lens.web import make_app

from _agentirc_server import AgentIRCTestServer

_BASIC_SEED = Path(__file__).parent / "fixtures" / "basic.yaml"


def _dev_config(server_host: str, server_port: int, nick: str = "lens-test") -> LensConfig:
    return LensConfig(
        auth_mode="dev",
        dev_nick=nick,
        dev_email="dev@local",
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host=server_host,
        server_port=server_port,
        web_bind="127.0.0.1",
        web_port=0,
    )


async def _serve_lens(session: Session, host: str, port: int) -> AsyncIterator[TestClient]:
    """Spin up an aiohttp TestClient against `session`.

    The session-factory passed to make_app simply returns the already-
    connected `session` — under dev mode the registry will see the
    synthetic dev principal on first request, double-check finds a hit
    only after the first call, so the factory is invoked exactly once
    and its return value reused.
    """
    config = _dev_config(host, port, nick=session.nick)
    factory = lambda _nick: session   # noqa: E731 — single-line closure is the point
    app: web.Application = make_app(config, factory)
    test_server = TestServer(app)
    client = TestClient(test_server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def agentirc_server() -> AsyncIterator[AgentIRCTestServer]:
    server = AgentIRCTestServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest_asyncio.fixture
async def lens_session(agentirc_server: AgentIRCTestServer) -> AsyncIterator[Session]:
    session = Session(host=agentirc_server.host, port=agentirc_server.port, nick="lens-test")
    await session.connect()
    try:
        yield session
    finally:
        await session.disconnect()


@pytest_asyncio.fixture
async def lens_client(lens_session: Session, agentirc_server: AgentIRCTestServer) -> AsyncIterator[TestClient]:
    async for client in _serve_lens(lens_session, agentirc_server.host, agentirc_server.port):
        yield client


@pytest_asyncio.fixture
async def seeded_lens_client(lens_session: Session, agentirc_server: AgentIRCTestServer) -> AsyncIterator[TestClient]:
    from irc_lens.seed import apply_seed

    apply_seed(lens_session, _BASIC_SEED)
    async for client in _serve_lens(lens_session, agentirc_server.host, agentirc_server.port):
        yield client
```

The `factory = lambda _nick: session` is the test seam: dev-mode tests need exactly one Session and the conftest reuses the one the agentirc fixture already opened. Production wiring constructs a real factory in `serve.py`.

- [ ] **Step 2: Rewrite `src/irc_lens/web/app.py`**

```python
"""aiohttp ``Application`` factory for irc-lens.

Phase 2: takes a :class:`LensConfig` and a session factory rather than
a single connected Session. Builds the per-principal
:class:`SessionRegistry`, mounts the dev-mode identity middleware, and
exposes a `/healthz` endpoint.

CF-mode middleware lands in Phase 3.
"""
from __future__ import annotations

from importlib.resources import files

from aiohttp import web

from irc_lens.config import LensConfig
from irc_lens.web import routes
from irc_lens.web.identity import Identity, derive_nick
from irc_lens.web.routes import _MAX_INPUT_BODY
from irc_lens.web.sessions import SessionFactory, SessionRegistry


def _dev_identity_middleware(config: LensConfig):
    """Synthesizes the dev identity on every request.

    Real-world dev mode has a single human at the keyboard; the lens
    treats every request as them. CF mode replaces this in Phase 3.
    """
    assert config.auth_mode == "dev"
    assert config.dev_email is not None
    identity = Identity(
        principal=config.dev_email,
        nick=config.dev_nick or "lens",
        raw_jwt_subject="dev",
    )

    @web.middleware
    async def middleware(request: web.Request, handler):
        # Skip identity synthesis for static assets and /healthz.
        if request.path.startswith("/static/") or request.path == "/healthz":
            return await handler(request)
        request["identity"] = identity
        return await handler(request)

    return middleware


def make_app(config: LensConfig, session_factory: SessionFactory) -> web.Application:
    if config.auth_mode != "dev":
        # Phase 3 will branch here. Until then, callers must pass dev mode.
        raise RuntimeError(
            "make_app: only auth.mode='dev' is wired in Phase 2"
        )

    middleware = _dev_identity_middleware(config)
    app = web.Application(client_max_size=_MAX_INPUT_BODY, middlewares=[middleware])

    registry = SessionRegistry(factory=session_factory)
    app["registry"] = registry
    app["config"] = config

    app.router.add_get("/", routes.get_index)
    app.router.add_post("/input", routes.post_input)
    app.router.add_get("/events", routes.get_events)
    app.router.add_get("/healthz", routes.get_healthz)

    static_dir = files("irc_lens").joinpath("static")
    app.router.add_static(
        "/static/",
        path=str(static_dir),
        name="static",
        show_index=False,
        follow_symlinks=False,
    )

    return app
```

- [ ] **Step 3: Update `src/irc_lens/web/routes.py`**

Add a `get_healthz` handler at the bottom:

```python
async def get_healthz(_request: web.Request) -> web.Response:
    """Opaque health probe. No auth, no IRC state, no allowlist leak."""
    return web.json_response({"ok": True})
```

Replace each `request.app["session"]` use with `await _resolve_session(request)`. Add this helper near the top of the module:

```python
from irc_lens.web.sessions import SessionRegistry


async def _resolve_session(request: web.Request):
    """Look up (or lazily open) the Session for this request's identity.

    The dev-mode middleware always sets request["identity"]. Static and
    /healthz paths skip the middleware and never call this helper.
    """
    identity = request["identity"]
    registry: SessionRegistry = request.app["registry"]
    return await registry.get_or_open(identity)
```

In `get_index`, change:
```python
session = request.app["session"]
```
to:
```python
session = await _resolve_session(request)
```

In `post_input`, the existing line:
```python
session = request.app["session"]
```
becomes:
```python
session = await _resolve_session(request)
```

In `get_events`, the existing:
```python
session = request.app["session"]
```
becomes:
```python
session = await _resolve_session(request)
```

- [ ] **Step 4: Run the existing test suite — every dev-mode test must still pass**

Run: `uv run pytest -x -v`
Expected: all non-playwright, non-cloudflare tests pass. If any test referenced `app["session"]` directly, update it to `app["registry"]` or rebuild via the new fixtures.

- [ ] **Step 5: Run a quick `/healthz` smoke**

In an interactive Python session, or as an inline test addition, hit `/healthz` via `lens_client` and assert 200 + `{"ok": True}`. (Phase 4 has a dedicated test file; this is just sanity.)

- [ ] **Step 6: Commit**

```bash
git add src/irc_lens/web/app.py src/irc_lens/web/routes.py tests/conftest.py
git commit -m "feat(web): per-principal Session registry under dev mode

make_app(session) -> make_app(config, session_factory). Routes resolve
the Session via the registry on each request. Dev-mode middleware
synthesizes a fixed identity. /healthz lands as the unauthenticated
probe. Existing tests migrate via the new dev_config helper in
conftest."
```

---

### Task 2.6: Wire `serve.py` to use the new `make_app` signature in dev mode

**Files:**
- Modify: `src/irc_lens/cli/_commands/serve.py`

- [ ] **Step 1: Replace `make_app(session)` and the singleton-Session model**

Edit `_serve_async` in `serve.py`. Replace the existing `app = make_app(session)` block plus the single-Session connect with:

```python
from irc_lens.config import LensConfig, default_config_path, load_config
from irc_lens.web.sessions import disconnect_all
from irc_lens.session import Session
```

Above `_serve_async`, add a small helper:

```python
def _build_dev_config(args: argparse.Namespace) -> LensConfig:
    """In Phase 2, dev mode still requires --nick. Build a synthetic
    LensConfig from the CLI args so existing serve invocations work
    without a config file. Phase 4 reverses this: config file becomes
    primary; CLI args override.
    """
    return LensConfig(
        auth_mode="dev",
        dev_nick=args.nick,
        dev_email="dev@local",
        cf_aud=None,
        cf_team_domain=None,
        allowed_emails=(),
        allowed_service_tokens=(),
        server_name="dev",
        server_host=args.host,
        server_port=args.port,
        web_bind=args.bind,
        web_port=args.web_port,
    )
```

Then in `_serve_async`, replace the body up through `app = make_app(session)` with:

```python
config = _build_dev_config(args)
session = Session(host=config.server_host, port=config.server_port, nick=config.dev_nick, icon=args.icon)
try:
    await session.connect()
except LensConnectionLost as exc:
    raise AfiError(...)  # unchanged
try:
    await session.wait_for_welcome()
except LensConnectionLost as exc:
    await session.disconnect()
    raise AfiError(...)  # unchanged

if args.seed:
    try:
        from irc_lens.seed import apply_seed
        apply_seed(session, Path(args.seed))
    except Exception:
        await session.disconnect()
        raise

# Single-Session-in-dev: the factory always returns the same instance.
factory = lambda _nick: session   # noqa: E731
app = make_app(config, factory)
runner = web.AppRunner(app, handle_signals=True)
# … unchanged through site.start()

try:
    await asyncio.Event().wait()
finally:
    await disconnect_all(app["registry"])
    await runner.cleanup()
```

Remove the now-redundant standalone `await session.disconnect()` in the outer `finally`; `disconnect_all` covers it.

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -x -v`
Expected: all tests pass, including `test_serve_cli.py` which checks the CLI surface.

- [ ] **Step 3: Manual smoke against a local AgentIRC**

If a local Culture server is available:

Run: `uv run irc-lens serve --nick lens-test --web-port 18765` (in one terminal)
Then: `curl -sf http://127.0.0.1:18765/healthz`
Expected: `{"ok": true}`.

If no local AgentIRC, skip — phase 4 tests verify the same thing in CI.

- [ ] **Step 4: Commit**

```bash
git add src/irc_lens/cli/_commands/serve.py
git commit -m "feat(serve): dev-mode wires through SessionRegistry

Builds a synthetic dev LensConfig from CLI args, hands a
factory-of-one to make_app. disconnect_all replaces the bare
session.disconnect() in the shutdown path."
```

---

## Phase 3 — CF auth middleware + JWT verification

### Task 3.1: Build the in-tree fake JWKS server fixture

**Files:**
- Create: `tests/_jwks_server.py`

- [ ] **Step 1: Write the fixture module**

```python
"""Tiny aiohttp app that mimics Cloudflare's JWKS endpoint for tests.

Tests mint JWTs locally with the matching private key and point the
lens at this server's URL. Mirrors the in-tree
``_agentirc_server.py`` pattern so we don't add a network dep.
"""
from __future__ import annotations

import json
from typing import Any

import jwt
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeJWKS:
    """Generate a keypair, expose the JWK set, mint signed JWTs."""

    def __init__(self, kid: str = "test-kid-1") -> None:
        self._kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._site: web.TCPSite | None = None
        self._runner: web.AppRunner | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def team_domain(self) -> str:
        # The lens treats `https://<team_domain>` as the issuer; use the
        # bare host so URL composition works for both /cdn-cgi/access/certs
        # and `iss` claim assertions.
        return f"{self.host}:{self.port}"

    @property
    def issuer(self) -> str:
        return f"http://{self.team_domain}"

    def public_jwk(self) -> dict[str, Any]:
        numbers = self._key.public_key().public_numbers()
        from base64 import urlsafe_b64encode

        def _b64(n: int) -> str:
            blen = (n.bit_length() + 7) // 8
            return urlsafe_b64encode(n.to_bytes(blen, "big")).rstrip(b"=").decode()

        return {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": self._kid,
            "n": _b64(numbers.n),
            "e": _b64(numbers.e),
        }

    def mint(self, *, aud: str, claims: dict[str, Any], kid: str | None = None) -> str:
        payload = {"iss": self.issuer, "aud": aud, **claims}
        return jwt.encode(
            payload,
            self._key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            algorithm="RS256",
            headers={"kid": kid or self._kid},
        )

    async def start(self) -> None:
        app = web.Application()

        async def certs(_req: web.Request) -> web.Response:
            return web.json_response({"keys": [self.public_jwk()]})

        app.router.add_get("/cdn-cgi/access/certs", certs)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, 0)
        await self._site.start()
        # Resolve the actual port the OS assigned.
        sockets = list(self._runner.addresses)
        if sockets:
            self.port = sockets[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
```

- [ ] **Step 2: Smoke import**

Run: `uv run python -c "from tests._jwks_server import FakeJWKS; print(FakeJWKS)"`

If the import fails because `tests` isn't a package: confirm by checking the existing `_agentirc_server.py`. The existing test layout imports it as a sibling module via `conftest.py` adding `tests/` to `sys.path` indirectly (pytest does this by default for files alongside `conftest.py`). Adjust `from tests._jwks_server` → `from _jwks_server` in actual test files if needed.

- [ ] **Step 3: Commit**

```bash
git add tests/_jwks_server.py
git commit -m "test: in-tree fake JWKS server fixture"
```

---

### Task 3.2: Write failing tests for the CF auth middleware

**Files:**
- Create: `tests/test_auth_middleware.py`

- [ ] **Step 1: Write failing tests**

```python
"""CF Access JWT validation + middleware behavior."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.web import make_app

from _jwks_server import FakeJWKS


def _cf_config(jwks: FakeJWKS, allowed: list[str]) -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=tuple(allowed),
        allowed_service_tokens=(),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
    )


@pytest_asyncio.fixture
async def jwks() -> AsyncIterator[FakeJWKS]:
    j = FakeJWKS()
    await j.start()
    try:
        yield j
    finally:
        await j.stop()


@pytest_asyncio.fixture
async def cf_client(jwks: FakeJWKS) -> AsyncIterator[TestClient]:
    """A lens TestClient in cloudflare-access mode wired to FakeJWKS.

    Sessions never actually open in these tests — the middleware
    intercepts before any handler that would call get_or_open.
    The factory raises if invoked, to catch accidental routing.
    """
    config = _cf_config(jwks, allowed=["alice@example.com"])

    def boom_factory(_nick: str):
        raise AssertionError("session factory must not run during auth tests")

    app = make_app(config, boom_factory)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def test_missing_jwt_returns_401(cf_client: TestClient) -> None:
    resp = await cf_client.get("/")
    assert resp.status == 401
    body = await resp.json()
    assert "error" in body and "hint" in body


async def test_valid_jwt_in_header_passes_to_handler(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s-1"})
    # The boom_factory raises if the handler tries to open a session.
    # /healthz skips auth and the registry, so use it as the canary that the
    # *middleware* accepted the token.
    resp = await cf_client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status == 200


async def test_valid_jwt_in_cookie_also_accepted(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s-2"})
    cf_client.session.cookie_jar.update_cookies({"CF_Authorization": token})
    resp = await cf_client.get("/healthz")
    assert resp.status == 200


async def test_wrong_audience_returns_401(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-other", claims={"email": "alice@example.com", "sub": "s"})
    resp = await cf_client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status == 401


async def test_email_not_on_allowlist_returns_403(cf_client: TestClient, jwks: FakeJWKS) -> None:
    token = jwks.mint(aud="aud-test", claims={"email": "mallory@example.com", "sub": "s"})
    resp = await cf_client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status == 403
    body = await resp.json()
    assert "allowlist" in body["error"].lower()


async def test_static_path_skips_auth(cf_client: TestClient) -> None:
    # Static asset path must not require a JWT (browser fetches assets first
    # before the SSO redirect lands on every request).
    resp = await cf_client.get("/static/missing.js")
    # 404 from the static handler is fine; 401 would mean middleware ran.
    assert resp.status in (200, 404)


async def test_kid_miss_then_refresh_succeeds(cf_client: TestClient, jwks: FakeJWKS) -> None:
    """If the JWT's kid isn't in cache, we refetch JWKS once and retry."""
    # First request populates the cache with the current kid.
    t1 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"})
    r1 = await cf_client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": t1})
    assert r1.status == 200
    # Mint with a different kid; the lens must refetch JWKS to discover it
    # and accept the JWT after the refresh.
    t2 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"}, kid="rotated-kid")
    # FakeJWKS still serves only the original kid, so this MUST fail.
    r2 = await cf_client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": t2})
    assert r2.status == 401
```

- [ ] **Step 2: Run, expect fail**

Run: `uv run pytest tests/test_auth_middleware.py -v`
Expected: most tests fail with a `make_app` assertion (`only auth.mode='dev' is wired in Phase 2`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth_middleware.py
git commit -m "test: failing tests for CF auth middleware"
```

---

### Task 3.3: Implement `web/auth.py` (JWT verification + JWKS cache + middleware)

**Files:**
- Create: `src/irc_lens/web/auth.py`

- [ ] **Step 1: Write the module**

```python
"""Cloudflare Access JWT verification and middleware.

Validates the JWT against the Cloudflare-published JWKS, pinning
audience and issuer. Caches the JWK set in process; on a `kid` we
don't recognize, refresh once and retry — but never on every request.
Identity (email or service-token common-name) becomes
``request['identity']``; missing/invalid → 401, allowlist deny → 403.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
import jwt
from aiohttp import web

from irc_lens.config import LensConfig
from irc_lens.web.identity import Identity, derive_nick

logger = logging.getLogger(__name__)

_JWKS_PATH = "/cdn-cgi/access/certs"


def _http_error(status: int, error: str, hint: str) -> web.Response:
    return web.json_response({"error": error, "hint": hint}, status=status)


class _JWKSCache:
    """In-process JWK set cache with kid-miss-then-refresh semantics."""

    def __init__(self, team_domain: str) -> None:
        self._url = self._build_url(team_domain)
        self._keys: dict[str, Any] = {}
        self._last_fetch: float = 0.0

    @staticmethod
    def _build_url(team_domain: str) -> str:
        # Tests pass `host:port` as team_domain so we can talk over HTTP;
        # production team_domain is a real Cloudflare hostname (use HTTPS).
        scheme = "http" if ":" in team_domain else "https"
        return f"{scheme}://{team_domain}{_JWKS_PATH}"

    async def _refresh(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(self._url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                r.raise_for_status()
                payload = await r.json()
        self._keys = {k["kid"]: k for k in payload.get("keys", [])}
        self._last_fetch = time.time()

    async def get_key(self, kid: str) -> Any:
        if kid in self._keys:
            return self._keys[kid]
        # Avoid kid-miss-flood: only refresh if last fetch is older than 5 s.
        if time.time() - self._last_fetch < 5.0 and self._keys:
            raise KeyError(kid)
        await self._refresh()
        if kid not in self._keys:
            raise KeyError(kid)
        return self._keys[kid]

    async def warm(self) -> None:
        await self._refresh()


def _build_issuer(team_domain: str) -> str:
    scheme = "http" if ":" in team_domain else "https"
    return f"{scheme}://{team_domain}"


def _principal_from_claims(claims: dict[str, Any]) -> str | None:
    """Email under interactive SSO; common_name under service tokens."""
    email = claims.get("email")
    if isinstance(email, str) and email:
        return email
    cn = claims.get("common_name")
    if isinstance(cn, str) and cn:
        return cn
    return None


def build_cloudflare_middleware(config: LensConfig):
    assert config.auth_mode == "cloudflare-access"
    assert config.cf_aud and config.cf_team_domain
    cache = _JWKSCache(config.cf_team_domain)
    issuer = _build_issuer(config.cf_team_domain)
    aud = config.cf_aud
    allowed_emails = set(config.allowed_emails)
    allowed_tokens = set(config.allowed_service_tokens)
    server_name = config.server_name

    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.path.startswith("/static/") or request.path == "/healthz":
            return await handler(request)

        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            token = request.cookies.get("CF_Authorization")
        if not token:
            return _http_error(
                401,
                "missing Cloudflare Access identity",
                "ensure this request is reaching the lens through cloudflared",
            )

        try:
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")
            if not kid:
                raise jwt.InvalidTokenError("missing kid in JWT header")
            jwk_data = await cache.get_key(kid)
            from jwt.algorithms import RSAAlgorithm
            public_key = RSAAlgorithm.from_jwk(jwk_data)
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=aud,
                issuer=issuer,
            )
        except jwt.ExpiredSignatureError:
            return _http_error(401, "Cloudflare Access JWT expired", "sign in again")
        except jwt.InvalidAudienceError:
            return _http_error(
                401,
                "Cloudflare Access JWT failed verification",
                "audience mismatch — verify auth.cloudflare.aud in the lens config",
            )
        except jwt.InvalidIssuerError:
            return _http_error(
                401,
                "Cloudflare Access JWT failed verification",
                "issuer mismatch — verify auth.cloudflare.team_domain in the lens config",
            )
        except (KeyError, jwt.InvalidTokenError) as exc:
            return _http_error(
                401,
                "Cloudflare Access JWT failed verification",
                f"verify the request came through cloudflared ({type(exc).__name__})",
            )
        except aiohttp.ClientError as exc:
            return _http_error(
                502,
                "could not reach Cloudflare JWKS",
                f"check connectivity to {config.cf_team_domain} ({exc})",
            )

        principal = _principal_from_claims(claims)
        if not principal:
            return _http_error(
                401,
                "JWT carried neither email nor common_name",
                "verify the Access policy issues an email or service-token JWT",
            )

        # Allowlist enforcement: emails and service tokens are sibling lists.
        is_email = "email" in claims and isinstance(claims["email"], str)
        if is_email and principal not in allowed_emails:
            return _http_error(
                403,
                f"{principal} not on allowlist",
                "add to auth.allowed_emails in the lens config",
            )
        if (not is_email) and principal not in allowed_tokens:
            return _http_error(
                403,
                f"service token {principal} not on allowlist",
                "add to auth.allowed_service_tokens in the lens config",
            )

        try:
            nick = derive_nick(server_name, principal)
        except ValueError as exc:
            logger.error("nick derivation failed: %s", exc)
            return _http_error(
                500,
                "nick derivation failed",
                "principal sanitizes to empty; pick a different identity",
            )

        request["identity"] = Identity(
            principal=principal,
            nick=nick,
            raw_jwt_subject=str(claims.get("sub", "")),
        )
        logger.info(
            "auth=ok principal=%s nick=%s method=%s path=%s",
            principal, nick, request.method, request.path,
        )
        return await handler(request)

    return middleware


async def warm_jwks(config: LensConfig) -> None:
    """Fail fast at startup if Cloudflare's JWKS is unreachable."""
    if config.auth_mode != "cloudflare-access":
        return
    cache = _JWKSCache(config.cf_team_domain or "")
    await cache.warm()
```

- [ ] **Step 2: Update `make_app` to install the right middleware per mode**

In `src/irc_lens/web/app.py`, remove the early `RuntimeError` guard and the `_dev_identity_middleware`-only path. Replace with:

```python
from irc_lens.web.auth import build_cloudflare_middleware


def make_app(config: LensConfig, session_factory: SessionFactory) -> web.Application:
    if config.auth_mode == "dev":
        middleware = _dev_identity_middleware(config)
    elif config.auth_mode == "cloudflare-access":
        middleware = build_cloudflare_middleware(config)
    else:
        raise RuntimeError(f"unknown auth_mode {config.auth_mode!r}")

    app = web.Application(client_max_size=_MAX_INPUT_BODY, middlewares=[middleware])
    # … rest unchanged from Task 2.5.
```

- [ ] **Step 3: Run middleware tests, expect green**

Run: `uv run pytest tests/test_auth_middleware.py -v`
Expected: all 7 tests PASS. The kid-miss test asserts the second request fails (FakeJWKS only serves one kid); the lens must refetch but still not find the rotated kid → 401.

- [ ] **Step 4: Run full suite — dev-mode tests must still pass**

Run: `uv run pytest -x -v`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/irc_lens/web/auth.py src/irc_lens/web/app.py
git commit -m "feat(web): Cloudflare Access JWT middleware with JWKS cache"
```

---

### Task 3.4: Add a positive kid-miss-refresh test (rotation that *succeeds*)

**Files:**
- Modify: `tests/_jwks_server.py`
- Modify: `tests/test_auth_middleware.py`

The previous test only proved we *fail* on an unknown kid. We also need to prove we *succeed* when the JWKS rotates and a refresh discovers the new key.

- [ ] **Step 1: Add a key-rotation method to `FakeJWKS`**

In `tests/_jwks_server.py`, after `__init__`, add:

```python
def rotate(self, new_kid: str) -> None:
    """Replace the key + kid; subsequent /certs returns the new key only."""
    self._kid = new_kid
    self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
```

- [ ] **Step 2: Add the success-path test**

Append to `tests/test_auth_middleware.py`:

```python
async def test_rotated_kid_accepted_after_refresh(cf_client: TestClient, jwks: FakeJWKS) -> None:
    """JWT signed with a new key after rotation must validate post-refresh."""
    # Warm cache with the original kid.
    t1 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"})
    r1 = await cf_client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": t1})
    assert r1.status == 200

    # Wait past the 5 s anti-flood window so the next miss triggers a real refresh.
    import asyncio
    await asyncio.sleep(5.1)

    # Rotate, mint with the new key, expect success after JWKS refresh.
    jwks.rotate("kid-after-rotation")
    t2 = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"})
    r2 = await cf_client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": t2})
    assert r2.status == 200
```

The 5 s sleep makes this test slow; mark it explicitly:

```python
import pytest
@pytest.mark.slow
async def test_rotated_kid_accepted_after_refresh(...):
    ...
```

Add the `slow` marker to `pyproject.toml`:

```toml
markers = [
    "playwright: …",
    "cloudflare: …",
    "slow: tests with intentional sleeps; default-included.",
]
```

(`slow` stays in the default run; the marker is just a label.)

- [ ] **Step 3: Run the new test**

Run: `uv run pytest tests/test_auth_middleware.py::test_rotated_kid_accepted_after_refresh -v`
Expected: PASS in ~5.5 s.

- [ ] **Step 4: Commit**

```bash
git add tests/_jwks_server.py tests/test_auth_middleware.py pyproject.toml
git commit -m "test: rotated-kid success path through JWKS refresh"
```

---

### Task 3.5: Service-token branch test

**Files:**
- Modify: `tests/test_auth_middleware.py`

- [ ] **Step 1: Add tests for the service-token claim shape**

Append to `tests/test_auth_middleware.py`:

```python
async def test_service_token_common_name_accepted(jwks: FakeJWKS) -> None:
    """A JWT with `common_name` (no `email`) is accepted iff CN is in
    auth.allowed_service_tokens."""
    config = LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None,
        dev_email=None,
        cf_aud="aud-test",
        cf_team_domain=jwks.team_domain,
        allowed_emails=(),
        allowed_service_tokens=("ci-bot",),
        server_name="testsrv",
        server_host="127.0.0.1",
        server_port=6667,
        web_bind="127.0.0.1",
        web_port=0,
    )

    def boom_factory(_n: str):
        raise AssertionError("not expected to run")

    app = make_app(config, boom_factory)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        token = jwks.mint(aud="aud-test", claims={"common_name": "ci-bot", "sub": "svc"})
        r = await client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status == 200

        bad = jwks.mint(aud="aud-test", claims={"common_name": "rogue", "sub": "svc"})
        r = await client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": bad})
        assert r.status == 403
    finally:
        await client.close()
```

- [ ] **Step 2: Run, expect green**

Run: `uv run pytest tests/test_auth_middleware.py::test_service_token_common_name_accepted -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth_middleware.py
git commit -m "test: service-token common-name allowlist branch"
```

---

### Task 3.6: Wire `serve.py` to load config and select mode

**Files:**
- Modify: `src/irc_lens/cli/_commands/serve.py`

Phase 4 will tighten the CLI rules; this task just lets `serve` consume a real config file.

- [ ] **Step 1: Add `--config` flag and load logic**

In `register()`, add:

```python
p.add_argument(
    "--config",
    default=None,
    help="Path to irc-lens config.yaml (default: ~/.config/irc-lens/config.yaml).",
)
```

In `cmd_serve`, before `_configure_logging`, resolve the config:

```python
config_path = Path(args.config) if args.config else default_config_path()
if config_path.exists():
    config = load_config(config_path)
else:
    # Backward-compat: if no config file, fall back to the synthetic dev
    # config built from CLI args. Phase 4 reverses this: missing file is
    # an error, with `irc-lens config init` in the hint.
    config = _build_dev_config(args)
```

In `_serve_async`, replace the body so the branching lives inline:

```python
async def _serve_async(args: argparse.Namespace, config: LensConfig) -> None:
    if config.auth_mode == "dev":
        # Existing dev path: open one Session at boot.
        session = Session(host=config.server_host, port=config.server_port,
                          nick=config.dev_nick, icon=args.icon)
        await session.connect()
        await session.wait_for_welcome()
        if args.seed:
            from irc_lens.seed import apply_seed
            apply_seed(session, Path(args.seed))
        factory = lambda _n: session   # noqa: E731
    else:
        # CF mode: warm JWKS, defer IRC connect to first authenticated request.
        from irc_lens.web.auth import warm_jwks
        try:
            await warm_jwks(config)
        except Exception as exc:
            raise AfiError(
                code=EXIT_ENV_ERROR,
                message=f"could not reach Cloudflare JWKS at {config.cf_team_domain}: {exc}",
                remediation="verify network egress and team_domain spelling",
            ) from exc

        def factory(nick: str) -> Session:
            return Session(host=config.server_host, port=config.server_port,
                           nick=nick, icon=None)

    app = make_app(config, factory)
    runner = web.AppRunner(app, handle_signals=True)
    await runner.setup()
    site = web.TCPSite(runner, host=config.web_bind, port=config.web_port)
    try:
        await site.start()
    except OSError as exc:
        raise AfiError(...)
    # … rest unchanged.
    try:
        await asyncio.Event().wait()
    finally:
        await disconnect_all(app["registry"])
        await runner.cleanup()
```

Pass `config` through:

```python
asyncio.run(_serve_async(args, config))
```

- [ ] **Step 2: Run full suite**

Run: `uv run pytest -x -v`
Expected: green. The `test_serve_cli.py` suite still uses `--nick`-required-required-via-argparse, which Phase 4 changes; in Phase 3 it should keep passing because we haven't removed `--nick`.

- [ ] **Step 3: Smoke CF mode**

Run: `uv run irc-lens config init --path /tmp/lens.yaml`
Edit `/tmp/lens.yaml` to set `auth.mode: cloudflare-access` with a bogus `team_domain` (e.g. `nope.invalid`).
Run: `uv run irc-lens serve --config /tmp/lens.yaml --nick dummy --web-port 18765`
Expected: exits 2 with `error: could not reach Cloudflare JWKS at nope.invalid` and the hint.

- [ ] **Step 4: Commit**

```bash
git add src/irc_lens/cli/_commands/serve.py
git commit -m "feat(serve): load config from file, branch on auth.mode

Dev mode keeps the singleton-Session-at-boot. CF mode warms JWKS at
startup (fail-fast on unreachable Cloudflare) and defers IRC connect
until the first authenticated request. --config flag added."
```

---

### Task 3.7: Audit-log integration smoke test

**Files:**
- Create: `tests/test_audit_log.py`

- [ ] **Step 1: Write the test**

```python
"""Authenticated requests emit one structured audit line on stderr."""
from __future__ import annotations

import logging

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from irc_lens.config import LensConfig
from irc_lens.web import make_app

from _jwks_server import FakeJWKS


async def test_authenticated_request_logs_principal(jwks: FakeJWKS, caplog) -> None:
    config = LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None, dev_email=None,
        cf_aud="aud-test", cf_team_domain=jwks.team_domain,
        allowed_emails=("alice@example.com",), allowed_service_tokens=(),
        server_name="testsrv",
        server_host="127.0.0.1", server_port=6667,
        web_bind="127.0.0.1", web_port=0,
    )

    def boom(_n: str):
        raise AssertionError("not expected")

    app = make_app(config, boom)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        token = jwks.mint(aud="aud-test", claims={"email": "alice@example.com", "sub": "s"})
        with caplog.at_level(logging.INFO, logger="irc_lens.web.auth"):
            r = await client.get("/healthz", headers={"Cf-Access-Jwt-Assertion": token})
            assert r.status == 200
        msgs = [rec.getMessage() for rec in caplog.records if rec.name == "irc_lens.web.auth"]
        assert any("auth=ok" in m and "alice@example.com" in m for m in msgs)
    finally:
        await client.close()
```

The `jwks` fixture is in `tests/test_auth_middleware.py`. To share, lift it into `tests/conftest.py`:

```python
@pytest_asyncio.fixture
async def jwks() -> AsyncIterator[FakeJWKS]:
    j = FakeJWKS()
    await j.start()
    try:
        yield j
    finally:
        await j.stop()
```

(import `FakeJWKS` from `_jwks_server` at the top of conftest).

Remove the duplicate fixture from `test_auth_middleware.py`.

- [ ] **Step 2: Run, expect green**

Run: `uv run pytest tests/test_audit_log.py -v`
Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -x -v`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_audit_log.py tests/conftest.py tests/test_auth_middleware.py
git commit -m "test: audit log emits one auth=ok line per authenticated request"
```

---

## Phase 4 — CLI/HTTP cleanups

### Task 4.1: `--nick` rejection in CF mode + bind coercion

**Files:**
- Create: `tests/test_serve_cf_mode.py`
- Modify: `src/irc_lens/cli/_commands/serve.py`

- [ ] **Step 1: Write failing tests**

```python
"""--nick is rejected in CF mode; --bind on a non-loopback is coerced."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from irc_lens.cli._commands.serve import _validate_cli_against_config
from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError
from irc_lens.config import LensConfig


def _cf_config() -> LensConfig:
    return LensConfig(
        auth_mode="cloudflare-access",
        dev_nick=None, dev_email=None,
        cf_aud="a", cf_team_domain="t.cloudflareaccess.com",
        allowed_emails=("a@example.com",),
        allowed_service_tokens=(),
        server_name="spark",
        server_host="127.0.0.1", server_port=6667,
        web_bind="127.0.0.1", web_port=8765,
    )


def test_nick_rejected_in_cf_mode(tmp_path: Path) -> None:
    cfg = _cf_config()
    with pytest.raises(AfiError) as exc:
        _validate_cli_against_config(cfg, nick="something", bind="127.0.0.1")
    assert exc.value.code == EXIT_USER_ERROR
    assert "--nick" in exc.value.message


def test_bind_coerced_to_loopback_in_cf_mode(caplog) -> None:
    cfg = _cf_config()
    with caplog.at_level(logging.WARNING):
        coerced = _validate_cli_against_config(cfg, nick=None, bind="0.0.0.0")
    assert coerced.web_bind == "127.0.0.1"
    assert any("coerced" in r.getMessage().lower() for r in caplog.records)


def test_loopback_bind_unchanged_in_cf_mode() -> None:
    cfg = _cf_config()
    coerced = _validate_cli_against_config(cfg, nick=None, bind="127.0.0.1")
    assert coerced.web_bind == "127.0.0.1"
```

- [ ] **Step 2: Implement `_validate_cli_against_config`**

In `src/irc_lens/cli/_commands/serve.py`:

```python
import logging
logger = logging.getLogger("irc_lens.serve")

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _validate_cli_against_config(
    config: LensConfig,
    nick: str | None,
    bind: str | None,
) -> LensConfig:
    """Apply CF-mode CLI rules. Returns a possibly-coerced config."""
    if config.auth_mode != "cloudflare-access":
        return config

    if nick is not None:
        raise AfiError(
            code=EXIT_USER_ERROR,
            message="--nick is not valid when auth.mode is cloudflare-access",
            remediation=(
                "remove --nick — in CF mode the nick is derived per "
                "authenticated user from auth.allowed_emails"
            ),
        )

    effective_bind = bind if bind is not None else config.web_bind
    if effective_bind not in _LOOPBACK:
        logger.warning(
            "web.bind=%s is not loopback; coerced to 127.0.0.1 because "
            "cloudflared terminates locally",
            effective_bind,
        )
        # Frozen dataclass — return a new one with web_bind replaced.
        from dataclasses import replace
        return replace(config, web_bind="127.0.0.1")
    return config
```

Then in `cmd_serve`, before `_serve_async`:

```python
config = _validate_cli_against_config(
    config,
    nick=args.nick,
    bind=args.bind if args.bind != "127.0.0.1" else None,  # only if user passed a non-default
)
```

(The default `--bind 127.0.0.1` should pass through silently; only an explicitly passed non-loopback triggers the notice. Easiest: track whether the user passed `--bind` via a sentinel. Replace the argparse default for `--bind` with `None` and treat `None` as "not provided".)

Update the argparse spec:

```python
p.add_argument(
    "--bind",
    default=None,
    help="…",
)
```

And in `_build_dev_config` / wherever `args.bind` is consumed downstream, fall back to `config.web_bind` when `args.bind is None`.

- [ ] **Step 3: Run new tests, expect green**

Run: `uv run pytest tests/test_serve_cf_mode.py -v`
Expected: 3 PASS.

- [ ] **Step 4: Run full suite**

Run: `uv run pytest -x -v`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_serve_cf_mode.py src/irc_lens/cli/_commands/serve.py
git commit -m "feat(serve): reject --nick in CF mode, coerce non-loopback --bind"
```

---

### Task 4.2: `/healthz` test

**Files:**
- Create: `tests/test_healthz.py`

- [ ] **Step 1: Write the test**

```python
"""/healthz is opaque, unauthenticated, leaks no IRC state."""
from __future__ import annotations

from aiohttp.test_utils import TestClient


async def test_healthz_returns_only_ok(lens_client: TestClient) -> None:
    r = await lens_client.get("/healthz")
    assert r.status == 200
    body = await r.json()
    assert body == {"ok": True}


async def test_healthz_without_auth_in_cf_mode_works() -> None:
    """A /healthz hit on a CF-mode app skips the middleware entirely."""
    from _jwks_server import FakeJWKS
    from irc_lens.config import LensConfig
    from irc_lens.web import make_app

    jwks = FakeJWKS()
    await jwks.start()
    try:
        config = LensConfig(
            auth_mode="cloudflare-access",
            dev_nick=None, dev_email=None,
            cf_aud="a", cf_team_domain=jwks.team_domain,
            allowed_emails=("alice@example.com",), allowed_service_tokens=(),
            server_name="spark",
            server_host="127.0.0.1", server_port=6667,
            web_bind="127.0.0.1", web_port=0,
        )
        def boom(_n): raise AssertionError("no")
        app = make_app(config, boom)
        from aiohttp.test_utils import TestServer
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            r = await client.get("/healthz")
            assert r.status == 200
            assert await r.json() == {"ok": True}
        finally:
            await client.close()
    finally:
        await jwks.stop()
```

- [ ] **Step 2: Run, expect green**

Run: `uv run pytest tests/test_healthz.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_healthz.py
git commit -m "test: /healthz opaque + unauthenticated in both modes"
```

---

### Task 4.3: Origin/Referer floor on `POST /input`

**Files:**
- Create: `tests/test_origin_check.py`
- Modify: `src/irc_lens/web/routes.py`

- [ ] **Step 1: Write failing tests**

```python
"""POST /input rejects cross-origin requests as a CSRF floor."""
from __future__ import annotations

from aiohttp.test_utils import TestClient


async def test_post_input_no_origin_allowed(lens_client: TestClient) -> None:
    """No Origin header (curl, cloudflared probe) is allowed."""
    r = await lens_client.post("/input", data={"text": "hello"})
    assert r.status in (204, 503)


async def test_post_input_matching_origin_allowed(lens_client: TestClient) -> None:
    # In dev mode the public hostname isn't pinned; same-origin always OK.
    origin = f"http://127.0.0.1:{lens_client.server.port}"
    r = await lens_client.post("/input", data={"text": "hello"},
                               headers={"Origin": origin})
    assert r.status in (204, 503)


async def test_post_input_foreign_origin_rejected(lens_client: TestClient) -> None:
    r = await lens_client.post("/input", data={"text": "hello"},
                               headers={"Origin": "https://evil.example.com"})
    assert r.status == 403
    body = await r.json()
    assert "origin" in body["error"].lower()
```

- [ ] **Step 2: Implement the check in `routes.py`**

Add near the top of `routes.py`:

```python
def _origin_ok(request: web.Request) -> bool:
    """Best-effort same-origin check against the configured public host.

    Dev mode: any same-origin (matches the request's own Host) passes.
    CF mode: matches against the configured public hostname (set in
    Phase 4 via a new config field — for now, allow same-Host).
    Absent Origin header: pass (curl, cloudflared probes).
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    # Strip scheme; compare host:port.
    from urllib.parse import urlparse
    parsed = urlparse(origin)
    origin_netloc = parsed.netloc
    request_host = request.headers.get("Host", "")
    return origin_netloc == request_host
```

In `post_input`, immediately after the content-length check:

```python
if not _origin_ok(request):
    return web.json_response(
        {"error": "Origin does not match request host",
         "hint": "this is a CSRF defense; submit from the lens UI itself"},
        status=403,
    )
```

- [ ] **Step 3: Run tests, expect green**

Run: `uv run pytest tests/test_origin_check.py -v`
Expected: 3 PASS.

- [ ] **Step 4: Full suite green**

Run: `uv run pytest -x -v`

- [ ] **Step 5: Commit**

```bash
git add tests/test_origin_check.py src/irc_lens/web/routes.py
git commit -m "feat(web): same-host Origin check on POST /input (CSRF floor)"
```

---

### Task 4.4: Tighten config-file-required behavior

**Files:**
- Modify: `src/irc_lens/cli/_commands/serve.py`
- Modify: `tests/test_serve_cli.py` (existing — adjust expectations)

The Phase 3 fallback ("if no config, build synthetic dev config from CLI args") is removed. Missing file → `error:` + `hint: irc-lens config init`.

- [ ] **Step 1: Update serve.py**

In `cmd_serve`, replace the conditional load with:

```python
config_path = Path(args.config) if args.config else default_config_path()
config = load_config(config_path)   # raises AfiError on missing file
```

Drop the `_build_dev_config` helper (no longer used) and its imports.

- [ ] **Step 2: Update existing serve CLI tests**

In `tests/test_serve_cli.py`, look for the test that checks `--nick` is required. The contract changes: it's now config that's required. Either:

a) Adjust to set `--config <fixture>` to point at a tmp_path dev config.
b) Add a new test for "missing config errors" and rewrite the `--nick` requirement test to verify the new behavior.

Choose (a) for the smallest blast radius. Add a fixture:

```python
@pytest.fixture
def dev_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text("""
auth:
  mode: dev
  dev:
    nick: lens-test
    email: dev@local
server:
  name: spark
  host: 127.0.0.1
  port: 6667
""")
    return p
```

And add a new test:

```python
def test_serve_missing_config_errors(tmp_path: Path) -> None:
    rc = main(["serve", "--config", str(tmp_path / "nope.yaml")])
    assert rc == 1
```

- [ ] **Step 3: Run full suite**

Run: `uv run pytest -x -v`
Expected: green.

- [ ] **Step 4: Smoke**

Run: `uv run irc-lens serve` (no flags, no config file at default path)
Expected: exit 1, stderr `error: no config at …` + `hint: run 'irc-lens config init'…`.

- [ ] **Step 5: Commit**

```bash
git add src/irc_lens/cli/_commands/serve.py tests/test_serve_cli.py
git commit -m "feat(serve): require config file (no synthetic-dev fallback)"
```

---

### Task 4.5: Bump version, update docs/cli.md, update README

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/irc_lens/__init__.py` (if it has `__version__`)
- Modify: `docs/cli.md`
- Modify: `README.md`

- [ ] **Step 1: Bump version**

Run: `uv run /version-bump minor` if a version-bump script exists; otherwise hand-edit `pyproject.toml` `version = "0.4.2"` → `"0.5.0"` and the matching `__init__.py`.

- [ ] **Step 2: Update `docs/cli.md`**

Add a new "Configuration" section before any existing serve docs:

```markdown
## Configuration

irc-lens reads `~/.config/irc-lens/config.yaml` by default (override with
`--config <path>`, respecting `$XDG_CONFIG_HOME`). Initialize with:

    irc-lens config init

The starter file is in `auth.mode: dev`, suitable for a local AgentIRC
on `127.0.0.1:6667`. To deploy behind Cloudflare Access, switch
`auth.mode` to `cloudflare-access` and set `auth.cloudflare.aud`,
`auth.cloudflare.team_domain`, and `auth.allowed_emails`. See
[deployment-cloudflare-access.md](deployment-cloudflare-access.md).

### `--nick` and `--bind`

In `auth.mode: dev`, `--nick` overrides `auth.dev.nick`. In
`auth.mode: cloudflare-access`, passing `--nick` is a hard error: the
nick is derived per authenticated user from `auth.allowed_emails`.
A non-loopback `--bind` (or `web.bind`) under CF mode is silently
coerced to `127.0.0.1` because cloudflared terminates locally.
```

- [ ] **Step 3: Update README**

At the bottom of `README.md`, after any existing sections, add:

```markdown
## Production deployment

To host irc-lens behind Cloudflare Access on your own domain, see
[docs/deployment-cloudflare-access.md](docs/deployment-cloudflare-access.md).
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/irc_lens/__init__.py docs/cli.md README.md
git commit -m "docs: cli.md config section, README pointer, bump 0.5.0"
```

---

## Phase 5 — Docs + round-trip scripts + real CF round-trip test

### Task 5.1: Write `docs/auth.md`

**Files:**
- Create: `docs/auth.md`

- [ ] **Step 1: Write the file**

```markdown
# Authentication and Identity

irc-lens supports two auth modes, selected by `auth.mode` in the config
file: `dev` (no auth, single synthetic identity) and
`cloudflare-access` (per-user JWT-validated identity behind Cloudflare
Access).

## Identity model

- One `Session` per authenticated principal, opened lazily on first
  request.
- Nick is derived: `<server_name>-<sanitized-local-part>`. Sanitization
  drops everything outside `[a-z0-9-]` from the email's local part (or
  from the service-token common name).
- The principal — `email` for interactive SSO, `common_name` for
  service tokens — keys the registry. Two browsers signed in as the
  same principal share one Session and one IRC connection.

## JWT validation rules

For each authenticated request the lens:

1. Reads the JWT from the `Cf-Access-Jwt-Assertion` header (preferred)
   or the `CF_Authorization` cookie.
2. Looks up the signing key by `kid` against an in-process JWKS cache.
3. On `kid` miss: refreshes the cache once (rate-limited to once every
   5 s) and retries; permanent miss → 401.
4. Verifies signature, audience (`auth.cloudflare.aud`), and issuer
   (`https://<auth.cloudflare.team_domain>`).
5. Reads `email` (or `common_name`) and checks against
   `auth.allowed_emails` / `auth.allowed_service_tokens`.
6. Derives the nick and stashes the `Identity` on `request["identity"]`.

## Dev mode

In `auth.mode: dev`, the same middleware is installed but synthesizes
`Identity(principal=auth.dev.email, nick=auth.dev.nick, ...)` on every
request. Handlers see the same contract as in CF mode.

## Failure modes

| Status | Cause |
| --- | --- |
| 401 | missing or invalid JWT |
| 403 | allowlist denied / Origin mismatch on POST /input |
| 500 | nick derivation produced empty (server-config bug) |
| 502 | JWKS unreachable on first fetch |
| 503 | Session unhealthy / cannot reach AgentIRC |

## Audit log

Every authenticated request emits one structured line on stderr:

    auth=ok principal=<email-or-common-name> nick=<derived> method=<verb> path=<path>

Auth denials log:

    auth=denied principal=<...> reason=<short-tag>

When `--log-json` is passed, the same data goes through the
`_JsonLineFormatter` and lands as one JSON object per line.

## Service tokens

Cloudflare Access supports service tokens for non-interactive callers
(CI, scripts). They authenticate via `CF-Access-Client-Id` and
`CF-Access-Client-Secret` headers; Cloudflare mints a JWT that carries
`common_name` instead of `email`. List the allowed common-names under
`auth.allowed_service_tokens`. Service tokens have no MFA and no SSO
identity; treat them as long-lived secrets.
```

- [ ] **Step 2: Lint**

Run: `markdownlint-cli2 docs/auth.md`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add docs/auth.md
git commit -m "docs(auth): protocol-level reference for the lens auth model"
```

---

### Task 5.2: Write `docs/security-checklist.md`

**Files:**
- Create: `docs/security-checklist.md`

- [ ] **Step 1: Write the file**

```markdown
# Security Checklist — public-facing irc-lens

Before opening your hostname to the internet, walk this list. Every
item is a yes/no.

## Network exposure

- [ ] AgentIRC is bound to `127.0.0.1` (or a private mesh interface),
      never `0.0.0.0`.
- [ ] `irc-lens serve`'s `web.bind` is `127.0.0.1`. CF mode coerces
      this automatically; verify with `ss -lntp | grep 8765`.
- [ ] No firewall rule forwards an inbound port to either service.
      cloudflared is the only reachable surface.

## Cloudflare config

- [ ] `auth.mode: cloudflare-access` in the active config file.
- [ ] `auth.cloudflare.aud` matches the AUD shown in the Access app's
      "Application Audience (AUD) Tag" field.
- [ ] `auth.cloudflare.team_domain` matches your Cloudflare team
      domain (`<team>.cloudflareaccess.com`).
- [ ] `auth.allowed_emails` matches the Access policy's email list.
      Both must say yes for a request to land — defense in depth.
- [ ] The IdP (Google SSO, GitHub, etc.) enforces MFA on every
      account in `auth.allowed_emails`.

## cloudflared

- [ ] cloudflared's tunnel `ingress` rule points to
      `http://localhost:<web.port>`, not `http://0.0.0.0:<web.port>`.
- [ ] cloudflared and irc-lens both run as a non-root user.
- [ ] cloudflared's `tunnel-credentials` file is `chmod 600`.

## Operational

- [ ] Logs are persisted somewhere auditable (systemd journal is
      fine). Review `auth=denied` lines after each test session.
- [ ] `GET /healthz` returns `{"ok": true}` and nothing else; the
      response does not reveal whether any user has a live session.
- [ ] You have a way to rotate the IdP credentials and the CF API
      token without downtime.

## Things to revisit later

These ship in v1 with a known floor; tighten as the deployment grows:

- CSRF defense on `POST /input` is currently Origin/Referer only. See
  [issue #27](https://github.com/agentculture/irc-lens/issues/27)
  for the CSRF-token uplift.
- The CF round-trip test runs only manually / via agent. Automation
  on GitHub Actions is tracked in
  [issue #28](https://github.com/agentculture/irc-lens/issues/28).
- Group-based authorization (using the `groups` JWT claim) is not
  consumed in v1.
```

- [ ] **Step 2: Lint**

Run: `markdownlint-cli2 docs/security-checklist.md`

- [ ] **Step 3: Commit**

```bash
git add docs/security-checklist.md
git commit -m "docs: pre-launch security checklist"
```

---

### Task 5.3: Write `docs/deployment-cloudflare-access.md`

**Files:**
- Create: `docs/deployment-cloudflare-access.md`

- [ ] **Step 1: Write the file** (domain-neutral; placeholders only)

```markdown
# Deploying irc-lens behind Cloudflare Access on your own domain

This runbook walks you through hosting irc-lens at a public hostname
on your own Cloudflare-managed zone, gated by Cloudflare Access. The
result: anyone you allowlist can sign in via your IdP and reach a
private AgentIRC mesh; nobody else can.

## Prerequisites

- A Cloudflare account with `<your-zone>` (e.g. `example.com`)
  managed.
- An IdP set up in Cloudflare Access (Google SSO, GitHub, etc.).
  Cloudflare's dashboard has a wizard.
- `cloudflared` installed on the host machine. See Cloudflare's docs
  for current install instructions.
- `irc-lens` and AgentIRC running (or about to run) on the same host.

## 1. Pick a hostname

Decide on `<your-hostname>` under `<your-zone>` (e.g.
`lens.<your-zone>`). The runbook uses these placeholders below.

## 2. Create the tunnel

    cloudflared tunnel login                   # one-time, browser-based
    cloudflared tunnel create irc-lens

This writes a credentials JSON to `~/.cloudflared/<tunnel-uuid>.json`.

## 3. Route DNS

    cloudflared tunnel route dns irc-lens <your-hostname>

Cloudflare creates a CNAME `<your-hostname>` → `<tunnel-uuid>.cfargotunnel.com`.

## 4. Configure cloudflared

Create `~/.cloudflared/config.yml`:

    tunnel: <tunnel-uuid>
    credentials-file: /home/<user>/.cloudflared/<tunnel-uuid>.json

    ingress:
      - hostname: <your-hostname>
        service: http://localhost:8765
      - service: http_status:404

## 5. Run cloudflared as a service (systemd example)

    sudo cloudflared service install
    sudo systemctl start cloudflared
    sudo systemctl enable cloudflared

## 6. Create the Access application

In the Cloudflare dashboard → Zero Trust → Access → Applications:

- Application type: Self-hosted.
- Application domain: `<your-hostname>`.
- Identity providers: select your configured IdP.
- Add policy: Allow + `Emails` → list the same emails you'll put in
  `auth.allowed_emails`.

Note the **Application Audience (AUD) Tag** — you need this for the
lens config.

## 7. Configure irc-lens

    irc-lens config init

Edit `~/.config/irc-lens/config.yaml`:

    auth:
      mode: cloudflare-access
      cloudflare:
        aud: <your-aud-tag>
        team_domain: <your-team>.cloudflareaccess.com
      allowed_emails:
        - <your-email>
    server:
      name: <agentirc-server-name>
      host: 127.0.0.1
      port: 6667
    web:
      bind: 127.0.0.1
      port: 8765

## 8. Start in this order

1. AgentIRC (so it's ready when the lens connects).
2. `irc-lens serve` (validates JWKS, binds 127.0.0.1:8765).
3. `cloudflared` (already running as systemd unit).

Reverse on shutdown.

## 9. Verify

- Visit `https://<your-hostname>/healthz` → `{"ok": true}`.
- Visit `https://<your-hostname>/` → SSO redirect → lens UI.
- Send a slash-command — it dispatches into AgentIRC under the
  derived nick `<agentirc-server-name>-<sanitized-email-local>`.

## Troubleshooting

- **401 at /** → JWT not reaching the lens. Check cloudflared logs;
  `cloudflared tunnel info irc-lens` should show a healthy edge.
- **403 not on allowlist** → email passed CF Access but is missing
  from `auth.allowed_emails`. Add it and restart.
- **502 on first request** → lens couldn't reach
  `https://<team_domain>/cdn-cgi/access/certs` at startup. Verify
  egress.
- **503 on /input** → AgentIRC is unreachable. Check
  `server.host`/`server.port` and the IRCd's status.

## Security checklist

Walk [security-checklist.md](security-checklist.md) before opening
the hostname to the internet.
```

- [ ] **Step 2: Lint**

Run: `markdownlint-cli2 docs/deployment-cloudflare-access.md`

- [ ] **Step 3: Commit**

```bash
git add docs/deployment-cloudflare-access.md
git commit -m "docs: domain-neutral CF Access deployment runbook"
```

---

### Task 5.4: Update `docs/architecture.md`

**Files:**
- Modify: `docs/architecture.md`

- [ ] **Step 1: Add deployment-modes subsection**

Append to `docs/architecture.md`:

```markdown
## Deployment modes

irc-lens has two operational modes selected by `auth.mode` in the
config:

### `dev` mode

A single `Session` opens at startup against the configured AgentIRC
on `auth.dev.nick`. The identity middleware injects a synthetic
`Identity` for every request. Existing tests run in this mode.

### `cloudflare-access` mode

Each authenticated user gets their own lazy-opened `Session` with a
nick derived from their email's local part (sanitized to
`[a-z0-9-]`). The identity middleware validates the
Cloudflare-issued JWT against the team's JWKS, pins audience and
issuer, and enforces the lens-side allowlist as a second line of
defense behind the Cloudflare Access policy. JWKS is cached
in-process with kid-miss-then-refresh semantics.

The lens never opens an inbound port in CF mode; cloudflared
terminates the tunnel locally and the lens listens on
`127.0.0.1:<web.port>`.
```

- [ ] **Step 2: Lint and commit**

```bash
markdownlint-cli2 docs/architecture.md
git add docs/architecture.md
git commit -m "docs(arch): document dev vs cloudflare-access deployment modes"
```

---

### Task 5.5: Round-trip scripts (`setup.sh`, `teardown.sh`)

**Files:**
- Create: `scripts/cf-roundtrip/setup.sh`
- Create: `scripts/cf-roundtrip/teardown.sh`
- Create: `scripts/cf-roundtrip/README.md`

These scripts use the Cloudflare HTTP API directly (the `gh`-style approach via `curl`) to keep the dependency surface zero beyond `bash`, `curl`, and `jq`.

- [ ] **Step 1: Write `setup.sh`**

```bash
#!/usr/bin/env bash
# Idempotent CF round-trip setup. Re-runs are safe: existing resources
# are reused, only missing ones are created. Prints / writes
# `.cf-roundtrip.env` with values the test reads at runtime.
set -euo pipefail

: "${CF_API_TOKEN:?must be set}"
: "${CF_ACCOUNT_ID:?must be set}"
: "${CF_ZONE_ID:?must be set}"
: "${CF_TEST_HOSTNAME:?must be set}"
: "${CF_TEAM_DOMAIN:?must be set}"

BASE="https://api.cloudflare.com/client/v4"
HDR=(-H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json")
TUNNEL_NAME="irc-lens-roundtrip"
APP_NAME="irc-lens roundtrip"
TOKEN_FILE="${HOME}/.config/irc-lens/cf-roundtrip-token.json"
ENV_OUT="${ENV_OUT:-.cf-roundtrip.env}"

cf_get() { curl -sS "${HDR[@]}" "$BASE$1"; }
cf_post() { curl -sS "${HDR[@]}" -X POST "$BASE$1" -d "$2"; }

echo "[1/5] tunnel" >&2
TUNNEL_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME" \
  | jq -r '.result[0].id // empty')"
if [[ -z "$TUNNEL_ID" ]]; then
  TUNNEL_SECRET="$(openssl rand -base64 32)"
  TUNNEL_ID="$(cf_post "/accounts/$CF_ACCOUNT_ID/cfd_tunnel" \
    "$(jq -n --arg n "$TUNNEL_NAME" --arg s "$TUNNEL_SECRET" \
        '{name:$n, tunnel_secret:$s}')" \
    | jq -r '.result.id')"
fi
echo "  tunnel_id=$TUNNEL_ID" >&2

echo "[2/5] dns" >&2
DNS_ID="$(cf_get "/zones/$CF_ZONE_ID/dns_records?name=$CF_TEST_HOSTNAME" \
  | jq -r '.result[0].id // empty')"
if [[ -z "$DNS_ID" ]]; then
  cf_post "/zones/$CF_ZONE_ID/dns_records" \
    "$(jq -n --arg n "$CF_TEST_HOSTNAME" --arg c "$TUNNEL_ID.cfargotunnel.com" \
        '{type:"CNAME", name:$n, content:$c, proxied:true}')" >/dev/null
fi

echo "[3/5] access app" >&2
APP_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/apps?name=$APP_NAME" \
  | jq -r '.result[0].id // empty')"
if [[ -z "$APP_ID" ]]; then
  APP_ID="$(cf_post "/accounts/$CF_ACCOUNT_ID/access/apps" \
    "$(jq -n --arg n "$APP_NAME" --arg d "$CF_TEST_HOSTNAME" \
        '{name:$n, domain:$d, type:"self_hosted", session_duration:"24h"}')" \
    | jq -r '.result.id')"
fi
echo "  app_id=$APP_ID" >&2

AUD="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID" | jq -r '.result.aud')"

echo "[4/5] service token" >&2
if [[ -f "$TOKEN_FILE" ]]; then
  CLIENT_ID="$(jq -r .client_id "$TOKEN_FILE")"
  CLIENT_SECRET="$(jq -r .client_secret "$TOKEN_FILE")"
else
  TOKEN_JSON="$(cf_post "/accounts/$CF_ACCOUNT_ID/access/service_tokens" \
    "$(jq -n '{name:"irc-lens-roundtrip"}')" | jq -r '.result')"
  CLIENT_ID="$(echo "$TOKEN_JSON" | jq -r .client_id)"
  CLIENT_SECRET="$(echo "$TOKEN_JSON" | jq -r .client_secret)"
  mkdir -p "$(dirname "$TOKEN_FILE")"
  echo "$TOKEN_JSON" >"$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi

echo "[5/5] policy" >&2
EXISTING="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID/policies" \
  | jq -r '.result[] | select(.name=="allow-svc-token") | .id' | head -1)"
if [[ -z "$EXISTING" ]]; then
  cf_post "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID/policies" \
    "$(jq -n --arg cid "$CLIENT_ID" \
        '{name:"allow-svc-token", decision:"non_identity", precedence:1,
          include:[{service_token:{token_id:$cid}}]}')" >/dev/null
fi

cat >"$ENV_OUT" <<EOF
IRC_LENS_TEST_AUD=$AUD
IRC_LENS_TEST_HOSTNAME=$CF_TEST_HOSTNAME
IRC_LENS_TEST_TEAM_DOMAIN=$CF_TEAM_DOMAIN
IRC_LENS_TEST_CLIENT_ID=$CLIENT_ID
IRC_LENS_TEST_CLIENT_SECRET=$CLIENT_SECRET
EOF
echo "wrote $ENV_OUT" >&2
```

- [ ] **Step 2: Write `teardown.sh`**

```bash
#!/usr/bin/env bash
# Remove every resource setup.sh creates. Idempotent (404s ignored).
set -euo pipefail

: "${CF_API_TOKEN:?}"
: "${CF_ACCOUNT_ID:?}"
: "${CF_ZONE_ID:?}"
: "${CF_TEST_HOSTNAME:?}"

BASE="https://api.cloudflare.com/client/v4"
HDR=(-H "Authorization: Bearer $CF_API_TOKEN")

cf_del() { curl -sS "${HDR[@]}" -X DELETE "$BASE$1" || true; }
cf_get() { curl -sS "${HDR[@]}" "$BASE$1"; }

# Service token
TOK_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/service_tokens" \
  | jq -r '.result[] | select(.name=="irc-lens-roundtrip") | .id' | head -1)"
[[ -n "$TOK_ID" ]] && cf_del "/accounts/$CF_ACCOUNT_ID/access/service_tokens/$TOK_ID"

# Access app
APP_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/apps?name=irc-lens%20roundtrip" \
  | jq -r '.result[0].id // empty')"
[[ -n "$APP_ID" ]] && cf_del "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID"

# DNS
DNS_ID="$(cf_get "/zones/$CF_ZONE_ID/dns_records?name=$CF_TEST_HOSTNAME" \
  | jq -r '.result[0].id // empty')"
[[ -n "$DNS_ID" ]] && cf_del "/zones/$CF_ZONE_ID/dns_records/$DNS_ID"

# Tunnel
TUN_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/cfd_tunnel?name=irc-lens-roundtrip" \
  | jq -r '.result[0].id // empty')"
[[ -n "$TUN_ID" ]] && cf_del "/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUN_ID"

rm -f "${HOME}/.config/irc-lens/cf-roundtrip-token.json"
echo "teardown complete" >&2
```

- [ ] **Step 3: Make executable + add README**

Run: `chmod +x scripts/cf-roundtrip/setup.sh scripts/cf-roundtrip/teardown.sh`

Write `scripts/cf-roundtrip/README.md`:

```markdown
# CF round-trip helpers

`setup.sh` provisions (or reuses) a Cloudflare Tunnel, DNS record,
Access application, allow-policy, and service token for the
`@pytest.mark.cloudflare` round-trip test.

## Required env

| Var | Purpose |
| --- | --- |
| `CF_API_TOKEN` | Cloudflare token with Account:Tunnel:Edit, Account:Access:Apps and Policies:Edit, Zone:DNS:Edit on `CF_ZONE_ID` |
| `CF_ACCOUNT_ID` | your Cloudflare account ID |
| `CF_ZONE_ID` | the zone hosting `CF_TEST_HOSTNAME` |
| `CF_TEST_HOSTNAME` | the hostname the test will hit |
| `CF_TEAM_DOMAIN` | `<team>.cloudflareaccess.com` |

## Run

    ./scripts/cf-roundtrip/setup.sh
    set -a; source .cf-roundtrip.env; set +a
    pytest -m cloudflare -v

## Teardown

    ./scripts/cf-roundtrip/teardown.sh
```

- [ ] **Step 4: Commit**

```bash
git add scripts/cf-roundtrip/
git commit -m "feat(scripts): idempotent CF round-trip setup/teardown"
```

---

### Task 5.6: The CF round-trip test itself

**Files:**
- Create: `tests/test_cf_roundtrip.py`

- [ ] **Step 1: Write the test**

```python
"""Real Cloudflare round-trip. Skipped unless setup.sh has run.

Requires: AgentIRC running locally on 127.0.0.1:6667 (or a fixture),
`cloudflared` installed, and the env vars in .cf-roundtrip.env loaded.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import aiohttp
import pytest

pytestmark = pytest.mark.cloudflare

REQUIRED_ENV = (
    "IRC_LENS_TEST_AUD",
    "IRC_LENS_TEST_HOSTNAME",
    "IRC_LENS_TEST_TEAM_DOMAIN",
    "IRC_LENS_TEST_CLIENT_ID",
    "IRC_LENS_TEST_CLIENT_SECRET",
)


def _missing_env() -> str | None:
    for k in REQUIRED_ENV:
        if not os.environ.get(k):
            return k
    return None


@pytest.fixture(scope="module")
def lens_subprocess(tmp_path_factory) -> subprocess.Popen:
    if _missing_env():
        pytest.skip(f"missing env: {_missing_env()}")
    if not shutil.which("cloudflared"):
        pytest.skip("cloudflared not on PATH")

    cfg_dir = tmp_path_factory.mktemp("lens-cf")
    cfg = cfg_dir / "config.yaml"
    cfg.write_text(f"""
auth:
  mode: cloudflare-access
  cloudflare:
    aud: {os.environ['IRC_LENS_TEST_AUD']}
    team_domain: {os.environ['IRC_LENS_TEST_TEAM_DOMAIN']}
  allowed_emails: []
  allowed_service_tokens:
    - {os.environ['IRC_LENS_TEST_CLIENT_ID']}
server:
  name: roundtrip
  host: 127.0.0.1
  port: 6667
web:
  bind: 127.0.0.1
  port: 8765
""")
    proc = subprocess.Popen(
        ["uv", "run", "irc-lens", "serve", "--config", str(cfg)],
        env={**os.environ, "IRC_LENS_TEST_HOOKS": "1"},
    )
    try:
        # Wait for local /healthz before yielding.
        for _ in range(30):
            if _local_healthz():
                break
            time.sleep(1)
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _local_healthz() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


async def _get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status, await r.read()


async def _post(url: str, headers: dict[str, str], data: dict) -> int:
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, data=data,
                          timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status


@pytest.mark.asyncio
async def test_cloudflare_round_trip(lens_subprocess) -> None:
    host = os.environ["IRC_LENS_TEST_HOSTNAME"]
    base = f"https://{host}"
    auth_headers = {
        "CF-Access-Client-Id": os.environ["IRC_LENS_TEST_CLIENT_ID"],
        "CF-Access-Client-Secret": os.environ["IRC_LENS_TEST_CLIENT_SECRET"],
    }

    # Wait until the public hostname starts answering — DNS may need to
    # settle on first run, and cloudflared takes a few seconds to come up.
    deadline = time.time() + 60
    while time.time() < deadline:
        status, _ = await _get(f"{base}/healthz", headers=auth_headers)
        if status == 200:
            break
        await asyncio.sleep(2)
    else:
        pytest.fail("public /healthz never came up")

    # Unauthenticated request must be blocked at the CF edge.
    status_unauth, _ = await _get(f"{base}/", headers={})
    assert status_unauth in (401, 302)

    # Service-token call lands on the lens.
    status_auth, body = await _get(f"{base}/", headers=auth_headers)
    assert status_auth == 200
    assert b"<html" in body or b"<!DOCTYPE" in body

    # POST /input via service token. With AgentIRC offline the response
    # may be 503; that still proves the auth path landed in the handler.
    status_post = await _post(
        f"{base}/input",
        headers={**auth_headers, "Origin": base},
        data={"text": "/help"},
    )
    assert status_post in (204, 503)
```

- [ ] **Step 2: Smoke run (only if env is loaded)**

If you've run `setup.sh` and have AgentIRC + cloudflared up:

```bash
set -a; source .cf-roundtrip.env; set +a
uv run pytest -m cloudflare -v
```

Expected: 1 PASS in 30–60 s. If skipped, the env vars aren't loaded — that's fine; the test must skip cleanly outside the setup.

- [ ] **Step 3: Run the default suite to confirm the test stays opt-in**

Run: `uv run pytest -v`
Expected: `test_cf_roundtrip` is deselected by default (per `addopts = "-m 'not playwright and not cloudflare'"`).

- [ ] **Step 4: Commit**

```bash
git add tests/test_cf_roundtrip.py
git commit -m "test(cf): real Cloudflare round-trip via service token"
```

---

## Final integration

After all phases:

- [ ] **Run the full test matrix**

```bash
uv run pytest -v                          # default
uv run pytest -m playwright -v            # browser e2e
uv run pytest -m slow -v                  # rotation test
# CF round-trip is operator-driven:
# uv run pytest -m cloudflare -v
```

- [ ] **Walk the security checklist** against your real deployment.

- [ ] **PR opening**: each phase corresponds to one PR. Use the existing pr-review skill (`/pr-review`) if available; otherwise `gh pr create` per project conventions.

---

## Spec-coverage cross-check

| Spec section | Tasks |
| --- | --- |
| Configuration surface | T1.1 (deps/marker), T1.2–T1.3 (loader), T1.4–T1.5 (init verb), T4.5 (cli docs) |
| Auth and identity | T2.1–T2.2 (Identity/derive_nick), T3.1–T3.5 (middleware + JWT), T3.7 (audit log), T5.1 (auth.md) |
| Per-user Session registry | T2.3–T2.4 (registry), T2.5 (make_app rewrite), T2.6 (serve wiring) |
| Routes (`/`, `/input`, `/events`, `/healthz`) | T2.5 (registry use + /healthz), T4.2 (/healthz tests), T4.3 (Origin floor) |
| Testing tiers | unit (T1.2, T2.1, T2.3, T3.5, T4.1), integration (T3.2, T3.4, T3.7, T4.2–T4.3), real-CF (T5.6) |
| Documentation | T4.5 (cli.md, README), T5.1 (auth.md), T5.2 (security-checklist), T5.3 (deployment runbook), T5.4 (architecture.md) |
| Round-trip scripts | T5.5 |
| Rollout phases | Phase 1 → Phase 5 mapping at the top |
| Compatibility notes | T2.5/T2.6 preserve dev-mode behavior; T4.4 documents the one breaking change (config file required) |
