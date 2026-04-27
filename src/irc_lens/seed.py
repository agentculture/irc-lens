"""``--seed`` YAML fixture loader.

Phase 8 makes ``irc-lens serve --seed <path>`` overlay deterministic
view state on top of a freshly-connected ``Session`` so Playwright
tests (Phase 9c) start from a known DOM without scripting an entire
AgentIRC conversation. The IRC connection is still established —
seed mode only touches in-memory UI state, not the wire.

Schema (matches the spec verbatim — see
``docs/superpowers/specs/2026-04-27-irc-lens-handover-design.md``
lines 233–261). All five top-level keys are optional; a seed with
only ``current_channel`` is valid::

    joined_channels:
      - "#general"
      - "#ops"
    preload_messages:
      - {channel: "#general", nick: "alice", text: "hello world", timestamp: 1714000000}
      - {channel: "#general", nick: "bob",   text: "hi alice",    timestamp: 1714000005}
    roster:
      - {nick: "alice", type: "human", online: true}
      - {nick: "bob",   type: "agent", online: true}
    current_channel: "#general"

Validation discipline:

* Unknown top-level keys raise (typo guard).
* Per-section type errors raise with the field name in the message.
* ``current_channel`` must appear in ``joined_channels`` (catches
  the easy mistake of pinning the active view to a channel the UI
  cannot render).

All errors raise :class:`AfiError` with ``code = EXIT_USER_ERROR``
and a remediation hint, satisfying the rubric's stderr contract.

TODO(phase-10): lift this docstring into ``docs/cli.md`` under the
``--seed`` schema section so the doc surface is human-readable.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from irc_lens.cli._errors import EXIT_USER_ERROR, AfiError

if TYPE_CHECKING:
    from irc_lens.session import Session


_TOP_LEVEL_KEYS = {
    "joined_channels",
    "preload_messages",
    "roster",
    "current_channel",
}

_HINT_SCHEMA = (
    "see the schema in src/irc_lens/seed.py or the spec example at "
    "docs/superpowers/specs/2026-04-27-irc-lens-handover-design.md "
    "lines 237–259"
)


def _err(message: str, hint: str = _HINT_SCHEMA) -> AfiError:
    return AfiError(code=EXIT_USER_ERROR, message=message, remediation=hint)


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _err(f"{label} must be a mapping, got {type(value).__name__}")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise _err(f"{label} must be a list, got {type(value).__name__}")
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise _err(f"{label} must be a string, got {type(value).__name__}")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise _err(f"{label} must be a boolean, got {type(value).__name__}")
    return value


def _require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(f"{label} must be a number, got {type(value).__name__}")
    return float(value)


def _validate_joined_channels(raw: Any) -> list[str]:
    items = _require_list(raw, "joined_channels")
    out: list[str] = []
    for i, entry in enumerate(items):
        ch = _require_str(entry, f"joined_channels[{i}]")
        if not ch.startswith("#"):
            raise _err(f"joined_channels[{i}]={ch!r}: channel must start with '#'")
        out.append(ch)
    return out


def _validate_preload_messages(raw: Any) -> list[dict[str, Any]]:
    items = _require_list(raw, "preload_messages")
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(items):
        m = _require_mapping(entry, f"preload_messages[{i}]")
        for required in ("channel", "nick", "text"):
            if required not in m:
                raise _err(f"preload_messages[{i}] missing required key {required!r}")
        out.append(
            {
                "channel": _require_str(m["channel"], f"preload_messages[{i}].channel"),
                "nick": _require_str(m["nick"], f"preload_messages[{i}].nick"),
                "text": _require_str(m["text"], f"preload_messages[{i}].text"),
                "timestamp": (
                    _require_number(m["timestamp"], f"preload_messages[{i}].timestamp")
                    if "timestamp" in m
                    else None
                ),
            }
        )
    return out


def _validate_roster(raw: Any) -> list[dict[str, Any]]:
    items = _require_list(raw, "roster")
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(items):
        m = _require_mapping(entry, f"roster[{i}]")
        if "nick" not in m:
            raise _err(f"roster[{i}] missing required key 'nick'")
        out.append(
            {
                "nick": _require_str(m["nick"], f"roster[{i}].nick"),
                "type": _require_str(m.get("type", "human"), f"roster[{i}].type"),
                "online": _require_bool(m.get("online", True), f"roster[{i}].online"),
            }
        )
    return out


def load_seed(path: Path) -> dict[str, Any]:
    """Read, parse, and validate a seed YAML file.

    Pure — does not touch ``Session``. Returns a normalized dict with
    every section present (empty list / empty string when omitted) so
    :func:`apply_seed` doesn't need to re-check shapes.
    """
    if not path.exists():
        raise _err(
            f"seed file not found: {path}",
            hint="check the path; common location is tests/fixtures/*.yaml",
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _err(
            f"seed file {path} is not valid YAML: {exc}",
            hint="run `python -c \"import yaml; yaml.safe_load(open('<path>'))\"` to localize the parse error",
        ) from exc
    except OSError as exc:
        raise _err(f"cannot read seed file {path}: {exc}") from exc

    if raw is None:
        raw = {}
    raw = _require_mapping(raw, f"seed file {path}: top-level")

    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise _err(
            f"seed file {path}: unknown top-level keys: {sorted(unknown)}",
            hint=f"allowed keys: {sorted(_TOP_LEVEL_KEYS)}",
        )

    joined = _validate_joined_channels(raw.get("joined_channels", []))
    preload = _validate_preload_messages(raw.get("preload_messages", []))
    roster = _validate_roster(raw.get("roster", []))
    current = _require_str(raw.get("current_channel", ""), "current_channel")

    if current and current not in joined:
        raise _err(
            f"current_channel={current!r} is not in joined_channels {joined}",
            hint="add the channel to joined_channels or clear current_channel",
        )

    return {
        "joined_channels": joined,
        "preload_messages": preload,
        "roster": roster,
        "current_channel": current,
    }


def apply_seed(session: "Session", path: Path) -> None:
    """Overlay a seed file onto ``session`` state.

    Call **after** ``await session.connect()`` succeeds and **before**
    ``make_app(session)``. No SSE events are published — there are no
    subscribers yet, and the initial ``GET /`` Jinja render reads the
    live ``Session`` directly.
    """
    from irc_lens.session import EntityItem

    data = load_seed(path)
    for ch in data["joined_channels"]:
        session.joined_channels.add(ch)
    for entry in data["preload_messages"]:
        session.buffer.add(
            entry["channel"],
            entry["nick"],
            entry["text"],
            timestamp=entry["timestamp"],
        )
    if data["roster"]:
        session.set_roster(
            [EntityItem(nick=r["nick"], type=r["type"], online=r["online"]) for r in data["roster"]]
        )
    if data["current_channel"]:
        session.set_current_channel(data["current_channel"])
