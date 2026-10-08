"""The database layer: ``Database`` (one writer thread + reader threads), schema, migrations, pragmas, meta helpers
and the FACADE over the ``db_*`` modules (SPEC §2.3, §3, §6.2; the function tables are in ``docs/DB_API.md``).

Importable even when ``sqlite3`` is missing: ``sqlite_problem()`` reports it and ``Database.start()`` refuses to run.
Query functions never live here; they live in ``db_users``, ``db_chats``, ``db_messages`` and ``db_receipts`` and are
re-exported at the bottom of this module so that callers write ``db.<function>``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import Future
from functools import partial
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import util
from .util import FatalError

try:
    import sqlite3
except ImportError:  # the server reports this through sqlite_problem() instead of a traceback
    sqlite3 = None  # type: ignore[assignment]

log = logging.getLogger("chatd.db")

MIN_SQLITE = (3, 24)

# --------------------------------------------------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------------------------------------------------


class DbError(Exception):
    """Base class of every error raised by the database layer itself (not by SQLite)."""


class SchemaTooNew(DbError, FatalError):
    """The database was written by a newer program (SPEC §2.4): exit 78, never touch it."""

    def __init__(self, found: int, supported: int) -> None:
        super().__init__("database schema v%d is newer than this program supports (v%d)" % (found, supported))
        self.found = found
        self.supported = supported


class WalUnavailable(DbError, FatalError):
    """``PRAGMA journal_mode=WAL`` did not answer ``wal`` (network share, unsupported file system): exit 78."""

    def __init__(self, mode: str) -> None:
        super().__init__("SQLite refused WAL mode (journal_mode is %r); use a local disk for the data dir" % (mode,))
        self.mode = mode


class ServerBusy(DbError):
    """A time-boxed query was interrupted, or the hash executor queue is full: answer ``server_busy``/429."""

    def __init__(self, message: str = "server busy", retry_after: float = 1.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class DatabaseClosed(DbError):
    """A job was submitted to a ``Database`` that is closed (or was never started)."""


class RequestError(DbError):
    """A protocol-level failure raised by a db function (SPEC §7.1.1): ``code`` plus the optional ``err`` fields.

    Mirrors ``db_chats.ChatError`` field for field. ``code`` uses the WebSocket taxonomy (``bad_request``,
    ``unauthorized``, ``not_member``, ``not_found``, ``forbidden``, ``invalid_state``, ``conflict``, ``too_large``,
    ``window_expired``) except for failures that exist only on REST, which carry their REST code directly
    (``setup_code_required``, ``bad_setup_code``, ``bad_join_code``, ``registration_closed``, ``disabled``,
    ``quota_exceeded``). :attr:`rest_code` maps the WebSocket spelling of the registration conflicts to REST.
    """

    def __init__(
        self,
        code: str,
        msg: str,
        reason: Optional[str] = None,
        retry_after: Optional[float] = None,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
    ) -> None:
        super().__init__(code, msg)
        self.code = code
        self.msg = msg
        self.reason = reason
        self.retry_after = retry_after
        self.chat_id = chat_id
        self.message_id = message_id

    @property
    def rest_code(self) -> str:
        """The code a REST endpoint answers with (SPEC §4.2), e.g. ``username_taken`` or ``registration_closed``."""
        if self.code == "conflict" and self.reason in ("username_taken", "name_taken"):
            return str(self.reason)
        if self.code == "invalid_state" and self.reason == "max_users":
            return "registration_closed"
        return self.code

    def to_err(self) -> Dict[str, Any]:
        """The ``err`` object of a failed ``res`` (optional keys only when set)."""
        err: Dict[str, Any] = {"code": self.code, "msg": self.msg}
        for key in ("reason", "retry_after", "chat_id", "message_id"):
            value = getattr(self, key)
            if value is not None:
                err[key] = value
        return err

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.msg)


# --------------------------------------------------------------------------------------------------------------------
# SQLite availability
# --------------------------------------------------------------------------------------------------------------------


def sqlite_problem() -> Optional[str]:
    """``None`` when ``sqlite3`` imports and is at least 3.24, else a human-readable, actionable message."""
    if sqlite3 is None:
        return (
            "This Python has no usable 'sqlite3' module (a broken or minimal installation). Install a full Python "
            "3.8+ build (python.org installer), then run `python -m chatd doctor`."
        )
    if tuple(sqlite3.sqlite_version_info) < MIN_SQLITE:
        return (
            "SQLite %s is too old: DeskTalk needs 3.24 or newer. Use a newer Python build "
            "and run `python -m chatd doctor`." % (sqlite3.sqlite_version,)
        )
    return None


def _require_sqlite() -> None:
    problem = sqlite_problem()
    if problem is not None:
        raise FatalError(problem)


# --------------------------------------------------------------------------------------------------------------------
# Schema v1 (verbatim from SPEC §3) and migrations
# --------------------------------------------------------------------------------------------------------------------

SCHEMA_V1_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);       -- schema_version, workspace_name, registration_open, join_code, instance_id (random hex, created with the DB, kept by restore), last_backup_at

CREATE TABLE users(
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,                      -- stored lowercase, ^[a-z0-9._-]{3,32}$
  display_name TEXT NOT NULL,                                        -- 1..40 chars, normalised (§4.3)
  display_key TEXT NOT NULL UNIQUE,                                  -- NFKC+casefold of display_name: display names are unique (§4.3)
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin','member')),
  status_text TEXT NOT NULL DEFAULT '',                              -- "about", <=140 chars
  read_receipts INTEGER NOT NULL DEFAULT 1,
  show_last_seen INTEGER NOT NULL DEFAULT 1,
  disabled INTEGER NOT NULL DEFAULT 0,
  must_change_password INTEGER NOT NULL DEFAULT 0,                   -- set by admin.create_user / admin.reset_password (§4.3)
  created_at REAL NOT NULL, last_seen_at REAL, last_login_at REAL    -- last_login_at is set by /api/register and /api/login only, NOT by admin.create_user or CLI create-admin (`spec.activated`, §6.1); NULL = never logged in (User.activated=false)
);

CREATE TABLE sessions(
  token_hash TEXT PRIMARY KEY,                                       -- sha256 hex of the opaque token (its first 16 hex chars are the public session id, §4.2)
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL, last_used_at REAL NOT NULL, expires_at REAL NOT NULL,
  user_agent TEXT, ip TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE chats(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('direct','group')),
  title TEXT,                                                        -- groups only (1..60 chars)
  description TEXT NOT NULL DEFAULT '',                              -- <=500 chars
  direct_key TEXT UNIQUE,                                            -- "minUserId:maxUserId" (direct only; self-chat "5:5")
  is_default INTEGER NOT NULL DEFAULT 0,                             -- the auto-joined "Everyone" group
  only_admins_post INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id), created_at REAL NOT NULL,
  last_message_id INTEGER, last_activity_at REAL NOT NULL
);

CREATE TABLE chat_members(
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin','member')),
  joined_at REAL NOT NULL,
  history_from_id INTEGER NOT NULL DEFAULT 0,      -- member can't see messages with id <= this (joined later)
  cleared_before_id INTEGER NOT NULL DEFAULT 0,    -- "clear chat" (for me only)
  last_read_id INTEGER NOT NULL DEFAULT 0,         -- PRIVATE truth for unread counters
  read_receipt_id INTEGER NOT NULL DEFAULT 0,      -- PUBLIC read watermark; advances only while users.read_receipts=1
  delivered_id INTEGER NOT NULL DEFAULT 0,         -- PUBLIC delivered watermark
  muted_until REAL NOT NULL DEFAULT 0,             -- 0 = not muted; 4102444800 = forever
  pinned_at REAL,                                  -- pinned chat (NULL = not pinned)
  archived INTEGER NOT NULL DEFAULT 0,
  listed INTEGER NOT NULL DEFAULT 1,               -- 0 only for the PEER of a dormant direct chat (§3.2(3)): such a row counts as NOT a member until the first message or the peer's own chat.open_direct
  PRIMARY KEY(chat_id,user_id)
);
CREATE INDEX chat_members_user ON chat_members(user_id);

CREATE TABLE attachments(
  id TEXT PRIMARY KEY,                                               -- 32 hex chars (uuid4().hex)
  uploader_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL, mime TEXT NOT NULL,                            -- name sanitised (§5.6); mime = SNIFFED type, never the client's (§5.5)
  kind TEXT NOT NULL CHECK(kind IN ('image','audio','video','file')),
  size INTEGER NOT NULL, path TEXT NOT NULL,                         -- relative to data/uploads
  width INTEGER, height INTEGER, duration REAL,
  created_at REAL NOT NULL
);
CREATE INDEX attachments_uploader ON attachments(uploader_id, created_at);   -- upload quotas (§5.1)
CREATE INDEX attachments_created ON attachments(created_at);                 -- orphan sweep (§2.4)

CREATE TABLE messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  sender_id INTEGER REFERENCES users(id),                            -- NULL for system messages
  client_id TEXT,                                                    -- idempotency key from the client
  kind TEXT NOT NULL CHECK(kind IN ('text','image','audio','video','file','system')),
  body TEXT NOT NULL DEFAULT '',
  reply_to_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
  forwarded INTEGER NOT NULL DEFAULT 0,
  attachment_id TEXT REFERENCES attachments(id),
  mentions TEXT NOT NULL DEFAULT '[]',                               -- JSON array of user ids: serialisation cache of message_mentions (never queried in SQL)
  system TEXT,                                                       -- JSON, only for kind='system'
  created_at REAL NOT NULL, edited_at REAL, deleted_at REAL
);
CREATE UNIQUE INDEX messages_client ON messages(sender_id, client_id) WHERE client_id IS NOT NULL;
CREATE INDEX messages_chat ON messages(chat_id, id);
CREATE INDEX messages_attachment ON messages(attachment_id) WHERE attachment_id IS NOT NULL;   -- /files access check, orphan sweep
CREATE INDEX messages_reply ON messages(reply_to_id) WHERE reply_to_id IS NOT NULL;            -- FK ON DELETE SET NULL
CREATE INDEX messages_sender ON messages(sender_id, id);                                       -- admin.users message_count
CREATE TABLE message_mentions(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY(message_id,user_id));   -- server-derived (§7.4)
CREATE INDEX message_mentions_user ON message_mentions(user_id, message_id);

CREATE TABLE receipts(
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,   -- the RECIPIENT
  delivered_at REAL, read_at REAL,                                   -- written only for chats with <= 50 members (§3.3); read_at only when the PUBLIC read watermark advances
  PRIMARY KEY(message_id,user_id)
);
CREATE TABLE reactions(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, emoji TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(message_id,user_id));                                  -- one reaction per user per message
CREATE TABLE stars(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, created_at REAL NOT NULL,
  PRIMARY KEY(user_id,message_id));                                  -- stars.created_at is reserved (not read in v2; stars are ordered by message id)
CREATE TABLE pins(chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, pinned_by INTEGER REFERENCES users(id),
  pinned_at REAL NOT NULL, PRIMARY KEY(chat_id,message_id));          -- max 5 per chat; pinned_by is audit-only (never serialised)
CREATE TABLE hidden_messages(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, PRIMARY KEY(user_id,message_id)); -- "delete for me"
CREATE TABLE audit_log(id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL,
  target_id INTEGER, ip TEXT);                                       -- every admin.* mutation, CLI create/reset and restore (CLI actor NULL); `action` vocabulary and `target_id` meaning: Rules below
"""  # noqa: E501


