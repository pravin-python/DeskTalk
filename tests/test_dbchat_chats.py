"""chat.* requests of ``chatd/db_chats.py``: direct/dormant rules, groups, membership, prefs, clear (SPEC 3.2, 7.4)."""

from __future__ import annotations

import unittest
from typing import Any, List

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db_chats, db_messages, db_receipts, util


class OpenDirectTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")

    def test_new_chat_is_dormant_and_event_goes_to_caller_only(self) -> None:
        out = self.write(db_chats.chat_open_direct, self.a, self.b)
        chat = out["res"]["chat"]
        self.assertEqual(
            (chat["kind"], chat["title"], chat["created_by"], chat["peer_id"]), ("direct", None, None, self.b)
        )
        self.assertEqual([m["user_id"] for m in chat["members"]], [self.a, self.b])
        self.assertIsNone(chat["last_message_id"])
        self.assertIsNone(chat["last_message"])
        self.assertEqual(self.types(out), ["ev.chat_update"])
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.a})
        rows = dict(self.sql("SELECT user_id, listed FROM chat_members WHERE chat_id = ?", (chat["id"],)))
        self.assertEqual(rows, {self.a: 1, self.b: 0})
        self.assertEqual(out["index"][0]["members"], {self.a: "member"})
        joined = self.scalar(
            "SELECT joined_at FROM chat_members WHERE chat_id = ? AND user_id = ?", (chat["id"], self.a)
        )
        self.assertEqual(chat["created_at"], joined)

    def test_existing_listed_chat_is_returned_unchanged_without_events(self) -> None:
        first = self.write(db_chats.chat_open_direct, self.a, self.b)
        again = self.write(db_chats.chat_open_direct, self.a, self.b)
        self.assertEqual(again["events"], [])
        self.assertTrue(again["noop"])
        self.assertEqual(again["res"]["chat"]["id"], first["res"]["chat"]["id"])

    def test_unlisted_peer_is_not_a_member_for_every_request(self) -> None:
        chat_id = self.dm(self.a, self.b)
        source = self.mid(self.send(self.a, self.everyone, "visible to bob"))
        b = self.b
        self.fails("not_member", db_chats.chat_get, b, chat_id, reader=True)
        self.fails("not_member", db_messages.chat_history, b, chat_id, reader=True)
        self.fails("not_member", db_messages.msg_send, b, chat_id, "cid-12345678", "hi")
        self.fails("not_member", db_receipts.receipt_read, b, chat_id, 1)
        self.fails("not_member", db_receipts.receipt_delivered, b, [{"chat_id": chat_id, "up_to_id": 1}])
        self.fails("not_member", db_chats.chat_prefs, b, chat_id, None, True)
        self.fails("not_member", db_chats.chat_clear, b, chat_id)
        self.fails("not_member", db_messages.msg_search, b, "ab", chat_id, reader=True)
        self.fails("not_member", db_messages.msg_shared, b, chat_id, "media", reader=True)
        self.fails("not_member", db_messages.msg_starred, b, chat_id, reader=True)
        self.fails("not_member", db_messages.msg_forward, b, [source], [chat_id], "fwd-12345678")
        ready = self.read(db_chats.build_ready, b)
        self.assertNotIn(chat_id, [c["id"] for c in ready["chats"]])

    def test_unlisted_peer_opening_lists_it_for_that_caller_only(self) -> None:
        chat_id = self.dm(self.a, self.b)
        out = self.write(db_chats.chat_open_direct, self.b, self.a)
        self.assertEqual(out["res"]["chat"]["id"], chat_id)
        self.assertIsNone(out["res"]["chat"]["created_by"])
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.b})
        self.assertEqual(self.chat_of(self.b, chat_id)["id"], chat_id)
        self.assertEqual(self.write(db_chats.chat_open_direct, self.b, self.a)["events"], [])

    def test_first_message_lists_the_peer_and_updates_before_the_message(self) -> None:
        chat_id = self.dm(self.a, self.b)
        out = self.send(self.a, chat_id, "hello")
        self.assertEqual(self.types(out)[:2], ["ev.chat_update", "ev.message"])
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.b})
        update = self.payloads(out, "ev.chat_update", self.b)[0]["chat"]
        self.assertEqual(update["me"]["unread"], 1)
        self.assertEqual(update["last_message"]["body"], "hello")
        self.assertEqual(
            self.scalar("SELECT listed FROM chat_members WHERE chat_id = ? AND user_id = ?", (chat_id, self.b)), 1
        )
        self.assertEqual(out["index"][0]["members"], {self.a: "member", self.b: "member"})
        later = self.send(self.a, chat_id, "again")
        self.assertNotIn("ev.chat_update", self.types(later))

    def test_self_chat(self) -> None:
        out = self.write(db_chats.chat_open_direct, self.a, self.a)
        chat = out["res"]["chat"]
        self.assertEqual(chat["peer_id"], self.a)
        self.assertEqual(len(chat["members"]), 1)
        sent = self.send(self.a, chat["id"], "note to self")
        self.assertIsNone(sent["res"]["message"]["status"])
        self.assertNotIn("ev.receipt", self.types(sent))
        self.assertEqual(self.audience(sent, "ev.message"), {self.a})
        self.assertEqual(self.me(self.a, chat["id"])["unread"], 0)
        self.assertEqual(self.write(db_receipts.receipt_read, self.a, chat["id"], 99)["res"]["unread"], 0)

    def test_new_chat_with_disabled_peer_is_invalid_state(self) -> None:
        self.set_user(self.b, disabled=1)
        self.fails("invalid_state", db_chats.chat_open_direct, self.a, self.b)

    def test_existing_chat_survives_a_later_disable(self) -> None:
        chat_id = self.dm(self.a, self.b)
        self.send(self.a, chat_id, "hi")
        self.set_user(self.b, disabled=1)
        self.assertEqual(self.write(db_chats.chat_open_direct, self.a, self.b)["res"]["chat"]["id"], chat_id)
        self.fails("invalid_state", db_messages.msg_send, self.a, chat_id, "cid-87654321", "hello")

    def test_errors(self) -> None:
        self.fails("not_found", db_chats.chat_open_direct, self.a, 9999)
        self.fails("bad_request", db_chats.chat_open_direct, self.a, True)
        self.set_user(self.a, disabled=1)
        self.fails("unauthorized", db_chats.chat_open_direct, self.a, self.b)

    def test_direct_chat_id_lookup(self) -> None:
        self.assertIsNone(self.read(db_chats.direct_chat_id, self.a, self.b))
        chat_id = self.dm(self.a, self.b)
        self.assertEqual(self.read(db_chats.direct_chat_id, self.b, self.a), chat_id)

    def test_direct_chats_reject_group_operations(self) -> None:
        chat_id = self.dm(self.a, self.b)
        self.send(self.a, chat_id, "x")
        calls: List[Any] = [
            (db_chats.chat_update, self.a, chat_id, "T"),
            (db_chats.chat_add_members, self.a, chat_id, [self.b]),
            (db_chats.chat_remove_member, self.a, chat_id, self.b),
            (db_chats.chat_set_admin, self.a, chat_id, self.b, True),
            (db_chats.chat_leave, self.a, chat_id),
        ]
        for fn, *args in calls:
            self.fails("invalid_state", fn, *args)
        # invalid_state precedes forbidden: the (non-admin) peer gets the same code
        self.fails("invalid_state", db_chats.chat_update, self.b, chat_id, "T")


