"""Smoke tests for the cited slash-command parser.

The full coverage sweep across every ``CommandType`` lands in Phase 9a's
``test_commands.py``. This file exists so Phase 2 has an executable
import-guard: if the citation breaks, pytest fails immediately rather
than waiting until Session.execute() lights up in Phase 3.
"""

from __future__ import annotations

from irc_lens.commands import CommandType, ParsedCommand, parse_command


def test_parse_chat_no_slash() -> None:
    parsed = parse_command("hello world")
    assert parsed.type is CommandType.CHAT
    assert parsed.text == "hello world"


def test_parse_join_simple() -> None:
    parsed = parse_command("/join #ops")
    assert parsed.type is CommandType.JOIN
    assert parsed.args == ["#ops"]


def test_parse_send_with_trailing_text() -> None:
    parsed = parse_command("/send alice hello there friend")
    assert parsed.type is CommandType.SEND
    assert parsed.args == ["alice"]
    assert parsed.text == "hello there friend"


def test_parse_topic_with_trailing_text() -> None:
    parsed = parse_command("/topic #ops new project topic")
    assert parsed.type is CommandType.TOPIC
    assert parsed.args == ["#ops"]
    assert parsed.text == "new project topic"


def test_parse_unknown_slash_command() -> None:
    parsed = parse_command("/wat")
    assert parsed.type is CommandType.UNKNOWN
    assert parsed.text == "/wat"


def test_parse_empty_input() -> None:
    parsed = parse_command("")
    assert parsed.type is CommandType.CHAT
    assert parsed.text == ""


def test_parsed_command_dataclass_fields() -> None:
    pc = ParsedCommand(type=CommandType.HELP)
    assert pc.args == []
    assert pc.text == ""
