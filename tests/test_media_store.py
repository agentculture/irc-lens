"""Unit tests for `irc_lens.web.store` (media-support PR 2, task t5).

Covers the acceptance criteria from
`docs/plans/2026-07-02-media-support.md` (task t5):

1. tokens (`secrets.token_urlsafe(16)`), the `dir/principal/token.ext`
   layout, magic-byte sniff vs. extension-allowlist agreement, and SVG
   (or any other non-allowlisted type) rejection via a typed error.
2. the per-file cap enforced *while streaming* (no full-file buffering
   before the size check) and the total-store cap evicting the
   oldest-by-mtime file.
3. `resolve()` happy/absent/traversal-proof lookup and principal
   sanitization.

This task owns only `src/irc_lens/web/store.py` and this test file —
it does not touch `routes.py` or `app.py` (that is task t6's job); the
route layer will map `MediaTypeError`/`MediaTooLargeError` to
`{error, hint}` JSON responses using each exception's `.message` /
`.hint` attributes.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from irc_lens.web.store import (
    CONTENT_TYPES,
    MediaError,
    MediaStore,
    MediaTooLargeError,
    MediaTypeError,
    StoredMedia,
)

# ---------------------------------------------------------------------------
# Sample payloads — minimal magic-byte prefixes for each allowlisted type,
# padded so every sample is comfortably larger than the sniff window's
# minimum requirement but still tiny.
# ---------------------------------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF_BYTES = b"GIF89a" + b"\x00" * 16
WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 8
MP3_ID3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 16
MP3_FRAMESYNC_BYTES = bytes([0xFF, 0xFB]) + b"\x00" * 16
OGG_BYTES = b"OggS" + b"\x00" * 16
WAV_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"fmt " + b"\x00" * 8
WEBM_BYTES = bytes([0x1A, 0x45, 0xDF, 0xA3]) + b"\x00" * 16
M4A_BYTES = b"\x00\x00\x00\x18" + b"ftyp" + b"M4A \x00\x00\x02\x00" + b"\x00" * 8
FLAC_BYTES = b"fLaC" + b"\x00" * 16

SVG_BYTES = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b"<rect/></svg>"
)

GARBAGE_BYTES = b"\x01\x02\x03\x04not a known format" * 4


async def _chunks(data: bytes, chunk_size: int | None = None) -> AsyncIterator[bytes]:
    """Yield `data` as one chunk, or split into `chunk_size`-byte pieces."""
    if chunk_size is None:
        yield data
        return
    for start in range(0, len(data), chunk_size):
        yield data[start : start + chunk_size]


async def _never_iterated() -> AsyncIterator[bytes]:
    """An async generator that fails the test if it's ever consumed."""
    raise AssertionError("chunks should not be consumed for a rejected extension")
    yield b""  # pragma: no cover — unreachable; makes this an async generator


# ---------------------------------------------------------------------------
# Happy save
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,data,kind",
    [
        ("photo.png", PNG_BYTES, "image"),
        ("photo.jpg", JPEG_BYTES, "image"),
        ("photo.jpeg", JPEG_BYTES, "image"),
        ("photo.gif", GIF_BYTES, "image"),
        ("photo.webp", WEBP_BYTES, "image"),
        ("clip.mp3", MP3_ID3_BYTES, "audio"),
        ("clip.mp3", MP3_FRAMESYNC_BYTES, "audio"),
        ("clip.ogg", OGG_BYTES, "audio"),
        ("clip.wav", WAV_BYTES, "audio"),
        ("clip.webm", WEBM_BYTES, "audio"),
        ("clip.m4a", M4A_BYTES, "audio"),
        ("clip.flac", FLAC_BYTES, "audio"),
    ],
)
async def test_save_happy_path(tmp_path: Path, filename: str, data: bytes, kind: str) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    result = await store.save("alice@example.com", filename, _chunks(data))

    assert isinstance(result, StoredMedia)
    assert result.kind == kind
    assert result.size == len(data)
    assert result.ext == filename.rsplit(".", 1)[-1].lower()
    assert result.path.is_file()
    assert result.path.read_bytes() == data
    # dir/principal/token.ext layout.
    assert result.path.parent == tmp_path / "alice_example.com"
    assert result.path.name == f"{result.token}.{result.ext}"


