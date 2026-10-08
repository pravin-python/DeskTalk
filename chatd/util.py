"""Shared helpers (SPEC §6.2 ``util.py`` row): clocks, ids, JSON, logging, LAN discovery, Windows-safe file
operations, the single-instance lock and the text-normalisation rules of §4.3.

Everything here is standard library only and importable without ``sqlite3``. Nobody else re-implements these.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
import unicodedata
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

if os.name == "nt":  # pragma: no cover - exercised on one platform only
    import msvcrt
else:  # pragma: no cover
    import fcntl

log = logging.getLogger("chatd.util")


class FatalError(Exception):
    """A startup/environment failure that ends the command with ``code`` and no traceback (SPEC §2.2, §6.2).

    ``code`` is the process exit code: 78 (default) for sqlite3 missing/old, an unwritable data dir, WAL unavailable
    or a schema newer than the code; 73 for :class:`AlreadyRunning`. ``__main__`` prints only the message.
    """

    def __init__(self, message: str, code: int = 78) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------------------------------------------------
# Identity constants (SPEC §3, §4.3)
# --------------------------------------------------------------------------------------------------------------------

#: ``users.username`` grammar. ``\Z`` (not ``$``) so that ``"abc\n"`` never matches.
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}\Z")

#: Usernames (and display names) that only an admin / the CLI may create (SPEC §4.3(3)).
RESERVED_USERNAMES = frozenset(
    ("admin", "administrator", "root", "system", "support", "helpdesk", "it", "hr", "everyone", "desktalk")
)

#: Key used by the login throttles for every username that does not match :data:`USERNAME_RE` (SPEC §4.1).
INVALID_LOGIN_KEY = "?"

DB_FILENAME = "chat.db"


# --------------------------------------------------------------------------------------------------------------------
# Clocks and test scaling (SPEC §2.1, §2.3)
# --------------------------------------------------------------------------------------------------------------------

_test_scale = 1.0


def now() -> float:
    """Wall-clock seconds since the Unix epoch. Persisted timestamps only; durations use ``time.monotonic()``."""
    return time.time()


def set_test_scale(scale: float) -> None:
    """Set the factor applied by :func:`scaled`. Called once at startup (1.0 unless ``DESKTALK_TEST=1``)."""
    global _test_scale
    value = float(scale)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("test scale must be a finite number > 0")
    _test_scale = value


def scaled(seconds: float) -> float:
    """Return ``seconds`` multiplied by the test scale. Every in-process timer constant goes through this."""
    return seconds * _test_scale


# --------------------------------------------------------------------------------------------------------------------
# Ids, codes, JSON
# --------------------------------------------------------------------------------------------------------------------


def new_id() -> str:
    """A new 32-character lowercase hex id (``uuid4().hex``): attachment ids, instance ids."""
    return uuid.uuid4().hex


def short_code() -> str:
    """An 8-character URL-safe random code (``secrets.token_urlsafe(6)``): setup code and join code (SPEC §4.3)."""
    return secrets.token_urlsafe(6)


def json_dumps(obj: Any, ensure_ascii: bool = True) -> str:
    """Compact JSON text. ``allow_nan`` is off: NaN/Infinity can never be emitted."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=ensure_ascii, allow_nan=False)


def _reject_constant(name: str) -> Any:
    raise ValueError("invalid JSON constant")


