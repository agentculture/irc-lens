"""Cited IRC primitives — message parser, in-memory buffer, transport.

Cited from culture@57d3ba8 per the citation table in CITATION.md. The
files in this package are kept byte-faithful to upstream where possible;
the only adaptations are minimum-viable rewiring (imports) and the
explicit removals called out by the spec (CAP REQ for message-tags,
agent-runtime telemetry/OTEL infrastructure).
"""

from __future__ import annotations

from irc_lens.irc.buffer import BufferedMessage, MessageBuffer
from irc_lens.irc.message import Message
from irc_lens.irc.transport import IRCTransport

__all__ = [
    "BufferedMessage",
    "IRCTransport",
    "Message",
    "MessageBuffer",
]
