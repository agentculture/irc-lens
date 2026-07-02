"""Media blob store: tokens, magic-byte sniffing, caps, eviction.

`docs/superpowers/specs/2026-07-02-media-support-design.md` ("Upload
path" / "Why SVG is excluded") is the spec this module implements. It
is a pure module — no ``aiohttp`` imports — constructed with a root
directory and two byte caps; a later route task wires config and maps
the typed exceptions below to HTTP responses.

Layout: ``<root>/<principal>/<token>.<ext>``, where ``token =
secrets.token_urlsafe(16)`` (128-bit, unguessable — it *is* the
capability, per the design doc's "Why capability URLs on `/media/`").
Principal subdirectories exist for bookkeeping/eviction only; nothing
downstream trusts them for access control.

Extension allowlists are canonical in :mod:`irc_lens.web.media` (task
t1) — this module imports them rather than redefining them, per the
plan's instruction to reuse the t1 allowlists. SVG is deliberately
absent from both: serving a scriptable document format from an
auth-exempt capability URL is a stored-XSS vector on our own origin
(see the design doc's "Why SVG is excluded"), so ``.svg`` filenames
and sniffed XML/SVG content are both rejected before anything is
written to a final path.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple

from irc_lens.web.media import _AUDIO_EXTENSIONS, _IMAGE_EXTENSIONS

# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class MediaError(Exception):
    """Base for typed media-store failures.

    ``message``/``hint`` (not the CLI's ``{code, message, remediation}``
    triple — HTTP error responses in this codebase are the two-field
    ``{error, hint}`` shape used throughout ``web/routes.py``) let the
    route layer build that response without inventing text:
    ``{"error": exc.message, "hint": exc.hint}``.
    """

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class MediaTypeError(MediaError):
    """Extension not on the allowlist, or sniffed content disagrees
    with the extension (including SVG/XML content of any name)."""


class MediaTooLargeError(MediaError):
    """The stream exceeded ``max_file_bytes`` while it was being
    written; the partial temp file has already been deleted."""


# ---------------------------------------------------------------------------
# Extension / content-type tables
# ---------------------------------------------------------------------------

#: extension -> "image" | "audio", built from the t1 allowlists.
_EXT_KIND: dict[str, str] = {ext: "image" for ext in _IMAGE_EXTENSIONS}
_EXT_KIND.update({ext: "audio" for ext in _AUDIO_EXTENSIONS})

#: extension -> Content-Type, for the route layer to set on `GET
#: /media/<token>.<ext>` responses. Deliberately a superset-free 1:1
#: map (``jpg``/``jpeg`` both resolve to ``image/jpeg``) so the route
#: layer never has to special-case aliases itself.
CONTENT_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "webm": "audio/webm",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
}

# extension -> the set of sniffed-kind labels (see `_sniff` below) that
# are allowed to satisfy it. `jpg`/`jpeg` both sniff to `"jpeg"`; every
# other extension has exactly one legal sniff result.
_ALLOWED_SNIFF: dict[str, frozenset[str]] = {
    "png": frozenset({"png"}),
    "jpg": frozenset({"jpeg"}),
    "jpeg": frozenset({"jpeg"}),
    "gif": frozenset({"gif"}),
    "webp": frozenset({"webp"}),
    "mp3": frozenset({"mp3"}),
    "ogg": frozenset({"ogg"}),
    "wav": frozenset({"wav"}),
    "webm": frozenset({"webm"}),
    "m4a": frozenset({"m4a"}),
    "flac": frozenset({"flac"}),
}

# How many leading bytes to buffer before sniffing. Large enough to
# cover every magic-byte check below (the widest is the 12-byte
# RIFF+WEBP/WAVE probe) plus slack for the XML/SVG prolog probe.
_SNIFF_BYTES = 64

_ALLOWED_EXTENSIONS_TEXT = ", ".join(sorted(_EXT_KIND))


# ---------------------------------------------------------------------------
# Public value types
# ---------------------------------------------------------------------------


class StoredMedia(NamedTuple):
    """The result of a successful :meth:`MediaStore.save`."""

    token: str
    ext: str
    kind: str  # "image" | "audio"
    path: Path
    size: int


# ---------------------------------------------------------------------------
# Sniffing
# ---------------------------------------------------------------------------


def _looks_like_svg(data: bytes) -> bool:
    """True if `data` looks like an XML document with an ``<svg>`` root
    or a bare ``<svg ...>`` element — with or without a leading UTF-8
    BOM/whitespace, both of which are legal before an XML prolog."""
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    head = stripped[:512].lower()
    if head.startswith(b"<?xml"):
        return b"<svg" in head
    return head.startswith(b"<svg")


def _sniff(data: bytes) -> str | None:
    """Return the sniffed type label for the leading bytes of a file,
    or ``None`` when nothing recognized matches.

    Labels: ``png jpeg gif webp mp3 ogg wav webm m4a flac svg``. ``svg``
    is not a member of any extension's allowlist (see
    ``_ALLOWED_SNIFF``) — sniffing it is purely so the caller can raise
    an SVG-specific error message instead of a generic mismatch.
    """
    if _looks_like_svg(data):
        return "svg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if data.startswith(b"fLaC"):
        return "flac"
    if data.startswith(b"ID3"):
        return "mp3"
    # MPEG audio frame sync: 11 set bits (0xFF + top 3 bits of the next
    # byte). Checked after the more specific signatures above since
    # it's the least distinctive pattern (only 2 bytes).
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    # ISO base media file format ("ftyp" box at offset 4) covers m4a.
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "m4a"
    return None


def _verify_sniff(data: bytes, ext: str) -> None:
    """Raise :class:`MediaTypeError` unless `data`'s sniffed type
    agrees with `ext`'s allowlist entry."""
    sniffed = _sniff(data)
    if sniffed == "svg":
        raise MediaTypeError(
            "SVG uploads are not allowed",
            "SVG is a scriptable document format and is excluded from "
            "the upload allowlist (see docs/superpowers/specs/"
            "2026-07-02-media-support-design.md, \"Why SVG is "
            "excluded\"); convert to a raster format such as PNG.",
        )
    allowed = _ALLOWED_SNIFF.get(ext)
    if not allowed or sniffed not in allowed:
        raise MediaTypeError(
            f"file content does not match its .{ext} extension",
            "upload a file whose content matches its extension "
            f"(allowed: {_ALLOWED_EXTENSIONS_TEXT})",
        )


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