class _BlindProxy:
    """Delegates to a real connection but hides the first ``direct_key`` lookup: the UNIQUE race of SPEC 3."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.blind = True

    def execute(self, sql: str, params: Any = ()) -> Any:
        if self.blind and sql.startswith("SELECT id FROM chats WHERE direct_key"):
            self.blind = False
            return self._conn.execute("SELECT 1 WHERE 0")
        return self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class OpenDirectRaceTest(support.DbChatCase):
    def test_unique_direct_key_race_is_resolved_by_reselecting(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        chat_id = self.dm(a, b)
        out = self.write(lambda conn: db_chats.chat_open_direct(_BlindProxy(conn), a, b))
        self.assertEqual(out["res"]["chat"]["id"], chat_id)
        self.assertTrue(out["noop"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM chats WHERE kind = 'direct'"), 1)


class GroupTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")
        self.d = self.user("dave")

    def test_create_group_happy_path(self) -> None:
        members = [self.b, self.c, self.b, self.a]
        out = self.write(db_chats.chat_create_group, self.a, "  Project   X ", members, "line1\n\nline2")
        chat = out["res"]["chat"]
        self.assertEqual(chat["title"], "Project X")
        self.assertEqual(chat["description"], "line1\n\nline2")
        self.assertEqual(
            [(m["user_id"], m["role"]) for m in chat["members"]],
            [(self.a, "admin"), (self.b, "member"), (self.c, "member")],
        )
        self.assertEqual(chat["last_message"]["system"]["event"], "created")
        self.assertEqual(chat["last_message"]["system"]["target_ids"], [self.b, self.c])
        self.assertEqual(chat["me"]["unread"], 0)
        self.assertEqual(self.types(out), ["ev.chat_update", "ev.message"])
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.a, self.b, self.c})
        self.assertEqual(self.audience(out, "ev.message"), {self.a, self.b, self.c})
        self.assertEqual(set(out["chats"]), {(u, chat["id"]) for u in (self.a, self.b, self.c)})
        self.assertEqual(out["index"][0]["members"], {self.a: "admin", self.b: "member", self.c: "member"})
        self.assertEqual(len(out["recipients"]), 3)
        # every member's chat already holds the system message and counts nothing as unread
        self.assertEqual(self.me(self.b, chat["id"])["unread"], 0)

    def test_create_group_validation(self) -> None:
        self.fails("bad_request", db_chats.chat_create_group, self.a, "   ", [])
        self.fails("bad_request", db_chats.chat_create_group, self.a, "x" * 61, [])
        self.fails("bad_request", db_chats.chat_create_group, self.a, "T", "nope")
        self.fails("bad_request", db_chats.chat_create_group, self.a, "T", [0])
        self.fails("bad_request", db_chats.chat_create_group, self.a, "T", [], "d" * 501)
        self.fails("bad_request", db_chats.chat_create_group, self.a, "T", list(range(1, 202)))
        self.fails("not_found", db_chats.chat_create_group, self.a, "T", [self.b, 9999])
        self.set_user(self.c, disabled=1)
        self.fails("invalid_state", db_chats.chat_create_group, self.a, "T", [self.c])
        # evaluation order: unknown user (resolve) before disabled user (state)
        self.fails("not_found", db_chats.chat_create_group, self.a, "T", [self.c, 9999])
        out = self.write(db_chats.chat_create_group, self.a, "Solo", [])
        self.assertEqual(len(out["res"]["chat"]["members"]), 1)

    def test_member_limit(self) -> None:
        many = self.bulk_users(200)
        self.fails("invalid_state", db_chats.chat_create_group, self.a, "Big", many, reason="max_members")
        out = self.write(db_chats.chat_create_group, self.a, "Big", many[:199])
        self.assertEqual(len(out["res"]["chat"]["members"]), 200)
        chat_id = out["res"]["chat"]["id"]
        self.fails("invalid_state", db_chats.chat_add_members, self.a, chat_id, many[199:200] + [self.b])

    def bulk_users(self, count: int) -> List[int]:
        def insert(conn: Any) -> List[int]:
            ids = []
            for i in range(count):
                name = "bulk%03d" % i
                cur = conn.execute(
                    "INSERT INTO users(username, display_name, display_key, pw_hash, created_at, last_login_at)"
                    " VALUES (?, ?, ?, 'x', 1, 1)",
                    (name, name, name),
                )
                ids.append(int(cur.lastrowid))
            return ids

        return self.write(insert)

    def test_update(self) -> None:
        chat_id = self.group(self.a, self.b)
        out = self.write(db_chats.chat_update, self.a, chat_id, "Renamed", "about", True)
        chat = out["res"]["chat"]
        self.assertEqual((chat["title"], chat["description"], chat["only_admins_post"]), ("Renamed", "about", True))
        self.assertEqual(self.types(out), ["ev.chat_update", "ev.message"])
        self.assertEqual(chat["last_message"]["system"]["event"], "renamed")
        self.assertEqual(chat["last_message"]["system"]["title"], "Renamed")
        self.assertEqual(out["index"][0]["only_admins_post"], True)
        # no-op: every field equals the stored value
        same = self.write(db_chats.chat_update, self.a, chat_id, "Renamed", "about", True)
        self.assertTrue(same["noop"])
        self.assertEqual(same["events"], [])
        # description only: no system message
        only = self.write(db_chats.chat_update, self.a, chat_id, None, "new")
        self.assertEqual(self.types(only), ["ev.chat_update"])

    def test_update_errors_and_order(self) -> None:
        chat_id = self.group(self.a, self.b)
        self.fails("bad_request", db_chats.chat_update, self.a, chat_id)
        self.fails("bad_request", db_chats.chat_update, self.a, chat_id, "")
        self.fails("forbidden", db_chats.chat_update, self.b, chat_id, "x")
        self.fails("not_member", db_chats.chat_update, self.d, chat_id, "x")
        self.fails("not_member", db_chats.chat_update, self.a, 9999, "x")
        # the default group can be renamed by its admin
        out = self.write(db_chats.chat_update, self.a, self.everyone, "All hands")
        self.assertEqual(out["res"]["chat"]["title"], "All hands")

    def test_add_members(self) -> None:
        chat_id = self.group(self.a, self.b)
        self.send(self.a, chat_id, "old message")
        out = self.write(db_chats.chat_add_members, self.a, chat_id, [self.b, self.c, self.d])
        self.assertEqual(self.types(out), ["ev.chat_update", "ev.chat_members", "ev.message"])
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.c, self.d})
        members_event = out["events"][1]["groups"][0]
        self.assertEqual(sorted(members_event["user_ids"]), [self.a, self.b])
        self.assertEqual([m["user_id"] for m in members_event["d"]["added"]], [self.c, self.d])
        self.assertEqual(members_event["d"]["removed"], [])
        self.assertEqual(self.audience(out, "ev.message"), {self.a, self.b, self.c, self.d})
        # new members cannot see history but see their own notice
        history = self.read(db_messages.chat_history, self.c, chat_id)["messages"]
        self.assertEqual([m["kind"] for m in history], ["system"])
        self.assertEqual(history[0]["system"]["event"], "added")
        self.assertEqual(self.me(self.c, chat_id)["unread"], 0)
        # all already members: no-op
        again = self.write(db_chats.chat_add_members, self.a, chat_id, [self.b, self.c])
        self.assertTrue(again["noop"])
        self.assertEqual(again["events"], [])

    def test_add_members_order_and_errors(self) -> None:
        chat_id = self.group(self.a, self.b)
        self.fails("bad_request", db_chats.chat_add_members, self.a, chat_id, [])
        self.fails("bad_request", db_chats.chat_add_members, self.a, chat_id, list(range(1, 52)))
        self.fails("not_found", db_chats.chat_add_members, self.a, chat_id, [9999])
        self.fails("forbidden", db_chats.chat_add_members, self.b, chat_id, [self.c])
        self.fails("not_member", db_chats.chat_add_members, self.c, chat_id, [self.d])
        self.set_user(self.d, disabled=1)
        self.fails("invalid_state", db_chats.chat_add_members, self.a, chat_id, [self.d], reason="disabled")
        # default group: invalid_state even for a non-admin; unknown user still wins (resolve first)
        self.fails("invalid_state", db_chats.chat_add_members, self.b, self.everyone, [self.c])
        self.fails("not_found", db_chats.chat_add_members, self.b, self.everyone, [9999])
        self.fails("invalid_state", db_chats.chat_add_members, self.a, self.everyone, [self.c])

    def test_readding_a_removed_member_starts_fresh(self) -> None:
        chat_id = self.group(self.a, self.b)
        self.send(self.a, chat_id, "one")
        self.write(db_chats.chat_prefs, self.b, chat_id, None, True)
        self.write(db_chats.chat_remove_member, self.a, chat_id, self.b)
        self.send(self.a, chat_id, "two")
        self.write(db_chats.chat_add_members, self.a, chat_id, [self.b])
        bodies = [m["body"] for m in self.read(db_messages.chat_history, self.b, chat_id)["messages"]]
        self.assertNotIn("one", bodies)
        self.assertNotIn("two", bodies)
        self.assertFalse(self.me(self.b, chat_id)["pinned"])

    def test_remove_member(self) -> None:
        chat_id = self.group(self.a, self.b, self.c)
        message = self.mid(self.send(self.a, chat_id, "star me"))
        self.write(db_messages.msg_star, self.b, message, True)
        out = self.write(db_chats.chat_remove_member, self.a, chat_id, self.b)
        self.assertEqual(self.types(out), ["ev.chat_removed", "ev.chat_members", "ev.message"])
        self.assertEqual(self.audience(out, "ev.chat_removed"), {self.b})
        members_event = out["events"][1]
        self.assertEqual(sorted(members_event["groups"][0]["user_ids"]), [self.a, self.c])
        self.assertEqual(members_event["groups"][0]["d"]["removed"], [self.b])
        self.assertEqual(self.audience(out, "ev.message"), {self.a, self.c})
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM stars WHERE user_id = ?", (self.b,)), 0)
        self.fails("not_member", db_chats.chat_get, self.b, chat_id, reader=True)
        self.assertNotIn(self.b, out["index"][0]["members"])

    def test_remove_member_order(self) -> None:
        chat_id = self.group(self.a, self.b, self.c)
        self.fails("not_found", db_chats.chat_remove_member, self.a, chat_id, self.d)
        self.fails("invalid_state", db_chats.chat_remove_member, self.a, chat_id, self.a, reason="use_leave")
        # a non-admin removing themselves gets use_leave (applicability precedes the role check)
        self.fails("invalid_state", db_chats.chat_remove_member, self.b, chat_id, self.b, reason="use_leave")
        self.fails("forbidden", db_chats.chat_remove_member, self.b, chat_id, self.c)
        self.fails("invalid_state", db_chats.chat_remove_member, self.a, self.everyone, self.b)
        self.fails("not_member", db_chats.chat_remove_member, self.d, chat_id, self.a)

    def test_set_admin_and_last_admin(self) -> None:
        chat_id = self.group(self.a, self.b)
        out = self.write(db_chats.chat_set_admin, self.a, chat_id, self.b, True)
        self.assertEqual(self.types(out), ["ev.chat_members", "ev.message"])
        self.assertEqual(
            out["events"][0]["groups"][0]["d"]["updated"][0],
            {"user_id": self.b, "role": "admin", "delivered_up_to": 0, "read_up_to": 0},
        )
        self.assertEqual(sorted(out["events"][0]["groups"][0]["user_ids"]), [self.a, self.b])
        self.assertEqual(out["res"]["chat"]["last_message"]["system"]["event"], "promoted")
        self.assertTrue(self.write(db_chats.chat_set_admin, self.a, chat_id, self.b, True)["noop"])
        demoted = self.write(db_chats.chat_set_admin, self.b, chat_id, self.a, False)
        self.assertEqual(demoted["res"]["chat"]["last_message"]["system"]["event"], "demoted")
        # b is now the only admin: cannot demote themselves
        self.fails("invalid_state", db_chats.chat_set_admin, self.b, chat_id, self.b, False, reason="last_admin")

    def test_set_admin_errors(self) -> None:
        chat_id = self.group(self.a, self.b)
        self.fails("not_found", db_chats.chat_set_admin, self.a, chat_id, self.d, True)
        self.fails("forbidden", db_chats.chat_set_admin, self.b, chat_id, self.b, True)
        self.fails("invalid_state", db_chats.chat_set_admin, self.a, self.everyone, self.b, True)
        self.fails("bad_request", db_chats.chat_set_admin, self.a, chat_id, self.b, "yes")

    def test_last_admin_counts_enabled_admins_only(self) -> None:
        chat_id = self.group(self.a, self.b, self.c)
        self.write(db_chats.chat_set_admin, self.a, chat_id, self.b, True)
        self.set_user(self.b, disabled=1)
        # b is a disabled admin: a is the last ENABLED admin and cannot step down
        self.fails("invalid_state", db_chats.chat_set_admin, self.a, chat_id, self.a, False)

    def test_leave_promotes_the_longest_serving_enabled_member(self) -> None:
        chat_id = self.group(self.a, self.b, self.c, self.d)
        self.set_user(self.b, disabled=1)
        out = self.write(db_chats.chat_leave, self.a, chat_id)
        self.assertEqual(out["res"], {})
        self.assertEqual(self.types(out), ["ev.chat_removed", "ev.chat_members", "ev.message", "ev.message"])
        d = out["events"][1]["groups"][0]["d"]
        self.assertEqual(d["removed"], [self.a])
        self.assertEqual([m["user_id"] for m in d["updated"]], [self.c])
        self.assertEqual(
            self.scalar("SELECT role FROM chat_members WHERE chat_id = ? AND user_id = ?", (chat_id, self.c)), "admin"
        )
        events = [
            g["d"]["message"]["system"]["event"]
            for ev in out["events"]
            if ev["t"] == "ev.message"
            for g in ev["groups"]
        ]
        self.assertEqual(events, ["left", "promoted"])
        # every remaining member receives the single ev.chat_members {removed, updated}
        self.assertEqual(sorted(out["events"][1]["groups"][0]["user_ids"]), [self.b, self.c, self.d])
        self.assertEqual(out["promoted"]["user_id"], self.c)

    def test_leave_no_promotion_while_another_enabled_admin_exists(self) -> None:
        chat_id = self.group(self.a, self.b)
        self.write(db_chats.chat_set_admin, self.a, chat_id, self.b, True)
        out = self.write(db_chats.chat_leave, self.a, chat_id)
        self.assertEqual(self.types(out), ["ev.chat_removed", "ev.chat_members", "ev.message"])
        self.assertIsNone(out["promoted"])

    def test_leave_with_only_disabled_admin_left_still_promotes(self) -> None:
        chat_id = self.group(self.a, self.b, self.c)
        self.write(db_chats.chat_set_admin, self.a, chat_id, self.b, True)
        self.set_user(self.b, disabled=1)
        out = self.write(db_chats.chat_leave, self.a, chat_id)
        self.assertEqual(out["promoted"]["user_id"], self.c)

    def test_last_member_leaving_keeps_the_chat_row(self) -> None:
        chat_id = self.group(self.a)
        out = self.write(db_chats.chat_leave, self.a, chat_id)
        self.assertEqual(self.types(out), ["ev.chat_removed"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM chats WHERE id = ?", (chat_id,)), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM chat_members WHERE chat_id = ?", (chat_id,)), 0)
        self.assertEqual(out["index"][0]["members"], {})

    def test_leave_errors(self) -> None:
        self.fails("invalid_state", db_chats.chat_leave, self.b, self.everyone)
        self.fails("not_member", db_chats.chat_leave, self.d, self.group(self.a, self.b))

    def test_get(self) -> None:
        chat_id = self.group(self.a, self.b)
        self.assertEqual(self.read(db_chats.chat_get, self.b, chat_id)["chat"]["id"], chat_id)
        self.fails("not_member", db_chats.chat_get, self.d, chat_id, reader=True)
        self.fails("not_member", db_chats.chat_get, self.a, 12345, reader=True)
        self.fails("bad_request", db_chats.chat_get, self.a, "1", reader=True)


class PrefsClearTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.chats = [self.group(self.a, self.b, title="G%d" % i) for i in range(5)]

    def test_mute_semantics(self) -> None:
        chat_id = self.chats[0]
        out = self.write(db_chats.chat_prefs, self.a, chat_id, 4102444800)
        self.assertEqual(out["res"]["chat"]["me"]["muted_until"], 4102444800)
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.a})
        past = self.write(db_chats.chat_prefs, self.a, chat_id, util.now() - 5)
        self.assertEqual(past["res"]["chat"]["me"]["muted_until"], 0)
        self.assertTrue(self.write(db_chats.chat_prefs, self.a, chat_id, 0)["noop"])
        self.fails("bad_request", db_chats.chat_prefs, self.a, chat_id, -1)
        self.fails("bad_request", db_chats.chat_prefs, self.a, chat_id, 4102444801)
        self.fails("bad_request", db_chats.chat_prefs, self.a, chat_id)

    def test_pin_rules_final_state(self) -> None:
        for chat_id in self.chats[:3]:
            self.write(db_chats.chat_prefs, self.a, chat_id, None, True)
        self.fails("invalid_state", db_chats.chat_prefs, self.a, self.chats[3], None, True, reason="pin_limit")
        again = self.write(db_chats.chat_prefs, self.a, self.chats[0], None, True)
        self.assertTrue(again["noop"])
        self.fails("bad_request", db_chats.chat_prefs, self.a, self.chats[3], None, True, True)
        # archive clears the pin; the archived chat cannot be pinned, unless the same request unarchives it
        arch = self.write(db_chats.chat_prefs, self.a, self.chats[0], None, None, True)
        self.assertEqual((arch["res"]["chat"]["me"]["archived"], arch["res"]["chat"]["me"]["pinned"]), (True, False))
        self.fails("invalid_state", db_chats.chat_prefs, self.a, self.chats[0], None, True)
        back = self.write(db_chats.chat_prefs, self.a, self.chats[0], None, True, False)
        self.assertEqual((back["res"]["chat"]["me"]["archived"], back["res"]["chat"]["me"]["pinned"]), (False, True))
        # the pin limit is per user
        self.write(db_chats.chat_prefs, self.b, self.chats[3], None, True)

    def test_unpin(self) -> None:
        self.write(db_chats.chat_prefs, self.a, self.chats[0], None, True)
        out = self.write(db_chats.chat_prefs, self.a, self.chats[0], None, False)
        self.assertFalse(out["res"]["chat"]["me"]["pinned"])
        self.assertIsNone(out["res"]["chat"]["me"]["pinned_at"])

    def test_clear(self) -> None:
        chat_id = self.chats[0]
        for i in range(3):
            self.send(self.a, chat_id, "m%d" % i)
        before = self.chat_of(self.b, chat_id)
        self.assertEqual(before["me"]["unread"], 3)
        out = self.write(db_chats.chat_clear, self.b, chat_id)
        chat = out["res"]["chat"]
        self.assertIsNone(chat["last_message"])
        self.assertEqual(chat["me"]["unread"], 0)
        self.assertEqual(chat["me"]["cleared_before_id"], chat["last_message_id"])
        self.assertEqual(chat["last_activity_at"], before["last_activity_at"])
        self.assertEqual(self.types(out)[:2], ["ev.chat_update", "ev.read_sync"])
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.b})
        self.assertEqual(out["read_sync"][self.b]["unread"], 0)
        self.assertEqual(self.read(db_messages.chat_history, self.b, chat_id)["messages"], [])
        # delivered advanced (public), read watermark did not
        member = {m["user_id"]: m for m in chat["members"]}[self.b]
        self.assertEqual(member["delivered_up_to"], chat["last_message_id"])
        self.assertEqual(member["read_up_to"], 0)
        self.assertEqual(self.audience(out, "ev.receipt"), {self.b, self.a})
        self.assertTrue(self.write(db_chats.chat_clear, self.b, chat_id)["noop"])
        # the other member is unaffected
        self.assertEqual(len(self.read(db_messages.chat_history, self.a, chat_id)["messages"]), 4)

    def test_clear_empty_chat_is_a_noop(self) -> None:
        chat_id = self.dm(self.a, self.b)
        out = self.write(db_chats.chat_clear, self.a, chat_id)
        self.assertTrue(out["noop"])
        self.assertEqual(out["events"], [])


class ListOrderTest(support.DbChatCase):
    def test_ready_order_pinned_first_then_activity_then_id(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        g1 = self.group(a, b, title="g1")
        g2 = self.group(a, b, title="g2")
        g3 = self.group(a, b, title="g3")
        self.send(b, g1, "newest in g1")
        self.write(db_chats.chat_prefs, a, g3, None, True)
        self.write(db_chats.chat_prefs, a, g2, None, True)
        ids = [c["id"] for c in self.read(db_chats.build_ready, a)["chats"]]
        self.assertEqual(ids[:3], [g2, g3, g1])
        self.assertEqual(ids[3], self.everyone)


class ChatOrderTieTest(support.DbChatCase):
    def test_equal_activity_is_ordered_by_chat_id_descending(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        first, second, third = (self.group(a, b, title="t%d" % i) for i in range(3))
        self.sql("UPDATE chats SET last_activity_at = 5000.0")
        ids = [c["id"] for c in self.read(db_chats.build_ready, a)["chats"]]
        self.assertEqual(ids, [third, second, first, self.everyone])


class MembershipIndexTest(support.DbChatCase):
    def test_index_matches_state(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        c = self.user("carol")
        group = self.group(a, b)
        dm = self.dm(a, c)
        self.set_user(c, disabled=1)
        index = self.read(db_chats.load_membership_index)
        self.assertEqual(index[group]["members"], {a: "admin", b: "member"})
        self.assertEqual(index[self.everyone]["members"], {a: "admin", b: "member", c: "member"})
        self.assertTrue(index[dm]["peer_disabled"])
        self.assertEqual(index[dm]["members"], {a: "member"})
        self.assertFalse(index[group]["peer_disabled"])
        self.assertEqual(self.read(db_chats.index_entry, group), index[group])


if __name__ == "__main__":
    unittest.main()
