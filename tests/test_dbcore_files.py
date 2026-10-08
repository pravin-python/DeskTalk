"""Tests of the attachment functions of ``chatd.db_users``: insert, access rule, quotas, orphan sweep, query plans."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chatd import db, util  # noqa: E402

HOUR = 3600.0


def att_id(number: int) -> str:
    return "%032x" % number


class FilesCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.uploads = os.path.join(self.data, "uploads")
        self.database = db.Database(os.path.join(self.data, "chat.db"))
        self.database.open()
        spec = {"username": "ravi", "display_name": "Ravi", "pw_hash": "h", "activated": True}
        await self.database.run(db.register_user, spec, 2000, self.data)
        for name in ("amit", "carol", "dave", "erin", "frank", "gina"):
            await self.database.run(
                db.register_user, dict(spec, username=name, display_name=name.title(), role="member"), 2000, self.data
            )

    async def asyncTearDown(self) -> None:
        await self.database.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def attachment(self, number: int, size: int = 100, **extra: Any) -> Dict[str, Any]:
        base = {
            "id": att_id(number),
            "name": "file%d.png" % number,
            "mime": "image/png",
            "kind": "image",
            "size": size,
            "path": "%s/%s" % (att_id(number)[:2], att_id(number)),
            "width": 10,
            "height": 20,
            "duration": None,
        }
        base.update(extra)
        return base

    async def insert(
        self, uploader: int, number: int, size: int = 100, ts: Optional[float] = None, **limits: Any
    ) -> Any:
        return await self.database.run(
            lambda conn: db.insert_attachment(conn, uploader, self.attachment(number, size), ts=ts, **limits)
        )

    async def sql(self, statement: str, *params: Any) -> None:
        await self.database.run(lambda conn: conn.execute(statement, params))

    async def query(self, statement: str, *params: Any) -> list:
        return await self.database.run_read(lambda conn: conn.execute(statement, params).fetchall())

    async def message(self, message_id: int, chat_id: int, sender: int, attachment: Optional[str], **cols: Any) -> None:
        deleted = cols.get("deleted_at")
        await self.sql(
            "INSERT INTO messages(id, chat_id, sender_id, kind, body, attachment_id, created_at, deleted_at)"
            " VALUES (?, ?, ?, 'image', '', ?, 1, ?)",
            message_id,
            chat_id,
            sender,
            attachment,
            deleted,
        )

    async def member(self, chat_id: int, user_id: int, **cols: Any) -> None:
        await self.sql(
            "INSERT INTO chat_members(chat_id, user_id, joined_at, history_from_id, cleared_before_id, listed)"
            " VALUES (?, ?, 1, ?, ?, ?)",
            chat_id,
            user_id,
            cols.get("history_from_id", 0),
            cols.get("cleared_before_id", 0),
            cols.get("listed", 1),
        )

    async def group(self, chat_id: int) -> None:
        await self.sql(
            "INSERT INTO chats(id, kind, title, created_by, created_at, last_activity_at)"
            " VALUES (?, 'group', 'G', 1, 1, 1)",
            chat_id,
        )

    async def expect(self, code: str, awaitable: Any) -> db.RequestError:
        with self.assertRaises(db.RequestError) as raised:
            await awaitable
        self.assertEqual(raised.exception.code, code)
        return raised.exception


class InsertAndQuotaTest(FilesCase):
    async def test_insert_returns_the_public_object(self) -> None:
        result = await self.insert(2, 1, 123, ts=50.0)
        self.assertEqual(
            result,
            {
                "id": att_id(1),
                "name": "file1.png",
                "mime": "image/png",
                "size": 123,
                "kind": "image",
                "url": "/files/" + att_id(1),
                "width": 10,
                "height": 20,
                "duration": None,
            },
        )
        stored = await self.database.run_read(db.get_attachment, att_id(1))
        self.assertEqual(
            (stored["uploader_id"], stored["path"], stored["created_at"]), (2, att_id(1)[:2] + "/" + att_id(1), 50.0)
        )
        self.assertIsNone(await self.database.run_read(db.get_attachment, att_id(2)))

    async def test_insert_validates_uploader_and_id(self) -> None:
        await self.expect("unauthorized", self.insert(99, 1))
        await self.sql("UPDATE users SET disabled = 1 WHERE id = 2")
        await self.expect("unauthorized", self.insert(2, 1))
        await self.expect("bad_request", self.database.run(db.insert_attachment, 3, self.attachment(1, id="NOT-HEX")))
        await self.expect("bad_request", self.database.run(db.insert_attachment, 3, self.attachment(1, id="A" * 32)))
        self.assertEqual(await self.query("SELECT COUNT(*) FROM attachments"), [(0,)])

    async def test_malformed_rows_are_refused_and_hints_are_dropped(self) -> None:
        for bad in (
            {"kind": "svg"},
            {"kind": None},
            {"size": -1},
            {"size": True},
            {"size": "5"},
            {"name": ""},
            {"mime": None},
            {"path": ""},
            {"path": "../outside"},
            {"path": "ab/../../outside"},
            {"path": "/etc/passwd"},
            {"path": "C:/windows"},
            {"path": "ab" + chr(92) + ".." + chr(92) + "x"},
        ):
            await self.expect("bad_request", self.database.run(db.insert_attachment, 2, self.attachment(1, **bad)))
        self.assertEqual(await self.query("SELECT COUNT(*) FROM attachments"), [(0,)])
        hints = {"width": 0, "height": 99999, "duration": float("nan")}
        result = await self.database.run(db.insert_attachment, 2, self.attachment(1, **hints))
        self.assertEqual((result["width"], result["height"], result["duration"]), (None, None, None))
        self.assertEqual(await self.query("SELECT width, height, duration FROM attachments"), [(None, None, None)])
        good = self.attachment(2, kind="audio", width=None, height=None, duration=12)
        self.assertEqual((await self.database.run(db.insert_attachment, 2, good))["duration"], 12.0)
        as_file = self.attachment(3, kind="file", width=5, height=6, duration=3.5)
        stored = await self.database.run(db.insert_attachment, 2, as_file)
        self.assertEqual((stored["width"], stored["height"], stored["duration"]), (5, 6, None))  # no duration for files
        too_long = self.attachment(4, kind="video", duration=86401)
        self.assertIsNone((await self.database.run(db.insert_attachment, 2, too_long))["duration"])

    async def test_unattached_quota_counts_only_unreferenced_rows(self) -> None:
        now = util.now()
        await self.insert(2, 1, 400, now - 10)
        await self.insert(2, 2, 400, now - 10)
        await self.group(2)
        await self.member(2, 2)
        await self.message(10, 2, 2, att_id(1))  # attached: no longer counts
        usage = await self.database.run_read(db.upload_usage, 2, now)
        self.assertEqual(usage, {"unattached_bytes": 400, "recent_bytes": 800})
        await self.insert(2, 3, 600, now, max_unattached_bytes=1000)  # 400 + 600 == limit: allowed
        await self.expect("quota_exceeded", self.insert(2, 4, 1, now, max_unattached_bytes=1000))
        await self.insert(3, 5, 900, now, max_unattached_bytes=1000)  # another user has its own quota
        self.assertEqual(await self.query("SELECT COUNT(*) FROM attachments"), [(4,)])

    async def test_rolling_quota_uses_a_24_hour_window(self) -> None:
        now = util.now()
        await self.insert(2, 1, 700, now - 25 * HOUR)  # outside the window
        await self.insert(2, 2, 700, now - 23 * HOUR)
        self.assertEqual((await self.database.run_read(db.upload_usage, 2, now))["recent_bytes"], 700)
        await self.insert(2, 3, 300, now, max_recent_bytes=1000)
        await self.expect("quota_exceeded", self.insert(2, 4, 1, now, max_recent_bytes=1000))
        usage = await self.database.run_read(db.upload_usage, 2, now + 2 * HOUR)  # the 23 h old row left the window
        self.assertEqual(usage["recent_bytes"], 300)

    async def test_failed_quota_check_stores_nothing(self) -> None:
        await self.expect("quota_exceeded", self.insert(2, 1, 5000, max_unattached_bytes=10))
        self.assertEqual(await self.query("SELECT COUNT(*) FROM attachments"), [(0,)])

    async def test_storage_bytes(self) -> None:
        self.assertEqual(await self.database.run_read(db.attachment_storage_bytes), 0)
        await self.insert(2, 1, 111)
        await self.insert(3, 2, 222)
        self.assertEqual(await self.database.run_read(db.attachment_storage_bytes), 333)


class AccessRuleTest(FilesCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # users: 1 ravi, 2 amit, 3 carol, 4 dave, 5 erin, 6 frank, 7 gina
        await self.insert(2, 1)  # uploaded by amit
        await self.group(2)
        await self.member(2, 2)
        await self.member(2, 3, history_from_id=10)  # joined after the message: cannot see id 10
        await self.member(2, 4, listed=0)  # the unlisted peer counts as not a member
        await self.member(2, 5, cleared_before_id=10)  # cleared the chat
        await self.member(2, 6)
        await self.member(2, 7)
        await self.message(10, 2, 2, att_id(1))
        await self.sql("INSERT INTO hidden_messages(user_id, message_id) VALUES (6, 10)")

    async def can(self, user_id: int, number: int = 1) -> bool:
        return await self.database.run_read(db.attachment_access, user_id, att_id(number)) is not None

    async def test_the_matrix(self) -> None:
        self.assertTrue(await self.can(2), "the uploader")
        self.assertTrue(await self.can(7), "a member who can see the message")
        self.assertFalse(await self.can(1), "a user outside the chat (even an admin)")
        self.assertFalse(await self.can(3), "a late joiner: history_from_id >= message id")
        self.assertFalse(await self.can(4), "listed = 0")
        self.assertFalse(await self.can(5), "cleared_before_id >= message id")
        self.assertFalse(await self.can(6), "hid the message")
        self.assertFalse(await self.can(99), "unknown user")

    async def test_a_disabled_account_reaches_nothing(self) -> None:
        self.assertTrue(await self.can(2))
        await self.sql("UPDATE users SET disabled = 1 WHERE id IN (2, 7)")
        self.assertFalse(await self.can(2), "the uploader, now disabled")
        self.assertFalse(await self.can(7), "a member, now disabled")

    async def test_access_returns_the_row(self) -> None:
        row = await self.database.run_read(db.attachment_access, 7, att_id(1))
        self.assertEqual(
            (row["id"], row["path"], row["mime"], row["kind"], row["size"]),
            (att_id(1), att_id(1)[:2] + "/" + att_id(1), "image/png", "image", 100),
        )

    async def test_an_unattached_upload_is_private_to_its_uploader(self) -> None:
        await self.insert(2, 2)
        self.assertTrue(await self.can(2, 2))
        self.assertFalse(await self.can(7, 2))
        self.assertFalse(await self.can(1, 2))

    async def test_a_forwarded_copy_grants_access_through_the_other_chat(self) -> None:
        await self.group(3)
        await self.member(3, 1)
        await self.member(3, 3)
        await self.message(20, 3, 1, att_id(1), forwarded=1)
        self.assertTrue(await self.can(1))
        self.assertTrue(await self.can(3))  # carol cannot see the original but sees the copy
        await self.sql("UPDATE messages SET deleted_at = 5, attachment_id = NULL WHERE id = 20")
        self.assertFalse(await self.can(1))

    async def test_deleted_messages_grant_nothing(self) -> None:
        await self.insert(2, 3)
        await self.message(30, 2, 2, att_id(3), deleted_at=9.0)
        self.assertFalse(await self.can(7, 3))
        self.assertTrue(await self.can(2, 3))  # the uploader keeps access to their own upload

    async def test_leaving_the_chat_revokes_member_access_but_not_the_uploaders(self) -> None:
        await self.sql("DELETE FROM chat_members WHERE chat_id = 2 AND user_id IN (2, 7)")
        self.assertFalse(await self.can(7))
        self.assertTrue(await self.can(2))

    async def test_malformed_ids_never_reach_the_database(self) -> None:
        for bad in (
            None,
            5,
            "",
            "xyz",
            "A" * 32,
            att_id(1) + "0",
            att_id(1)[:-1],
            att_id(1) + "\n",
            "../" + att_id(1)[3:],
        ):
            self.assertIsNone(await self.database.run_read(db.attachment_access, 7, bad), bad)


class DropAttachmentTest(FilesCase):
    async def test_drop_only_when_unreferenced(self) -> None:
        await self.insert(2, 1)
        await self.insert(2, 2)
        await self.group(2)
        await self.member(2, 2)
        await self.message(10, 2, 2, att_id(1))
        self.assertIsNone(await self.database.run(db.drop_attachment_if_unreferenced, att_id(1)))
        self.assertEqual(
            await self.database.run(db.drop_attachment_if_unreferenced, att_id(2)), att_id(2)[:2] + "/" + att_id(2)
        )
        self.assertIsNone(await self.database.run(db.drop_attachment_if_unreferenced, att_id(2)))
        self.assertIsNone(await self.database.run(db.drop_attachment_if_unreferenced, att_id(3)))
        self.assertEqual(await self.query("SELECT id FROM attachments"), [(att_id(1),)])
        await self.sql("UPDATE messages SET attachment_id = NULL WHERE id = 10")  # delete-for-everyone
        self.assertEqual(
            await self.database.run(db.drop_attachment_if_unreferenced, att_id(1)), att_id(1)[:2] + "/" + att_id(1)
        )


class OrphanSweepTest(FilesCase):
    def write(self, relative: str, age_seconds: float = 0.0, body: bytes = b"x") -> str:
        path = os.path.join(self.uploads, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def exists(self, relative: str) -> bool:
        return os.path.exists(os.path.join(self.uploads, *relative.split("/")))

    def stored(self, number: int) -> str:
        return "%s/%s" % (att_id(number)[:2], att_id(number))

    async def sweep(self) -> Dict[str, int]:
        conn = self.database.connect_extra()
        try:
            return db.sweep_orphans(conn, self.uploads)
        finally:
            conn.close()

    async def test_the_sweep_of_spec_2_4(self) -> None:
        now = util.now()
        await self.group(2)
        await self.member(2, 2)
        # 1: unattached and older than 2 h: row and file go
        await self.insert(2, 1, ts=now - 3 * HOUR)
        self.write(self.stored(1), 3 * HOUR)
        # 2: unattached but young: stays
        await self.insert(2, 2, ts=now - 1 * HOUR)
        self.write(self.stored(2), 1 * HOUR)
        # 3: attached and old: stays
        await self.insert(2, 3, ts=now - 30 * HOUR)
        self.write(self.stored(3), 30 * HOUR)
        await self.message(10, 2, 2, att_id(3))
        # 4: young row whose file is missing, unreferenced: the row goes
        await self.insert(2, 4, ts=now - 60)
        # 5: row whose file is missing but referenced by a message: kept (and logged)
        await self.insert(2, 5, ts=now - 60)
        await self.message(11, 2, 2, att_id(5))
        # temp files
        self.write(".tmp/old.part", 2 * HOUR)
        self.write(".tmp/new.part", 60)
        self.write(".tmp/keep.txt", 5 * HOUR)
        # files without a row
        self.write("ab/" + att_id(100), 25 * HOUR)  # old orphan: deleted
        self.write("ab/" + att_id(101), 2 * HOUR)  # young orphan: kept
        self.write("ab/not-a-hex-name.dat", 48 * HOUR)  # not ours: kept
        self.write("zz/" + att_id(102), 48 * HOUR)  # not a shard directory: kept

        with self.assertLogs("chatd.db_users", level=logging.WARNING) as logged:
            counts = await self.sweep()
        self.assertEqual(
            counts, {"unattached": 1, "parts": 1, "missing_rows": 1, "orphan_files": 1, "missing_referenced": 1}
        )
        self.assertEqual(sum("referenced" in line for line in logged.output), 1)
        self.assertEqual(
            [r[0] for r in await self.query("SELECT id FROM attachments ORDER BY id")],
            [att_id(2), att_id(3), att_id(5)],
        )
        self.assertFalse(self.exists(self.stored(1)))
        self.assertTrue(self.exists(self.stored(2)))
        self.assertTrue(self.exists(self.stored(3)))
        self.assertFalse(self.exists(".tmp/old.part"))
        self.assertTrue(self.exists(".tmp/new.part"))
        self.assertTrue(self.exists(".tmp/keep.txt"))
        self.assertFalse(self.exists("ab/" + att_id(100)))
        self.assertTrue(self.exists("ab/" + att_id(101)))
        self.assertTrue(self.exists("ab/not-a-hex-name.dat"))
        self.assertTrue(self.exists("zz/" + att_id(102)))

    async def test_a_second_sweep_finds_nothing(self) -> None:
        await self.insert(2, 1, ts=util.now() - 3 * HOUR)
        self.write(self.stored(1), 3 * HOUR)
        self.assertEqual((await self.sweep())["unattached"], 1)
        self.assertEqual(
            await self.sweep(),
            {"unattached": 0, "parts": 0, "missing_rows": 0, "orphan_files": 0, "missing_referenced": 0},
        )

    async def test_works_without_an_uploads_directory(self) -> None:
        self.assertEqual((await self.sweep())["parts"], 0)

    async def test_paths_cannot_escape_the_uploads_directory(self) -> None:
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("precious")
        await self.insert(2, 1, ts=util.now() - 3 * HOUR)
        await self.sql("UPDATE attachments SET path = ? WHERE id = ?", "../../outside.txt", att_id(1))
        await self.sweep()
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(await self.query("SELECT COUNT(*) FROM attachments"), [(0,)])

    async def test_batches_cover_every_row(self) -> None:
        for number in range(1, 8):
            await self.insert(2, number, ts=util.now() - 60)
        conn = self.database.connect_extra()
        try:
            counts = db.sweep_orphans(conn, self.uploads, batch=3)
        finally:
            conn.close()
        self.assertEqual(counts["missing_rows"], 7)


class QueryPlanTest(FilesCase):
    async def plan(self, statement: str, params: tuple) -> List[str]:
        rows = await self.database.run_read(
            lambda conn: conn.execute("EXPLAIN QUERY PLAN " + statement, params).fetchall()
        )
        return [row[3] for row in rows]

    def assert_no_scan(self, details: List[str]) -> None:
        self.assertTrue(details)
        for detail in details:
            self.assertFalse(detail.startswith("SCAN"), "table scan in plan: %s" % details)

    async def test_upload_quota_queries(self) -> None:
        unattached = await self.plan(db.UPLOAD_UNATTACHED_BYTES_SQL, (2,))
        self.assert_no_scan(unattached)
        self.assertTrue(any("attachments_uploader" in d for d in unattached), unattached)
        self.assertTrue(any("messages_attachment" in d for d in unattached), unattached)
        recent = await self.plan(db.UPLOAD_RECENT_BYTES_SQL, (2, 0.0))
        self.assert_no_scan(recent)
        self.assertTrue(any("attachments_uploader" in d for d in recent), recent)

    async def test_files_access_check(self) -> None:
        details = await self.plan(db.ATTACHMENT_ACCESS_SQL, (att_id(1), 7, 7, 7))
        self.assert_no_scan(details)
        self.assertTrue(any("messages_attachment" in d for d in details), details)
        self.assertFalse(any("SCAN messages" in d or "SCAN m" in d for d in details))

    async def test_orphan_sweep_queries(self) -> None:
        details = await self.plan(db.ORPHAN_UNATTACHED_SQL, (0.0, 500))
        self.assert_no_scan(details)
        self.assertTrue(any("attachments_created" in d for d in details), details)
        self.assertTrue(any("messages_attachment" in d for d in details), details)
        self.assert_no_scan(await self.plan(db.ATTACHMENT_BATCH_SQL, ("", 500)))

    async def test_admin_users_message_count_uses_the_sender_index(self) -> None:
        details = await self.plan(
            "SELECT (SELECT COUNT(*) FROM messages m WHERE m.sender_id = u.id) FROM users u ORDER BY u.id", ()
        )
        self.assertFalse(any(d.startswith(("SCAN messages", "SCAN m ")) or d == "SCAN m" for d in details), details)

    async def test_the_plans_stay_index_driven_with_data(self) -> None:
        await self.group(2)
        await self.member(2, 2)
        for number in range(1, 60):
            await self.insert(2, number, ts=util.now() - number)
            if number % 2:
                await self.message(100 + number, 2, 2, att_id(number))
        await self.sql("ANALYZE")
        for statement, params in (
            (db.UPLOAD_UNATTACHED_BYTES_SQL, (2,)),
            (db.UPLOAD_RECENT_BYTES_SQL, (2, 0.0)),
            (db.ATTACHMENT_ACCESS_SQL, (att_id(1), 2, 2, 2)),
            (db.ORPHAN_UNATTACHED_SQL, (util.now(), 500)),
        ):
            self.assert_no_scan(await self.plan(statement, params))


if __name__ == "__main__":
    unittest.main()
