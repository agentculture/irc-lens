"""Pin the negative-case contract for the ``serve`` and ``config`` host
commands registered via ``app.add_command`` (t4 of the
adopt-agentfront-across-cli-and-site plan).

Both commands are wired through agentfront's ``App.add_command`` with a
``configure`` hook (see ``irc_lens.cli._commands.serve.register_into`` and
``irc_lens.cli._commands.config_cmd.register_into``), which inherits
agentfront's ``_ArgumentParser`` override. This module exists specifically
to pin that an unknown flag/verb on either command still routes through the
``error:``/``hint:`` contract with exit code 1 and no Python traceback —
the acceptance criterion doesn't rely on ``tests/test_serve_cli.py`` or
``tests/test_config_init.py`` for this (those files are frozen), so the
negative case gets its own home here.
"""
from __future__ import annotations

import pytest

from irc_lens.cli import main


def test_serve_bogus_flag_exits_one_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`irc-lens serve --bogus-flag` must exit 1 with error:/hint: on stderr
    and no traceback — the argparse-level failure never reaches `cmd_serve`."""
    rc = main(["serve", "--bogus-flag"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_config_bogus_verb_exits_one_with_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`irc-lens config bogus-verb` must exit 1 with error:/hint: on stderr
    and no traceback — the config noun's subparsers reject an unknown verb
    via the same structured-error parser as the top-level dispatcher."""
    rc = main(["config", "bogus-verb"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert "Traceback" not in err
