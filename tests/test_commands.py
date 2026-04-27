"""Unit tests for the cited slash-command parser.

Phase 2 seeded a smoke suite to guard the citation; Phase 9a (build
plan line 264) extends it to the full ``CommandType`` round-trip
contract. The parametrized tests at the bottom iterate the verb→type
dicts directly so adding a new verb to ``commands.py`` automatically
gets coverage — no per-verb test maintenance required.
"""

from __future__ import annotations

import pytest

from irc_lens.commands import (
    _COMMANDS,
    _TEXT_COMMANDS,
    CommandType,
    ParsedCommand,
    parse_command,
)


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


# ---------------------------------------------------------------------------
# Phase 9a: parametrized round-trip across every entry of the verb dicts.
# Importing the dicts directly ties the test to the parser's source of truth,
# so a new verb gets coverage automatically without editing this file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb,expected_type", sorted(_COMMANDS.items()))
def test_parse_simple_command_round_trip(verb: str, expected_type: CommandType) -> None:
    parsed = parse_command(f"/{verb}")
    assert parsed.type is expected_type
    assert parsed.args == []


@pytest.mark.parametrize("verb,expected_type", sorted(_COMMANDS.items()))
def test_parse_simple_command_carries_args(verb: str, expected_type: CommandType) -> None:
    parsed = parse_command(f"/{verb} arg1 arg2")
    assert parsed.type is expected_type
    assert parsed.args == ["arg1", "arg2"]


@pytest.mark.parametrize("verb,spec", sorted(_TEXT_COMMANDS.items()))
def test_parse_text_command_splits_at_arg_boundary(
    verb: str, spec: tuple[CommandType, int]
) -> None:
    expected_type, arg_count = spec
    args = [f"a{i}" for i in range(arg_count)]
    text_words = ["free", "form", "trailing", "text"]
    parsed = parse_command(f"/{verb} " + " ".join(args + text_words))
    assert parsed.type is expected_type
    assert parsed.args == args
    assert parsed.text == " ".join(text_words)


def test_parse_command_is_case_insensitive_on_verb() -> None:
    """Pin the .lower() in parse_command — uppercase slashes still parse."""
    parsed = parse_command("/JOIN #ops")
    assert parsed.type is CommandType.JOIN
    assert parsed.args == ["#ops"]


def test_parse_lone_slash_is_chat() -> None:
    """`/` alone is treated as chat text per the parser's split guard."""
    parsed = parse_command("/")
    assert parsed.type is CommandType.CHAT
    assert parsed.text == "/"


def test_parse_chat_strips_surrounding_whitespace() -> None:
    parsed = parse_command("   hello   ")
    assert parsed.type is CommandType.CHAT
    assert parsed.text == "hello"
