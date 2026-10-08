"""Tests of ``chatd.db_users`` (db-core): registration, identity rules, sessions, profile, admin functions."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chatd import auth, db, util  # noqa: E402


class UsersCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.database = db.Database(os.path.join(self.data, "chat.db"))
        self.database.open()
        auth.configure(scrypt_n=1024)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        auth.hasher.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------------------------------------------

    def spec(self, username: str, display_name: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        base = {
            "username": username,
            "display_name": display_name if display_name is not None else str(username).title(),
            "pw_hash": "hash-" + str(username),
            "role": "member",
            "activated": True,
            "must_change_password": False,
            "actor_id": None,
            "ip": "10.0.0.1",
        }
        base.update(extra)
        return base

    async def register(self, username: str, display_name: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        return await self.database.run(db.register_user, self.spec(username, display_name, **extra), 2000, self.data)

    async def public(self, username: str, display_name: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        extra.setdefault("check_setup_code", True)
        extra.setdefault("check_registration", True)
        return await self.register(username, display_name, **extra)

    async def query(self, sql: str, *params: Any) -> list:
        return await self.database.run_read(lambda conn: conn.execute(sql, params).fetchall())

    async def expect(self, code: str, reason: Optional[str], awaitable: Any) -> db.RequestError:
        with self.assertRaises(db.RequestError) as raised:
            await awaitable
        self.assertEqual((raised.exception.code, raised.exception.reason), (code, reason))
        return raised.exception

    async def first_admin(self, username: str = "ravi", display_name: str = "Ravi Kumar") -> Dict[str, Any]:
        return await self.register(username, display_name, role="admin")


class FirstUserTest(UsersCase):
    async def test_first_user_is_admin_and_creates_everyone(self) -> None:
        result = await self.first_admin()
        self.assertTrue(result["first_user"])
        self.assertEqual(result["event"], "created")
        self.assertEqual(result["user"]["role"], "admin")
        self.assertEqual(result["member"], {"user_id": 1, "role": "admin", "delivered_up_to": 0, "read_up_to": 0})
        self.assertTrue(result["me"]["show_last_seen"])
        self.assertFalse(result["me"]["must_change_password"])
        chat = (await self.query("SELECT id, kind, title, is_default, created_by, last_message_id FROM chats"))[0]
        self.assertEqual(chat[1:5], ("group", "DeskTalk", 1, 1))
        self.assertEqual(chat[5], result["message_id"])
        message = (await self.query("SELECT kind, sender_id, body, system FROM messages"))[0]
        self.assertEqual(message[:3], ("system", None, "Welcome to DeskTalk"))
        self.assertEqual(
            json.loads(message[3]),
            {"event": "created", "actor_id": 1, "target_ids": [1], "title": "DeskTalk", "message_id": None},
        )
        self.assertRegex(await self.database.run_read(db.get_meta, "join_code"), r"^[A-Za-z0-9_-]{8}$")
        member = (await self.query("SELECT role, history_from_id, last_read_id, delivered_id FROM chat_members"))[0]
        self.assertEqual(member, ("admin", 0, 0, 0))
        # the events the hub fans out after the commit are built in the same transaction
        event = result["message_event"]
        self.assertEqual((event["t"], [g["user_ids"] for g in event["groups"]]), ("ev.message", [[1]]))
        message = event["groups"][0]["d"]["message"]
        self.assertEqual(
            (message["id"], message["kind"], message["body"]), (result["message_id"], "system", "Welcome to DeskTalk")
        )
        self.assertEqual(message["system"]["event"], "created")
        self.assertEqual(
            result["index"],
            {
                "chat_id": result["chat_id"],
                "kind": "group",
                "only_admins_post": False,
                "members": {1: "admin"},
                "peer_disabled": False,
                "self_chat": False,
            },
        )

    async def test_first_user_ignores_requested_role_and_uses_workspace_setting(self) -> None:
        await self.database.run(db.set_meta, "workspace_name", "Acme")
        result = await self.register("ravi", "Ravi", role="member")
        self.assertEqual(result["user"]["role"], "admin")
        self.assertEqual((await self.query("SELECT title FROM chats"))[0][0], "Acme")

    async def test_setup_code_is_verified_inside_the_transaction(self) -> None:
        code = await auth.ensure_setup_code(self.database, self.data)
        await self.expect("setup_code_required", None, self.public("ravi", setup_code=None))
        await self.expect("bad_setup_code", None, self.public("ravi", setup_code="WRONG123"))
        self.assertEqual(await self.query("SELECT COUNT(*) FROM users"), [(0,)])
        result = await self.public("ravi", setup_code=code)
        self.assertEqual(result["user"]["role"], "admin")
        self.assertTrue(os.path.exists(os.path.join(self.data, "setup_code.txt")))  # the hub clears it after commit
        auth.clear_setup_code(self.data)
        self.assertFalse(os.path.exists(os.path.join(self.data, "setup_code.txt")))

    async def test_setup_code_directory_defaults_to_the_database_directory(self) -> None:
        code = await auth.ensure_setup_code(self.database, self.data)
        spec = self.spec("ravi", "Ravi", check_setup_code=True, setup_code=code)
        result = await self.database.run(db.register_user, spec)  # no data_dir argument
        self.assertTrue(result["first_user"])

    async def test_public_registration_cannot_pick_a_reserved_username_even_as_first_admin(self) -> None:
        code = await auth.ensure_setup_code(self.database, self.data)
        error = await self.expect("conflict", "username_taken", self.public("admin", setup_code=code))
        self.assertEqual(error.rest_code, "username_taken")

    async def test_first_admin_may_use_a_reserved_display_name(self) -> None:
        code = await auth.ensure_setup_code(self.database, self.data)
        result = await self.public("ravi", "Admin", setup_code=code)
        self.assertEqual(result["user"]["display_name"], "Admin")

    async def test_two_racing_first_registrations_make_exactly_one_admin(self) -> None:
        code = await auth.ensure_setup_code(self.database, self.data)
        gate = threading.Event()  # both threads start together
        outcomes: list = []

        def attempt(username: str) -> None:
            spec = self.spec(
                username, username.title(), check_setup_code=True, check_registration=True, setup_code=code
            )
            gate.wait(10)
            try:
                outcomes.append(self.database.run_sync(db.register_user, spec, 2000, self.data))
            except db.RequestError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=attempt, args=(name,)) for name in ("alice", "bobby")]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(30)
        winners = [o for o in outcomes if isinstance(o, dict)]
        losers = [o for o in outcomes if isinstance(o, db.RequestError)]
        self.assertEqual((len(winners), len(losers)), (1, 1))
        self.assertTrue(winners[0]["first_user"])
        self.assertEqual(losers[0].code, "registration_closed")
        self.assertEqual(await self.query("SELECT COUNT(*), SUM(role = 'admin') FROM users"), [(1, 1)])


class LaterUserTest(UsersCase):
    async def test_later_user_joins_everyone_with_a_joined_message_and_no_history(self) -> None:
        first = await self.first_admin()
        before = (await self.query("SELECT last_message_id, last_activity_at FROM chats"))[0]
        second = await self.register("amit", "Amit")
        self.assertFalse(second["first_user"])
        self.assertEqual(second["event"], "joined")
        self.assertEqual(second["chat_id"], first["chat_id"])
        self.assertEqual(second["member"]["delivered_up_to"], first["message_id"])
        self.assertEqual(second["member"]["read_up_to"], first["message_id"])
        after = (await self.query("SELECT last_message_id, last_activity_at FROM chats"))[0]
        self.assertEqual(after[0], second["message_id"])  # joined sets last_message_id ...
        self.assertEqual(after[1], before[1])  # ... but never re-sorts the chat list
        message = (await self.query("SELECT body, system FROM messages WHERE id = ?", second["message_id"]))[0]
        self.assertEqual(message[0], "Amit joined")
        self.assertEqual(json.loads(message[1])["actor_id"], None)
        self.assertEqual(json.loads(message[1])["target_ids"], [2])
        row = (await self.query("SELECT history_from_id, last_read_id FROM chat_members WHERE user_id = 2"))[0]
        self.assertEqual(row, (first["message_id"], first["message_id"]))
        self.assertLess(row[0], second["message_id"])  # the new member sees their own notice
        event = second["message_event"]  # every current member, the new one included, gets the system message
        self.assertEqual((event["t"], [g["user_ids"] for g in event["groups"]]), ("ev.message", [[1, 2]]))
        message = event["groups"][0]["d"]["message"]
        self.assertEqual(
            (message["id"], message["body"], message["system"]["event"]),
            (second["message_id"], "Amit joined", "joined"),
        )
        self.assertEqual(second["index"]["members"], {1: "admin", 2: "member"})
        self.assertEqual(second["index"]["chat_id"], first["chat_id"])

    async def test_admin_created_users_keep_role_and_are_not_activated(self) -> None:
        await self.first_admin()
        result = await self.register(
            "admin2", "Second Admin", role="admin", activated=False, must_change_password=True, actor_id=1
        )
        self.assertEqual(result["user"]["role"], "admin")
        self.assertFalse(result["user"]["activated"])
        self.assertTrue(result["me"]["must_change_password"])
        self.assertEqual(result["member"]["role"], "admin")  # Everyone mirrors users.role
        row = (await self.query("SELECT last_login_at, last_seen_at FROM users WHERE id = 2"))[0]
        self.assertEqual(row, (None, None))
        audit = await self.query("SELECT actor_id, action, target_id, ip FROM audit_log")
        self.assertEqual(audit, [(1, "admin.create_user", 2, "10.0.0.1")])

    async def test_registered_users_are_activated_at_once(self) -> None:
        await self.first_admin()
        result = await self.register("amit", "Amit", activated=True)
        self.assertTrue(result["user"]["activated"])
        self.assertIsNotNone((await self.query("SELECT last_login_at FROM users WHERE id = 2"))[0][0])

    async def test_actor_must_be_an_enabled_admin(self) -> None:
        await self.first_admin()
        await self.register("amit", "Amit")
        await self.expect("forbidden", None, self.register("bobby", "Bobby", actor_id=2))
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 1"))
        await self.expect("unauthorized", None, self.register("bobby", "Bobby", actor_id=1))
        await self.expect("unauthorized", None, self.register("bobby", "Bobby", actor_id=99))

    async def test_max_users_binds_everybody(self) -> None:
        await self.first_admin()
        await self.database.run(db.register_user, self.spec("amit", "Amit"), 2, self.data)
        error = await self.expect(
            "invalid_state", "max_users", self.database.run(db.register_user, self.spec("bobby", "Bobby"), 2, self.data)
        )
        self.assertEqual(error.rest_code, "registration_closed")
        await self.expect(
            "invalid_state",
            "max_users",
            self.database.run(db.register_user, self.spec("bobby", "Bobby", actor_id=1, role="admin"), 2, self.data),
        )
        self.assertEqual(await self.query("SELECT COUNT(*) FROM users"), [(2,)])

    async def test_public_registration_needs_open_registration_and_the_join_code(self) -> None:
        await self.first_admin()
        await self.expect("registration_closed", None, self.public("amit", join_code=None))
        await self.database.run(db.admin_settings, 1, "DeskTalk", False, None, None, True)
        code = await self.database.run_read(db.get_meta, "join_code")
        await self.expect("bad_join_code", None, self.public("amit", join_code=None))
        await self.expect("bad_join_code", None, self.public("amit", join_code="nope"))
        result = await self.public("amit", join_code=code, role="admin")
        self.assertEqual(result["user"]["role"], "member")  # a public caller can never choose a role
        # max_users on the public path answers registration_closed first
        error = await self.expect(
            "registration_closed",
            None,
            self.database.run(
                db.register_user, self.spec("bobby", check_registration=True, join_code=code), 2, self.data
            ),
        )
        self.assertEqual(error.rest_code, "registration_closed")

    async def test_registration_open_default_comes_from_the_caller_until_an_admin_edits_it(self) -> None:
        await self.first_admin()
        spec = self.spec(
            "amit", check_registration=True, join_code=await self.database.run_read(db.get_meta, "join_code")
        )
        result = await self.database.run(db.register_user, spec, 2000, self.data, None, True)
        self.assertEqual(result["user"]["username"], "amit")

    async def test_username_rules(self) -> None:
        await self.first_admin()
        for bad in ("ab", "a" * 33, "bad name", "Caf\u00e9", "x\n", 7, None):
            await self.expect("bad_request", None, self.register(bad, "Name"))  # type: ignore[arg-type]
        created = await self.register("MiXeD.Case_1-x", "Mixed")
        self.assertEqual(created["user"]["username"], "mixed.case_1-x")  # stored lowercase
        await self.expect("conflict", "username_taken", self.register("Mixed.CASE_1-x", "Other"))
        await self.expect("conflict", "username_taken", self.register("ravi", "Other"))

    async def test_reserved_usernames_are_admin_only(self) -> None:
        await self.first_admin()
        for name in sorted(n for n in util.RESERVED_USERNAMES if len(n) >= 3):  # 'it' and 'hr' are too short anyway
            await self.expect(
                "conflict", "username_taken", self.register(name, name.title() + "x", check_setup_code=True)
            )
        trusted = await self.register("helpdesk", "Help Desk", actor_id=1)
        self.assertEqual(trusted["user"]["username"], "helpdesk")
        cli = await self.register("root", "Root")  # no flags at all: the CLI path
        self.assertEqual(cli["user"]["username"], "root")

    async def test_display_name_rules(self) -> None:
        await self.first_admin()
        await self.register("amit", "Amit  Sharma\u200b")
        self.assertEqual((await self.query("SELECT display_name FROM users WHERE id = 2"))[0][0], "Amit Sharma")
        for bad in ("", "   ", "\u200b", "x" * 41, 5):
            await self.expect("bad_request", None, self.register("bobby", bad))  # type: ignore[arg-type]
        await self.register("carol", "C" * 40)
        await self.expect("conflict", "name_taken", self.register("bobby", "AMIT sharma"))
        await self.expect("conflict", "name_taken", self.register("bobby", "\uff21mit Sharma"))  # NFKC equal

    async def test_reverse_rules_between_usernames_and_display_names(self) -> None:
        await self.first_admin()
        await self.register("amit", "Amit")
        await self.register("sam", "Sally Jones")
        # a display name must not equal another user's username (case-insensitively) ...
        await self.expect("conflict", "name_taken", self.register("bobby", "AMIT"))
        await self.expect("conflict", "name_taken", self.register("bobby", "Sam"))
        # ... and a new username must not equal another user's display key
        await self.register("dave.x", "Dave.D")
        await self.expect("conflict", "username_taken", self.register("dave.d", "Whoever"))
        # the new user's own username may equal their own display key
        own = await self.register("erin.e", "Erin.E")
        self.assertEqual(own["user"]["display_name"], "Erin.E")

    async def test_reserved_display_names_need_an_admin(self) -> None:
        await self.first_admin()
        await self.expect("conflict", "name_taken", self.register("amit", "Admin", check_setup_code=True))
        await self.expect("conflict", "name_taken", self.register("amit", "HR", check_setup_code=True))
        await self.database.run(db.admin_settings, 1, "DeskTalk", False, None, None, True)
        code = await self.database.run_read(db.get_meta, "join_code")
        await self.expect("conflict", "name_taken", self.public("amit", "HR", join_code=code))
        ok = await self.register("hrlead", "HR", actor_id=1)  # an admin may
        self.assertEqual(ok["user"]["display_name"], "HR")

    async def test_missing_password_hash_is_rejected(self) -> None:
        await self.expect("bad_request", None, self.register("ravi", "Ravi", pw_hash=""))
        await self.expect("bad_request", None, self.register("ravi", "Ravi", pw_hash=None))

    async def test_failed_registration_leaves_nothing_behind(self) -> None:
        await self.first_admin()
        await self.expect("conflict", "name_taken", self.register("bobby", "Ravi Kumar"))
        self.assertEqual(await self.query("SELECT COUNT(*) FROM users"), [(1,)])
        self.assertEqual(await self.query("SELECT COUNT(*) FROM messages"), [(1,)])
        self.assertEqual(await self.query("SELECT COUNT(*) FROM chat_members"), [(1,)])


class SessionTest(UsersCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.first_admin()
        await self.register("amit", "Amit", activated=False)

    async def login(self, user_id: int = 1, ts: Optional[float] = None, **extra: Any) -> Dict[str, Any]:
        token, digest = auth.new_session_token()
        result = await self.database.run(
            lambda conn: db.complete_login(conn, user_id, digest, "10.0.0.2", "UA/1", 30, ts=ts, **extra)
        )
        result["token"], result["hash"] = token, digest
        return result

    async def test_first_login_activates_and_sets_timestamps(self) -> None:
        first = await self.login(2, ts=1000.0)
        self.assertTrue(first["first_login"])
        self.assertTrue(first["user"]["activated"])
        self.assertEqual(first["expires_at"], 1000.0 + 30 * 86400)
        self.assertEqual(
            (await self.query("SELECT last_login_at, last_seen_at FROM users WHERE id = 2"))[0], (1000.0, 1000.0)
        )
        second = await self.login(2, ts=2000.0)
        self.assertFalse(second["first_login"])
        self.assertEqual((await self.query("SELECT last_login_at FROM users WHERE id = 2"))[0], (2000.0,))

    async def test_disabled_and_unknown_users_cannot_log_in(self) -> None:
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 2"))
        await self.expect("disabled", None, self.login(2))
        await self.expect("unauthorized", None, self.login(77))
        self.assertEqual(await self.query("SELECT COUNT(*) FROM sessions"), [(0,)])

    async def test_rehash_is_compare_and_set(self) -> None:
        old = "hash-ravi"
        await self.login(1, old_pw_hash=old, new_pw_hash="new-hash")
        self.assertEqual((await self.query("SELECT pw_hash FROM users WHERE id = 1"))[0][0], "new-hash")
        await self.login(1, old_pw_hash=old, new_pw_hash="stale-overwrite")  # the password changed meanwhile
        self.assertEqual((await self.query("SELECT pw_hash FROM users WHERE id = 1"))[0][0], "new-hash")

    async def test_login_record_lookup(self) -> None:
        record = await self.database.run_read(db.get_login_record, "RAVI")
        self.assertEqual((record["id"], record["pw_hash"], record["disabled"]), (1, "hash-ravi", False))
        self.assertIsNone(await self.database.run_read(db.get_login_record, "nobody"))
        self.assertIsNone(await self.database.run_read(db.get_login_record, "a b"))
        self.assertIsNone(await self.database.run_read(db.get_login_record, None))
        by_id = await self.database.run_read(db.get_password_record, 2)
        self.assertEqual(by_id["username"], "amit")
        self.assertIsNone(await self.database.run_read(db.get_password_record, 5))

    async def test_lookup_touch_and_expiry(self) -> None:
        session = await self.login(1, ts=1000.0)
        found = await self.database.run_read(db.lookup_session, session["hash"], 1500.0)
        self.assertEqual(
            found,
            {"user_id": 1, "last_used_at": 1000.0, "expires_at": 1000.0 + 30 * 86400, "must_change_password": False},
        )
        self.assertIsNone(await self.database.run_read(db.lookup_session, session["hash"], 1000.0 + 30 * 86400))
        self.assertIsNone(await self.database.run_read(db.lookup_session, "nope", 1500.0))
        expires = await self.database.run(db.touch_session, session["hash"], 30, 5000.0)
        self.assertEqual(expires, 5000.0 + 30 * 86400)
        found = await self.database.run_read(db.lookup_session, session["hash"], 6000.0)
        self.assertEqual((found["last_used_at"], found["expires_at"]), (5000.0, expires))
        self.assertIsNone(await self.database.run(db.touch_session, "nope", 30, 5000.0))

    async def test_lookup_hides_disabled_users_and_reports_forced_change(self) -> None:
        session = await self.login(1)
        await self.database.run(lambda c: c.execute("UPDATE users SET must_change_password = 1 WHERE id = 1"))
        self.assertTrue((await self.database.run_read(db.lookup_session, session["hash"]))["must_change_password"])
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 1"))
        self.assertIsNone(await self.database.run_read(db.lookup_session, session["hash"]))

    async def test_list_and_revoke_sessions(self) -> None:
        a = await self.login(1, ts=1000.0)
        b = await self.login(1, ts=2000.0)
        other = await self.login(2, ts=1500.0)
        listing = await self.database.run_read(db.list_sessions, 1, a["hash"], 3000.0)
        self.assertEqual([s["id"] for s in listing], [b["hash"][:16], a["hash"][:16]])  # most recently used first
        self.assertEqual([s["current"] for s in listing], [False, True])
        self.assertEqual(set(listing[0]), {"id", "created_at", "last_used_at", "ip", "user_agent", "current"})
        self.assertEqual(listing[0]["ip"], "10.0.0.2")
        # only the caller's own sessions match
        self.assertIsNone(await self.database.run(db.revoke_session, 1, other["hash"][:16]))
        self.assertIsNone(await self.database.run(db.revoke_session, 1, "zzzz"))
        self.assertIsNone(await self.database.run(db.revoke_session, 1, None))
        self.assertEqual(await self.database.run(db.revoke_session, 1, b["hash"][:16]), b["hash"])
        self.assertEqual(await self.database.run(db.revoke_other_sessions, 1, "other-hash"), [a["hash"]])
        self.assertEqual(len(await self.database.run_read(db.list_sessions, 2, None, 3000.0)), 1)
        # expired sessions are not listed
        self.assertEqual(await self.database.run_read(db.list_sessions, 2, None, 1500.0 + 31 * 86400), [])

    async def test_logout_and_revoke_all(self) -> None:
        a = await self.login(1)
        b = await self.login(1)
        self.assertFalse(await self.database.run(db.delete_session, 2, a["hash"]))  # not that user's session
        self.assertTrue(await self.database.run(db.delete_session, 1, a["hash"]))
        self.assertEqual(await self.database.run(db.revoke_user_sessions, 1), [b["hash"]])
        self.assertEqual(await self.query("SELECT COUNT(*) FROM sessions"), [(0,)])

    async def test_purge_expired(self) -> None:
        a = await self.login(1, ts=1000.0)
        await self.login(1, ts=5_000_000.0)
        self.assertEqual(await self.database.run(db.purge_expired_sessions, 1000.0 + 30 * 86400 + 1), 1)
        self.assertIsNone(await self.database.run_read(db.lookup_session, a["hash"], 1001.0))

    async def test_session_statuses(self) -> None:
        a = await self.login(1)
        b = await self.login(2)
        gone = await self.login(2)
        await self.database.run(db.delete_session, 2, gone["hash"])
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 2"))
        states = await self.database.run_read(
            db.session_statuses, [(1, a["hash"]), (2, b["hash"]), (2, gone["hash"]), (1, "unknown")]
        )
        self.assertEqual(
            states, {a["hash"]: "ok", b["hash"]: "disabled", gone["hash"]: "disabled", "unknown": "revoked"}
        )
        self.assertEqual(await self.database.run_read(db.session_statuses, []), {})
        many = [(1, "h%d" % i) for i in range(1200)]  # more than one chunk of IN parameters
        self.assertEqual(set((await self.database.run_read(db.session_statuses, many)).values()), {"revoked"})

    async def test_change_password(self) -> None:
        a = await self.login(1)
        b = await self.login(1)
        await self.database.run(lambda c: c.execute("UPDATE users SET must_change_password = 1 WHERE id = 1"))
        revoked = await self.database.run(db.change_password, 1, "hash-ravi", "new", a["hash"])
        self.assertEqual(revoked, [b["hash"]])
        row = (await self.query("SELECT pw_hash, must_change_password FROM users WHERE id = 1"))[0]
        self.assertEqual(row, ("new", 0))
        await self.expect("conflict", None, self.database.run(db.change_password, 1, "hash-ravi", "x", a["hash"]))
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 1"))
        await self.expect("unauthorized", None, self.database.run(db.change_password, 1, "new", "x", a["hash"]))

    async def test_set_user_password(self) -> None:
        a = await self.login(2)
        self.assertEqual(await self.database.run(db.set_user_password, 2, "fresh", True), [a["hash"]])
        self.assertEqual(
            (await self.query("SELECT pw_hash, must_change_password FROM users WHERE id = 2"))[0], ("fresh", 1)
        )
        b = await self.login(2)
        self.assertEqual(await self.database.run(db.set_user_password, 2, "again", False, False), [])
        self.assertIsNotNone(await self.database.run_read(db.lookup_session, b["hash"]))
        await self.expect("not_found", None, self.database.run(db.set_user_password, 9, "x", False))

    async def test_session_fields_are_clipped(self) -> None:
        digest = auth.token_hash("x" * 40)
        await self.database.run(db.create_session, 1, digest, "1" * 200, "ua\x00" + "u" * 1000, 30)
        row = (await self.query("SELECT ip, user_agent FROM sessions"))[0]
        self.assertEqual((len(row[0]), len(row[1])), (64, 300))
        self.assertNotIn("\x00", row[1])


class DirectoryTest(UsersCase):
    async def test_user_shapes(self) -> None:
        await self.first_admin()
        await self.register("amit", "Amit", activated=False)
        users = await self.database.run_read(db.list_users)
        self.assertEqual([u["id"] for u in users], [1, 2])
        self.assertEqual(
            set(users[0]),
            {
                "id",
                "username",
                "display_name",
                "status_text",
                "role",
                "last_seen",
                "disabled",
                "activated",
                "read_receipts",
            },
        )
        self.assertFalse(users[1]["activated"])
        self.assertIsNone(users[1]["last_seen"])
        me = await self.database.run_read(db.get_me, 1)
        self.assertEqual(set(me) - set(users[0]), {"show_last_seen", "must_change_password"})
        self.assertEqual(await self.database.run_read(db.get_user, 2), users[1])
        self.assertIsNone(await self.database.run_read(db.get_user, 9))
        self.assertIsNone(await self.database.run_read(db.get_me, 9))

    async def test_last_seen_privacy(self) -> None:
        await self.first_admin()
        await self.database.run(db.set_last_seen, [1], 1234.5)
        self.assertEqual((await self.database.run_read(db.get_user, 1))["last_seen"], 1234.5)
        await self.database.run(lambda c: c.execute("UPDATE users SET show_last_seen = 0 WHERE id = 1"))
        self.assertIsNone((await self.database.run_read(db.get_user, 1))["last_seen"])

    async def test_set_last_seen_handles_many_ids_and_none(self) -> None:
        await self.first_admin()
        await self.database.run(db.set_last_seen, [], 5.0)
        await self.database.run(db.set_last_seen, list(range(1, 1500)), 77.0)
        self.assertEqual(await self.query("SELECT last_seen_at FROM users"), [(77.0,)])

    async def test_setup_state_queries(self) -> None:
        self.assertTrue(await self.database.run_read(db.needs_setup))
        self.assertEqual(await self.database.run_read(db.user_count), 0)
        await self.first_admin()
        self.assertFalse(await self.database.run_read(db.needs_setup))
        self.assertEqual(await self.database.run_read(db.user_count), 1)
        self.assertRegex(await self.database.run_read(db.get_instance_id), r"^[0-9a-f]{32}$")

    async def test_require_helpers(self) -> None:
        await self.first_admin()
        await self.register("amit", "Amit")
        self.assertEqual((await self.database.run_read(db.require_admin, 1))["role"], "admin")
        await self.expect("forbidden", None, self.database.run_read(db.require_admin, 2))
        await self.expect("unauthorized", None, self.database.run_read(db.require_active_user, 9))
        await self.expect("unauthorized", None, self.database.run_read(db.require_active_user, True))
        await self.expect("unauthorized", None, self.database.run_read(db.require_active_user, None))
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 2"))
        await self.expect("unauthorized", None, self.database.run_read(db.require_active_user, 2))


class ProfileTest(UsersCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.first_admin()
        await self.register("amit", "Amit")
        await self.register("sam", "Sam Smith")

    async def update(self, user_id: int, **fields: Any) -> Dict[str, Any]:
        return await self.database.run(db.profile_update, user_id, fields)

    async def test_changes_are_reported(self) -> None:
        result = await self.update(2, display_name="  Amit   Kumar ", status_text=" busy  now ", read_receipts=False)
        self.assertTrue(result["changed"])
        self.assertEqual(result["changed_fields"], ["display_name", "read_receipts", "status_text"])
        self.assertEqual(
            (result["me"]["display_name"], result["me"]["status_text"], result["me"]["read_receipts"]),
            ("Amit Kumar", "busy now", False),
        )
        key = (await self.query("SELECT display_key FROM users WHERE id = 2"))[0][0]
        self.assertEqual(key, "amit kumar")
        shown = await self.update(2, show_last_seen=False)
        self.assertFalse(shown["me"]["show_last_seen"])

    async def test_no_op_changes_nothing(self) -> None:
        result = await self.update(2, display_name="Amit", status_text="", read_receipts=True, show_last_seen=True)
        self.assertFalse(result["changed"])
        self.assertEqual(result["changed_fields"], [])
        again = await self.update(2, display_name="AMIT")  # same key, different spelling: a real change
        self.assertTrue(again["changed"])
        self.assertEqual(again["me"]["display_name"], "AMIT")

    async def test_validation(self) -> None:
        await self.expect("bad_request", None, self.update(2))
        await self.expect("bad_request", None, self.update(2, unknown=1))
        await self.expect("bad_request", None, self.update(2, display_name=""))
        await self.expect("bad_request", None, self.update(2, display_name="x" * 41))
        await self.expect("bad_request", None, self.update(2, status_text="x" * 141))
        await self.expect("bad_request", None, self.update(2, read_receipts="yes"))
        await self.expect("bad_request", None, self.update(2, show_last_seen=1))
        await self.update(2, status_text="x" * 140)

    async def test_display_name_conflicts(self) -> None:
        await self.expect("conflict", "name_taken", self.update(2, display_name="sam smith"))
        await self.expect("conflict", "name_taken", self.update(2, display_name="Sam"))  # another user's username
        await self.expect("conflict", "name_taken", self.update(2, display_name="HR"))  # reserved, not an admin
        await self.expect("conflict", "name_taken", self.update(2, display_name="everyone"))
        admin = await self.update(1, display_name="HR")
        self.assertEqual(admin["me"]["display_name"], "HR")
        own = await self.update(2, display_name="amit")  # own username is excepted
        self.assertEqual(own["me"]["display_name"], "amit")

    async def test_disabled_or_unknown_caller(self) -> None:
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 3"))
        await self.expect("unauthorized", None, self.update(3, status_text="x"))
        await self.expect("unauthorized", None, self.update(9, status_text="x"))


class AdminTest(UsersCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.first_admin()
        await self.register("amit", "Amit")

    async def test_everything_is_admin_only(self) -> None:
        await self.expect("forbidden", None, self.database.run_read(db.admin_users, 2))
        await self.expect("forbidden", None, self.database.run_read(db.admin_stats, 2))
        await self.expect("forbidden", None, self.database.run_read(db.admin_audit, 2))
        await self.expect("forbidden", None, self.database.run(db.admin_settings, 2, "x", False))
        await self.expect("forbidden", None, self.database.run(db.admin_reset_password, 2, 1, "h"))
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 1"))
        await self.expect("unauthorized", None, self.database.run_read(db.admin_users, 1))

    async def test_admin_users_has_the_extra_fields(self) -> None:
        await self.database.run(
            lambda c: c.execute(
                "INSERT INTO messages(chat_id, sender_id, kind, body, created_at) VALUES (1, 2, 'text', 'hi', 1)"
            )
        )
        users = await self.database.run_read(db.admin_users, 1)
        self.assertEqual([(u["id"], u["message_count"]) for u in users], [(1, 0), (2, 1)])
        self.assertEqual(
            set(users[0]) - set(await self.database.run_read(db.get_user, 1)),
            {"created_at", "last_login_at", "message_count", "must_change_password"},
        )

    async def test_reset_password(self) -> None:
        token, digest = auth.new_session_token()
        await self.database.run(db.create_session, 2, digest, "1.1.1.1", "ua", 30)
        result = await self.database.run(db.admin_reset_password, 1, 2, "temp-hash", "9.9.9.9", 50.0)
        self.assertEqual(result, {"user_id": 2, "username": "amit", "revoked": [digest]})
        self.assertEqual(
            (await self.query("SELECT pw_hash, must_change_password FROM users WHERE id = 2"))[0], ("temp-hash", 1)
        )
        self.assertEqual(await self.query("SELECT COUNT(*) FROM sessions"), [(0,)])
        self.assertEqual(
            await self.query("SELECT ts, actor_id, action, target_id, ip FROM audit_log"),
            [(50.0, 1, "admin.reset_password", 2, "9.9.9.9")],
        )
        await self.expect("not_found", None, self.database.run(db.admin_reset_password, 1, 99, "h"))
        self.assertEqual(await self.query("SELECT COUNT(*) FROM audit_log"), [(1,)])

    async def test_settings_read_is_free_of_side_effects(self) -> None:
        result = await self.database.run(db.admin_settings, 1, "DeskTalk", False)
        self.assertFalse(result["changed"])
        self.assertEqual(set(result["workspace"]), {"name", "registration_open", "join_code"})
        self.assertEqual(result["workspace"]["name"], "DeskTalk")
        self.assertFalse(result["workspace"]["registration_open"])
        self.assertRegex(result["workspace"]["join_code"], r"^[A-Za-z0-9_-]{8}$")
        self.assertEqual(await self.query("SELECT COUNT(*) FROM audit_log"), [(0,)])
        self.assertEqual(
            await self.query("SELECT COUNT(*) FROM meta WHERE key IN ('workspace_name', 'registration_open')"), [(0,)]
        )
        again = await self.database.run_read(db.admin_settings, 1, "DeskTalk", False)  # also valid on a reader
        self.assertEqual(again, result)

    async def test_settings_changes_audit_once_and_no_ops_do_not(self) -> None:
        noop = await self.database.run(db.admin_settings, 1, "DeskTalk", False, "1.2.3.4", "DeskTalk", False, False)
        self.assertFalse(noop["changed"])
        self.assertEqual(await self.query("SELECT COUNT(*) FROM audit_log"), [(0,)])
        old_code = noop["workspace"]["join_code"]
        changed = await self.database.run(
            db.admin_settings, 1, "DeskTalk", False, "1.2.3.4", "  New   Name ", True, True, 77.0
        )
        self.assertTrue(changed["changed"])
        self.assertEqual((changed["workspace"]["name"], changed["workspace"]["registration_open"]), ("New Name", True))
        self.assertNotEqual(changed["workspace"]["join_code"], old_code)
        self.assertEqual(
            await self.query("SELECT ts, actor_id, action, target_id, ip FROM audit_log"),
            [(77.0, 1, "admin.settings", None, "1.2.3.4")],
        )
        self.assertEqual((await self.query("SELECT title FROM chats"))[0][0], "DeskTalk")  # Everyone is never renamed
        same = await self.database.run(db.admin_settings, 1, "DeskTalk", False, None, "New Name", True)
        self.assertFalse(same["changed"])
        self.assertEqual(await self.query("SELECT COUNT(*) FROM audit_log"), [(1,)])
        rotated = await self.database.run(db.admin_settings, 1, "DeskTalk", False, None, None, None, True)
        self.assertNotEqual(rotated["workspace"]["join_code"], changed["workspace"]["join_code"])

    async def test_settings_validation(self) -> None:
        await self.expect("bad_request", None, self.database.run(db.admin_settings, 1, "D", False, None, ""))
        await self.expect("bad_request", None, self.database.run(db.admin_settings, 1, "D", False, None, "x" * 41))
        await self.expect("bad_request", None, self.database.run(db.admin_settings, 1, "D", False, None, 5))
        await self.expect("bad_request", None, self.database.run(db.admin_settings, 1, "D", False, None, None, "yes"))
        await self.expect(
            "bad_request", None, self.database.run(db.admin_settings, 1, "D", False, None, None, None, "yes")
        )
        self.assertEqual(await self.query("SELECT COUNT(*) FROM audit_log"), [(0,)])

    async def test_workspace_settings_precedence(self) -> None:
        self.assertEqual(
            await self.database.run_read(db.workspace_settings, "Cfg", True), {"name": "Cfg", "registration_open": True}
        )
        await self.database.run(db.admin_settings, 1, "Cfg", True, None, "Db", False)
        self.assertEqual(
            await self.database.run_read(db.workspace_settings, "Cfg", True), {"name": "Db", "registration_open": False}
        )
        with_code = await self.database.run_read(db.workspace_settings, "Cfg", True, True)
        self.assertIn("join_code", with_code)

    async def test_stats_and_audit(self) -> None:
        for index in range(120):
            await self.database.run(db.audit, 1, "admin.update_user", 2, "ip", float(index))
        stats = await self.database.run_read(db.admin_stats, 1)
        self.assertEqual(stats, {"users": 2, "chats": 1, "messages": 2, "attachments": 0, "last_backup_at": None})
        await self.database.run(db.set_meta, "last_backup_at", "123.5")
        self.assertEqual((await self.database.run_read(db.admin_stats, 1))["last_backup_at"], 123.5)
        await self.database.run(db.set_meta, "last_backup_at", "garbage")
        self.assertIsNone((await self.database.run_read(db.admin_stats, 1))["last_backup_at"])
        entries = await self.database.run_read(db.admin_audit, 1)
        self.assertEqual(len(entries), 100)
        self.assertEqual(entries[0]["ts"], 119.0)
        self.assertEqual(set(entries[0]), {"id", "ts", "actor_id", "action", "target_id", "ip"})
        self.assertGreater(entries[0]["id"], entries[1]["id"])
        self.assertEqual(len(await self.database.run_read(db.admin_audit, 1, 5)), 5)
        self.assertEqual(len(await self.database.run_read(db.admin_audit, 1, 0)), 1)
        self.assertEqual(len(await self.database.run_read(db.admin_audit, 1, 10**6)), 100)
        self.assertEqual(len(await self.database.run_read(db.admin_audit, 1, "x")), 100)


class DirectDatabaseTest(unittest.TestCase):
    def test_register_user_needs_the_default_chat_for_later_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = db.Database(os.path.join(tmp, "chat.db"))
            database.open()
            try:
                spec = {"username": "ravi", "display_name": "Ravi", "pw_hash": "h", "activated": True}
                database.run_sync(db.register_user, spec, 2000, tmp)
                database.run_sync(lambda c: c.execute("UPDATE chats SET is_default = 0"))
                with self.assertRaises(db.DbError):
                    database.run_sync(db.register_user, dict(spec, username="amit", display_name="Amit"), 2000, tmp)
                self.assertEqual(database.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM users").fetchone()[0]), 1)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