def split_statements(script: str) -> List[str]:
    """Split an SQL script into single statements with ``sqlite3.complete_statement`` (comments stay attached)."""
    statements: List[str] = []
    buffer: List[str] = []
    for line in script.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate.strip())
            buffer = []
    return statements


def get_meta(conn: Any, key: str, default: Optional[str] = None) -> Optional[str]:
    """``meta.value`` for ``key`` or ``default``."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else default


def set_meta(conn: Any, key: str, value: Any) -> None:
    """Upsert ``meta(key, str(value))``."""
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _migrate_v1(conn: Any) -> None:
    for statement in split_statements(SCHEMA_V1_DDL):
        conn.execute(statement)
    set_meta(conn, "instance_id", util.new_id())


#: ``MIGRATIONS[i]`` upgrades schema version ``i`` to ``i + 1`` inside one ``BEGIN IMMEDIATE`` transaction.
MIGRATIONS: List[Callable[[Any], None]] = [_migrate_v1]
SCHEMA_VERSION = len(MIGRATIONS)


def read_schema_version(conn: Any) -> int:
    """The stored schema version; ``0`` for an empty database. Raises ``FatalError`` for a foreign database."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "meta" not in tables:
        if tables - {"sqlite_sequence"}:
            raise FatalError("this file is not a DeskTalk database (no meta table)")
        return 0
    value = get_meta(conn, "schema_version")
    try:
        return int(value) if value is not None else 0
    except ValueError:
        raise FatalError("this file is not a DeskTalk database (bad schema_version)")