async def test_save_token_is_url_safe_16_bytes(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    result = await store.save("alice", "photo.png", _chunks(PNG_BYTES))

    # secrets.token_urlsafe(16) always yields a 22-character string
    # drawn from the URL-safe base64 alphabet (no padding).
    assert len(result.token) == 22
    assert re.fullmatch(r"[A-Za-z0-9_-]+", result.token)


async def test_content_types_cover_every_allowlisted_extension(tmp_path: Path) -> None:
    for ext in ("png", "jpg", "jpeg", "gif", "webp", "mp3", "ogg", "wav", "webm", "m4a", "flac"):
        assert ext in CONTENT_TYPES
    assert CONTENT_TYPES["png"] == "image/png"
    assert CONTENT_TYPES["m4a"] == "audio/mp4"
    assert CONTENT_TYPES["ogg"] == "audio/ogg"


async def test_save_skips_empty_chunks(tmp_path: Path) -> None:
    """Empty `bytes` chunks (e.g. a multipart reader yielding a
    boundary artifact) are skipped rather than counted or written."""
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)

    async def chunks() -> AsyncIterator[bytes]:
        yield PNG_BYTES[:4]
        yield b""
        yield PNG_BYTES[4:]
        yield b""

    result = await store.save("alice", "photo.png", chunks())
    assert result.size == len(PNG_BYTES)
    assert result.path.read_bytes() == PNG_BYTES


async def test_two_saves_of_same_content_get_distinct_tokens(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    first = await store.save("alice", "photo.png", _chunks(PNG_BYTES))
    second = await store.save("alice", "photo.png", _chunks(PNG_BYTES))

    assert first.token != second.token
    assert first.path != second.path
    assert first.path.is_file()
    assert second.path.is_file()


# ---------------------------------------------------------------------------
# Sniff-vs-extension mismatch, unknown types, SVG rejection
# ---------------------------------------------------------------------------


async def test_sniff_mismatch_rejected(tmp_path: Path) -> None:
    """A `.png` filename whose actual bytes are a JPEG must be rejected —
    the sniff and the extension disagree."""
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)

    with pytest.raises(MediaTypeError) as exc_info:
        await store.save("alice", "photo.png", _chunks(JPEG_BYTES))

    assert isinstance(exc_info.value, MediaError)
    assert exc_info.value.message
    assert exc_info.value.hint
    # No leftover partial files.
    assert list(tmp_path.rglob("*.part")) == []
    assert list(tmp_path.rglob("*.png")) == []


async def test_unrecognized_content_rejected(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)

    with pytest.raises(MediaTypeError):
        await store.save("alice", "photo.png", _chunks(GARBAGE_BYTES))


async def test_unknown_extension_rejected_without_reading_chunks(tmp_path: Path) -> None:
    """An extension outside both allowlists (e.g. `.zip`) is rejected
    before the store ever touches the chunk stream."""
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)

    with pytest.raises(MediaTypeError) as exc_info:
        await store.save("alice", "archive.zip", _never_iterated())

    assert "zip" in exc_info.value.message or "archive.zip" in exc_info.value.message
    # Extension rejection happens before any directory or file is
    # created — the store root stays completely empty.
    assert list(tmp_path.rglob("*")) == []


async def test_extensionless_filename_rejected(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)

    with pytest.raises(MediaTypeError):
        await store.save("alice", "noextension", _never_iterated())


async def test_svg_extension_rejected_without_reading_chunks(tmp_path: Path) -> None:
    """SVG rejection pinned explicitly, per acceptance criterion 1: an
    `.svg` filename is rejected outright, before any chunk is read —
    even if the content were somehow benign."""
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)

    with pytest.raises(MediaTypeError) as exc_info:
        await store.save("alice", "logo.svg", _never_iterated())

    assert exc_info.value.message
    assert exc_info.value.hint
    # Extension rejection happens before any directory or file is
    # created — the store root stays completely empty.
    assert list(tmp_path.rglob("*")) == []


async def test_svg_content_rejected_even_under_allowlisted_extension(tmp_path: Path) -> None:
    """SVG/XML content disguised under an allowlisted extension (e.g.
    `.png`) is still rejected once the bytes are sniffed."""
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)

    with pytest.raises(MediaTypeError) as exc_info:
        await store.save("alice", "sneaky.png", _chunks(SVG_BYTES))

    assert "svg" in exc_info.value.message.lower()
    assert list(tmp_path.rglob("*.part")) == []
    assert list(tmp_path.rglob("*.png")) == []


