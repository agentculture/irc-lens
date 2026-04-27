"""Phase 8 — `--seed` YAML fixture loader.

Drives :mod:`irc_lens.seed` directly; no aiohttp / asyncio in scope.
The spec verification gate (`curl / | grep chat-line | wc -l ≥ 2`) is
exercised here as a unit test against `render_index(session)` so we
don't need a live server to prove the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from irc_lens.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, AfiError
from irc_lens.seed import apply_seed, load_seed
from irc_lens.session import Session
from irc_lens.web.render import render_index

FIXTURE = Path(__file__).parent / "fixtures" / "basic.yaml"


def _session() -> Session:
    return Session(host="example.invalid", port=6667, nick="lens")


def test_apply_seed_populates_session_state() -> None:
    s = _session()
    apply_seed(s, FIXTURE)

    assert s.joined_channels == {"#general", "#ops"}
    assert s.current_channel == "#general"
    assert [(e.nick, e.type, e.online) for e in s.roster] == [
        ("alice", "human", True),
        ("bob", "agent", True),
    ]
    msgs = s.buffer.read("#general", limit=200)
    assert [(m.nick, m.text, m.timestamp) for m in msgs] == [
        ("alice", "hello world", 1714000000.0),
        ("bob", "hi alice", 1714000005.0),
    ]


def test_apply_seed_renders_two_chat_lines() -> None:
    """The Phase 8 verification gate, lifted into pytest."""
    s = _session()
    apply_seed(s, FIXTURE)
    html = render_index(s)
    assert html.count('data-testid="chat-line"') >= 2


def test_apply_seed_missing_file_raises_afierror_with_hint(tmp_path: Path) -> None:
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), tmp_path / "nope.yaml")
    assert ei.value.code == EXIT_USER_ERROR
    assert "not found" in ei.value.message
    assert ei.value.remediation


def test_apply_seed_invalid_yaml_raises_afierror_with_hint(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("joined_channels: [unclosed\n", encoding="utf-8")
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert ei.value.code == EXIT_USER_ERROR
    assert "not valid YAML" in ei.value.message
    assert ei.value.remediation


def test_apply_seed_unknown_top_level_key_raises(tmp_path: Path) -> None:
    bad = tmp_path / "extra.yaml"
    bad.write_text("typo_field: 1\n", encoding="utf-8")
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert "unknown top-level keys" in ei.value.message
    assert "typo_field" in ei.value.message


def test_apply_seed_current_channel_not_joined_raises(tmp_path: Path) -> None:
    bad = tmp_path / "orphan.yaml"
    bad.write_text(
        "joined_channels: ['#a']\ncurrent_channel: '#b'\n", encoding="utf-8"
    )
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert "not in joined_channels" in ei.value.message


def test_apply_seed_partial_seed_only_current_channel(tmp_path: Path) -> None:
    """Every section is optional; pinning just the active channel works
    (after listing it in joined_channels)."""
    seed = tmp_path / "tiny.yaml"
    seed.write_text(
        "joined_channels: ['#solo']\ncurrent_channel: '#solo'\n", encoding="utf-8"
    )
    s = _session()
    apply_seed(s, seed)
    assert s.current_channel == "#solo"
    assert s.joined_channels == {"#solo"}
    assert s.roster == []


def test_apply_seed_empty_file_is_a_noop(tmp_path: Path) -> None:
    """An empty seed parses to {} and overlays nothing — useful as a
    "verify the path resolves but don't change anything" smoke test."""
    seed = tmp_path / "empty.yaml"
    seed.write_text("", encoding="utf-8")
    s = _session()
    before_state = (s.joined_channels.copy(), s.current_channel, list(s.roster))
    apply_seed(s, seed)
    assert (s.joined_channels, s.current_channel, list(s.roster)) == before_state


def test_apply_seed_rejects_non_hash_channel(tmp_path: Path) -> None:
    bad = tmp_path / "nohash.yaml"
    bad.write_text("joined_channels: ['general']\n", encoding="utf-8")
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert "must start with '#'" in ei.value.message


def test_apply_seed_preload_message_missing_field(tmp_path: Path) -> None:
    bad = tmp_path / "incomplete_msg.yaml"
    bad.write_text(
        "joined_channels: ['#x']\n"
        "preload_messages:\n"
        "  - {channel: '#x', nick: 'a'}\n",
        encoding="utf-8",
    )
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert "missing required key 'text'" in ei.value.message


def test_load_seed_returns_normalized_dict() -> None:
    """load_seed is the pure half — apply_seed builds on top of it.
    Pin the public shape so test code can assert against it."""
    data = load_seed(FIXTURE)
    assert set(data) == {"joined_channels", "preload_messages", "roster", "current_channel"}
    assert data["preload_messages"][0]["timestamp"] == pytest.approx(1714000000.0)


def test_buffer_add_accepts_explicit_timestamp() -> None:
    """Pin the additive buffer.add(timestamp=...) contract."""
    from irc_lens.irc.buffer import MessageBuffer

    b = MessageBuffer()
    b.add("#x", "alice", "hi", timestamp=1714000000.0)
    [m] = b.read("#x")
    assert m.timestamp == pytest.approx(1714000000.0)


def test_buffer_add_defaults_to_now_when_timestamp_omitted() -> None:
    """Backwards-compat guard: existing callers pass three args and
    must still get `time.time()` populated."""
    import time

    from irc_lens.irc.buffer import MessageBuffer

    b = MessageBuffer()
    before = time.time()
    b.add("#x", "alice", "hi")
    after = time.time()
    [m] = b.read("#x")
    assert before <= m.timestamp <= after


# ---------------------------------------------------------------------------
# PR #10 review fallout: reject malformed timestamps + non-UTF-8 bytes; pin
# the user-vs-env exit-code split.
# ---------------------------------------------------------------------------


def test_apply_seed_rejects_nan_timestamp(tmp_path: Path) -> None:
    bad = tmp_path / "nan.yaml"
    bad.write_text(
        "joined_channels: ['#x']\n"
        "preload_messages:\n"
        "  - {channel: '#x', nick: 'a', text: 'hi', timestamp: .nan}\n",
        encoding="utf-8",
    )
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert ei.value.code == EXIT_USER_ERROR
    assert "finite" in ei.value.message.lower()


def test_apply_seed_rejects_inf_timestamp(tmp_path: Path) -> None:
    bad = tmp_path / "inf.yaml"
    bad.write_text(
        "joined_channels: ['#x']\n"
        "preload_messages:\n"
        "  - {channel: '#x', nick: 'a', text: 'hi', timestamp: .inf}\n",
        encoding="utf-8",
    )
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert ei.value.code == EXIT_USER_ERROR
    assert "finite" in ei.value.message.lower()


def test_apply_seed_rejects_out_of_range_timestamp(tmp_path: Path) -> None:
    """Finite but un-renderable: time.localtime raises OverflowError."""
    bad = tmp_path / "range.yaml"
    bad.write_text(
        "joined_channels: ['#x']\n"
        "preload_messages:\n"
        "  - {channel: '#x', nick: 'a', text: 'hi', timestamp: 1.0e+30}\n",
        encoding="utf-8",
    )
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert ei.value.code == EXIT_USER_ERROR
    assert "out of range" in ei.value.message


def test_apply_seed_rejects_invalid_utf8(tmp_path: Path) -> None:
    bad = tmp_path / "binary.yaml"
    bad.write_bytes(b"\xff\xfe\x00binary garbage")
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), bad)
    assert ei.value.code == EXIT_USER_ERROR
    assert "UTF-8" in ei.value.message


def test_apply_seed_read_failure_uses_env_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission/IO failure on an existing file is environmental,
    not user-input — mirror serve.py's bind-port branch."""
    seed = tmp_path / "exists.yaml"
    seed.write_text("joined_channels: []\n", encoding="utf-8")

    def boom(self, *_a, **_kw):
        raise PermissionError(13, "permission denied", str(self))

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), seed)
    assert ei.value.code == EXIT_ENV_ERROR
    assert "cannot read seed file" in ei.value.message


def test_apply_seed_missing_file_keeps_user_error_code(tmp_path: Path) -> None:
    """Pin the precedent: user-supplied missing path is a user
    error (mirrors LensConnectionLost on a wrong --host)."""
    with pytest.raises(AfiError) as ei:
        apply_seed(_session(), tmp_path / "absent.yaml")
    assert ei.value.code == EXIT_USER_ERROR
