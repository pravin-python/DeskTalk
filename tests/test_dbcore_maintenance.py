"""Tests of ``chatd.maintenance`` (db-core): create-admin, reset-password, backup, prune, restore, schema info."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chatd import auth, db, maintenance, util  # noqa: E402

N = 1024
PASSWORD = "correct horse battery staple"


class MaintenanceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.path = util.db_path(self.data)

    def tearDown(self) -> None:
        util._pending_deletes = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def admin(self, username: str = "ravi", **kwargs: Any) -> Any:
        kwargs.setdefault("scrypt_n", N)
        return maintenance.create_admin(self.data, username, PASSWORD, **kwargs)

    def rows(self, sql: str, *params: Any, path: Optional[str] = None) -> List[tuple]:
        conn = sqlite3.connect(path or self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def fail(self, call: Any, *args: Any, **kwargs: Any) -> util.FatalError:
        with self.assertRaises(util.FatalError) as raised:
            call(*args, **kwargs)
        return raised.exception


class CreateAdminTest(MaintenanceCase):
    def test_creates_the_database_the_admin_and_everyone(self) -> None:
        user = self.admin()
        self.assertEqual((user["username"], user["role"], user["display_name"]), ("ravi", "admin", "ravi"))
        self.assertFalse(user["activated"])  # never logged in until the first /api/login
        self.assertEqual(self.rows("SELECT role, last_login_at, must_change_password FROM users"), [("admin", None, 0)])
        self.assertEqual(self.rows("SELECT title, is_default FROM chats"), [("DeskTalk", 1)])
        self.assertEqual(self.rows("SELECT kind, body FROM messages"), [("system", "Welcome to DeskTalk")])
        self.assertEqual(self.rows("SELECT value FROM meta WHERE key = 'schema_version'"), [(str(db.SCHEMA_VERSION),)])
        self.assertEqual(
            self.rows("SELECT actor_id, action, target_id, ip FROM audit_log"), [(None, "cli.create_admin", 1, None)]
        )
        self.assertTrue(os.path.exists(os.path.join(self.data, "control", "reload")))
        stored = self.rows("SELECT pw_hash FROM users")[0][0]
        self.assertEqual(auth.verify_password_sync(PASSWORD, stored, N), (True, False))

    def test_display_name_and_workspace_options(self) -> None:
        user = self.admin("ravi", display_name="Ravi Kumar", workspace_name="Acme")
        self.assertEqual(user["display_name"], "Ravi Kumar")
        self.assertEqual(self.rows("SELECT title FROM chats"), [("Acme",)])

    def test_later_admins_join_everyone_with_a_joined_message(self) -> None:
        self.admin("ravi")
        second = self.admin("sara")
        self.assertEqual(second["role"], "admin")
        self.assertEqual(
            self.rows("SELECT user_id, role FROM chat_members ORDER BY user_id"), [(1, "admin"), (2, "admin")]
        )
        bodies = [r[0] for r in self.rows("SELECT body FROM messages ORDER BY id")]
        self.assertEqual(bodies, ["Welcome to DeskTalk", "sara joined"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM chats"), [(1,)])

    def test_reserved_usernames_are_allowed_here(self) -> None:
        self.assertEqual(self.admin("admin")["username"], "admin")
        self.assertEqual(self.admin("Root.Support")["username"], "root.support")  # lowercased

    def test_policy_and_syntax_errors_leave_no_database_behind(self) -> None:
        for username, password in (
            ("ab", PASSWORD),
            ("bad name", PASSWORD),
            ("ravi", "short"),
            ("ravi", "password123"),
            ("ravi", "ravi"),
        ):
            self.assertIsInstance(
                self.fail(maintenance.create_admin, self.data, username, password, scrypt_n=N), util.FatalError
            )
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("weak password", str(self.fail(maintenance.create_admin, self.data, "ravi", "short", scrypt_n=N)))
        self.assertIn("username", str(self.fail(maintenance.create_admin, self.data, "x", PASSWORD, scrypt_n=N)))

    def test_conflicting_display_name_is_reported(self) -> None:
        self.admin("ravi", display_name="Chief")
        error = self.fail(maintenance.create_admin, self.data, "sara", PASSWORD, display_name="chief", scrypt_n=N)
        self.assertIn("display name", str(error))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM users"), [(1,)])  # rolled back as a whole

    def test_max_users_is_honoured(self) -> None:
        self.admin("ravi")
        self.fail(maintenance.create_admin, self.data, "sara", PASSWORD, max_users=1, scrypt_n=N)

    def test_promoting_an_existing_user(self) -> None:
        self.admin("ravi")
        conn = db.open_connection(self.path)
        try:
            spec = {
                "username": "amit",
                "display_name": "Amit",
                "pw_hash": "old-hash",
                "activated": True,
                "role": "member",
            }
            conn.execute("BEGIN IMMEDIATE")
            db.register_user(conn, spec, 2000, self.data)
            conn.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, last_used_at, expires_at)"
                " VALUES ('t', 2, 0, 0, 9)"
            )
            conn.execute("UPDATE users SET disabled = 1, must_change_password = 1 WHERE id = 2")
            conn.execute("COMMIT")
        finally:
            conn.close()
        self.assertEqual(self.rows("SELECT role FROM chat_members WHERE user_id = 2"), [("member",)])
        user = self.admin("AMIT")
        self.assertEqual((user["id"], user["role"], user["disabled"]), (2, "admin", False))
        self.assertEqual(
            self.rows("SELECT role, disabled, must_change_password FROM users WHERE id = 2"), [("admin", 0, 0)]
        )
        self.assertEqual(
            self.rows("SELECT role FROM chat_members WHERE user_id = 2"), [("admin",)]
        )  # mirrored in Everyone
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions"), [(0,)])
        stored = self.rows("SELECT pw_hash FROM users WHERE id = 2")[0][0]
        self.assertEqual(auth.verify_password_sync(PASSWORD, stored, N), (True, False))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM users"), [(2,)])
        self.assertEqual(self.rows("SELECT action, target_id FROM audit_log ORDER BY id")[-1], ("cli.create_admin", 2))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM messages"), [(2,)])  # no extra system message for a promotion

    def test_default_hash_cost_follows_the_configured_hasher(self) -> None:
        auth.configure(scrypt_n=2048)
        try:
            maintenance.create_admin(self.data, "ravi", PASSWORD)
        finally:
            auth.configure(scrypt_n=N)
        self.assertTrue(self.rows("SELECT pw_hash FROM users")[0][0].startswith("scrypt$2048$"))

    def test_a_newer_database_is_refused(self) -> None:
        self.admin("ravi")
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        error = self.fail(maintenance.create_admin, self.data, "sara", PASSWORD, scrypt_n=N)
        self.assertEqual(error.code, 78)
        self.assertIn("newer", str(error))


class ResetPasswordTest(MaintenanceCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin("ravi")
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, last_used_at, expires_at) VALUES ('t', 1, 0, 0, 9)"
        )
        conn.execute("UPDATE users SET must_change_password = 1")
        conn.commit()
        conn.close()
        os.remove(os.path.join(self.data, "control", "reload"))

    def test_sets_the_password_revokes_sessions_and_signals_the_server(self) -> None:
        maintenance.reset_password(self.data, "RAVI", "a brand new password", scrypt_n=N)
        stored, forced = self.rows("SELECT pw_hash, must_change_password FROM users")[0]
        self.assertEqual(auth.verify_password_sync("a brand new password", stored, N), (True, False))
        self.assertEqual(forced, 0)  # only --must-change sets it
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions"), [(0,)])
        self.assertEqual(
            self.rows("SELECT actor_id, action, target_id FROM audit_log ORDER BY id")[-1],
            (None, "cli.reset_password", 1),
        )
        self.assertTrue(os.path.exists(os.path.join(self.data, "control", "reload")))

    def test_must_change_flag(self) -> None:
        maintenance.reset_password(self.data, "ravi", "a brand new password", True, scrypt_n=N)
        self.assertEqual(self.rows("SELECT must_change_password FROM users"), [(1,)])

    def test_errors(self) -> None:
        self.assertIn(
            "no user named", str(self.fail(maintenance.reset_password, self.data, "ghost", "a brand new password"))
        )
        self.assertIn("weak password", str(self.fail(maintenance.reset_password, self.data, "ravi", "short")))
        self.assertIn("weak password", str(self.fail(maintenance.reset_password, self.data, "ravi", "ravi")))
        self.fail(maintenance.reset_password, self.data, None, "a brand new password")
        self.fail(maintenance.reset_password, os.path.join(self.tmp, "elsewhere"), "ravi", "a brand new password")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "elsewhere", "chat.db")))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions"), [(1,)])  # untouched by the failures


class TouchReloadTest(MaintenanceCase):
    def test_creates_and_bumps_the_file(self) -> None:
        target = os.path.join(self.data, "control", "reload")
        maintenance.touch_reload(self.data)
        first = os.stat(target).st_mtime
        time.sleep(0.05)
        maintenance.touch_reload(self.data)
        self.assertGreater(os.stat(target).st_mtime, first)
        with open(target, encoding="utf-8") as handle:
            self.assertGreater(float(handle.read()), 0)


class BackupTest(MaintenanceCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin("ravi")
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, last_used_at, expires_at)"
            " VALUES ('secret-hash', 1, 0, 0, 9)"
        )
        conn.commit()
        conn.close()
        self.out = os.path.join(self.tmp, "backups-out")

    def test_plain_snapshot(self) -> None:
        target = maintenance.backup(self.path, self.out)
        self.assertRegex(os.path.basename(target), r"^chat-\d{8}-\d{6}\.db$")
        self.assertEqual(os.listdir(self.out), [os.path.basename(target)])  # no .tmp left
        self.assertEqual(self.rows("SELECT username FROM users", path=target), [("ravi",)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions", path=target), [(0,)])  # emptied in the copy only
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions"), [(1,)])
        self.assertEqual(self.rows("PRAGMA journal_mode", path=target), [("delete",)])
        self.assertEqual(self.rows("PRAGMA integrity_check", path=target), [("ok",)])
        with open(target, "rb") as handle:
            self.assertNotIn(b"secret-hash", handle.read())

    def test_prefix_and_unique_names(self) -> None:
        first = maintenance.backup(self.path, self.out, prefix="auto")
        second = maintenance.backup(self.path, self.out, prefix="auto")
        self.assertNotEqual(first, second)
        for target in (first, second):
            self.assertRegex(os.path.basename(target), r"^auto-\d{8}-\d{6}(-\d+)?\.db$")

    def test_with_uploads_layout(self) -> None:
        uploads = os.path.join(self.data, "uploads")
        os.makedirs(os.path.join(uploads, "ab"))
        os.makedirs(os.path.join(uploads, ".tmp"))
        with open(os.path.join(uploads, "ab", "a" * 32), "wb") as handle:
            handle.write(b"payload")
        with open(os.path.join(uploads, ".tmp", "half.part"), "wb") as handle:
            handle.write(b"partial")
        target = maintenance.backup(self.path, self.out, with_uploads=True)
        self.assertRegex(os.path.basename(target), r"^chat-\d{8}-\d{6}$")
        self.assertEqual(sorted(os.listdir(target)), ["chat.db", "uploads"])
        with open(os.path.join(target, "uploads", "ab", "a" * 32), "rb") as handle:
            self.assertEqual(handle.read(), b"payload")
        self.assertFalse(os.path.exists(os.path.join(target, "uploads", ".tmp")))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions", path=os.path.join(target, "chat.db")), [(0,)])
        self.assertEqual([n for n in os.listdir(self.out) if n.endswith(".tmp")], [])

    def test_with_uploads_without_an_uploads_directory(self) -> None:
        target = maintenance.backup(self.path, self.out, with_uploads=True)
        self.assertEqual(os.listdir(target), ["chat.db"])

    def test_refuses_the_served_web_directory(self) -> None:
        for out in (os.path.join(ROOT, "web", "backups"), os.path.join(ROOT, "web")):
            error = self.fail(maintenance.backup, self.path, out)
            self.assertIn("web/", str(error))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "web", "backups")))

    def test_other_refusals(self) -> None:
        self.fail(maintenance.backup, os.path.join(self.tmp, "missing.db"), self.out)
        self.fail(maintenance.backup, self.path, self.out, prefix="../x")
        self.fail(maintenance.backup, self.path, self.out, prefix="")

    def test_snapshot_of_a_database_that_is_being_written(self) -> None:
        database = db.Database(self.path)
        database.open()
        stop = threading.Event()

        def writer() -> None:
            counter = 0
            while not stop.is_set():
                counter += 1
                database.run_sync(db.set_meta, "n", counter)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            target = maintenance.backup(self.path, self.out)
        finally:
            stop.set()
            thread.join(30)
            database.close()
        self.assertEqual(self.rows("PRAGMA integrity_check", path=target), [("ok",)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM users", path=target), [(1,)])


class PruneTest(MaintenanceCase):
    def test_only_the_named_prefix_is_pruned(self) -> None:
        os.makedirs(self.tmp, exist_ok=True)
        names = ["auto-20260101-0%d0000.db" % i for i in range(10)] + ["auto-20260101-090000-1.db"]
        keepers = [
            "chat-20250101-000000.db",
            "pre-migrate-v1-20250101-000000.db",
            "auto-note.db",
            "auto-20260101-000000.db.tmp",
        ]
        for name in names + keepers:
            with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        os.makedirs(os.path.join(self.tmp, "pre-restore-20250101-000000"))
        removed = maintenance.prune_backups(self.tmp, "auto", 7)
        self.assertEqual(len(removed), 4)
        left = sorted(
            n for n in os.listdir(self.tmp) if n.startswith("auto-") and n.endswith(".db") and "note" not in n
        )
        self.assertEqual(len(left), 7)
        self.assertEqual(left, sorted(names)[-7:])
        for name in keepers:
            self.assertTrue(os.path.exists(os.path.join(self.tmp, name)), name)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "pre-restore-20250101-000000")))
        self.assertEqual(maintenance.prune_backups(self.tmp, "auto", 7), [])
        self.assertEqual(maintenance.prune_backups(os.path.join(self.tmp, "missing"), "auto", 7), [])


class RestoreTest(MaintenanceCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin("ravi")
        self.out = os.path.join(self.tmp, "snapshots")

    def mutate(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE users SET status_text = 'changed after the backup'")
        conn.execute("INSERT INTO meta(key, value) VALUES ('marker', 'new')")
        conn.commit()
        conn.close()

    def test_round_trip_of_a_plain_snapshot(self) -> None:
        snapshot = maintenance.backup(self.path, self.out)
        self.mutate()
        with open(self.path + "-wal", "wb") as handle:
            handle.write(b"junk that must never be replayed onto the restored file")
        with open(self.path + "-shm", "wb") as handle:
            handle.write(b"junk")
        maintenance.restore(snapshot, self.data)
        self.assertEqual(self.rows("SELECT status_text FROM users"), [("",)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta WHERE key = 'marker'"), [(0,)])
        self.assertEqual(self.rows("PRAGMA integrity_check"), [("ok",)])
        parked = [n for n in os.listdir(os.path.join(self.data, "backups")) if n.startswith("pre-restore-")]
        self.assertEqual(len(parked), 1)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.data, "backups", parked[0]))), ["chat.db", "chat.db-shm", "chat.db-wal"]
        )
        conn = sqlite3.connect(os.path.join(self.data, "backups", parked[0], "chat.db"))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'marker'").fetchone(), (1,)
        )  # the old data is kept
        conn.close()
        self.assertEqual(
            self.rows("SELECT actor_id, action, target_id FROM audit_log ORDER BY id")[-1], (None, "cli.restore", None)
        )
        self.assertFalse(os.path.exists(self.path + ".restore"))
        server = db.Database(self.path)  # the restored database is a normal one
        server.open()
        server.close()
        util.instance_lock(self.data).release()  # and the lock was released again

    def test_with_uploads_restores_the_files_too(self) -> None:
        uploads = os.path.join(self.data, "uploads", "ab")
        os.makedirs(uploads)
        stored = os.path.join(uploads, "b" * 32)
        with open(stored, "wb") as handle:
            handle.write(b"original")
        snapshot = maintenance.backup(self.path, self.out, with_uploads=True)
        os.remove(stored)  # uploads are write-once: the server replaces or deletes files, it never edits them in place
        with open(stored, "wb") as handle:
            handle.write(b"overwritten")
        with open(os.path.join(uploads, "c" * 32), "wb") as handle:
            handle.write(b"added later")
        maintenance.restore(snapshot, self.data)
        with open(stored, "rb") as handle:
            self.assertEqual(handle.read(), b"original")
        self.assertFalse(os.path.exists(os.path.join(uploads, "c" * 32)))
        parked = os.path.join(
            self.data,
            "backups",
            [n for n in os.listdir(os.path.join(self.data, "backups")) if n.startswith("pre-restore-")][0],
        )
        with open(os.path.join(parked, "uploads", "ab", "c" * 32), "rb") as handle:
            self.assertEqual(handle.read(), b"added later")

    def test_a_plain_snapshot_leaves_uploads_alone(self) -> None:
        snapshot = maintenance.backup(self.path, self.out)
        os.makedirs(os.path.join(self.data, "uploads", "ab"))
        with open(os.path.join(self.data, "uploads", "ab", "d" * 32), "wb") as handle:
            handle.write(b"keep")
        maintenance.restore(snapshot, self.data)
        self.assertTrue(os.path.exists(os.path.join(self.data, "uploads", "ab", "d" * 32)))

    def test_restores_into_an_empty_data_dir(self) -> None:
        snapshot = maintenance.backup(self.path, self.out)
        target = os.path.join(self.tmp, "fresh-data")
        maintenance.restore(snapshot, target)
        self.assertEqual(self.rows("SELECT username FROM users", path=util.db_path(target)), [("ravi",)])

    def test_refuses_while_the_server_holds_the_instance_lock(self) -> None:
        snapshot = maintenance.backup(self.path, self.out)
        self.mutate()
        lock = util.instance_lock(self.data, {"port": 8765})
        try:
            error = self.fail(maintenance.restore, snapshot, self.data)
        finally:
            lock.release()
        self.assertEqual(error.code, 73)
        self.assertIn("pid %d" % os.getpid(), str(error))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta WHERE key = 'marker'"), [(1,)])  # nothing moved
        self.assertFalse(os.path.exists(os.path.join(self.data, "backups")))

    def test_a_damaged_snapshot_changes_nothing(self) -> None:
        snapshot = maintenance.backup(self.path, self.out)
        self.mutate()
        with open(snapshot, "r+b") as handle:
            handle.seek(0)
            handle.write(b"this is not a sqlite header at all!!")
        self.fail(maintenance.restore, snapshot, self.data)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM meta WHERE key = 'marker'"), [(1,)])
        self.assertFalse(os.path.exists(self.path + ".restore"))
        util.instance_lock(self.data).release()

    def test_a_foreign_or_newer_snapshot_is_refused(self) -> None:
        foreign = os.path.join(self.tmp, "foreign.db")
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE other(a)")
        conn.commit()
        conn.close()
        self.fail(maintenance.restore, foreign, self.data)
        newer = maintenance.backup(self.path, self.out)
        conn = sqlite3.connect(newer)
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        error = self.fail(maintenance.restore, newer, self.data)
        self.assertIn("newer", str(error))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM users"), [(1,)])

    def test_missing_snapshot(self) -> None:
        self.fail(maintenance.restore, os.path.join(self.tmp, "nope.db"), self.data)
        os.makedirs(os.path.join(self.tmp, "empty-folder"))
        self.fail(maintenance.restore, os.path.join(self.tmp, "empty-folder"), self.data)


class SchemaInfoTest(MaintenanceCase):
    def test_missing_database(self) -> None:
        info = maintenance.schema_info(self.data)
        self.assertFalse(info["exists"])
        self.assertIsNone(info["schema_version"])
        self.assertEqual(info["code_schema_version"], db.SCHEMA_VERSION)
        self.assertFalse(os.path.exists(self.data))  # looking never creates anything

    def test_facts_of_a_healthy_database(self) -> None:
        self.admin("ravi")
        server = db.Database(self.path)
        server.open()
        server.run_sync(db.admin_settings, 1, "DeskTalk", False, None, "Acme", True)
        try:
            before = sorted(os.listdir(self.data))
            info = maintenance.schema_info(self.data)  # while a server holds the database open
            self.assertEqual(sorted(os.listdir(self.data)), before)
        finally:
            server.close()
        self.assertTrue(info["exists"])
        self.assertEqual(
            (info["schema_version"], info["journal_mode"], info["integrity"], info["error"]),
            (db.SCHEMA_VERSION, "wal", "ok", None),
        )
        self.assertEqual(info["meta"], {"workspace_name": "Acme", "registration_open": "1"})
        self.assertEqual(info["path"], self.path)

    def test_meta_is_empty_until_an_admin_edits_it(self) -> None:
        self.admin("ravi")
        self.assertEqual(maintenance.schema_info(self.data)["meta"], {})

    def test_newer_and_damaged_databases_are_reported_not_raised(self) -> None:
        self.admin("ravi")
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        self.assertEqual(maintenance.schema_info(self.data)["schema_version"], 7)
        with open(self.path, "wb") as handle:
            handle.write(b"garbage" * 1000)
        info = maintenance.schema_info(self.data)
        self.assertIsNotNone(info["error"])
        self.assertTrue(info["exists"])


CLI_ADMIN = """
import sys, json
sys.path.insert(0, sys.argv[1])
from chatd import maintenance
user = maintenance.create_admin(sys.argv[2], sys.argv[3], "correct horse battery staple", scrypt_n=1024)
print(json.dumps(user))
"""


class CrossProcessTest(MaintenanceCase):
    def run_cli(self, username: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", CLI_ADMIN, ROOT, self.data, username],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_the_cli_writes_while_the_server_runs_and_the_writer_sees_it(self) -> None:
        self.admin("ravi")
        server = db.Database(self.path)
        server.open()
        try:
            before = server.run_sync(lambda c: None) or None
            version = server.run_sync(db.data_version)
            process = self.run_cli("sara")
            out, err = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, err)
            self.assertEqual(json.loads(out)["username"], "sara")
            self.assertNotEqual(server.run_sync(db.data_version), version)  # the control poll's trigger (SPEC §2.4)
            users = server.run_sync(lambda c: [r[0] for r in c.execute("SELECT username FROM users ORDER BY id")])
            self.assertEqual(users, ["ravi", "sara"])
            self.assertIsNone(before)
        finally:
            server.close()
        self.assertTrue(os.path.exists(os.path.join(self.data, "control", "reload")))

    def test_two_processes_racing_to_create_the_first_admin(self) -> None:
        for attempt in range(3):
            data = os.path.join(self.tmp, "race%d" % attempt)
            self.data = data
            processes = [self.run_cli("alpha"), self.run_cli("bravo")]
            results = [p.communicate(timeout=120) + (p.returncode,) for p in processes]
            for out, err, code in results:
                self.assertEqual(code, 0, err)
            path = util.db_path(data)
            self.assertEqual(self.rows("SELECT COUNT(*) FROM users", path=path), [(2,)])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM chats", path=path), [(1,)])
            self.assertEqual(
                sorted(r[0] for r in self.rows("SELECT body FROM messages", path=path) if r[0].startswith("Welcome")),
                ["Welcome to DeskTalk"],
            )
            self.assertEqual(self.rows("SELECT value FROM meta WHERE key = 'schema_version'", path=path), [("1",)])
            self.assertTrue(
                re.match(
                    r"^[A-Za-z0-9_-]{8}$", self.rows("SELECT value FROM meta WHERE key = 'join_code'", path=path)[0][0]
                )
            )


class ImportabilityTest(unittest.TestCase):
    def test_module_imports_nothing_that_needs_sqlite(self) -> None:
        code = (
            "import sys; sys.modules['sqlite3'] = None; sys.path.insert(0, %r)\n"
            "from chatd import maintenance, util\n"
            "try:\n    maintenance.schema_info('nowhere')\nexcept ImportError:\n    print('import error as designed')\n"
            % ROOT
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("import error as designed", result.stdout)
        self.assertIsInstance(maintenance.MaintenanceError("x"), util.FatalError)


if __name__ == "__main__":
    unittest.main()