_PRINCIPAL_DISALLOWED_RE = re.compile(r"[^a-z0-9._-]")

#: strict `resolve()` shape — no `/`, no extra `.`, ext lowercase only.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+\.[a-z0-9]+$")


def _sanitize_principal(principal: str) -> str:
    """Sanitize `principal` into a safe single path segment.

    Lowercased, then every character outside ``[a-z0-9._-]`` becomes
    ``_``. Leading/trailing ``.`` are then stripped so a principal that
    sanitizes to exactly ``"."`` or ``".."`` (e.g. the literal string
    ``".."``, which contains no character the regex above would touch)
    collapses to the empty string and falls back to ``"_"`` instead of
    naming a real filesystem traversal segment. Because the regex never
    lets a ``/`` (or any other path separator) through, the result is
    always a single path component — joining it under the store root
    can never escape that root.
    """
    lowered = (principal or "").strip().lower()
    cleaned = _PRINCIPAL_DISALLOWED_RE.sub("_", lowered)
    cleaned = cleaned.strip(".")
    return cleaned or "_"


def _extract_ext(filename: str) -> str:
    """Return the lowercased extension of `filename` (no leading dot),
    or ``""`` when there isn't one. Only the basename is considered —
    any directory components the client sent are ignored."""
    name = Path(filename).name
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


# ---------------------------------------------------------------------------
# MediaStore
# ---------------------------------------------------------------------------


