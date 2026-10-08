"""Tests of ``chatd.db`` (db-core): schema, migrations, pragmas, run/run_raw/run_read semantics, facade."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chatd import db, util  # noqa: E402


def spec_ddl_statements() -> list:
    """The statements of the first ``sql`` block of docs/SPEC.md §3, or [] when the spec is not available."""
    path = os.path.join(ROOT, "docs", "SPEC.md")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("```sql") + len("```sql")
    return db.split_statements(text[start : text.index("```", start)].strip("\n"))


class TempDbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.path = os.path.join(self.data, "chat.db")
        self.database = db.Database(self.path)
        self.database.open()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class SchemaTest(TempDbCase):
    async def test_ddl_is_the_spec_ddl(self) -> None:
        expected = spec_ddl_statements()
        if not expected:
            self.skipTest("docs/SPEC.md is not available")
        self.assertEqual(db.split_statements(db.SCHEMA_V1_DDL), expected)

    async def test_schema_has_every_table_and_index(self) -> None:
        def names(conn: sqlite3.Connection) -> tuple:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            return tables, indexes

        tables, indexes = await self.database.run_read(names)
        self.assertTrue(
            {
                "meta",
                "users",
                "sessions",
                "chats",
                "chat_members",
                "attachments",
                "messages",
                "message_mentions",
                "receipts",
                "reactions",
                "stars",
                "pins",
                "hidden_messages",
                "audit_log",
            }
            <= tables
        )
        self.assertTrue(
            {
                "sessions_user",
                "chat_members_user",
                "attachments_uploader",
                "attachments_created",
                "messages_client",
                "messages_chat",
                "messages_attachment",
                "messages_reply",
                "messages_sender",
                "message_mentions_user",
            }
            <= indexes
        )
        columns = await self.database.run_read(lambda c: [r[1] for r in c.execute("PRAGMA table_info(chat_members)")])
        self.assertIn("listed", columns)

    async def test_meta_after_creation(self) -> None:
        self.assertEqual(self.database.schema_version, db.SCHEMA_VERSION)
        self.assertEqual(db.SCHEMA_VERSION, len(db.MIGRATIONS))
        version = await self.database.run_read(db.get_meta, "schema_version")
        instance = await self.database.run_read(db.get_meta, "instance_id")
        self.assertEqual(version, str(db.SCHEMA_VERSION))
        self.assertRegex(instance, r"^[0-9a-f]{32}$")
        self.assertIsNone(await self.database.run_read(db.get_meta, "nope"))
        self.assertEqual(await self.database.run_read(db.get_meta, "nope", "dflt"), "dflt")

    async def test_set_meta_upserts(self) -> None:
        await self.database.run(db.set_meta, "k", 1)
        await self.database.run(db.set_meta, "k", "two")
        self.assertEqual(await self.database.run_read(db.get_meta, "k"), "two")

    async def test_reopen_keeps_data_and_instance_id(self) -> None:
        instance = await self.database.run_read(db.get_meta, "instance_id")
        await self.database.run(db.set_meta, "keep", "me")
        await self.database.close()
        self.database = db.Database(self.path)
        self.database.open()
        self.assertEqual(await self.database.run_read(db.get_meta, "keep"), "me")
        self.assertEqual(await self.database.run_read(db.get_meta, "instance_id"), instance)
        self.assertEqual(
            os.listdir(os.path.join(self.data, "backups")) if os.path.isdir(os.path.join(self.data, "backups")) else [],
            [],
        )


class PragmaTest(TempDbCase):
    async def test_writer_and_reader_pragmas(self) -> None:
        def pragmas(conn: sqlite3.Connection) -> dict:
            return {
                name: conn.execute("PRAGMA " + name).fetchone()[0]
                for name in ("journal_mode", "foreign_keys", "busy_timeout", "synchronous", "query_only")
            }

        writer = await self.database.run_raw(pragmas)
        self.assertEqual(
            writer,
            {"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 5000, "synchronous": 2, "query_only": 0},
        )
        reader = await self.database.run_read(pragmas)
        self.assertEqual((reader["journal_mode"], reader["foreign_keys"], reader["query_only"]), ("wal", 1, 1))
        self.assertEqual(reader["busy_timeout"], 5000)

    async def test_fold_is_registered_on_every_kind_of_connection(self) -> None:
        query = "SELECT fold('Stra\u00dfe')"
        self.assertEqual(await self.database.run_raw(lambda c: c.execute(query).fetchone()[0]), "strasse")
        self.assertEqual(await self.database.run_read(lambda c: c.execute(query).fetchone()[0]), "strasse")
        extra = self.database.connect_extra()
        try:
            self.assertEqual(extra.execute(query).fetchone()[0], "strasse")
            self.assertEqual(extra.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(extra.execute("PRAGMA query_only").fetchone()[0], 0)
        finally:
            extra.close()

    async def test_register_functions_is_public_for_foreign_connections(self) -> None:
        own = sqlite3.connect(":memory:")
        try:
            db.register_functions(own)
            self.assertEqual(own.execute("SELECT fold('ABC')").fetchone()[0], "abc")
            self.assertIsNone(own.execute("SELECT fold(NULL)").fetchone()[0])
        finally:
            own.close()

    async def test_readers_are_read_only(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            await self.database.run_read(db.set_meta, "x", "y")

    async def test_wal_refusal_raises_wal_unavailable(self) -> None:
        class Conn:
            def __init__(self) -> None:
                self.closed = False

            def create_function(self, *a: object, **k: object) -> None:
                return None

            def execute(self, sql: str) -> "Conn":
                return self

            def fetchone(self) -> tuple:
                return ("delete",)

            def close(self) -> None:
                self.closed = True

        fake = Conn()
        with mock.patch.object(db, "WAL_RETRY_SECONDS", 0.1), mock.patch.object(
            db.sqlite3, "connect", return_value=fake
        ), self.assertRaises(db.WalUnavailable) as raised:
            db.open_connection(self.path)
        self.assertTrue(fake.closed)
        self.assertEqual(raised.exception.code, 78)
        self.assertEqual(raised.exception.mode, "delete")
        self.assertIsInstance(raised.exception, util.FatalError)

    async def test_a_transient_wal_answer_is_retried(self) -> None:
        # another process converting the same new file can make the pragma answer "delete" or "database is locked"
        answers = [("delete",), sqlite3.OperationalError("database is locked"), ("wal",)]

        class Conn:
            def create_function(self, *a: object, **k: object) -> None:
                return None

            def execute(self, sql: str) -> "Conn":
                if sql.startswith("PRAGMA journal_mode"):
                    answer = answers.pop(0)
                    if isinstance(answer, Exception):
                        raise answer
                    self.answer = answer
                return self

            def fetchone(self) -> tuple:
                return self.answer

            def close(self) -> None:
                return None

        with mock.patch.object(db.sqlite3, "connect", return_value=Conn()):
            db.open_connection(self.path)
        self.assertEqual(answers, [])

    async def test_other_pragma_errors_are_not_retried(self) -> None:
        class Conn:
            def create_function(self, *a: object, **k: object) -> None:
                return None

            def execute(self, sql: str) -> "Conn":
                raise sqlite3.OperationalError("disk I/O error")

            def close(self) -> None:
                return None

        with mock.patch.object(db.sqlite3, "connect", return_value=Conn()), self.assertRaises(sqlite3.OperationalError):
            db.open_connection(self.path)

    async def test_simultaneous_first_opens_of_a_new_file_all_succeed(self) -> None:
        errors: list = []
        target = os.path.join(self.tmp, "race", "chat.db")
        os.makedirs(os.path.dirname(target))
        gate = threading.Event()

        def opener() -> None:
            gate.wait(10)
            try:
                db.open_connection(target).close()
            except (db.DbError, sqlite3.Error) as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=opener) for _ in range(8)]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])


class RunSemanticsTest(TempDbCase):
    async def test_run_commits_and_returns(self) -> None:
        def write(conn: sqlite3.Connection) -> int:
            self.assertTrue(conn.in_transaction)
            db.set_meta(conn, "a", "1")
            return 42

        self.assertEqual(await self.database.run(write), 42)
        self.assertEqual(await self.database.run_read(db.get_meta, "a"), "1")

    async def test_run_rolls_back_on_any_exception_and_reraises(self) -> None:
        def boom(conn: sqlite3.Connection) -> None:
            db.set_meta(conn, "ghost", "1")
            raise KeyError("x")

        with self.assertRaises(KeyError):
            await self.database.run(boom)
        self.assertIsNone(await self.database.run_read(db.get_meta, "ghost"))
        self.assertFalse(await self.database.run_raw(lambda c: c.in_transaction))

    async def test_run_rolls_back_on_integrity_errors(self) -> None:
        def bad(conn: sqlite3.Connection) -> None:
            db.set_meta(conn, "partial", "1")
            conn.execute("INSERT INTO users(username) VALUES ('x')")  # NOT NULL constraint

        with self.assertRaises(sqlite3.IntegrityError):
            await self.database.run(bad)
        self.assertIsNone(await self.database.run_read(db.get_meta, "partial"))
        await self.database.run(db.set_meta, "after", "ok")  # the writer is still healthy

    async def test_a_failed_commit_is_rolled_back(self) -> None:
        def deferred_violation(conn: sqlite3.Connection) -> None:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, last_used_at, expires_at)"
                " VALUES ('t', 99, 0, 0, 0)"
            )

        with self.assertRaises(sqlite3.IntegrityError):
            await self.database.run(deferred_violation)  # fails at COMMIT
        self.assertFalse(await self.database.run_raw(lambda c: c.in_transaction))
        self.assertEqual(
            await self.database.run_read(lambda c: c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]), 0
        )

    async def test_run_raw_is_outside_a_transaction(self) -> None:
        self.assertFalse(await self.database.run_raw(lambda conn: conn.in_transaction))
        first = await self.database.run_raw(db.data_version)
        await self.database.run(db.set_meta, "x", "1")
        self.assertEqual(
            await self.database.run_raw(db.data_version), first
        )  # own commits do not change it on the writer
        other = self.database.connect_extra()
        try:
            other.execute("BEGIN IMMEDIATE")
            db.set_meta(other, "y", "1")
            other.execute("COMMIT")
        finally:
            other.close()
        self.assertNotEqual(await self.database.run_raw(db.data_version), first)

    async def test_futures_resolve_in_submission_order(self) -> None:
        finished = []

        async def job(index: int) -> None:
            await self.database.run(db.set_meta, "n", index)
            finished.append(index)

        await asyncio.gather(*(job(i) for i in range(150)))
        self.assertEqual(finished, list(range(150)))
        self.assertEqual(await self.database.run_read(db.get_meta, "n"), "149")

    async def test_run_sync_matches_run(self) -> None:
        self.assertEqual(self.database.run_sync(lambda conn: db.set_meta(conn, "s", "1") or "done"), "done")
        self.assertEqual(await self.database.run_read(db.get_meta, "s"), "1")
        with self.assertRaises(ZeroDivisionError):
            self.database.run_sync(lambda conn: 1 / 0)

    async def test_read_snapshot_is_one_transaction(self) -> None:
        def two_reads(conn: sqlite3.Connection) -> tuple:
            first = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
            writer = sqlite3.connect(self.path, isolation_level=None)
            writer.execute("INSERT INTO meta(key, value) VALUES ('snap', '1')")
            writer.close()
            return first, conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0], conn.in_transaction

        first, second, in_tx = await self.database.run_read(two_reads)
        self.assertEqual(first, second)
        self.assertTrue(in_tx)
        self.assertFalse(await self.database.run_read(lambda conn: conn.in_transaction and False))

    async def test_interrupt_after_raises_server_busy(self) -> None:
        def forever(conn: sqlite3.Connection) -> None:
            conn.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
            ).fetchone()

        with self.assertRaises(db.ServerBusy) as raised:
            await self.database.run_read(forever, interrupt_after=0.2)
        self.assertGreater(raised.exception.retry_after, 0)
        self.assertEqual(await self.database.run_read(db.get_meta, "schema_version"), "1")  # the reader recovered

    async def test_without_interrupt_errors_are_not_translated(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            await self.database.run_read(lambda conn: conn.execute("SELECT * FROM nope"), interrupt_after=5)

    async def test_jobs_run_on_the_named_threads(self) -> None:
        names = await asyncio.gather(
            self.database.run(lambda conn: threading.current_thread().name),
            self.database.run_read(lambda conn: threading.current_thread().name),
        )
        self.assertTrue(names[0].startswith("db-writer"))
        self.assertTrue(names[1].startswith("db-reader"))

    async def test_closing_the_future_waiter_does_not_cancel_the_write(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow(conn: sqlite3.Connection) -> None:
            started.set()
            release.wait(10)
            db.set_meta(conn, "late", "yes")

        task = asyncio.ensure_future(self.database.run(slow))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 10)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.database.run(db.set_meta, "barrier", "1")
        self.assertEqual(await self.database.run_read(db.get_meta, "late"), "yes")


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_runs_on_the_database_threads_and_truncates_the_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = db.Database(os.path.join(tmp, "d", "chat.db"))
            database.open()
            await database.run(db.set_meta, "x", "1")
            wal = os.path.join(tmp, "d", "chat.db-wal")
            self.assertTrue(os.path.exists(wal))
            threads = [t for t in threading.enumerate() if t.name.startswith("db-")]
            self.assertEqual(len(threads), 4)  # one writer, three readers
            self.assertIsNone(await database.close())  # awaitable
            database.close()  # idempotent, plain call
            self.assertFalse(any(t.name.startswith("db-") and t.is_alive() for t in threading.enumerate()))
            self.assertTrue(not os.path.exists(wal) or os.path.getsize(wal) == 0)
            with self.assertRaises(db.DatabaseClosed):
                await database.run(db.set_meta, "x", "2")
            with self.assertRaises(db.DatabaseClosed):
                await database.run_read(db.get_meta, "x")

    async def test_start_and_migrate_are_separate_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = db.Database(os.path.join(tmp, "chat.db"), types.SimpleNamespace(data_dir=tmp))
            database.start()
            self.assertEqual(database.schema_version, 0)
            database.migrate()
            self.assertEqual(database.schema_version, db.SCHEMA_VERSION)
            with self.assertRaises(db.DbError):
                database.start()
            database.close()

    async def test_reader_count_can_be_chosen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = db.Database(os.path.join(tmp, "chat.db"), 2)
            database.open()
            self.assertEqual(len([t for t in threading.enumerate() if t.name.startswith("db-reader")]), 2)
            database.close()

    async def test_open_for_seeding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = types.SimpleNamespace(data_dir=tmp)
            seeded = db.open_for_seeding(cfg)
            seeded.run_sync(db.set_meta, "seed", "1")
            seeded.close()
            self.assertTrue(os.path.exists(os.path.join(tmp, "chat.db")))
            again = db.open_for_seeding(cfg)
            self.assertEqual(again.run_sync(db.get_meta, "seed"), "1")
            again.close()

    async def test_unusable_path_is_a_fatal_error_without_leftover_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "file")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("x")
            database = db.Database(os.path.join(blocker, "sub", "chat.db"))
            with self.assertRaises(util.FatalError):
                database.open()
            self.assertFalse(any(t.name.startswith("db-") and t.is_alive() for t in threading.enumerate()))


class DamagedDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_file_that_is_not_a_database_is_a_fatal_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chat.db")
            with open(path, "wb") as handle:
                handle.write(b"this is not a sqlite database at all" * 200)
            with self.assertRaises(util.FatalError) as raised:
                db.Database(path).open()
            self.assertEqual(raised.exception.code, 78)
            self.assertFalse(any(t.name.startswith("db-") and t.is_alive() for t in threading.enumerate()))

    async def test_corruption_found_while_migrating_is_fatal_but_a_locked_database_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = db.Database(os.path.join(tmp, "chat.db"))
            database.start()
            damaged = mock.patch.object(db, "apply_migrations", side_effect=sqlite3.DatabaseError("malformed"))
            locked = mock.patch.object(db, "apply_migrations", side_effect=sqlite3.OperationalError("locked"))
            try:
                with damaged, self.assertRaises(util.FatalError):
                    database.migrate()
                with locked, self.assertRaises(sqlite3.OperationalError):
                    database.migrate()
            finally:
                database.close()

    async def test_request_errors_names_both_protocol_error_types(self) -> None:
        kinds = db.request_errors()
        self.assertIn(db.RequestError, kinds)
        self.assertIn(db.ChatError, kinds)
        for kind in kinds:
            error = kind("not_found", "gone", "why")
            self.assertEqual((error.code, error.reason, error.to_err()["msg"]), ("not_found", "why", "gone"))

    async def test_a_refused_wal_is_logged_as_an_error(self) -> None:
        class Conn:
            def create_function(self, *a: object, **k: object) -> None:
                return None

            def execute(self, sql: str) -> "Conn":
                return self

            def fetchone(self) -> tuple:
                return ("delete",)

            def close(self) -> None:
                return None

        with mock.patch.object(db, "WAL_RETRY_SECONDS", 0.05), mock.patch.object(
            db.sqlite3, "connect", return_value=Conn()
        ), self.assertLogs("chatd.db", level="ERROR"), self.assertRaises(db.WalUnavailable):
            db.open_connection("x.db")


class MigrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "chat.db")
        self.backups = os.path.join(self.tmp, "backups")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def migration_two(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE extra(a INTEGER)")
        conn.execute("INSERT INTO extra(a) VALUES (7)")

    async def test_a_new_database_needs_no_backup(self) -> None:
        database = db.Database(self.path)
        database.open()
        database.close()
        self.assertFalse(os.path.exists(self.backups))

    async def test_upgrade_writes_a_pre_migrate_backup_first(self) -> None:
        first = db.Database(self.path)
        first.open()
        first.run_sync(db.set_meta, "marker", "v1-data")
        first.run_sync(
            lambda c: (
                c.execute(
                    "INSERT INTO users(username, display_name, display_key, pw_hash, created_at)"
                    " VALUES ('a','A','a','h',1)"
                )
                and c.execute(
                    "INSERT INTO sessions(token_hash, user_id, created_at, last_used_at, expires_at)"
                    " VALUES ('tok',1,0,0,9)"
                )
            )
        )
        first.close()

        with mock.patch.object(db, "MIGRATIONS", [db.MIGRATIONS[0], self.migration_two]):
            second = db.Database(self.path)
            second.open()
            self.assertEqual(second.schema_version, 2)
            self.assertEqual(second.run_sync(lambda c: c.execute("SELECT a FROM extra").fetchone()[0]), 7)
            self.assertEqual(second.run_sync(db.get_meta, "schema_version"), "2")
            second.close()
        files = os.listdir(self.backups)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^pre-migrate-v1-\d{8}-\d{6}\.db$")
        copy = sqlite3.connect(os.path.join(self.backups, files[0]))
        try:
            self.assertEqual(copy.execute("SELECT value FROM meta WHERE key='marker'").fetchone()[0], "v1-data")
            self.assertEqual(copy.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "1")
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)  # sessions are dropped
            self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertIsNone(copy.execute("SELECT name FROM sqlite_master WHERE name='extra'").fetchone())
        finally:
            copy.close()
        self.assertFalse(any(name.endswith(".tmp") for name in files))

    async def test_a_failing_migration_rolls_back_completely(self) -> None:
        first = db.Database(self.path)
        first.open()
        first.close()

        def broken(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE half(a)")
            raise RuntimeError("migration bug")

        with mock.patch.object(db, "MIGRATIONS", [db.MIGRATIONS[0], broken]):
            database = db.Database(self.path)
            with self.assertRaises(RuntimeError):
                database.open()
        check = sqlite3.connect(self.path)
        try:
            self.assertEqual(check.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "1")
            self.assertIsNone(check.execute("SELECT name FROM sqlite_master WHERE name='half'").fetchone())
        finally:
            check.close()
        self.assertFalse(any(t.name.startswith("db-") and t.is_alive() for t in threading.enumerate()))

    async def test_a_newer_database_is_refused_untouched(self) -> None:
        with mock.patch.object(db, "MIGRATIONS", [db.MIGRATIONS[0], self.migration_two]):
            future = db.Database(self.path)
            future.open()
            future.close()
        before = os.path.getsize(self.path)
        database = db.Database(self.path)
        with self.assertRaises(db.SchemaTooNew) as raised:
            database.open()
        self.assertEqual((raised.exception.found, raised.exception.supported, raised.exception.code), (2, 1, 78))
        self.assertIsInstance(raised.exception, util.FatalError)
        self.assertIn("newer", str(raised.exception))
        self.assertFalse(os.path.exists(self.backups))
        self.assertGreaterEqual(os.path.getsize(self.path), before)
        self.assertFalse(any(t.name.startswith("db-") and t.is_alive() for t in threading.enumerate()))

    async def test_a_foreign_database_is_refused(self) -> None:
        other = sqlite3.connect(self.path)
        other.execute("CREATE TABLE something(a)")
        other.commit()
        other.close()
        with self.assertRaises(util.FatalError):
            db.Database(self.path).open()


class FacadeTest(unittest.TestCase):
    def test_facade_reexports_every_public_name_without_collisions(self) -> None:
        import importlib

        seen = {}
        for short in db._FACADE_MODULES:
            try:
                module = importlib.import_module("chatd." + short)
            except ModuleNotFoundError as exc:
                if exc.name != "chatd." + short:
                    raise
                continue
            for name in db._public_names(module):
                self.assertNotIn(name, seen, "%s is exported by %s and %s" % (name, seen.get(name), short))
                seen[name] = short
                self.assertIs(getattr(db, name), getattr(module, name))
        self.assertIn("register_user", seen)

    def test_core_names_are_not_shadowed(self) -> None:
        self.assertTrue(callable(db.Database))
        for name in (
            "SchemaTooNew",
            "WalUnavailable",
            "ServerBusy",
            "DbError",
            "get_meta",
            "set_meta",
            "sqlite_problem",
        ):
            self.assertTrue(hasattr(db, name), name)

    def test_unknown_names_raise_attribute_error(self) -> None:
        with self.assertRaises(AttributeError):
            db.definitely_not_there  # noqa: B018

    def test_either_import_order_works(self) -> None:
        for first in ("chatd.db_users", "chatd.db"):
            code = (
                "import sys; sys.path.insert(0, %r)\nimport importlib\nimportlib.import_module(%r)\n"
                "from chatd import db, db_users\nassert db.register_user is db_users.register_user\n"
                "from chatd.db import register_user\nprint('ok')" % (ROOT, first)
            )
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.stdout.strip(), "ok", result.stderr)


class WithoutSqliteTest(unittest.TestCase):
    def test_modules_import_and_report_the_problem_when_sqlite3_is_missing(self) -> None:
        code = (
            "import sys; sys.modules['sqlite3'] = None; sys.path.insert(0, %r)\n"
            "from chatd import db, util, auth, maintenance, db_users\n"
            "print(db.sqlite_problem())\n"
            "try:\n    db.Database('x.db').start()\nexcept util.FatalError as exc:\n    print('fatal', exc.code)\n"
            % ROOT
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("doctor", result.stdout)
        self.assertIn("fatal 78", result.stdout)

    def test_sqlite_problem_is_none_here_and_reports_old_versions(self) -> None:
        self.assertIsNone(db.sqlite_problem())
        with mock.patch.object(db.sqlite3, "sqlite_version_info", (3, 22, 0)), mock.patch.object(
            db.sqlite3, "sqlite_version", "3.22.0"
        ):
            message = db.sqlite_problem()
        self.assertIn("3.22.0", message)
        self.assertIn("doctor", message)


class RequestErrorTest(unittest.TestCase):
    def test_to_err_and_rest_code(self) -> None:
        error = db.RequestError("conflict", "taken", "name_taken")
        self.assertEqual(error.to_err(), {"code": "conflict", "msg": "taken", "reason": "name_taken"})
        self.assertEqual(error.rest_code, "name_taken")
        self.assertEqual(db.RequestError("invalid_state", "m", "max_users").rest_code, "registration_closed")
        self.assertEqual(db.RequestError("forbidden", "m").rest_code, "forbidden")
        full = db.RequestError("not_member", "m", None, 2.0, 5, 6)
        self.assertEqual(
            full.to_err(), {"code": "not_member", "msg": "m", "retry_after": 2.0, "chat_id": 5, "message_id": 6}
        )


if __name__ == "__main__":
    unittest.main()
