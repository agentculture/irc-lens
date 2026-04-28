"""Cited from culture@57d3ba8: culture/console/commands.py.

Byte-faithful copy. The slash-command parser is console-specific (never
needed by an agent loop), which is why it lives in
``culture/console/`` upstream rather than under ``packages/agent-harness/``.
The lens consumes the same surface — `parse_command()` returns
``ParsedCommand`` objects that ``Session.execute()`` will dispatch on
in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class CommandType(Enum):
    CHAT = auto()
    CHANNELS = auto()
    JOIN = auto()
    PART = auto()
    WHO = auto()
    SEND = auto()
    READ = auto()
    OVERVIEW = auto()
    STATUS = auto()
    AGENTS = auto()
    START = auto()
    STOP = auto()
    RESTART = auto()
    ICON = auto()
    TOPIC = auto()
    KICK = auto()
    INVITE = auto()
    SERVER = auto()
    QUIT = auto()
    HELP = auto()
    # irc-lens additions: SWITCH is a pure view-state verb (no IRC
    # side-effect) used by the clickable sidebar; ME is CTCP ACTION.
    SWITCH = auto()
    ME = auto()
    UNKNOWN = auto()


@dataclass
class ParsedCommand:
    type: CommandType
    args: list[str] = field(default_factory=list)
    text: str = ""


# Commands where trailing words after args form free text
_TEXT_COMMANDS = {
    "send": (CommandType.SEND, 1),  # /send <target> <text...>
    "topic": (CommandType.TOPIC, 1),  # /topic <channel> <text...>
    "me": (CommandType.ME, 0),  # /me <text...> — CTCP ACTION
}

# Simple commands: name -> type
_COMMANDS: dict[str, CommandType] = {
    "channels": CommandType.CHANNELS,
    "join": CommandType.JOIN,
    "part": CommandType.PART,
    "who": CommandType.WHO,
    "read": CommandType.READ,
    "overview": CommandType.OVERVIEW,
    "status": CommandType.STATUS,
    "agents": CommandType.AGENTS,
    "start": CommandType.START,
    "stop": CommandType.STOP,
    "restart": CommandType.RESTART,
    "icon": CommandType.ICON,
    "kick": CommandType.KICK,
    "invite": CommandType.INVITE,
    "server": CommandType.SERVER,
    "quit": CommandType.QUIT,
    "help": CommandType.HELP,
    "switch": CommandType.SWITCH,
}


def parse_command(input_text: str) -> ParsedCommand:
    """Parse user input into a command or chat message."""
    stripped = input_text.strip()
    if not stripped:
        return ParsedCommand(type=CommandType.CHAT, text="")

    if not stripped.startswith("/"):
        return ParsedCommand(type=CommandType.CHAT, text=stripped)

    parts = stripped[1:].split()
    if not parts:
        return ParsedCommand(type=CommandType.CHAT, text=stripped)

    cmd_name = parts[0].lower()
    rest = parts[1:]

    # Text commands: split at boundary, rest is free text
    if cmd_name in _TEXT_COMMANDS:
        cmd_type, arg_count = _TEXT_COMMANDS[cmd_name]
        args = rest[:arg_count]
        text = " ".join(rest[arg_count:])
        return ParsedCommand(type=cmd_type, args=args, text=text)

    # Regular commands
    if cmd_name in _COMMANDS:
        return ParsedCommand(type=_COMMANDS[cmd_name], args=rest)

    return ParsedCommand(type=CommandType.UNKNOWN, text=stripped)
