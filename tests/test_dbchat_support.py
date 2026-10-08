"""Shared fixtures for the db-chat suites (``chatd/db_chats.py``, ``db_messages.py``, ``db_receipts.py``).

Not a test module itself: it holds :class:`DbChatCase`, a ``unittest.TestCase`` base class that opens a real
``db.Database`` in a temporary directory (writer through ``run_sync``, reader through a ``query_only`` connection),
controls ``util.now`` with a strictly increasing fake clock and offers small helpers to create users, chats and
messages without going through the hub.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Callable, Dict, List, Optional, Set
from unittest import mock

from chatd import db, db_chats, db_messages, util
from chatd.db_chats import ChatError


class Clock:
    """A fake ``util.now``: every call moves time forward by one millisecond; :meth:`advance` jumps."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        self.t += 0.001
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class DbChatCase(unittest.TestCase):
    """Real database + helpers.  ``self.write`` is the writer, ``self.read`` a reader connection."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dbchat-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "chat.db")
        self.database = db.Database(self.path)
        self.database.open()
        self.addCleanup(self.database.close)
        self.reader = db.open_connection(self.path, "reader")
        self.addCleanup(self.reader.close)
        self.clock = Clock()
        patcher = mock.patch.object(util, "now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        db_chats.configure_limits(max_body_chars=8000, edit_window_s=900, delete_window_s=172800, unread_cap=1000)
        self.everyone: Optional[int] = None
        self._ids = itertools.count(1)
        self._names: Dict[str, int] = {}

    # -- plumbing --------------------------------------------------------------------------------------------------

    def write(self, fn: Callable[..., Any], *args: Any) -> Any:
        result = self.database.run_sync(fn, *args)
        self.assert_wire_ready(result)
        return result

    def read(self, fn: Callable[..., Any], *args: Any) -> Any:
        self.reader.execute("BEGIN")
        try:
            result = fn(self.reader, *args)
        finally:
            self.reader.execute("COMMIT")
        self.assert_wire_ready(result)
        return result

    def assert_wire_ready(self, result: Any) -> None:
        """Everything a function hands to the hub for ``json.dumps`` must be JSON-serialisable."""
        if not isinstance(result, dict):
            return
        if "res" in result:  # an outcome: the res payload and every event payload go to json.dumps
            payload = [result["res"]] + [g["d"] for ev in result["events"] for g in ev["groups"]]
        elif all(isinstance(key, str) for key in result):  # a read function returns the res payload itself
            payload = [result]
        else:
            return
        json.dumps(payload, allow_nan=False)

    def sql(self, sql: str, params: Any = ()) -> List[Any]:
        return self.write(lambda conn: conn.execute(sql, params).fetchall())

    def scalar(self, sql: str, params: Any = ()) -> Any:
        rows = self.sql(sql, params)
        return rows[0][0] if rows else None

    def fails(
        self, code: str, fn: Callable[..., Any], *args: Any, reason: Optional[str] = None, reader: bool = False
    ) -> ChatError:
        """Run ``fn`` expecting ``ChatError(code)``; returns the error for further assertions."""
        runner = self.read if reader else self.write
        with self.assertRaises(ChatError) as caught:
            runner(fn, *args)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    # -- users -----------------------------------------------------------------------------------------------------

    def add_user(
        self,
        username: str,
        role: str = "member",
        activated: bool = True,
        disabled: bool = False,
        read_receipts: bool = True,
        show_last_seen: bool = True,
    ) -> int:
        now = util.now()

        def insert(conn: Any) -> int:
            cur = conn.execute(
                "INSERT INTO users(username, display_name, display_key, pw_hash, role, read_receipts, show_last_seen,"
                " disabled, created_at, last_login_at) VALUES (?, ?, ?, 'x', ?, ?, ?, ?, ?, ?)",
                (
                    username,
                    username.title(),
                    util.display_key(username.title()),
                    role,
                    int(read_receipts),
                    int(show_last_seen),
                    int(disabled),
                    now,
                    now if activated else None,
                ),
            )
            return int(cur.lastrowid)

        uid = self.write(insert)
        self._names[username] = uid
        return uid

    def user(self, username: str, **kwargs: Any) -> int:
        """Create a user and join ``Everyone`` like registration does (the first user becomes its admin)."""
        first = self.everyone is None
        uid = self.add_user(username, role="admin" if first else kwargs.pop("role", "member"), **kwargs)

        def join(conn: Any) -> None:
            ts = util.now()
            if first:
                self.everyone = db_chats.create_everyone_chat(conn, "Everyone", uid, ts)
                db_chats.add_member_row(conn, self.everyone, uid, "admin", ts)
                db_messages.insert_system_message(conn, self.everyone, "created", uid, [uid], "Welcome", ts=ts)
            else:
                db_chats.add_member_row(conn, self.everyone, uid, "member", ts)
                db_messages.insert_system_message(
                    conn, self.everyone, "joined", None, [uid], "joined", bump_activity=False, ts=ts
                )

        self.write(join)
        return uid

    def set_user(self, uid: int, **columns: Any) -> None:
        for column, value in columns.items():
            self.sql("UPDATE users SET %s = ? WHERE id = ?" % column, (value, uid))

    # -- chats and messages ----------------------------------------------------------------------------------------

    def group(self, owner: int, *members: int, title: str = "Team", description: Optional[str] = None) -> int:
        out = self.write(db_chats.chat_create_group, owner, title, list(members), description)
        return int(out["res"]["chat"]["id"])

    def dm(self, user: int, peer: int) -> int:
        return int(self.write(db_chats.chat_open_direct, user, peer)["res"]["chat"]["id"])

    def send(
        self,
        user: int,
        chat_id: int,
        body: Optional[str] = "hello",
        client_id: Optional[str] = None,
        attachment_id: Optional[str] = None,
        reply_to_id: Optional[int] = None,
        seen_up_to_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        cid = client_id or "cid-%08d" % next(self._ids)
        return self.write(db_messages.msg_send, user, chat_id, cid, body, attachment_id, reply_to_id, seen_up_to_id)

    def mid(self, outcome: Dict[str, Any]) -> int:
        return int(outcome["res"]["message"]["id"])

    def add_attachment(self, uploader: int, kind: str = "image", name: str = "a.png") -> str:
        attachment_id = util.new_id()
        self.sql(
            "INSERT INTO attachments(id, uploader_id, name, mime, kind, size, path, created_at)"
            " VALUES (?, ?, ?, 'image/png', ?, 10, ?, ?)",
            (attachment_id, uploader, name, kind, "aa/" + attachment_id, util.now()),
        )
        return attachment_id

    def chat_of(self, user: int, chat_id: int) -> Dict[str, Any]:
        return self.read(db_chats.chat_get, user, chat_id)["chat"]

    def me(self, user: int, chat_id: int) -> Dict[str, Any]:
        return self.chat_of(user, chat_id)["me"]

    # -- event inspection ------------------------------------------------------------------------------------------

    @staticmethod
    def types(outcome: Dict[str, Any]) -> List[str]:
        return [e["t"] for e in outcome["events"]]

    @staticmethod
    def payloads(outcome: Dict[str, Any], t: str, user: int) -> List[Dict[str, Any]]:
        """The ``d`` payloads of every ``t`` event that reaches ``user`` (``None`` audience = everybody)."""
        found = []
        for ev in outcome["events"]:
            if ev["t"] != t:
                continue
            for g in ev["groups"]:
                if g["user_ids"] is None or user in g["user_ids"]:
                    found.append(g["d"])
        return found

    @staticmethod
    def audience(outcome: Dict[str, Any], t: str) -> Set[int]:
        users: Set[int] = set()
        for ev in outcome["events"]:
            if ev["t"] == t:
                for g in ev["groups"]:
                    users.update(g["user_ids"] or [])
        return users