class MediaStore:
    """A filesystem-backed blob store for uploaded media.

    Pure module: no ``aiohttp`` import anywhere in this file. The route
    layer owns config wiring and HTTP error mapping; this class only
    raises :class:`MediaTypeError` / :class:`MediaTooLargeError`.
    """

    def __init__(self, root: Path, max_file_bytes: int, max_store_bytes: int) -> None:
        self.root = Path(root)
        self.max_file_bytes = max_file_bytes
        self.max_store_bytes = max_store_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(
        self,
        principal: str,
        filename: str,
        chunks: AsyncIterator[bytes],
    ) -> StoredMedia:
        """Stream `chunks` to a new token-named file under `principal`'s
        subdirectory, enforcing the per-file cap while streaming and
        sniffing magic bytes before the file is considered final.

        Raises:
            MediaTypeError: `filename`'s extension isn't on the
                allowlist (checked before `chunks` is touched at all),
                or the sniffed content disagrees with it.
            MediaTooLargeError: the stream exceeded `max_file_bytes`;
                the partial temp file is deleted before this raises.
        """
        ext = _extract_ext(filename)
        if ext not in _EXT_KIND:
            raise MediaTypeError(
                f"unsupported file type: {filename!r}",
                f"upload one of: {_ALLOWED_EXTENSIONS_TEXT}",
            )

        principal_dir = self.root / _sanitize_principal(principal)
        principal_dir.mkdir(parents=True, exist_ok=True)

        token = secrets.token_urlsafe(16)
        tmp_path = principal_dir / f".{token}.{ext}.part"
        final_path = principal_dir / f"{token}.{ext}"

        size = 0
        sniff_buf = bytearray()
        sniff_checked = False
        try:
            with tmp_path.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        raise MediaTooLargeError(
                            f"upload exceeds the {self.max_file_bytes}-byte per-file limit",
                            "upload a smaller file or raise `media.max_file_bytes` in the lens config",
                        )
                    handle.write(chunk)
                    if not sniff_checked:
                        sniff_buf.extend(chunk)
                        if len(sniff_buf) >= _SNIFF_BYTES:
                            _verify_sniff(bytes(sniff_buf), ext)
                            sniff_checked = True
                if not sniff_checked:
                    _verify_sniff(bytes(sniff_buf), ext)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        tmp_path.replace(final_path)
        self._evict(just_written=final_path)
        return StoredMedia(token=token, ext=ext, kind=_EXT_KIND[ext], path=final_path, size=size)

    def resolve(self, token_and_ext: str) -> Path | None:
        """Return the path for a ``<token>.<ext>`` capability string, or
        ``None`` when it's malformed or doesn't exist.

        `token_and_ext` must match ``^[A-Za-z0-9_-]+\\.[a-z0-9]+$``
        exactly — no ``/`` or extra ``.`` is ever accepted, so there is
        no path-traversal surface: the candidate is always a single
        path component joined under one principal subdirectory.
        """
        if not _TOKEN_RE.match(token_and_ext):
            return None
        if not self.root.exists():
            return None
        for principal_dir in self.root.iterdir():
            if not principal_dir.is_dir():
                continue
            candidate = principal_dir / token_and_ext
            if candidate.is_file():
                return candidate
        return None

    def _evict(self, just_written: Path) -> None:
        """While the store's total size exceeds `max_store_bytes`,
        delete the oldest file (by mtime) that isn't `just_written`.

        `just_written` is only ever spared eviction because
        `max_file_bytes` (enforced in `save`) is expected to be `<=
        max_store_bytes` — a well-formed config can never produce a
        single file that alone exceeds the store cap, so this method
        never needs to delete the file it was just asked to keep.
        """
        entries: list[tuple[float, Path, int]] = []
        total = 0
        for path in self.root.rglob("*"):
            if not path.is_file() or path.name.endswith(".part"):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            total += stat.st_size
            entries.append((stat.st_mtime, path, stat.st_size))

        if total <= self.max_store_bytes:
            return

        entries.sort(key=lambda entry: entry[0])
        for _mtime, path, size in entries:
            if total <= self.max_store_bytes:
                break
            if path == just_written:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            total -= size
