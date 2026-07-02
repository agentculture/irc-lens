"""``irc-lens tui`` — the terminal loop over agentfront's ``LiveDriver`` (t9).

agentfront renders four surfaces off one ``App`` registry — CLI, MCP, HTTP,
TAUI — but for TAUI it ships only the *state machine* (``Session``,
``reduce``, the ANSI/markdown/JSON renderers): "agentfront ships no runnable
loop; the host supplies it" (build-plan risk r4). This module is that loop.
It is deliberately small: a host command that either

* prints a static front and returns immediately (no TTY — the common case
  under CI, a pipe, or a redirected file), or
* opens exactly one :class:`agentfront.taui.session.Session`, wraps it in a
  :class:`agentfront.taui.driver.LiveDriver`, and shuttles real keystrokes
  into it until the human quits.

Non-TTY behaviour (pinned by the plan's acceptance criteria)
--------------------------------------------------------------
When either ``stdin`` or ``stdout`` is not a TTY, :func:`cmd_tui` never
touches terminal mode: it writes one hint line to stderr, then the SAME
markdown front the HTTP ``/front`` route serves
(``agentfront.taui.render.markdown.render_markdown(app.taui())`` — see
``agentfront.serve.http_front_agrees``, which this repo's own
``tests/test_front_agreement.py`` already pins) to stdout, and returns 0.
Reusing that renderer rather than hand-authoring separate help prose means
the non-interactive path can never drift from what a peer fetching
``/agent/front`` (t7) or a human at a real terminal sees — it is the exact
same registry-derived content, just a different tier.

Raw-mode loop (TTY only)
--------------------------
At a real TTY, :func:`_run_raw_loop` puts the terminal in cbreak/raw mode
(``termios``/``tty``, stdlib only) and reads one byte at a time. Three
byte shapes matter:

* ``\\x03`` (Ctrl-C) and the literal ``q`` map to LiveDriver's own quit key
  (``"q"``) — ``LiveDriver.feed_key("q")`` always sets ``running = False``,
  even with a blocking popup up, per its own no-quit-trap contract.
* ``\\r`` / ``\\n`` map to the logical key ``"enter"`` (only meaningful when
  a visible popup binds it to an action — see ``LiveDriver``'s popup-action
  routing; plain navigation ignores it in TAUI v1).
* ``\\x1b`` (ESC) is either a lone Escape keypress or the first byte of an
  arrow-key escape sequence (``ESC [ A``/``B``/``C``/``D``). Telling those
  apart from a blocking ``read(1)`` alone is impossible — a lone Escape
  sends exactly one byte and nothing more ever arrives — so
  :func:`_read_key` peeks with ``select.select([...], [...], [...],
  timeout)`` for a few milliseconds after the ESC byte before deciding: no
  more bytes within the window means a lone ``"esc"``; ``[`` followed by a
  direction letter maps to ``"up"``/``"down"``/``"left"``/``"right"``.

Every other byte is forwarded to :meth:`LiveDriver.feed_key` verbatim; the
reducer treats an unrecognised key as a no-op (see
``agentfront.taui.reducer._reduce_key``), so this never raises. The
terminal is restored in a ``finally`` no matter how the loop exits —
including an exception, which propagates up to
``agentfront.cli_surface.run_cli``'s own dispatcher (already responsible
for turning an unexpected exception into a structured, traceback-free
error — see ``irc_lens/cli/__init__.py``'s module docstring).

Tool execution
----------------
Dispatching a tool from the TUI goes through
``LiveDriver.dispatch``/``Session.dispatch`` — the exact same path
``agentfront.testing.drive`` and a live agent both use (see
``tools_live`` / ``tests/test_tui_cmd.py`` for the parity proof against the
plain CLI). The interactive keyboard loop above never calls ``dispatch``
itself: TAUI v1's reducer has no keybinding that turns a highlighted panel
item into a tool call outside of a popup's own bound action (see
``agentfront.taui.reducer``'s "'enter' and any other key -> no-op for v1"
navigation-only note) — inventing one here would be host-side behaviour
agentfront does not sanction anywhere else, so this module does not add it.
Tool dispatch is reachable from a human TUI session exactly as far as
agentfront's own reducer already takes it (popup actions); broader
in-loop command entry is out of scope for this task (build-plan risk r4:
"keep it last-wave and independently mergeable").
"""

from __future__ import annotations

import argparse
import select
import sys
from typing import TYPE_CHECKING, TextIO

from agentfront.taui.driver import LiveDriver
from agentfront.taui.session import Session

from irc_lens.cli._output import emit_diagnostic

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentfront.app import App

_TUI_HELP = (
    "Open the terminal UI (TAUI): a live, keyboard-driven view over the same "
    "App registry the CLI/MCP/HTTP surfaces share."
)

_NON_TTY_HINT = (
    "irc-lens tui needs an interactive terminal (stdin and stdout must both "
    "be a TTY) — printing the front instead of entering the raw-mode loop."
)

