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
    # side-effect) used by the clickable sidebar; ME is CTCP ACTION;
    # MESH switches to the live agent-mesh graph view.
    SWITCH = auto()
    ME = auto()
    MESH = auto()
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
    "mesh": CommandType.MESH,
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


# ---------------------------------------------------------------------------
# Tier-aware command surface (guest-mode chat UI, task t13)
# ---------------------------------------------------------------------------
# irc-lens addition (not part of the cited upstream parser). One table drives
# three things so they cannot drift: the inline command palette, the help
# pane, and the server-side guest allowlist in ``POST /input``.

#: Command types a sandbox session (guest, or an approved user viewing the
#: sandbox) may run. Everything else is refused before it reaches IRC:
#: agent-control and mesh commands (/kick /start /stop /restart /invite
#: /server /icon /topic /send /part /join /channels /agents /mesh /switch
#: /overview /status /quit) plus anything unrecognised. ``CHAT`` is plain text.
SANDBOX_ALLOWED = frozenset(
    {
        CommandType.CHAT,
        CommandType.HELP,
        CommandType.WHO,
        CommandType.ME,
        CommandType.READ,
    }
)

#: Command name typed for the sandbox toggle (approved users on the mesh).
SANDBOX_COMMAND = "/sandbox"


@dataclass(frozen=True)
class PaletteEntry:
    command: str  # as typed, e.g. "/help"
    label: str  # 1-3 word description
    sandbox: bool  # also available inside the sandbox
    href: str = ""  # non-empty: a page link, not a slash command


PALETTE: tuple[PaletteEntry, ...] = (
    PaletteEntry("/help", "Help", True),
    PaletteEntry("/who", "People here", True),
    PaletteEntry("/me", "Action", True),
    PaletteEntry("/read", "Earlier messages", True),
    PaletteEntry("/join", "Open a room", False),
    PaletteEntry("/channels", "All rooms", False),
    PaletteEntry("/agents", "All agents", False),
    PaletteEntry("/mesh", "Live map", False),
    PaletteEntry("/residents", "Who is busy", False, href="/residents"),
    PaletteEntry(SANDBOX_COMMAND, "Guest view", False),
)


def palette_for(tier: str, *, sandbox_toggle: bool = False) -> list[PaletteEntry]:
    """Commands to show for *tier* (``approved`` | ``guest`` | ``sandbox_preview``).

    Sandbox sessions (guests and approved users in the sandbox view) only see
    commands that work there. ``/sandbox`` is offered only to approved users
    on the real mesh, and only while guest mode is on (*sandbox_toggle*).
    """
    if tier != "approved":
        return [e for e in PALETTE if e.sandbox]
    return [e for e in PALETTE if e.command != SANDBOX_COMMAND or sandbox_toggle]


#: Rarer commands shown only in the help pane (real-mesh tier).
_HELP_EXTRA: tuple[PaletteEntry, ...] = (
    PaletteEntry("/part", "Leave room", False),
    PaletteEntry("/send", "Message elsewhere", False),
    PaletteEntry("/switch", "Change room", False),
    PaletteEntry("/topic", "Room topic", False),
    PaletteEntry("/icon", "Set icon", False),
    PaletteEntry("/overview", "Joined rooms", False),
    PaletteEntry("/status", "Connection", False),
)


def help_for(tier: str, *, sandbox_toggle: bool = False) -> list[PaletteEntry]:
    """Help-pane rows: the palette, plus rarer commands for real-mesh users."""
    rows = palette_for(tier, sandbox_toggle=sandbox_toggle)
    return rows + list(_HELP_EXTRA) if tier == "approved" else rows


def allowed_in_sandbox(parsed: ParsedCommand) -> bool:
    """True iff *parsed* may run in a sandbox session."""
    return parsed.type in SANDBOX_ALLOWED
