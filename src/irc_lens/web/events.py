"""SSE serialization for ``SessionEvent``.

The wire format is line-oriented: ``event: <name>`` followed by one
``data: <line>`` per line of payload, terminated by a blank line.
Multi-line payloads are common — every rendered HTML fragment contains
newlines — and the SSE spec requires one ``data:`` line per logical
line; a naive single ``data:`` would be silently truncated by browsers
at the first ``\\n``.

``SessionEvent`` itself lives in :mod:`irc_lens.session` (it predates
the web layer); we re-export it here so import sites that only need
the SSE surface don't have to reach into ``session``.
"""

from __future__ import annotations

from irc_lens.session import SessionEvent

__all__ = ["SessionEvent", "format_sse"]


def format_sse(event: SessionEvent) -> bytes:
    """Serialize one ``SessionEvent`` to SSE wire bytes (UTF-8).

    Always emits at least one ``data:`` line — an empty payload becomes
    ``data:`` (with the trailing space stripped, per the spec's lenient
    parser) so the terminator-blank-line rule still holds.
    """
    parts = [f"event: {event.name}"]
    payload = event.data or ""
    # `splitlines()` (no keepends) handles \n, \r\n, and \r uniformly.
    # An empty payload yields [], so we substitute [""] to guarantee one
    # `data:` line precedes the terminator.
    lines = payload.splitlines() or [""]
    for line in lines:
        parts.append(f"data: {line}" if line else "data:")
    parts.append("")  # terminating blank line per SSE spec
    return ("\n".join(parts) + "\n").encode("utf-8")