# ---------------------------------------------------------------------------
# Per-file cap — enforced while streaming, not after full buffering
# ---------------------------------------------------------------------------


async def test_per_file_cap_enforced_mid_stream(tmp_path: Path) -> None:
    """The cap must trip partway through a multi-chunk stream, and the
    generator must not be drained beyond the chunk that tips it over —
    proving the store isn't buffering the whole file before checking
    size."""
    store = MediaStore(root=tmp_path, max_file_bytes=250, max_store_bytes=10_000_000)

    consumed: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        yield PNG_BYTES + b"\x00" * (100 - len(PNG_BYTES))  # 100 bytes; cumulative 100
        consumed.append(1)
        yield b"\x00" * 100  # cumulative 200 — still under the 250 cap
        consumed.append(2)
        yield b"\x00" * 100  # cumulative 300 — tips over the 250 cap
        consumed.append(3)
        yield b"\x00" * 100  # must never be requested
        consumed.append(4)

    with pytest.raises(MediaTooLargeError) as exc_info:
        await store.save("alice", "big.png", chunks())

    assert exc_info.value.message
    assert exc_info.value.hint
    # The overflow is detected as soon as the 3rd chunk (which tips
    # cumulative size from 200 to 300, over the 250 cap) is received —
    # the store never asks the generator for a 4th chunk. `consumed`
    # only records markers *after* a yield resumes, so `[1, 2]` proves
    # exactly 3 chunks were pulled (chunks 1 and 2 fully processed,
    # chunk 3 received and triggered the raise) and the 4th was never
    # produced — i.e. no full-file buffering happened before this.
    assert consumed == [1, 2]
    # No partial or final file left behind.
    principal_dir = tmp_path / "alice"
    remaining = list(principal_dir.iterdir()) if principal_dir.exists() else []
    assert remaining == []


async def test_per_file_cap_partial_cleanup_leaves_no_orphan_files(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=50, max_store_bytes=10_000_000)

    with pytest.raises(MediaTooLargeError):
        await store.save("alice", "big.mp3", _chunks(MP3_ID3_BYTES + b"\x00" * 200, chunk_size=32))

    principal_dir = tmp_path / "alice"
    remaining = list(principal_dir.iterdir()) if principal_dir.exists() else []
    assert remaining == []


async def test_per_file_cap_at_exact_boundary_is_allowed(tmp_path: Path) -> None:
    """A file exactly at `max_file_bytes` is not rejected for size."""
    data = PNG_BYTES
    store = MediaStore(root=tmp_path, max_file_bytes=len(data), max_store_bytes=10_000_000)

    result = await store.save("alice", "exact.png", _chunks(data))
    assert result.size == len(data)


# ---------------------------------------------------------------------------
# Total-store cap — eviction oldest-by-mtime
# ---------------------------------------------------------------------------


async def test_eviction_deletes_oldest_by_mtime(tmp_path: Path) -> None:
    # Each save is ~100 bytes; cap fits two comfortably but not three.
    store = MediaStore(root=tmp_path, max_file_bytes=1_000, max_store_bytes=250)

    payload = PNG_BYTES + b"\x00" * (100 - len(PNG_BYTES))
    first = await store.save("alice", "one.png", _chunks(payload))
    _set_mtime(first.path, time.time() - 300)

    second = await store.save("alice", "two.png", _chunks(payload))
    _set_mtime(second.path, time.time() - 200)

    # This third save pushes total size over the 250-byte cap, which
    # must evict the oldest file (`first`) — not `second` or itself.
    third = await store.save("alice", "three.png", _chunks(payload))

    assert not first.path.exists()
    assert second.path.exists()
    assert third.path.exists()


async def test_eviction_never_deletes_the_file_just_written(tmp_path: Path) -> None:
    # max_store_bytes smaller than what even one file's worth of
    # padding would need to stay under after a second save — the
    # just-written file must survive regardless.
    store = MediaStore(root=tmp_path, max_file_bytes=1_000, max_store_bytes=10)

    payload = PNG_BYTES + b"\x00" * (100 - len(PNG_BYTES))
    first = await store.save("alice", "one.png", _chunks(payload))
    _set_mtime(first.path, time.time() - 100)

    second = await store.save("alice", "two.png", _chunks(payload))

    assert second.path.exists()
    # first should have been evicted trying to make room, even though
    # it alone can't bring total under the (deliberately tiny) cap.
    assert not first.path.exists()