def _rollback(conn: Any) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            log.error("ROLLBACK failed: %s", type(exc).__name__)


def snapshot_database(source: Any, dest_path: str, pages: int = 256, sleep: float = 0.02) -> None:
    """Write a consistent copy of ``source`` (a connection) to ``dest_path`` with the sqlite backup API.

    The copy is built in ``<dest>.tmp`` (``pages`` per step, ``sleep`` seconds between steps), the ``sessions`` table
    is emptied (``secure_delete`` so no token hash survives in free pages), it is switched to ``journal_mode=DELETE``
    (a single self-contained file) and then moved to ``dest_path`` with :func:`util.retry_file_op`.
    """
    _require_sqlite()
    temp = dest_path + ".tmp"
    util.retry_file_op(os.remove, temp)
    target = sqlite3.connect(temp, isolation_level=None)
    try:
        source.backup(target, pages=pages, sleep=sleep)
        target.execute("PRAGMA secure_delete = ON")
        target.execute("DELETE FROM sessions")
        target.execute("PRAGMA journal_mode = DELETE")
    except BaseException:
        target.close()
        util.retry_file_op(os.remove, temp)
        raise
    target.close()
    util.retry_file_op(os.replace, temp, dest_path)


def apply_migrations(conn: Any, backup_dir: str) -> Tuple[int, int]:
    """Bring ``conn``'s database to ``len(MIGRATIONS)`` and return ``(old_version, new_version)``.

    A database that already holds data is first copied to ``<backup_dir>/pre-migrate-v<old>-<timestamp>.db``; a
    newer-than-code database raises :class:`SchemaTooNew` before anything is touched. Each step runs in its own
    ``BEGIN IMMEDIATE`` and bumps ``meta.schema_version`` in the same transaction.
    """
    target = len(MIGRATIONS)
    old = read_schema_version(conn)
    if old > target:
        raise SchemaTooNew(old, target)
    if old == target:
        return old, old
    if old >= 1:
        os.makedirs(backup_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(backup_dir, "pre-migrate-v%d-%s.db" % (old, stamp))
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(backup_dir, "pre-migrate-v%d-%s-%d.db" % (old, stamp, counter))
            counter += 1
        snapshot_database(conn, dest)
        log.info("wrote %s before migrating schema v%d to v%d", os.path.basename(dest), old, target)
    for version in range(old + 1, target + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # another process (the CLI, a second server start) may have migrated while we waited for the write lock
            applied = read_schema_version(conn) >= version
            if not applied:
                MIGRATIONS[version - 1](conn)
                set_meta(conn, "schema_version", version)
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
        if not applied:
            log.info("database migrated to schema v%d", version)
    return old, target


# --------------------------------------------------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------------------------------------------------


def register_functions(conn: Any) -> None:
    """Register the SQL function ``fold(x)`` (casefold, deterministic; SPEC §2.3) on a connection.

    ``open_connection`` and therefore every ``Database`` connection already does this; call it only on a connection
    you opened yourself with ``sqlite3.connect``.
    """
    conn.create_function("fold", 1, util.fold, deterministic=True)


#: How long ``open_connection`` keeps retrying ``PRAGMA journal_mode=WAL`` before it reports ``WalUnavailable``.
WAL_RETRY_SECONDS = 3.0


def _enable_wal(conn: Any) -> None:
    """Switch to WAL and assert it (SPEC §2.3).

    While another process converts the same new file (two CLI commands or a CLI and a server starting together) the
    pragma can answer ``delete`` or fail with ``database is locked`` for a moment; both are retried for
    ``WAL_RETRY_SECONDS`` before ``WalUnavailable`` (a file system that really cannot do WAL) is raised.
    """
    deadline = time.monotonic() + WAL_RETRY_SECONDS
    while True:
        try:
            mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        except sqlite3.OperationalError as exc:
            transient = "locked" in str(exc) or "busy" in str(exc)
            if not transient or time.monotonic() >= deadline:
                raise
        else:
            if mode.lower() == "wal":
                return
            if time.monotonic() >= deadline:
                log.error("SQLite answered journal_mode=%s instead of wal: the data dir needs a local disk", mode)
                raise WalUnavailable(mode)
        time.sleep(0.05)


def open_connection(path: str, role: str = "extra") -> Any:
    """Open one connection with the pragmas of SPEC §3 and ``fold()`` registered.

    ``role``: ``"writer"`` and ``"extra"`` (own connection for backups/sweeps/CLI) use ``synchronous=FULL``;
    ``"reader"`` adds ``query_only=ON``. Always ``isolation_level=None`` (autocommit, no implicit transactions) and
    plain tuple rows. The caller owns the connection and must use it from the creating thread only.
    Raises :class:`WalUnavailable` when WAL cannot be enabled.
    """
    _require_sqlite()
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=True, timeout=5.0)
    try:
        register_functions(conn)
        _enable_wal(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if role == "reader":
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA synchronous = FULL")
    except BaseException:
        conn.close()
        raise
    return conn


def request_errors() -> Tuple[type, ...]:
    """Every protocol-level exception type a ``db.*`` function raises: ``RequestError`` and ``db_chats.ChatError``.

    Both carry ``code``, ``msg``, ``reason``, ``retry_after``, ``chat_id``, ``message_id`` and ``to_err()``; use
    ``except db.request_errors() as exc`` until the two classes are unified.
    """
    chat_error = getattr(sys.modules.get(_qualified("db_chats")), "ChatError", None)
    return (RequestError,) if chat_error is None else (RequestError, chat_error)


def data_version(conn: Any) -> int:
    """``PRAGMA data_version`` (use with :meth:`Database.run_raw`, SPEC §2.4)."""
    return int(conn.execute("PRAGMA data_version").fetchone()[0])


# --------------------------------------------------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------------------------------------------------


class _Pool:
    """``size`` threads that each own one connection and serve one shared FIFO queue.

    With ``size == 1`` jobs run, and their futures resolve, in submission order (the writer relies on it, SPEC §7.6).
    """

    def __init__(self, name: str, size: int, connect: Callable[[], Any], finalize: Callable[[Any], None]) -> None:
        self._name = name
        self._size = size
        self._connect = connect
        self._finalize = finalize
        self._jobs: Any = queue.SimpleQueue()
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        """Start the threads and wait until each connection is open; on failure stop them and re-raise."""
        ready: List[Future] = []
        for index in range(self._size):
            future: Future = Future()
            thread = threading.Thread(
                target=self._main, args=(future,), name="%s-%d" % (self._name, index), daemon=True
            )
            thread.start()
            self._threads.append(thread)
            ready.append(future)
        try:
            for future in ready:
                future.result()
        except BaseException:
            self.stop()
            raise

    def _main(self, ready: Future) -> None:
        try:
            conn = self._connect()
        except BaseException as exc:  # noqa: BLE001 - handed to start(), which re-raises it
            ready.set_exception(exc)
            return
        ready.set_result(None)
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                call, future = job
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = call(conn)
                except BaseException as exc:  # noqa: BLE001 - delivered to the awaiting caller
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        finally:
            try:
                self._finalize(conn)
            except sqlite3.Error as exc:
                log.error("closing a %s connection failed: %s", self._name, type(exc).__name__)

    def submit(self, call: Callable[[Any], Any]) -> Future:
        """Queue ``call(conn)``; the returned ``concurrent.futures.Future`` carries its result or exception."""
        future: Future = Future()
        self._jobs.put((call, future))
        return future

    def stop(self) -> None:
        """Let queued jobs finish, then close every connection on its own thread and join the threads."""
        for _ in self._threads:
            self._jobs.put(None)
        for thread in self._threads:
            thread.join(timeout=30)
            if thread.is_alive():
                log.error("database thread %s did not stop within 30 s", thread.name)
        self._threads = []


def _finalize_writer(conn: Any) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _finalize_reader(conn: Any) -> None:
    conn.close()


class _Completed:
    """An already-finished awaitable: ``Database.close()`` is blocking but ``await db.close()`` works as well."""

    def __await__(self) -> Iterator[Any]:
        return iter(())


class Database:
    """One writer thread plus reader threads over one SQLite file (SPEC §2.3, §6.2).

    Lifecycle: ``start()`` (threads, pragmas, WAL assertion), then ``migrate()`` (backup first, then the ordered
    migrations; ``open()`` does both), then ``run``/``run_raw``/``run_read``/``run_sync``, finally ``close()``.
    ``fn`` arguments are plain synchronous callables ``fn(conn, *a)`` that run on a database thread: they must not
    touch asyncio, the hub or the transport, and must never commit, roll back or run scripts. Rows are plain tuples.
    """

    def __init__(self, path: str, cfg: Any = None, readers: int = 3) -> None:
        if isinstance(cfg, int) and not isinstance(cfg, bool):  # Database(path, <reader count>)
            readers, cfg = cfg, None
        self.path = str(path)
        self.schema_version = 0
        data_dir = getattr(cfg, "data_dir", None)
        self._backup_dir = util.backups_dir(str(data_dir) if data_dir else os.path.dirname(os.path.abspath(self.path)))
        self._reader_count = max(1, int(readers))
        self._writer: Optional[_Pool] = None
        self._readers: Optional[_Pool] = None
        self._closed = False

    # -- lifecycle -------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Create the parent directory, start the writer and reader threads, set the pragmas, assert WAL (blocking).

        Raises ``util.FatalError`` (exit code 78): ``sqlite3`` missing/old, an unusable data dir, WAL unavailable
        (:class:`WalUnavailable`). Nothing keeps running after a failure.
        """
        if self._writer is not None:
            raise DbError("database already started")
        _require_sqlite()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        except OSError as exc:
            raise FatalError("cannot create the data directory: %s" % type(exc).__name__)
        writer = _Pool("db-writer", 1, partial(open_connection, self.path, "writer"), _finalize_writer)
        readers = _Pool(
            "db-reader", self._reader_count, partial(open_connection, self.path, "reader"), _finalize_reader
        )
        try:
            writer.start()
            readers.start()
        except FatalError:
            writer.stop()
            raise
        except (sqlite3.Error, OSError) as exc:
            writer.stop()
            raise FatalError("cannot open the database %s: %s" % (self.path, exc))
        self._writer = writer
        self._readers = readers

    def migrate(self) -> None:
        """Back up and migrate to ``SCHEMA_VERSION`` on the writer thread (blocking).

        Writes ``<data>/backups/pre-migrate-v<old>-<ts>.db`` first when the file already holds a schema; raises
        :class:`SchemaTooNew` (a ``util.FatalError``) when ``meta.schema_version`` is newer than this program.
        """
        try:
            _, self.schema_version = (
                self._pool(self._writer).submit(partial(apply_migrations, backup_dir=self._backup_dir)).result()
            )
        except sqlite3.DatabaseError as exc:
            if type(exc) is not sqlite3.DatabaseError:  # Operational (locked), Integrity, Programming: not corruption
                raise
            raise FatalError("the database file is damaged or is not a DeskTalk database: %s" % exc)

    def open(self) -> None:
        """``start()`` followed by ``migrate()``; closes everything again when the migration fails."""
        self.start()
        try:
            self.migrate()
        except BaseException:
            self.close()
            raise

    def close(self) -> Any:
        """Finish queued jobs, then ``wal_checkpoint(TRUNCATE)`` and close the writer connection ON the writer thread
        and every reader connection on its own thread (blocking, idempotent). The returned object is awaitable so that
        ``await db.close()`` is equally valid.
        """
        if not self._closed:
            self._closed = True
            for pool in (self._writer, self._readers):
                if pool is not None:
                    pool.stop()
        return _Completed()

    def connect_extra(self) -> Any:
        """A new connection for backups, orphan sweeps and CLI jobs: the writer's pragmas, ``fold`` registered.

        The caller closes it and uses it from one thread. Never use it for per-request work.
        """
        return open_connection(self.path, "extra")

    # -- jobs ------------------------------------------------------------------------------------------------------

    def _pool(self, pool: Optional[_Pool]) -> _Pool:
        if self._closed or pool is None:
            raise DatabaseClosed("database is closed")
        return pool

    @staticmethod
    def _transaction(fn: Callable[..., Any], args: Tuple[Any, ...], conn: Any) -> Any:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn, *args)
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
        return result

    async def run(self, fn: Callable[..., Any], *a: Any) -> Any:
        """Run ``fn(conn, *a)`` on the writer inside ``BEGIN IMMEDIATE`` .. ``COMMIT``.

        Any exception rolls back (when a transaction is open) and is re-raised. Futures resolve in submission order.
        One logical operation is exactly one ``run`` (SPEC §2.3).
        """
        job = self._pool(self._writer).submit(partial(self._transaction, fn, a))
        return await asyncio.wrap_future(job, loop=asyncio.get_running_loop())

    def run_sync(self, fn: Callable[..., Any], *a: Any) -> Any:
        """Blocking :meth:`run` for non-asyncio callers (tests, seeding). Never call it from inside a db function."""
        return self._pool(self._writer).submit(partial(self._transaction, fn, a)).result()

    async def run_raw(self, fn: Callable[..., Any], *a: Any) -> Any:
        """Run ``fn(conn, *a)`` on the writer thread outside any transaction (``PRAGMA data_version`` only)."""
        job = self._pool(self._writer).submit(lambda conn: fn(conn, *a))
        return await asyncio.wrap_future(job, loop=asyncio.get_running_loop())

    @staticmethod
    def _read(fn: Callable[..., Any], args: Tuple[Any, ...], interrupt_after: Optional[float], conn: Any) -> Any:
        timer: Optional[threading.Timer] = None
        if interrupt_after is not None:
            timer = threading.Timer(interrupt_after, conn.interrupt)
            timer.daemon = True
            timer.start()
        try:
            conn.execute("BEGIN")
            try:
                result = fn(conn, *args)
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        except sqlite3.OperationalError as exc:
            if interrupt_after is not None and "interrupt" in str(exc).lower():
                raise ServerBusy("query interrupted after %.1f s" % interrupt_after)
            raise
        finally:
            if timer is not None:
                timer.cancel()
        return result

    async def run_read(self, fn: Callable[..., Any], *a: Any, interrupt_after: Optional[float] = None) -> Any:
        """Run ``fn(conn, *a)`` on a reader thread in one short deferred ``BEGIN`` .. ``COMMIT`` (``query_only``).

        With ``interrupt_after`` seconds a timer calls ``conn.interrupt()``; the interrupted query raises
        :class:`ServerBusy`.
        """
        job = self._pool(self._readers).submit(partial(self._read, fn, a, interrupt_after))
        return await asyncio.wrap_future(job, loop=asyncio.get_running_loop())


def open_for_seeding(cfg: Any) -> Database:
    """``Database(<data_dir>/chat.db, cfg)`` after ``start()`` and ``migrate()``; no sockets, no hub (SPEC §6.2).

    Offline tests seed rows with ``db.run_sync(db.<fn>, ...)`` and then ``close()``.
    """
    database = Database(util.db_path(str(cfg.data_dir)), cfg)
    database.open()
    return database


# --------------------------------------------------------------------------------------------------------------------
# The FACADE: every public name of the db_* modules is also reachable as ``db.<name>`` (SPEC §1, §6.2)
# --------------------------------------------------------------------------------------------------------------------

_FACADE_MODULES = ("db_users", "db_chats", "db_messages", "db_receipts")
_MISSING = object()


def _qualified(short: str) -> str:
    return "%s.%s" % (__name__.rpartition(".")[0], short)


def _public_names(module: Any) -> List[str]:
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return [name for name in declared if not name.startswith("_")]
    return [
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and getattr(value, "__module__", None) == module.__name__
    ]


def _export_facade() -> None:
    if sqlite3 is None:
        return  # no db function can run without sqlite3; importing the modules would only raise
    for short in _FACADE_MODULES:
        qualified = _qualified(short)
        try:
            module = importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name != qualified:
                raise
            continue
        for name in _public_names(module):
            value = getattr(module, name, _MISSING)
            if value is _MISSING:
                continue  # the module is still being imported (import cycle); __getattr__ finds it later
            existing = globals().get(name, _MISSING)
            if existing is _MISSING:
                globals()[name] = value
            elif existing is not value:
                log.warning(
                    "db facade: %s of %s is already exported by an earlier module; keeping that one", name, short
                )


def __getattr__(name: str) -> Any:
    """Resolve facade names that were not yet defined while an import cycle was running (PEP 562)."""
    if not name.startswith("_"):
        for short in _FACADE_MODULES:
            module = sys.modules.get(_qualified(short))
            if module is not None and name in _public_names(module):
                value = getattr(module, name, _MISSING)
                if value is not _MISSING:
                    return value
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


_export_facade()