def _bounded_int(text: str) -> int:
    if len(text) > 18:
        raise ValueError("integer literal too long")
    return int(text)


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _no_dupes(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _depth_exceeds(obj: Any, limit: int) -> bool:
    stack: List[Tuple[Any, int]] = [(obj, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Iterable[Any] = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def json_loads_strict(text: str, max_depth: int = 8) -> Any:
    """Parse ``text`` with the inbound-frame rules of SPEC §7.1.

    Rejects ``NaN``/``Infinity``, integer literals longer than 18 characters, duplicate keys and nesting deeper than
    ``max_depth`` containers. Every failure (including ``RecursionError``) is raised as ``ValueError`` and never
    carries the payload.
    """
    try:
        obj = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_int=_bounded_int,
            parse_float=_finite_float,
            object_pairs_hook=_no_dupes,
        )
    except RecursionError:
        raise ValueError("JSON nested too deeply")
    except ValueError:  # json.JSONDecodeError embeds a slice of the input in its text: never propagate it
        raise ValueError("invalid JSON")
    if _depth_exceeds(obj, max_depth):
        raise ValueError("JSON nested too deeply")
    return obj


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def has_lone_surrogate(obj: Any) -> bool:
    """``True`` when a string anywhere inside ``obj`` (dict keys included) holds a code point of U+D800..U+DFFF.

    ``json.loads`` accepts the escape ``\\ud800``; SQLite binding and ``.encode()`` reject the resulting string, so
    SPEC §7.1 answers ``bad_request`` with ``reason:'invalid_text'``. Iterative: a deeply nested value cannot recurse.
    """
    stack: List[Any] = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE_RE.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


# --------------------------------------------------------------------------------------------------------------------
# Text normalisation (SPEC §4.3(4)) and case folding (SPEC §2.3)
# --------------------------------------------------------------------------------------------------------------------

_STRIPPED_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Zl", "Zp"))
_WS_RUN = re.compile(r"\s+")
_HWS_RUN = re.compile(r"[^\S\n]+")
_SPACE_AROUND_NL = re.compile(r" ?\n ?")


def normalize_text(value: str, allow_newline: bool = False) -> str:
    """Normalise display names, status texts, titles, descriptions and workspace names (SPEC §4.3(4)).

    NFKC, remove the Unicode categories ``Cc Cf Cs Co Zl Zp`` (``\\n`` survives when ``allow_newline``), collapse
    every run of whitespace to one space (newlines are kept as line breaks when ``allow_newline``) and trim.
    Length limits are the caller's job and count code points (``len(str)``).
    """
    text = unicodedata.normalize("NFKC", value)
    kept = [ch for ch in text if (allow_newline and ch == "\n") or unicodedata.category(ch) not in _STRIPPED_CATEGORIES]
    text = "".join(kept)
    collapsed = _SPACE_AROUND_NL.sub("\n", _HWS_RUN.sub(" ", text)) if allow_newline else _WS_RUN.sub(" ", text)
    return collapsed.strip()


def display_key(value: str) -> str:
    """``casefold(NFKC(value))``: the uniqueness key of display names (``users.display_key``, SPEC §4.3(4))."""
    return unicodedata.normalize("NFKC", value).casefold()


def fold(value: Any) -> Any:
    """The SQL function ``fold(x)`` registered on every connection (SPEC §2.3): ``casefold`` for strings."""
    return value.casefold() if isinstance(value, str) else value


def login_key(username: Any) -> str:
    """The throttle key of a login attempt: the lowercased username when it is a valid username, else ``?``.

    Only ASCII input is lowercased (``"\\u212a".lower()`` is ``"k"``, which would let a different string alias a
    real username).
    """
    if isinstance(username, str) and username.isascii():
        lowered = username.lower()
        if USERNAME_RE.match(lowered):
            return lowered
    return INVALID_LOGIN_KEY


# --------------------------------------------------------------------------------------------------------------------
# Logging (SPEC §2.5, §2.6)
# --------------------------------------------------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_QUEUE_SIZE = 50000
_ROTATE_GRACE = 1024 * 1024


def safe_log_value(value: Any, limit: int = 100) -> str:
    """``ascii()`` of an attacker-controlled value, at most ``limit`` characters long (SPEC §2.5)."""
    text = ascii(value)
    if len(text) > limit:
        text = text[: max(limit - 3, 0)] + "..."
    return text


def log_username(value: Any) -> str:
    """The username when it matches ``^[a-z0-9._-]{3,32}$`` (after ASCII lowercasing), else ``<invalid>``."""
    key = login_key(value)
    return "<invalid>" if key == INVALID_LOGIN_KEY else key


class SafeFormatter(logging.Formatter):
    """A formatter that escapes ``\\r`` and ``\\n`` inside the log *message* (tracebacks keep their line breaks)."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        original = record.message
        record.message = original.replace("\r", "\\r").replace("\n", "\\n")
        try:
            return super().formatMessage(record)
        finally:
            record.message = original


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A ``RotatingFileHandler`` that survives a failed rollover.

    On Windows another process (or an antivirus scan) can hold the log file open so that renaming it fails with
    ``PermissionError``. The failure is swallowed and the next attempt is made after another 1 MiB (SPEC §2.6).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._grace = 0
        self._rotate_failed = False

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.maxBytes <= 0:
            return False
        if self.stream is None:
            self.stream = self._open()
        message = "%s\n" % self.format(record)
        self.stream.seek(0, 2)
        return self.stream.tell() + len(message) >= self.maxBytes + self._grace

    def rotate(self, source: str, dest: str) -> None:
        try:
            super().rotate(source, dest)
        except OSError:
            self._rotate_failed = True

    def doRollover(self) -> None:
        self._rotate_failed = False
        try:
            super().doRollover()
        except OSError:
            self._rotate_failed = True
        if self._rotate_failed:
            self._grace += _ROTATE_GRACE
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    self.stream = None
        else:
            self._grace = 0


class _BoundedQueueHandler(logging.handlers.QueueHandler):
    """A ``QueueHandler`` that never blocks and never raises: a full queue (stalled console) drops the record."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        prepared = logging.makeLogRecord(record.__dict__)
        prepared.msg = record.getMessage()
        prepared.args = None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped = getattr(self, "dropped", 0) + 1


class _Listener(logging.handlers.QueueListener):
    def enqueue_sentinel(self) -> None:
        self.queue.put(self._sentinel, timeout=1.0)


class LogHandle:
    """What :func:`setup_logging` installed; ``stop()`` flushes and detaches it."""

    def __init__(self, handlers: List[logging.Handler], listener: Optional[_Listener]) -> None:
        self._handlers = handlers
        self._listener = listener

    def stop(self) -> None:
        root = logging.getLogger()
        if self._listener is not None:
            try:
                self._listener.stop()
            except queue.Full:
                log.warning("log listener did not stop (console stalled)")
            self._listener = None
        for handler in self._handlers:
            root.removeHandler(handler)
            try:
                handler.flush()
                handler.close()
            except (OSError, ValueError):
                log.warning("closing a log handler failed")
        self._handlers = []


_active_logging: Optional[LogHandle] = None


def _parse_level(level: Any) -> int:
    if isinstance(level, int):
        return level
    value = logging.getLevelName(str(level).upper())
    return value if isinstance(value, int) else logging.INFO


def setup_logging(level: Any, data_dir: Optional[Any], attach_file: bool) -> None:
    """Configure the root logger per SPEC §2.5 (``level`` is a name such as ``"INFO"``).

    * stderr (when it exists) goes through ``QueueHandler`` + ``QueueListener`` so a stalled console cannot block
      the event loop; the formatter escapes ``\\r\\n``.
    * with ``attach_file`` (``serve`` only) and a ``data_dir``: ``<data_dir>/logs/desktalk.log`` is a
      :class:`SafeRotatingFileHandler`, 5 MB x 5, UTF-8. A log directory that cannot be opened is reported and skipped;
      logging never prevents startup.
    Calling it again replaces the previous configuration; :func:`stop_logging` undoes it (flushes the listener).
    """
    global _active_logging
    stop_logging()
    root = logging.getLogger()
    root.setLevel(_parse_level(level))
    formatter = SafeFormatter(LOG_FORMAT)
    handlers: List[logging.Handler] = []
    listener: Optional[_Listener] = None

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        log_queue: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=_LOG_QUEUE_SIZE)
        queue_handler = _BoundedQueueHandler(log_queue)
        listener = _Listener(log_queue, stream_handler)
        listener.start()
        root.addHandler(queue_handler)
        handlers.append(queue_handler)

    if attach_file and data_dir is not None:
        log_dir = os.path.join(str(data_dir), "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            file_handler = SafeRotatingFileHandler(
                os.path.join(log_dir, "desktalk.log"), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        except OSError as exc:
            logging.getLogger("chatd").warning("cannot open the log file: %s", type(exc).__name__)
        else:
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
            handlers.append(file_handler)

    _active_logging = LogHandle(handlers, listener)


def stop_logging() -> None:
    """Detach and close what :func:`setup_logging` installed (idempotent); queued stderr records are flushed first."""
    global _active_logging
    handle, _active_logging = _active_logging, None
    if handle is not None:
        handle.stop()


# --------------------------------------------------------------------------------------------------------------------
# LAN discovery (SPEC §5.8)
# --------------------------------------------------------------------------------------------------------------------


def _usable_lan_address(address: Any) -> bool:
    if not isinstance(address, str):
        return False
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def lan_addresses() -> Tuple[Optional[str], List[str]]:
    """Return ``(primary, others)`` IPv4 addresses of this machine (SPEC §5.8).

    ``primary`` is the address the OS would use to reach the LAN (UDP ``connect`` trick, nothing is sent) or ``None``;
    ``others`` are the remaining non-loopback, non-169.254 addresses of ``gethostbyname_ex``/``getaddrinfo`` in
    discovery order without duplicates. Never raises.
    """
    primary: Optional[str] = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            candidate = probe.getsockname()[0]
        if _usable_lan_address(candidate):
            primary = candidate
    except OSError:
        primary = None

    found: List[str] = []
    try:
        host = socket.gethostname()
        found.extend(socket.gethostbyname_ex(host)[2])
        found.extend(info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET))
    except OSError:
        log.debug("host name resolution failed while discovering LAN addresses")
    others: List[str] = []
    for address in found:
        if address != primary and address not in others and _usable_lan_address(address):
            others.append(address)
    return primary, others


# --------------------------------------------------------------------------------------------------------------------
# Paths and private files
# --------------------------------------------------------------------------------------------------------------------


def db_path(data_dir: str) -> str:
    """``<data>/chat.db``."""
    return os.path.join(data_dir, DB_FILENAME)


def control_dir(data_dir: str) -> str:
    """``<data>/control`` (reload / stop.request / server.lock / pending-delete.txt, SPEC §2.4)."""
    return os.path.join(data_dir, "control")


def uploads_dir(data_dir: str) -> str:
    """``<data>/uploads``."""
    return os.path.join(data_dir, "uploads")


def backups_dir(data_dir: str) -> str:
    """``<data>/backups``."""
    return os.path.join(data_dir, "backups")


def write_private_file(path: str, text: str) -> None:
    """Write ``text`` (UTF-8) to ``path`` with mode 0600 on POSIX (Windows relies on the data-dir ACL, SPEC §10.2)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        log.debug("chmod 600 failed for a private file")


# --------------------------------------------------------------------------------------------------------------------
# Windows-safe file operations and the pending-delete list (SPEC §2.6)
# --------------------------------------------------------------------------------------------------------------------


class PendingDeleteList:
    """Paths whose deletion failed after all retries (SPEC §2.6), persisted as ``<data>/control/pending-delete.txt``.

    One path per line, relative to the data dir (absolute when the file lives outside it). Appended on failure,
    loaded at startup, swept hourly. All methods are thread-safe and never raise ``OSError``.
    """

    def __init__(self, data_dir: str) -> None:
        self._data_dir = os.path.abspath(data_dir)
        self._file = os.path.join(control_dir(self._data_dir), "pending-delete.txt")
        self._items: List[str] = []
        self._lock = threading.Lock()

    @property
    def file(self) -> str:
        """Absolute path of the persisted list."""
        return self._file

    def _stored(self, path: str) -> str:
        absolute = os.path.abspath(path)
        try:
            relative = os.path.relpath(absolute, self._data_dir)
        except ValueError:  # another drive on Windows
            return absolute
        return absolute if relative.startswith("..") else relative

    def _resolve(self, stored: str) -> str:
        return os.path.normpath(os.path.join(self._data_dir, stored))

    def add(self, path: str) -> None:
        """Remember ``path`` for a later deletion attempt (idempotent)."""
        stored = self._stored(path)
        with self._lock:
            if stored in self._items:
                return
            self._items.append(stored)
            try:
                os.makedirs(os.path.dirname(self._file), exist_ok=True)
                with open(self._file, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(stored + "\n")
            except OSError as exc:
                log.warning("cannot persist the pending-delete list: %s", type(exc).__name__)

    def load(self) -> int:
        """Replace the in-memory list with the persisted one; returns the number of entries."""
        with self._lock:
            try:
                with open(self._file, encoding="utf-8") as handle:
                    lines = [line.strip() for line in handle.read().splitlines()]
            except FileNotFoundError:
                lines = []
            except (OSError, UnicodeDecodeError) as exc:
                log.warning("cannot read the pending-delete list: %s", type(exc).__name__)
                lines = []
            self._items = []
            for line in lines:
                if line and "\x00" not in line and line not in self._items:
                    self._items.append(line)
            return len(self._items)

    def paths(self) -> List[str]:
        """Absolute paths currently pending."""
        with self._lock:
            return [self._resolve(item) for item in self._items]

    def sweep(self) -> int:
        """Try to delete every pending path once; rewrite the persisted list; return how many remain."""
        with self._lock:
            remaining: List[str] = []
            for stored in self._items:
                target = self._resolve(stored)
                try:
                    os.remove(target)
                except FileNotFoundError:
                    continue
                except OSError:
                    remaining.append(stored)
            self._items = remaining
            self._rewrite_locked()
            return len(remaining)

    def _rewrite_locked(self) -> None:
        temp = self._file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("".join(item + "\n" for item in self._items))
            os.replace(temp, self._file)
        except OSError as exc:
            log.warning("cannot rewrite the pending-delete list: %s", type(exc).__name__)


_pending_deletes: Optional[PendingDeleteList] = None


def pending_delete_load(data_dir: Any) -> None:
    """Create the process-wide pending-delete list for ``data_dir`` and load its persisted content (startup).

    :func:`retry_file_op` appends to this list; before this ran (CLI commands, tests) a final failure is only logged.
    """
    global _pending_deletes
    registry = PendingDeleteList(str(data_dir))
    count = registry.load()
    _pending_deletes = registry
    if count:
        log.info("%d file(s) are waiting in the pending-delete list", count)


def pending_delete_sweep() -> int:
    """Retry every pending deletion once, rewrite ``control/pending-delete.txt`` and return how many remain."""
    registry = _pending_deletes
    return registry.sweep() if registry is not None else 0


_DELETE_FUNCTIONS: Tuple[Callable[..., Any], ...] = (os.remove, os.unlink, os.rmdir)


def retry_file_op(fn: Callable[..., Any], *args: Any, retries: int = 5, base_delay: float = 0.05) -> bool:
    """Run ``fn(*args)`` retrying ``OSError`` (``PermissionError``, WinError 5/32) with ``50 ms * 2**n`` back-off.

    ``fn`` is an ``os.remove/os.replace/os.rename``-style callable. Up to ``retries`` retries are made.

    * deletions (``os.remove``, ``os.unlink``, ``os.rmdir``): a missing path counts as success; when every attempt
      fails ``args[0]`` is appended to the pending-delete list (:func:`pending_delete_load`) and ``False`` is returned,
      so a failed delete never propagates into a request.
    * every other operation (``os.replace``, ``os.rename``, ...): the last ``OSError`` is raised after the final
      attempt (a failed upload move must be reported); ``FileNotFoundError`` is not retried.
    Returns ``True`` when the operation succeeded.
    """
    is_delete = fn in _DELETE_FUNCTIONS
    last_error: Optional[OSError] = None
    for attempt in range(retries + 1):
        try:
            fn(*args)
        except FileNotFoundError:
            if is_delete:
                return True
            raise
        except OSError as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(scaled(base_delay * (2**attempt)))
        else:
            return True
    if is_delete and args:
        registry = _pending_deletes
        if registry is not None:
            registry.add(str(args[0]))
        log.warning("could not delete %s, deferred: %s", safe_log_value(str(args[0])), type(last_error).__name__)
        return False
    assert last_error is not None
    raise last_error


# --------------------------------------------------------------------------------------------------------------------
# Single-instance lock (SPEC §2.2)
# --------------------------------------------------------------------------------------------------------------------

_LOCK_OFFSET = 4096
_INFO_SIZE = 512
_BUSY_ERRNOS = (13, 11, 35, 36)  # EACCES, EAGAIN (linux/win), EAGAIN (macOS), EDEADLOCK (win)


class AlreadyRunning(FatalError):
    """Another process holds the instance lock (exit code 73). ``info`` is the published JSON (empty if unreadable)."""

    def __init__(self, info: Optional[Dict[str, Any]] = None) -> None:
        self.info: Dict[str, Any] = dict(info or {})
        super().__init__(self._describe(), code=73)

    @property
    def pid(self) -> Optional[int]:
        value = self.info.get("pid")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def port(self) -> Optional[int]:
        value = self.info.get("port")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _describe(self) -> str:
        if self.pid is None:
            return "DeskTalk is already running on this data dir (details unavailable)"
        port = self.port
        return "DeskTalk is already running on this data dir (pid %d, port %s)" % (
            self.pid,
            port if port is not None else "unknown",
        )


class InstanceLock:
    """A held instance lock. Keep the object for the process lifetime; ``update`` republishes the JSON at offset 0."""

    def __init__(self, fd: int, path: str, info: Dict[str, Any]) -> None:
        self._fd: Optional[int] = fd
        self.path = path
        self._info = dict(info)
        self._mutex = threading.Lock()

    @property
    def info(self) -> Dict[str, Any]:
        """The JSON currently published."""
        return dict(self._info)

    def _write(self, info: Dict[str, Any]) -> None:
        data = json_dumps(info).encode("utf-8")
        if len(data) >= _INFO_SIZE:
            raise ValueError("instance lock info must be shorter than %d bytes" % _INFO_SIZE)
        if self._fd is None:
            raise ValueError("instance lock already released")
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, data.ljust(_INFO_SIZE, b" "))

    def update(self, info: Dict[str, Any]) -> None:
        """Merge ``info`` into the published JSON and rewrite it (e.g. the real port once the sockets are bound)."""
        with self._mutex:
            merged = dict(self._info)
            merged.update(info)
            self._write(merged)
            self._info = merged

    def release(self) -> None:
        """Blank the published JSON, unlock and close. Idempotent."""
        with self._mutex:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b" " * _INFO_SIZE)
                _unlock(fd)
            except OSError:
                log.debug("releasing the instance lock raised")
            finally:
                os.close(fd)

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


def _try_lock(fd: int) -> bool:
    """Take the exclusive non-blocking lock on byte 4096. ``False`` when someone else holds it."""
    try:
        if os.name == "nt":
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _BUSY_ERRNOS or isinstance(exc, (BlockingIOError, PermissionError)):
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _lock_path(data_dir: str) -> str:
    return os.path.join(control_dir(data_dir), "server.lock")


def instance_lock(data_dir: str, info: Optional[Dict[str, Any]] = None) -> InstanceLock:
    """Take the single-instance lock ``<data>/control/server.lock`` or raise :class:`AlreadyRunning`.

    The lock covers one byte at offset 4096 (Windows byte-range locks are mandatory, so the JSON at offset 0 must
    stay readable by the process that lost the race, SPEC §2.6). The JSON ``{"pid": <pid>, **info}`` is written
    only after the lock is held.
    """
    os.makedirs(control_dir(data_dir), exist_ok=True)
    path = _lock_path(data_dir)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        locked = _try_lock(fd)
    except BaseException:
        os.close(fd)
        raise
    if not locked:
        os.close(fd)
        raise AlreadyRunning(read_lock_info(data_dir))
    lock = InstanceLock(fd, path, {})
    published: Dict[str, Any] = {"pid": os.getpid()}
    published.update(info or {})
    try:
        lock.update(published)
    except (OSError, ValueError):
        lock.release()
        raise
    return lock


def read_lock_info(data_dir: str) -> Optional[Dict[str, Any]]:
    """The JSON published by the lock holder, or ``None`` when absent, blank or unreadable (SPEC §2.2).

    The file is opened read-only and never locked. It may be stale when no process holds the lock (a crash leaves
    it behind); only :func:`instance_lock` can tell.
    """
    try:
        # Unbuffered: a buffered read asks for 8 KiB, which spans the locked byte at 4096 and fails on Windows.
        with open(_lock_path(data_dir), "rb", buffering=0) as handle:
            raw = handle.read(_INFO_SIZE) or b""
    except OSError:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8").strip() or "null")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