async def test_eviction_leaves_store_under_cap_when_possible(tmp_path: Path) -> None:
    """A cap that comfortably fits 2 files but not 3 should always
    converge back to exactly the 2 newest files, not over-evict down
    to a single survivor."""
    store = MediaStore(root=tmp_path, max_file_bytes=1_000, max_store_bytes=220)
    payload = PNG_BYTES + b"\x00" * (100 - len(PNG_BYTES))  # 100 bytes each

    saved = []
    for i in range(4):
        result = await store.save("alice", f"n{i}.png", _chunks(payload))
        _set_mtime(result.path, time.time() - (400 - i * 100))
        saved.append(result)

    total = sum(p.path.stat().st_size for p in saved if p.path.exists())
    assert total <= 220
    survivors = [p for p in saved if p.path.exists()]
    assert len(survivors) == 2
    # The two most-recently-written files (n2, n3) are the survivors —
    # eviction removed n0 and n1, the oldest.
    assert saved[2].path.exists()
    assert saved[3].path.exists()
    assert not saved[0].path.exists()
    assert not saved[1].path.exists()


def _set_mtime(path: Path, epoch_seconds: float) -> None:
    os.utime(path, (epoch_seconds, epoch_seconds))


# ---------------------------------------------------------------------------
# resolve() — happy / absent / traversal-proof
# ---------------------------------------------------------------------------


async def test_resolve_happy_path(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    result = await store.save("alice", "photo.png", _chunks(PNG_BYTES))

    resolved = store.resolve(f"{result.token}.{result.ext}")
    assert resolved == result.path


async def test_resolve_absent_token_returns_none(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    await store.save("alice", "photo.png", _chunks(PNG_BYTES))

    assert store.resolve("does-not-exist12345678.png") is None


async def test_resolve_on_empty_store_returns_none(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path / "media", max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    assert store.resolve("anything.png") is None


@pytest.mark.parametrize(
    "malformed",
    [
        "../../etc/passwd",
        "../secrets.png",
        "token/../../etc/passwd",
        "..%2Fpasswd.png",
        "token.PNG",  # uppercase extension not accepted
        "no-extension",
        "two.dots.png",
        "/etc/passwd",
        "",
        "token.",
        ".png",
    ],
)
async def test_resolve_rejects_malformed_or_traversal_tokens(tmp_path: Path, malformed: str) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    await store.save("alice", "photo.png", _chunks(PNG_BYTES))

    assert store.resolve(malformed) is None


async def test_resolve_traversal_cannot_escape_root(tmp_path: Path) -> None:
    """Even if an attacker could smuggle a `..`-shaped token past the
    regex (it can't — this pins the outcome, not just the regex), a
    sibling secret file outside the store root must never be reachable."""
    store_root = tmp_path / "media"
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"top-secret")

    store = MediaStore(root=store_root, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    await store.save("alice", "photo.png", _chunks(PNG_BYTES))

    assert store.resolve("../secret.png") is None


# ---------------------------------------------------------------------------
# Principal sanitization
# ---------------------------------------------------------------------------


async def test_principal_is_lowercased_and_special_chars_replaced(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    result = await store.save("Alice@Example.COM", "photo.png", _chunks(PNG_BYTES))

    assert result.path.parent.name == "alice_example.com"
    assert result.path.parent.parent == tmp_path


async def test_principal_dotdot_does_not_escape_store_root(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    result = await store.save("..", "photo.png", _chunks(PNG_BYTES))

    # The saved file must land inside the store root, never above it.
    assert tmp_path in result.path.parents
    assert result.path.parent != tmp_path.parent


async def test_principal_path_separators_cannot_create_subdirectories(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    result = await store.save("../../etc/passwd", "photo.png", _chunks(PNG_BYTES))

    # Exactly one path segment was created between the store root and
    # the file — no nested directories from the embedded "/".
    assert result.path.parent.parent == tmp_path
    assert tmp_path in result.path.parents


async def test_empty_principal_falls_back_to_safe_default(tmp_path: Path) -> None:
    store = MediaStore(root=tmp_path, max_file_bytes=1_000_000, max_store_bytes=10_000_000)
    result = await store.save("", "photo.png", _chunks(PNG_BYTES))

    assert result.path.parent.name  # non-empty, safe directory name
    assert result.path.parent.parent == tmp_path
