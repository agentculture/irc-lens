"""Task t5 — the live-verb catalog: ephemeral-session registry tools.

Every :class:`~irc_lens.commands.CommandType` verb the web console wires
into :attr:`~irc_lens.session.Session._exec_dispatch` gets a matching
agentfront tool here, so the same capability a human drives through the
browser console is also reachable as ``irc-lens <verb>`` / an MCP ``run``
call / (eventually) the TAUI surface — one registry, every surface, per
the agentfront adoption plan.

Ephemeral lifecycle (decision c29)
-----------------------------------
A live verb here does **not** share the long-lived ``Session`` `serve`
holds open for a browser tab. Each tool call opens its own throwaway
``Session`` against the server named by ``LensConfig`` (``--config`` or
the default path, via :func:`irc_lens.config.resolve_config` — the same
convention `serve` uses), performs exactly one verb, and disconnects in a
``finally``. That means:

* No state persists between two tool calls — a second call cannot see
  channels the first call joined. Verbs that need a joined channel as a
  precondition (``switch``, and ``mesh``'s channel list) join it
  themselves first, in the same ephemeral session, before doing the verb
  those channels are for.
* Read/history fidelity is bounded by what the server replays on that
  single fresh connection (accepted for v1 per c29) — there is no
  client-side backlog to fall back on the way a long-running browser
  session accumulates one.
* Tools require ``auth.mode: dev`` — a live verb has no per-request JWT
  to derive an identity from the way the web console's cloudflare-access
  mode does, so it borrows the config's one fixed ``auth.dev`` identity.
  cloudflare-access configs raise a clear ``AfiError`` rather than
  guessing an identity.

Query verbs vs. write verbs
----------------------------
``channels``/``who``/``read``/``mesh`` call the same **query methods**
``Session._exec_channels``/``_exec_who``/``_exec_read``/``_exec_mesh``
call internally (``list_channels()``/``who()``/``history()``/
``build_mesh_snapshot()``) rather than going through ``Session.execute``.
That is deliberate, not a shortcut: the ``_exec_*`` handlers only ever
*publish* an HTML-rendered SSE fragment (there is nobody subscribed to
publish to in an ephemeral session, and no way to recover structured data
from a rendered fragment even if there were) — the query methods are
where the actual structured data ``--json`` needs lives, and calling them
directly is *exactly* what the console handler itself does one line
later. ``send``/``join``/``part``/``switch``/``topic``/``me``/``icon`` are
write verbs with no such alternative: :meth:`Session.execute` (dispatching
the very same :class:`~irc_lens.commands.ParsedCommand` shape ``POST
/input`` builds) is the literal "same effect a console ``/command`` has"
path, so those seven route through it.

Failure mapping
-----------------
* Missing/bad ``--config`` → whatever :func:`irc_lens.config.resolve_config`
  raises (``EXIT_USER_ERROR``, hint points at ``irc-lens config init``).
* ``auth.mode: cloudflare-access`` → ``EXIT_USER_ERROR`` (no identity to
  borrow — see above).
* Cannot reach / register with AgentIRC at all (connect failure or a
  failed/rejected welcome) → ``EXIT_ENV_ERROR``: the *environment*
  (a configured, presumably-running server) failed to deliver a resource
  that should have been there. Mirrors the exit-code policy's "code 2 =
  environment failed to deliver an existing resource" (see
  ``feedback_exit_code_policy`` in project memory) rather than `serve`'s
  own historical choice of ``EXIT_USER_ERROR`` for the same failure —
  `serve` is a different command with its own established contract
  (guarded by its own tests) that this task does not touch.
* The connection drops mid-verb (``LensConnectionLost`` from a send path)
  → ``EXIT_ENV_ERROR`` for the same reason.
* A verb's own validation rejects the input (bad channel format, missing
  target/text — the exact same checks ``Session._exec_*`` already makes
  and publishes as an ``error`` SSE event) → ``EXIT_USER_ERROR``. Since
  nobody is subscribed to the ephemeral session's event bus,
  :func:`_execute_checked` subscribes *before* calling ``execute()`` so it
  can inspect what would have been published and raise the equivalent
  ``AfiError`` instead of silently "succeeding" at a verb that actually
  failed.

Excluded ``CommandType`` verbs
--------------------------------
``Session._exec_dispatch`` (the *same introspection point*
``tests/test_tools_catalog.py`` reads to build its diff) has more entries
than this module registers tools for. Every gap is a deliberate,
documented exclusion — see :data:`NON_TOOL_DISPATCH_VERBS` — never an
oversight; the catalog test asserts registered top-level tool names equal
``dispatch table keys - NON_TOOL_DISPATCH_VERBS`` exactly, so a verb that
is neither registered nor excluded fails the test rather than silently
going missing.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from irc_lens._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError
from irc_lens.commands import CommandType, ParsedCommand
from irc_lens.config import LensConfig, resolve_config
from irc_lens.session import LensConnectionLost, Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentfront.app import App

__all__ = [
    "NON_TOOL_DISPATCH_VERBS",
    "register_into",
    "send",
    "join",
    "part",
    "read",
    "channels",
    "who",
    "mesh",
    "switch",
    "topic",
    "me",
    "icon",
]

#: ``CommandType`` members present in ``Session._exec_dispatch`` that
#: deliberately do NOT get a registry tool here. Each entry is a reasoned
#: exclusion:
#:
#: * ``CHAT`` — not a nameable ``/verb``; it is ``commands.parse_command``'s
#:   implicit fallback for bare (no leading ``/``) text, and it only makes
#:   sense against a persisted ``current_channel`` an ephemeral one-shot
#:   session never has. ``send`` already covers "post a message to an
#:   explicit target" without that precondition.
#: * ``HELP`` / ``STATUS`` — pure browser-UI view switches
#:   (``Session._switch_view``) with no IRC side effect and nothing
#:   meaningful to return outside a live ``#info`` pane.
#: * ``OVERVIEW`` — same reasoning as HELP/STATUS, and additionally
#:   collides by name with agentfront's own reserved ``overview``
#:   meta-verb (``App._RESERVED_META_VERBS``) — registering a top-level
#:   tool named ``overview`` would break CLI parser construction outright.
#: * ``AGENTS`` — a sidebar aggregation convenience (union of ``WHO``
#:   across every *currently joined* channel) built for a live browser
#:   session; an ephemeral session's only joined channels are the ones
#:   the very same call joins, so it can add nothing beyond calling
#:   ``who`` on an explicit channel — already covered by ``who``/``mesh``.
#: * ``UNKNOWN`` — the parser's parse-failure sentinel, not a real verb.
NON_TOOL_DISPATCH_VERBS: frozenset[CommandType] = frozenset(
    {
        CommandType.CHAT,
        CommandType.HELP,
        CommandType.OVERVIEW,
        CommandType.STATUS,
        CommandType.AGENTS,
        CommandType.UNKNOWN,
    }
)


# ---------------------------------------------------------------------------
# Ephemeral connect / execute / disconnect plumbing
# ---------------------------------------------------------------------------


async def _connect(cfg: LensConfig) -> Session:
    """Open one throwaway ``Session`` against *cfg*'s AgentIRC server.

    Mirrors ``serve.py``'s ``_connect_dev_session`` (connect →
    ``wait_for_welcome``) but maps failures to ``EXIT_ENV_ERROR`` per this
    module's own policy (see the module docstring's "Failure mapping"
    section) rather than reusing `serve`'s ``EXIT_USER_ERROR`` choice for
    the same failure.
    """
    if cfg.auth_mode != "dev":
        raise AfiError(
            code=EXIT_USER_ERROR,
            message=f"live-verb tools require auth.mode: dev (got {cfg.auth_mode!r})",
            remediation=(
                "live tools connect with one fixed identity from `auth.dev` — "
                "cloudflare-access mode has no CLI-invocable identity to borrow "
                "(each web session there is tied to a browser-authenticated JWT)"
            ),
        )
    session = Session(host=cfg.server_host, port=cfg.server_port, nick=cfg.dev_nick or "")
    try:
        await session.connect()
        await session.wait_for_welcome()
    except LensConnectionLost as exc:
        await session.disconnect()
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"cannot reach AgentIRC at {cfg.server_host}:{cfg.server_port}: {exc}",
            remediation=(
                "verify the AgentIRC server is running and reachable, then retry "
                "— e.g. `culture server status <name>`"
            ),
        ) from exc
    return session


@asynccontextmanager
async def _live_session(config: str | None) -> AsyncIterator[Session]:
    """Resolve config, connect, yield the session, always disconnect.

    A ``LensConnectionLost`` raised while the caller's body runs (a send
    path hitting a broken pipe mid-verb) is translated to ``EXIT_ENV_ERROR``
    here so every verb gets the same mapping without repeating it.
    """
    cfg = resolve_config(config)
    session = await _connect(cfg)
    try:
        yield session
    except LensConnectionLost as exc:
        raise AfiError(
            code=EXIT_ENV_ERROR,
            message=f"AgentIRC connection lost mid-command: {exc}",
            remediation="retry once AgentIRC is reachable again",
        ) from exc
    finally:
        await session.disconnect()


async def _execute_checked(session: Session, parsed: ParsedCommand) -> None:
    """Run *parsed* through the console's own ``Session.execute`` dispatch.

    Subscribes to the session's event bus first — nothing else is
    listening in an ephemeral session — so a verb that fails validation
    (bad channel format, missing target/text) and publishes an ``error``
    event instead of raising is turned into a real ``AfiError`` here.
    Without this, a failed verb would look identical to a successful one
    on the CLI: exit 0, empty output.
    """
    sub = session.event_bus.subscribe()
    try:
        await session.execute(parsed)
    finally:
        events = sub.drain_nowait()
        sub.close()
    for event in events:
        if event.name == "error":
            message = json.loads(event.data).get("message", "command failed")
            raise AfiError(
                code=EXIT_USER_ERROR,
                message=message,
                remediation="check the verb's arguments — channel format, target, required text",
            )


def _split_channels(raw: str) -> list[str]:
    """Split a comma/whitespace-separated channel list for ``mesh``.

    Blank entries are dropped; order and duplicates are preserved (the
    caller joins each in order, so a duplicate is a harmless no-op
    re-join). No format validation here — ``Session.join`` already no-ops
    silently on a non-``#`` entry, matching its existing permissive
    contract for the direct (non-``_exec_join``) join path.
    """
    return [c.strip() for c in raw.replace(",", " ").split() if c.strip()]


# ---------------------------------------------------------------------------
# Async implementations — one per verb, reusable directly from async test
# code (the public sync wrappers below are the only shapes agentfront's CLI
# surface can dispatch, since it does not await a coroutine result).
# ---------------------------------------------------------------------------


async def _send_async(target: str, text: str, config: str | None) -> dict:
    async with _live_session(config) as session:
        await _execute_checked(session, ParsedCommand(type=CommandType.SEND, args=[target], text=text))
        return {"target": target, "text": text}


async def _join_async(channel: str, config: str | None) -> dict:
    async with _live_session(config) as session:
        await _execute_checked(session, ParsedCommand(type=CommandType.JOIN, args=[channel]))
        return {"channel": channel, "joined_channels": sorted(session.joined_channels)}


async def _part_async(channel: str, config: str | None) -> dict:
    async with _live_session(config) as session:
        await _execute_checked(session, ParsedCommand(type=CommandType.PART, args=[channel]))
        return {"channel": channel, "joined_channels": sorted(session.joined_channels)}


async def _read_async(channel: str, limit: int, config: str | None) -> list:
    async with _live_session(config) as session:
        return await session.history(channel, limit=limit)


async def _channels_async(config: str | None) -> list:
    async with _live_session(config) as session:
        return await session.list_channels()


async def _who_async(target: str, config: str | None) -> list:
    async with _live_session(config) as session:
        return await session.who(target)


async def _mesh_async(channel_list: str, config: str | None) -> dict:
    wanted = _split_channels(channel_list)
    async with _live_session(config) as session:
        for ch in wanted:
            await session.join(ch)
        return await session.build_mesh_snapshot()


async def _switch_async(channel: str, config: str | None) -> dict:
    async with _live_session(config) as session:
        # `_exec_switch` requires the channel be in `joined_channels`; an
        # ephemeral session never has one from a prior call, so join it
        # ourselves first — the direct method, not `execute(JOIN)`, to
        # skip that verb's extra history round-trip for what is here just
        # a precondition, not the verb under test.
        await session.join(channel)
        await _execute_checked(session, ParsedCommand(type=CommandType.SWITCH, args=[channel]))
        return {"current_channel": session.current_channel}


async def _topic_async(channel: str, text: str, config: str | None) -> dict:
    async with _live_session(config) as session:
        await _execute_checked(
            session, ParsedCommand(type=CommandType.TOPIC, args=[channel], text=text)
        )
        return {"channel": channel, "topic": text or None, "action": "set" if text else "read"}


async def _me_async(channel: str, text: str, config: str | None) -> dict:
    async with _live_session(config) as session:
        # `_exec_me` only requires `current_channel` truthy — it does not
        # require the channel be in `joined_channels` — so a direct local
        # mutation satisfies the precondition without an extra IRC round-trip.
        session.set_current_channel(channel)
        await _execute_checked(session, ParsedCommand(type=CommandType.ME, text=text))
        return {"channel": channel, "action": text}


async def _icon_async(emoji: str, config: str | None) -> dict:
    async with _live_session(config) as session:
        await _execute_checked(session, ParsedCommand(type=CommandType.ICON, args=[emoji]))
        return {"icon": emoji}


# ---------------------------------------------------------------------------
# Public tools — plain (sync) functions. agentfront's CLI surface
# (agentfront.cli_surface._make_dispatcher) calls a tool's function and,
# if the result is not None, JSON/text-renders it directly — it does not
# check `inspect.isawaitable` or await anything (only the MCP surface and
# its in-process testing twin do that). A `Session` lifecycle needs
# `asyncio`, so each tool below is a sync function that runs its async
# implementation to completion via `asyncio.run` — that makes it callable
# identically from the CLI, the MCP `run` tool, and any future TAUI/HTTP
# surface, with no surface-specific branching.
# ---------------------------------------------------------------------------


def send(target: str, text: str, config: str | None = None) -> dict:
    """Send a message to a channel or nick over one ephemeral AgentIRC session.

    Connects with the config's ``auth.dev`` identity, sends *text* to
    *target* via the same dispatch path ``POST /input``'s ``/send`` uses,
    then disconnects. Returns ``{"target", "text"}`` on success.
    """
    return asyncio.run(_send_async(target, text, config))


def join(channel: str, config: str | None = None) -> dict:
    """Join *channel* over one ephemeral AgentIRC session.

    Connects, joins (fetching the server's recent backlog exactly like the
    console's ``/join`` does), then disconnects. Returns
    ``{"channel", "joined_channels"}`` — the latter reflects only this
    one-shot session's state (always just *channel*, since nothing else
    was joined first).
    """
    return asyncio.run(_join_async(channel, config))


def part(channel: str, config: str | None = None) -> dict:
    """Part *channel* over one ephemeral AgentIRC session.

    Returns ``{"channel", "joined_channels"}``.
    """
    return asyncio.run(_part_async(channel, config))


def read(channel: str, limit: int = 50, config: str | None = None) -> list:
    """Pull up to *limit* recent history entries for *channel*.

    Calls the same ``Session.history`` query the console's ``/read`` uses,
    over one ephemeral session — so the result is bounded by what the
    server replays on a brand-new connection (c29: accepted for v1, no
    client-side backlog to fall back on). Returns a list of
    ``{"channel", "nick", "timestamp", "text"}`` entries.
    """
    return asyncio.run(_read_async(channel, limit, config))


def channels(config: str | None = None) -> list:
    """List channels visible on the configured AgentIRC server (LIST)."""
    return asyncio.run(_channels_async(config))


def who(target: str, config: str | None = None) -> list:
    """WHO a channel or nick on the configured AgentIRC server.

    Returns a list of ``{"nick", "user", "host", "server", "flags",
    "realname"}`` entries.
    """
    return asyncio.run(_who_async(target, config))


def mesh(channels: str, config: str | None = None) -> dict:
    """Build a live agent-mesh graph snapshot for one or more channels.

    *channels* is a comma- or space-separated list (e.g. ``"#ops,#dev"``).
    Since an ephemeral session starts with nothing joined, each named
    channel is joined first (in the same session) before the snapshot is
    built — the console's ``/mesh`` skips that step only because a
    persistent browser session is normally already joined to whatever it
    wants graphed. Returns katvan's mesh shape:
    ``{"nodes": [...], "edges": [...]}``.
    """
    return asyncio.run(_mesh_async(channels, config))


def switch(channel: str, config: str | None = None) -> dict:
    """Make *channel* the active channel over one ephemeral session.

    ``/switch`` is normally a pure view-state move with no IRC
    side-effect, valid only for an already-joined channel — meaningless
    on its own in a fresh ephemeral session, so this tool joins *channel*
    first (see the module docstring's ephemeral-lifecycle section) and
    then performs the switch. Returns ``{"current_channel"}``.
    """
    return asyncio.run(_switch_async(channel, config))


def topic(channel: str, text: str = "", config: str | None = None) -> dict:
    """Set (or attempt to read) *channel*'s topic.

    Passing *text* sets the topic (``TOPIC #chan :text``) and returns
    ``{"channel", "topic": text, "action": "set"}``. Omitting it sends a
    bare read-mode ``TOPIC #chan`` request, but ``Session`` has no query
    surface for the numeric reply yet (only ``IRCTransport``'s own
    fire-and-forget buffer handling) — the returned ``{"topic": None,
    "action": "read"}`` confirms the request was sent, not what the topic
    actually is. Pass *text* to get a useful result.
    """
    return asyncio.run(_topic_async(channel, text, config))


def me(channel: str, text: str, config: str | None = None) -> dict:
    """Send a CTCP ACTION (``/me``) line to *channel*.

    Returns ``{"channel", "action": text}``.
    """
    return asyncio.run(_me_async(channel, text, config))


def icon(emoji: str, config: str | None = None) -> dict:
    """Set this lens's icon (``ICON <emoji>``) for the ephemeral session.

    Returns ``{"icon": emoji}``.
    """
    return asyncio.run(_icon_async(emoji, config))


_LIVE_VERB_TOOLS = (send, join, part, read, channels, who, mesh, switch, topic, me, icon)


def register_into(app: "App") -> None:
    """Register every live AgentIRC verb as a top-level agentfront tool.

    Top-level (ungrouped) names — ``send``, ``join``, ... — per the plan:
    bare verb names, matching how the plan and the console's own ``/verb``
    convention name them. None collide with agentfront's reserved
    meta-verbs or with irc-lens's own host commands (``serve``, ``config``)
    or grouped tools (``cli overview``).
    """
    for tool_func in _LIVE_VERB_TOOLS:
        app.tool(tool_func)
