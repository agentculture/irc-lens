"""Purpose-authored agent-facing doc pages, registered on the ``App``.

:func:`register_docs` is the single place irc-lens's registry docs live.
``cli.build_app()`` calls it so the CLI ``explain``/``learn`` surfaces and the
(future) HTTP front all read from the same four pages. They are fresh prose
*about the running tool* — what irc-lens is, how to drive the chat console,
what you can invoke from a shell, and the conventions every invocation
honours — addressed to the agent reading them, not a copy of anything under
``docs/`` (the human-facing reference tree, which stays exactly where it is
and is cited by name rather than reproduced here; see
``tests/test_front_docs.py`` for the guard that keeps it that way).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentfront.app import App

_ABOUT = """\
# What irc-lens is

You are looking at irc-lens, a reactive web console that puts a browser
front end on a single AgentIRC connection. Point it at an AgentIRC server
with `irc-lens serve` and it opens one IRC session, holds it open for the
lifetime of the process, and renders every inbound event as an HTML
fragment pushed to exactly one browser tab over server-sent events. There
is no client-side templating and no agent loop hidden inside the console —
the browser is a thin renderer, and you (or a browser-automation harness
acting on your behalf) drive it the same way a human would, by typing into
one input box.

Treat irc-lens as an operator's console, not a bot. It never decides
anything on your behalf; every join, part, message, or view switch is a
line you send, and every reply comes back as a DOM update or an SSE event
you can read with a page-reading or fetch tool. That determinism is
deliberate: the same server-rendered fragments a human eyeballs are also
what a scripted agent can parse without guessing at hidden client state.

Two processes matter here: the AgentIRC server (the mesh you are actually
observing or acting in) and irc-lens itself (a pure client, holding no
state beyond one session and one browser tab). Start with `irc-lens config
init` for a starter config, then `irc-lens serve` to open the console.
From here, read `console` for the verbs you type into it, and `tools` for
the command-line surface you drive irc-lens itself with.
"""

_CONSOLE = """\
# Driving the chat console

Once `irc-lens serve` is running and you have the console open, everything
you do goes through one input box. Plain text with no leading slash is a
chat line to whichever channel is currently active — it round-trips as a
message and echoes into the log immediately, ahead of server confirmation.

Anything starting with `/` is a console verb rather than a chat line. The
ones you will reach for most:

- `/join <#channel>` — join a channel and make it the active one; the
  sidebar and the info pane both refresh.
- `/part <#channel>` — leave a channel.
- `/send <target> <text...>` — send to a channel or nick without switching
  your active pane, useful for dropping a line into a channel you are not
  currently looking at.
- `/switch <#channel>` — change which joined channel is active. This is a
  pure view-state move with no IRC side effect, the same jump the sidebar
  makes when you click a channel.
- `/read <#channel> [n]` — pull the last `n` buffered lines of a channel
  back into view.
- `/channels` — list channels reachable on the server.
- `/who <#channel>` — refresh a channel's roster.
- `/mesh` — switch the info pane to the live agent-mesh graph, a read-only
  view of which agents are connected and how they relate.
- `/topic <#channel> <text...>` — set a channel's topic.
- `/me <text...>` — send an action line (CTCP ACTION) to the active
  channel.
- `/icon <emoji>` — change the emoji this lens presents as its own icon.
- `/help`, `/overview`, `/status` — swap the info pane between its
  built-in views.

A verb irc-lens does not recognize, or one that fails downstream — an
unparseable channel name, an empty `/send` body, IRC dropping mid-flight —
never wedges your input box. It publishes a single `error` event, which
the browser surfaces as a toast, and the console keeps taking your next
line. Driving the console headlessly is the same contract: watch the SSE
stream for `error` events the way a human would watch for the toast, and
keep sending — there is nothing to reset.
"""

_TOOLS = """\
# The tool catalog

irc-lens itself, distinct from the console you type into, is an ordinary
command-line program. Every verb below runs as `irc-lens <verb>` and every
one of them accepts `--json` for a machine-parseable answer in place of
the human-readable text.

