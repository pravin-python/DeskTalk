"""SQLite me chat history - server restart ke baad bhi messages bache rahte hain."""

import sqlite3

KEEP_MESSAGES = 5000   # isse purane rows hata diye jaate hain
PRUNE_EVERY = 200      # itne inserts ke baad ek baar prune


class HistoryStore:
    """Room ke public messages. `path=":memory:"` se sirf RAM me (tests / no-history)."""

    def __init__(self, path):
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user TEXT NOT NULL,"
            " text TEXT NOT NULL,"
            " ts REAL NOT NULL)"
        )
        self._db.commit()
        self._since_prune = 0

    def add(self, user, text, ts):
        """Message save karo, uski id return karo."""
        cur = self._db.execute(
            "INSERT INTO messages (user, text, ts) VALUES (?, ?, ?)", (user, text, ts))
        self._db.commit()
        self._since_prune += 1
        if self._since_prune >= PRUNE_EVERY:
            self._since_prune = 0
            self._db.execute(
                "DELETE FROM messages WHERE id <= (SELECT MAX(id) FROM messages) - ?",
                (KEEP_MESSAGES,))
            self._db.commit()
        return cur.lastrowid

    def last_id(self):
        row = self._db.execute("SELECT MAX(id) FROM messages").fetchone()
        return row[0] or 0

    def recent(self, limit, since_id=0):
        """since_id ke baad ke sabse naye `limit` messages, purane se naye order me."""
        rows = self._db.execute(
            "SELECT id, user, text, ts FROM ("
            " SELECT id, user, text, ts FROM messages WHERE id > ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id",
            (since_id, limit)).fetchall()
        return [{"type": "msg", "id": r[0], "user": r[1], "text": r[2], "ts": r[3]} for r in rows]

    def close(self):
        try:
            self._db.close()
        except sqlite3.Error:
            pass
