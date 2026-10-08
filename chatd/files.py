"""Upload storage, content sniffing, name sanitising and file-serving helpers (SPEC 5.5 and 5.6).

Nothing here talks HTTP or SQL: ``http.py`` calls these helpers from the ``/api/upload`` and ``/files/<id>``
handlers.  Contracts worth knowing:

* :func:`classify` decides ``(mime, kind)`` from the first bytes of the file plus the one narrowing hint
  ``audio_only``; the client's ``Content-Type`` never reaches this module.
* :func:`sanitize_name` never returns an empty string, never contains path separators, control/format characters
  or Windows-reserved device names and always fits 200 characters and 255 UTF-8 bytes.
* :func:`save_upload` streams to ``<uploads>/.tmp/<uuid>.part`` and removes the temp file on every failure path;
  :func:`finalize_upload` moves it to ``<uploads>/<aa>/<id>`` with ``os.replace``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO, Callable, Dict, Optional, Tuple
from urllib.parse import quote, unquote

from . import util

log = logging.getLogger("chatd.files")

# ---- constants (SPEC 5.1) ----------------------------------------------------------------------------------------
MAX_UNATTACHED_BYTES = 500 * 1024 * 1024  # per user, uploads not yet attached to a message
MAX_DAILY_BYTES = 2 * 1024 * 1024 * 1024  # per user, rolling 24 h
MAX_UPLOADS_PER_USER = 3  # concurrent
MAX_UPLOADS_PER_IP = 6  # concurrent
DISK_RESERVE_MIN = 2 * 1024 * 1024 * 1024
DISK_RESERVE_FRACTION = 0.05
DOWNLOAD_CAP_USER = 8  # concurrent responses with a body over DOWNLOAD_CAP_MIN_BYTES
DOWNLOAD_CAP_IP = 16
DOWNLOAD_CAP_MIN_BYTES = 1024 * 1024
DOWNLOAD_WAIT_S = 30.0
CHUNK_SIZE = 64 * 1024
SNIFF_BYTES = 512
ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_STORED_PATH_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{32}$")

# ---- serving policy (SPEC 5.5) -----------------------------------------------------------------------------------
INLINE_TYPES = frozenset(
    (
        "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif",
        "audio/mpeg", "audio/mp4", "audio/ogg", "audio/webm", "audio/wav", "audio/flac",
        "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    )
)
OCTET_STREAM = "application/octet-stream"
FILE_CSP = "sandbox; default-src 'none'"

_MP4_VIDEO_BRANDS = frozenset(
    (b"isom", b"iso2", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V ", b"3gp4", b"3gp5", b"3g2a")
)
_BMP_DIB_SIZES = frozenset((12, 40, 52, 56, 64, 108, 124))


class UploadTooLarge(Exception):
    """The body delivered more bytes than allowed."""


class RangeNotSatisfiable(Exception):
    """A syntactically valid ``Range`` that selects no byte of the file (HTTP 416)."""


# --------------------------------------------------------------------------------------------------------------
# Content sniffing
# --------------------------------------------------------------------------------------------------------------


def kind_for_mime(mime: str) -> str:
    """``image|audio|video|file`` for a sniffed MIME type (SVG is never sniffed, so never an image)."""
    major = mime.split("/", 1)[0]
    return major if major in ("image", "audio", "video") and mime in INLINE_TYPES else "file"


def sniff(head: bytes, audio_only: bool = False) -> Tuple[str, str]:
    """Return ``(mime, kind)`` for the first bytes of an upload (allow-list of SPEC 5.6).

    ``audio_only`` is the validated client hint that may only *narrow* EBML (webm) and mp4-family video to audio.
    Anything unknown is ``("application/octet-stream", "file")``.
    """
    mime = _sniff_mime(head, audio_only)
    return mime, (kind_for_mime(mime) if mime != OCTET_STREAM else "file")


def _sniff_mime(head: bytes, audio_only: bool) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:2] == b"BM" and len(head) >= 18 and head[6:10] == b"\0\0\0\0" and (
        int.from_bytes(head[14:18], "little") in _BMP_DIB_SIZES
    ):
        return "image/bmp"
    if head[4:8] == b"ftyp":
        return _sniff_ftyp(head[8:12], audio_only)
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm" if audio_only else "video/webm"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if _is_mpeg_frame(head):
        return "audio/mpeg"
    return OCTET_STREAM


def _is_mpeg_frame(head: bytes) -> bool:
    """MPEG audio frame sync: 11 set bits, a valid version (not 01) and a valid layer (not 00)."""
    if len(head) < 2 or head[0] != 0xFF or (head[1] & 0xE0) != 0xE0:
        return False
    return ((head[1] >> 3) & 3) != 1 and ((head[1] >> 1) & 3) != 0


def _sniff_ftyp(brand: bytes, audio_only: bool) -> str:
    if brand in (b"avif", b"avis"):
        return "image/avif"
    if brand == b"qt  ":
        return "video/quicktime"
    if brand in (b"M4A ", b"M4B "):
        return "audio/mp4"
    if brand in _MP4_VIDEO_BRANDS:
        return "audio/mp4" if audio_only else "video/mp4"
    return OCTET_STREAM  # heic, heix, mif1, msf1, ... : not playable in a browser


def parse_meta(value: Optional[str]) -> Dict[str, Any]:
    """Validate the ``X-Meta`` header (SPEC 5.6); only well-formed hints survive, everything else is dropped.

    Keys in the result: ``width``/``height`` (int 1..16384), ``duration`` (finite float 0..86400) and
    ``audio_only`` (only ever ``True``).
    """
    if not value or len(value) > 1024:
        return {}
    try:
        data = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in ("width", "height"):
        v = data.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 16384:
            out[key] = v
    d = data.get("duration")
    if isinstance(d, (int, float)) and not isinstance(d, bool) and math.isfinite(d) and 0 <= d <= 86400:
        out["duration"] = float(d)
    if data.get("audio_only") is True:
        out["audio_only"] = True
    return out


def classify(head: bytes, meta: Dict[str, Any]) -> Tuple[str, str, Optional[int], Optional[int], Optional[float]]:
    """Combine sniffing and hints into ``(mime, kind, width, height, duration)`` for the attachment row.

    ``audio_only`` counts only when the hints carry no ``width``/``height``.  Dimensions are kept for images and
    videos, the duration for everything except plain files.
    """
    audio_only = bool(meta.get("audio_only")) and "width" not in meta and "height" not in meta
    mime, kind = sniff(head, audio_only)
    width = meta.get("width") if kind in ("image", "video") else None
    height = meta.get("height") if kind in ("image", "video") else None
    duration = meta.get("duration") if kind != "file" else None
    return mime, kind, width, height, duration


# --------------------------------------------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------------------------------------------

_STRIPPED_CATEGORIES = frozenset(("Cc", "Cf", "Zl", "Zp"))
_NAME_REPLACE = frozenset('<>:"/\\|?*')
_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%s" % d for d in "0123456789¹²³"]
    + ["LPT%s" % d for d in "0123456789¹²³"]
)
MAX_NAME_CHARS = 200
MAX_NAME_BYTES = 255


def decode_header_name(raw: Optional[str]) -> str:
    """Percent-decode an ``X-File-Name`` header value as UTF-8, strictly (SPEC 4.2).

    Raises ``ValueError`` for invalid UTF-8 or a lone surrogate; the caller answers ``400 bad_request`` with
    ``reason:"invalid_text"``.
    """
    if not raw:
        return ""
    try:
        text = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("invalid UTF-8")
    if util.has_lone_surrogate(text):
        raise ValueError("lone surrogate")
    return text


def sanitize_name(raw: str) -> str:
    """Make an uploaded file name safe to store, broadcast and offer for download (SPEC 5.6)."""
    name = unicodedata.normalize("NFC", raw)
    name = "".join(ch for ch in name if unicodedata.category(ch) not in _STRIPPED_CATEGORIES)
    name = re.split(r"[\\/]", name)[-1]
    name = "".join("_" if ch in _NAME_REPLACE else ch for ch in name)
    name = name.strip().rstrip(" .")
    if name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_STEMS:
        name = "_" + name
    name = _fit_name(name).rstrip(" .")
    return name or "file"


def _fit_name(name: str) -> str:
    """Shorten ``name`` to the character/byte limits, keeping a reasonable extension."""
    if len(name) <= MAX_NAME_CHARS and len(name.encode("utf-8")) <= MAX_NAME_BYTES:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 32:
        stem, ext = name, ""
    else:
        ext = "." + ext
    stem = stem[: max(1, MAX_NAME_CHARS - len(ext))]
    while len(stem) > 1 and len((stem + ext).encode("utf-8")) > MAX_NAME_BYTES:
        stem = stem[:-1]
    return stem + ext


def is_blocked(name: str, blocked_extensions: Any) -> bool:
    """True when the last suffix of the (sanitised) name is on the blocklist, case-insensitively."""
    if "." not in name:
        return False
    return name.rsplit(".", 1)[1].lower() in blocked_extensions


# --------------------------------------------------------------------------------------------------------------
# Serving helpers
# --------------------------------------------------------------------------------------------------------------


def served_type(mime: str, force_download: bool) -> Tuple[str, bool]:
    """``(Content-Type, inline)`` for the stored sniffed ``mime`` (SPEC 5.5).

    Only the allow-listed types may be shown inline; ``force_download`` (``?dl=1``) always yields an attachment.
    """
    if mime in INLINE_TYPES:
        return mime, not force_download
    return OCTET_STREAM, False


def content_disposition(name: str, inline: bool) -> str:
    """``Content-Disposition`` with an ASCII-only ``filename`` fallback and an RFC 5987 ``filename*``."""
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"
    encoded = quote(name, safe="!#$&+-.^_`|~")
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % ("inline" if inline else "attachment", fallback, encoded)


def etag_matches(header: Optional[str], etag: str) -> bool:
    """``If-None-Match`` evaluation (weak comparison, ``*`` matches)."""
    if not header:
        return False
    for part in header.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("W/"):
            part = part[2:]
        if part == etag:
            return True
    return False


_RANGE_RE = re.compile(r"^bytes=([0-9]{0,15})-([0-9]{0,15})$")


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """Resolve a single-range ``Range`` header to inclusive ``(start, end)``.

    Returns ``None`` when the header is absent, malformed or asks for several ranges (the full body is served);
    raises :class:`RangeNotSatisfiable` for a valid range outside the file.
    """
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:  # suffix: the last N bytes
        count = int(last)
        if count == 0 or size == 0:
            raise RangeNotSatisfiable
        return max(0, size - count), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if last and end < start:
        return None
    if start >= size:
        raise RangeNotSatisfiable
    return start, min(end, size - 1)


def stored_path(uploads_dir: Path, rel: str) -> Optional[Path]:
    """Absolute path of an attachment given its ``attachments.path`` value, or ``None`` if it looks wrong."""
    if not _STORED_PATH_RE.match(rel):
        return None
    return uploads_dir / rel[:2] / rel[3:]


def open_for_read(path: Path) -> Optional[Tuple[BinaryIO, os.stat_result]]:
    """Open a regular file for streaming (blocking: run it in an executor).  ``None`` when it is not there."""
    try:
        fh = open(path, "rb")  # noqa: SIM115 - the open handle is returned to the caller
    except OSError:
        return None
    try:
        st = os.fstat(fh.fileno())
    except OSError:
        fh.close()
        return None
    return fh, st


class FileStream:
    """Async iterator over ``length`` bytes of an open file starting at ``start``, read in executor chunks.

    ``aclose`` is idempotent, closes the file and runs ``on_close`` (used to release download slots); the HTTP
    layer always calls it, even for ``HEAD`` and when the client disconnected before the first chunk.
    """

    def __init__(self, fh: BinaryIO, start: int, length: int, on_close: Optional[Callable[[], None]] = None) -> None:
        self._fh: Optional[BinaryIO] = fh
        self._remaining = length
        self._on_close = on_close
        self._pos = start
        self._started = False

    def __aiter__(self) -> "FileStream":
        return self

    def _read(self, n: int) -> bytes:
        fh = self._fh
        if fh is None:
            return b""
        if not self._started:
            fh.seek(self._pos)
            self._started = True
        return fh.read(n)

    async def __anext__(self) -> bytes:
        if self._remaining <= 0 or self._fh is None:
            await self.aclose()
            raise StopAsyncIteration
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, self._read, min(CHUNK_SIZE, self._remaining))
        if not data:
            await self.aclose()
            raise StopAsyncIteration
        self._remaining -= len(data)
        return data

    async def aclose(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                log.debug("closing a served file failed")
        callback, self._on_close = self._on_close, None
        if callback is not None:
            callback()


class _Slot:
    __slots__ = ("sem", "refs")

    def __init__(self, limit: int) -> None:
        self.sem = asyncio.Semaphore(limit)
        self.refs = 0


class KeyedLimiter:
    """Per-key concurrency limit with waiting (SPEC 5.1 download caps).

    Semaphores are created lazily inside the running loop and dropped when no holder or waiter remains, so the
    dictionary never outgrows the number of active keys.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._slots: Dict[Any, _Slot] = {}

    async def acquire(self, key: Any, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a slot; ``False`` when none became free."""
        slot = self._slots.get(key)
        if slot is None:
            slot = self._slots[key] = _Slot(self._limit)
        slot.refs += 1
        try:
            await asyncio.wait_for(slot.sem.acquire(), timeout)
        except asyncio.TimeoutError:
            self._drop(key, slot)
            return False
        except BaseException:
            self._drop(key, slot)
            raise
        return True

    def release(self, key: Any) -> None:
        slot = self._slots.get(key)
        if slot is None:
            return
        slot.sem.release()
        self._drop(key, slot)

    def _drop(self, key: Any, slot: _Slot) -> None:
        slot.refs -= 1
        if slot.refs <= 0 and self._slots.get(key) is slot:
            del self._slots[key]


class UploadSlots:
    """Concurrent upload counters per user and per IP (SPEC 5.1: 3 per user, 6 per IP)."""

    def __init__(self, per_user: int = MAX_UPLOADS_PER_USER, per_ip: int = MAX_UPLOADS_PER_IP) -> None:
        self._per_user, self._per_ip = per_user, per_ip
        self._users: Dict[int, int] = {}
        self._ips: Dict[str, int] = {}

    def acquire(self, user_id: int, ip: str) -> bool:
        if self._users.get(user_id, 0) >= self._per_user or self._ips.get(ip, 0) >= self._per_ip:
            return False
        self._users[user_id] = self._users.get(user_id, 0) + 1
        self._ips[ip] = self._ips.get(ip, 0) + 1
        return True

    def release(self, user_id: int, ip: str) -> None:
        _decrement(self._users, user_id)
        _decrement(self._ips, ip)


def _decrement(table: Dict[Any, int], key: Any) -> None:
    left = table.get(key, 0) - 1
    if left > 0:
        table[key] = left
    else:
        table.pop(key, None)


def disk_has_room(data_dir: Path, content_length: int) -> bool:
    """``free >= content_length + max(2 GiB, 5 % of the volume)`` (SPEC 5.1).  Unreadable usage counts as room."""
    try:
        usage = shutil.disk_usage(str(data_dir))
    except OSError:
        log.warning("cannot read disk usage of the data directory")
        return True
    reserve = max(DISK_RESERVE_MIN, int(usage.total * DISK_RESERVE_FRACTION))
    return usage.free >= content_length + reserve


# --------------------------------------------------------------------------------------------------------------
# Streaming upload
# --------------------------------------------------------------------------------------------------------------


@dataclass
class SavedUpload:
    """A completed temp file: ``size`` bytes at ``tmp_path``; ``head`` = its first :data:`SNIFF_BYTES` bytes."""

    tmp_path: Path
    size: int
    head: bytes


def new_attachment_id() -> str:
    return uuid.uuid4().hex


def _discard(path: Path) -> None:
    """Delete a file; a failure after the retries is deferred to the pending-delete list by ``retry_file_op``."""
    util.retry_file_op(os.remove, str(path))


async def save_upload(chunks: AsyncIterator[bytes], tmp_dir: Path, max_bytes: int) -> SavedUpload:
    """Stream ``chunks`` into ``<tmp_dir>/<uuid>.part`` (disk work happens in the default executor).

    Raises :class:`UploadTooLarge` as soon as more than ``max_bytes`` arrive.  On any exception, including task
    cancellation (client disconnect), the partial file is removed before the exception propagates.
    """
    loop = asyncio.get_running_loop()
    path = tmp_dir / ("%s.part" % uuid.uuid4().hex)
    fh = await loop.run_in_executor(None, _open_new, path)
    size = 0
    head = bytearray()
    ok = False
    try:
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise UploadTooLarge
            if len(head) < SNIFF_BYTES:
                head += chunk[: SNIFF_BYTES - len(head)]
            await loop.run_in_executor(None, fh.write, chunk)
        await loop.run_in_executor(None, fh.close)
        ok = True
    finally:
        if not ok:
            await asyncio.shield(loop.run_in_executor(None, _abandon, fh, path))
    return SavedUpload(path, size, bytes(head))


def _open_new(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "wb")


def _abandon(fh: BinaryIO, path: Path) -> None:
    try:
        fh.close()
    except OSError:
        log.debug("closing an abandoned upload failed")
    _discard(path)


async def finalize_upload(saved: SavedUpload, uploads_dir: Path, attachment_id: str) -> str:
    """Move the temp file to ``<uploads>/<aa>/<id>``; returns the ``attachments.path`` value ``"<aa>/<id>"``."""
    rel = "%s/%s" % (attachment_id[:2], attachment_id)
    dest = uploads_dir / attachment_id[:2] / attachment_id
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _move, saved.tmp_path, dest)
    except BaseException:
        await asyncio.shield(loop.run_in_executor(None, _discard, saved.tmp_path))
        raise
    return rel


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    util.retry_file_op(os.replace, str(src), str(dest))


async def remove_stored(uploads_dir: Path, rel: str) -> None:
    """Delete a stored attachment file (retry + pending-delete list on Windows sharing violations)."""
    path = stored_path(uploads_dir, rel)
    if path is None:
        return
    await asyncio.get_running_loop().run_in_executor(None, _discard, path)