- `irc-lens serve` — the main act: connect to AgentIRC and open the web
  console described in `console`. Requires a config file.
- `irc-lens config init` / `irc-lens config overview` — write a starter
  config, or print a short summary of the config noun.
- `irc-lens cli overview` — a machine-readable rollup of irc-lens's own
  command-line surface: every noun, every verb, and which ones carry a
  doc page.
- `irc-lens learn` — the entry point for figuring out what irc-lens can do
  without reading source: purpose, the command list, the doc-page slugs
  (this one included), the exit-code policy, `--json` support, and a
  pointer at `explain`.
- `irc-lens explain [path]` — the doc reader; give it a registered verb or
  noun path to read that entry's own documentation directly.
- `irc-lens overview [path]` — a descriptive walk through whatever `path`
  names, never a pass/fail judgment; an unrecognized path still exits 0,
  with a warning section in place of an error.
- `irc-lens doctor [path]` — the one verb that does pass judgment: it
  audits irc-lens's own health, or a target's when given a path, and
  reports what is broken and how to fix it.

When you are scripting against irc-lens rather than reading its output
yourself, reach for `irc-lens learn --json` first to get the full catalog
as structured data — every doc slug and every tool path in one payload —
before falling back to parsing any human-facing text.
"""

_CONVENTIONS = """\
# Exit codes, --json, and the meta-verbs

Every irc-lens invocation ends in one of three exit-code bands, and the
band is the first thing to check before you parse anything else:

- `0` — the verb did what you asked; read stdout.
- `1` — you gave it something it could not act on: a bad flag, a channel
  name that will not parse, a config path that does not exist. Fix the
  input and retry.
- `2` — the environment failed it: a port already bound, AgentIRC
  unreachable, a file it could not write. Retrying the same input without
  changing the environment fails the same way.
- `3` and above are reserved and unused today.

On failure, irc-lens never lets a Python traceback reach you. It writes
exactly two lines to stderr instead:

    error: <what went wrong>
    hint: <what to do about it>

Pass `--json` on a failing call and the same two facts arrive as one JSON
object on stderr instead: `{"code", "message", "remediation"}`, so a
script can branch on `code` without scraping text. `--json` never mixes
the streams — results land on stdout, diagnostics and errors land on
stderr, in text mode and JSON mode alike.

Four verbs exist purely so you can introspect irc-lens without reading its
source, and each answers a different question: `learn` answers "what is
this tool, broadly"; `explain <path>` answers "what does this one verb or
noun do"; `overview [path]` answers "what is present here right now", and
deliberately never hard-fails — an unrecognized path is a warning section,
not an error; `doctor [path]` answers "what is wrong, and how do I fix
it", and is the one member of the four that does fail when something is
unhealthy. If you only remember one command for getting unstuck, make it
`irc-lens overview` — it lists everything else there is to discover,
including the doc pages you are reading now.

Every one of the four accepts `--json` with a stable shape worth scripting
against: `learn --json` lists every doc slug and tool path; `explain
<path> --json` returns `{"path", "doc"}` for a registered verb or noun;
`overview --json` returns `{"subject", "sections"}`; `doctor --json`
returns `{"healthy", "checks"}`, where every failed check carries a
non-empty remediation.
"""

# (slug, title, body) — the order here is the order add_doc registers them
# in, which is also the order app.list_docs() returns them in.
_PAGES: tuple[tuple[str, str, str], ...] = (
    ("about", "What irc-lens is", _ABOUT),
    ("console", "Driving the chat console", _CONSOLE),
    ("tools", "The tool catalog", _TOOLS),
    ("conventions", "Exit codes, --json, and the meta-verbs", _CONVENTIONS),
)


def register_docs(app: "App") -> None:
    """Register every purpose-authored doc page onto *app*'s registry.

    Each page is inline text via ``add_doc(text=...)`` — deliberately not
    ``add_docs_dir("docs/")``, which would just re-publish the human-facing
    reference tree instead of authoring content for the agent reading it.
    """
    for slug, title, text in _PAGES:
        app.add_doc(slug=slug, title=title, text=text)
