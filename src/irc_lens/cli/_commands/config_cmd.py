"""`irc-lens config` noun group: init + overview verbs."""
from __future__ import annotations

import argparse
from pathlib import Path

from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError
from irc_lens.cli._output import emit_diagnostic
from irc_lens.config import default_config_path

_STARTER = """\
# irc-lens — local dev config
# Written by `irc-lens config init`. See the spec in docs/superpowers/specs/ for the full schema.

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

media:
  enabled: true
  # dir: ~/.local/share/irc-lens/media    # default: $XDG_DATA_HOME/irc-lens/media or ~/.local/share/irc-lens/media
  # max_file_bytes: 10485760              # default: 10 MiB
  # max_store_bytes: 268435456            # default: 256 MiB
  # public_base_url: ""                   # default: derive from web.bind/web.port at use time
  remote_embeds: click                    # click, auto, or off
  # trusted_hosts: []                     # default: empty
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
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_STARTER)
    except OSError as exc:
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"could not write config to {target}: {exc}",
            remediation="check directory permissions or pick a different --path",
        ) from exc
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
