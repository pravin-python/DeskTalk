"""Chat history. SQLite on disk, so it survives a server restart.

Use :func:`open_store`. If the ``sqlite3`` module is missing (some minimal
Python installs) or the file cannot be opened, it falls back to a RAM-only
store with a warning instead of crashing the server.

Both stores share one interface and are thread-safe.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List

from .errors import StoreError

try:
    import sqlite3
except ImportError:  # pragma: no cover - depends on the Python build
    sqlite3 = None  # type: ignore[assignment]

__all__ = ["StoreError", "MemoryStore", "HistoryStore", "open_store"]

log = logging.getLogger(__name__)

KEEP_MESSAGES = 5000   # older rows are pruned
PRUNE_EVERY = 200      # prune once per this many inserts

Row = Dict[str, Any]


class MemoryStore:
    """RAM-only history; lost on restart. Same interface as HistoryStore."""

    persistent = False

    def __init__(self) -> None:
        self._rows: List[Row] = []
        self._next_id = 1
        self._lock = threading.Lock()

    def add(self, user: str, text: str, ts: float) -> int:
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            self._rows.append({"type": "msg", "id": mid, "user": user, "text": text, "ts": ts})
            del self._rows[:-KEEP_MESSAGES]
            return mid

    def last_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    def recent(self, limit: int, since_id: int = 0) -> List[Row]:
        """Newest ``limit`` messages with id > since_id, oldest first."""
        if limit <= 0:
            return []
        with self._lock:
            rows = [r for r in self._rows if r["id"] > since_id]
            return [dict(r) for r in rows[-limit:]]

    def close(self) -> None:
        pass


class HistoryStore:
    """SQLite-backed history. ``path=":memory:"`` also works."""

    persistent = True

    def __init__(self, path: str) -> None:
        if sqlite3 is None:
            raise StoreError("sqlite3 module is missing in this Python environment")
        self.path = path
        self._lock = threading.Lock()
        self._since_prune = 0
        try:
            self._db = sqlite3.connect(path, check_same_thread=False)
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error as exc:  # e.g. network drives; WAL is only an optimisation
                log.debug("WAL mode unavailable: %s", exc)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user TEXT NOT NULL,"
                " text TEXT NOT NULL,"
                " ts REAL NOT NULL)"
            )
            self._db.commit()
        except sqlite3.Error as exc:
            raise StoreError(str(exc))

    def _run(self, fn: Callable[[], Any]) -> Any:
        """Run a DB operation under the lock, translating sqlite errors to StoreError."""
        with self._lock:
            try:
                return fn()
            except sqlite3.Error as exc:
                try:
                    self._db.rollback()
                except sqlite3.Error:
                    pass
                raise StoreError(str(exc))

    def add(self, user: str, text: str, ts: float) -> int:
        """Save a message and return its id."""
        def op() -> int:
            cur = self._db.execute(
                "INSERT INTO messages (user, text, ts) VALUES (?, ?, ?)", (user, text, ts))
            self._since_prune += 1
            if self._since_prune >= PRUNE_EVERY:
                self._since_prune = 0
                self._db.execute(
                    "DELETE FROM messages WHERE id <= (SELECT MAX(id) FROM messages) - ?",
                    (KEEP_MESSAGES,))
            self._db.commit()
            return int(cur.lastrowid)
        return self._run(op)

    def last_id(self) -> int:
        def op() -> int:
            row = self._db.execute("SELECT MAX(id) FROM messages").fetchone()
            return int(row[0] or 0)
        return self._run(op)

    def recent(self, limit: int, since_id: int = 0) -> List[Row]:
        """Newest ``limit`` messages with id > since_id, oldest first."""
        if limit <= 0:
            return []

        def op() -> List[Row]:
            rows = self._db.execute(
                "SELECT id, user, text, ts FROM ("
                " SELECT id, user, text, ts FROM messages WHERE id > ? ORDER BY id DESC LIMIT ?"
                ") ORDER BY id",
                (since_id, limit)).fetchall()
            return [{"type": "msg", "id": r[0], "user": r[1], "text": r[2], "ts": r[3]} for r in rows]
        return self._run(op)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass


def open_store(path: str, log_fn: Callable[[str], None] = log.warning) -> "MemoryStore | HistoryStore":
    """Open the SQLite store; on any failure fall back to RAM (and say so)."""
    if path == ":memory:":
        return MemoryStore()
    try:
        return HistoryStore(path)
    except StoreError as exc:
        log_fn("Could not open history file ({}) - history will remain in RAM only.".format(exc))
        return MemoryStore()