#: Arrow-key final byte -> logical key name, per the reducer's own
#: "down"/"up" vocabulary (left/right have no reducer effect yet but are
#: forwarded anyway — an unrecognised key is a documented no-op).
_ARROW_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left"}

#: How long to wait, after an ESC byte, for the rest of an arrow-key escape
#: sequence before concluding it was a lone Escape keypress. Local-terminal
#: escape sequences arrive as one uninterrupted burst, so this only needs to
#: be long enough to never mistake a slow terminal emulator for "no more
#: bytes coming" — 50ms is generous for that and still imperceptible to a
#: human.
_ESCAPE_PEEK_SECONDS = 0.05

_CLEAR_SCREEN = "\x1b[2J\x1b[H"


def _has_pending_byte(stream: TextIO, timeout: float) -> bool:
    """True if *stream* has at least one byte ready to read within *timeout*s."""
    ready, _, _ = select.select([stream], [], [], timeout)
    return bool(ready)


def _read_key(stream: TextIO) -> str:
    """Read one logical key from *stream*, resolving escape sequences.

    Returns ``"q"`` for Ctrl-C or literal ``q``/EOF, ``"enter"`` for CR/LF,
    ``"esc"``/``"up"``/``"down"``/``"left"``/``"right"`` for a lone Escape or
    a recognised arrow sequence, and the raw character otherwise.
    """
    ch = stream.read(1)
    if ch == "" or ch == "\x03":  # EOF or Ctrl-C — never spin, just quit
        return "q"
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "q":
        return "q"
    if ch != "\x1b":
        return ch
    # Possible arrow-key escape sequence (`ESC [ <letter>`). A LONE Escape
    # keypress sends only this one byte with nothing to follow — peeking
    # with a short timeout (rather than a blocking read) is what lets the
    # two cases be told apart without ever hanging.
    if not _has_pending_byte(stream, _ESCAPE_PEEK_SECONDS):
        return "esc"
    ch2 = stream.read(1)
    if ch2 != "[" or not _has_pending_byte(stream, _ESCAPE_PEEK_SECONDS):
        return "esc"
    ch3 = stream.read(1)
    return _ARROW_KEYS.get(ch3, "esc")


def _paint(stdout: TextIO, frame: str) -> None:
    """Clear the screen and draw *frame*.

    Raw mode disables output post-processing (``OPOST``), so a bare ``\\n``
    would not return the cursor to column 0 (a "staircase" effect) —
    ``\\r\\n`` is written explicitly instead.
    """
    stdout.write(_CLEAR_SCREEN)
    stdout.write(frame.replace("\n", "\r\n"))
    stdout.flush()


def _run_raw_loop(driver: LiveDriver, stdin: TextIO, stdout: TextIO) -> None:
    """Enter cbreak/raw mode and shuttle keystrokes into *driver* until quit.

    The terminal is ALWAYS restored (``termios.tcsetattr`` in ``finally``),
    even if the loop raises — a bug in ``_read_key``/``feed_key`` must never
    leave the caller's shell in raw mode.
    """
    import termios
    import tty

    fd = stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        _paint(stdout, driver.render())
        while driver.running:
            key = _read_key(stdin)
            frame = driver.feed_key(key)
            _paint(stdout, frame)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        stdout.write("\r\n")
        stdout.flush()


def _print_front(app: "App") -> None:
    """Print the hint + registry-derived front for a non-interactive stdin/stdout."""
    emit_diagnostic(_NON_TTY_HINT)
    from agentfront.taui.render.markdown import render_markdown

    front = render_markdown(app.taui())
    sys.stdout.write(front if front.endswith("\n") else front + "\n")


def cmd_tui(args: argparse.Namespace) -> int:
    """Enter the TAUI loop at a real TTY; print the front and return 0 otherwise."""
    from irc_lens.cli import build_app

    app = build_app()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _print_front(app)
    else:
        session = Session(app)
        driver = LiveDriver(session)
        _run_raw_loop(driver, sys.stdin, sys.stdout)

    return 0


def _configure_tui(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Open the terminal UI (TAUI): a live, keyboard-driven view over the "
        "same App registry the CLI, MCP, and HTTP surfaces share. Requires a "
        "real TTY on both stdin and stdout; a piped or non-interactive "
        "invocation prints the registry front instead of entering raw mode."
    )
    p.epilog = (
        "keys:\n"
        "  up / down    move focus between panel items\n"
        "  esc          dismiss the topmost popup (or a lone Escape, no-op)\n"
        "  enter        activate a popup's bound action, if one is focused\n"
        "  q / Ctrl-C   quit, restoring the terminal\n"
    )
    p.formatter_class = argparse.RawDescriptionHelpFormatter


def register_into(app) -> None:
    """Register ``tui`` as a host command on the agentfront App."""
    app.add_command(
        "tui",
        handler=cmd_tui,
        help=_TUI_HELP,
        configure=_configure_tui,
    )
