"""msg.forward, msg.info, chat.history, msg.search and msg.shared (SPEC 7.4)."""

from __future__ import annotations

import hashlib
import unittest
from typing import Any, List

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db_chats, db_messages, db_receipts


class ForwardTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")
        self.g = self.group(self.a, self.b, self.c, title="source")
        self.h = self.group(self.a, self.b, title="target")
        self.dm_ab = self.dm(self.a, self.b)
        self.m1 = self.mid(self.send(self.b, self.g, "first"))
        self.m2 = self.mid(self.send(self.b, self.g, "second @carol", attachment_id=self.add_attachment(self.b)))

    def count(self) -> int:
        return self.scalar("SELECT COUNT(*) FROM messages")

    def forward(self, user: int, sources: Any, targets: Any, client_id: str = "fwd-00000001") -> Any:
        return self.write(db_messages.msg_forward, user, sources, targets, client_id)

    def test_creation_order_copies_and_events(self) -> None:
        out = self.forward(self.a, [self.m2, self.m1], [self.dm_ab, self.h])
        messages = out["res"]["messages"]
        self.assertEqual(
            [(m["chat_id"], m["body"]) for m in messages],
            [(self.dm_ab, "first"), (self.dm_ab, "second @carol"), (self.h, "first"), (self.h, "second @carol")],
        )
        for m in messages:
            self.assertTrue(m["forwarded"])
            self.assertEqual((m["reply_to"], m["mentions"], m["status"]), (None, [], "sent"))
        self.assertEqual(messages[1]["attachment"]["id"], messages[3]["attachment"]["id"])
        expected = hashlib.sha256(("fwd-00000001|%d|%d" % (self.dm_ab, self.m1)).encode()).hexdigest()[:32]
        self.assertEqual(messages[0]["client_id"], expected)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM attachments"), 1)
        types = self.types(out)
        self.assertEqual(types[:2], ["ev.chat_update", "ev.message"])  # dormant chat: the peer is listed first
        self.assertEqual(types.count("ev.message"), 4)
        self.assertEqual(types.count("ev.chat_update"), 1)
        self.assertEqual(len(out["records"]), 4)
        self.assertEqual([r["message_id"] for r in out["records"]], [m["id"] for m in messages])
        self.assertFalse(out["deduped"])
        self.assertEqual(out["index"][0]["members"], {self.a: "member", self.b: "member"})
        self.assertEqual(
            self.scalar("SELECT listed FROM chat_members WHERE chat_id = ? AND user_id = ?", (self.dm_ab, self.b)), 1
        )

    def test_retry_returns_existing_copies_even_after_the_source_was_deleted(self) -> None:
        first = self.forward(self.a, [self.m1, self.m2], [self.h])
        before = self.count()
        self.write(db_messages.msg_delete, self.b, self.m1, "everyone")
        retry = self.forward(self.a, [self.m2, self.m1], [self.h])
        self.assertTrue(retry["deduped"])
        self.assertEqual(retry["events"], [])
        self.assertEqual([m["id"] for m in retry["res"]["messages"]], [m["id"] for m in first["res"]["messages"]])
        self.assertEqual(self.count(), before)

    def test_retry_with_missing_copies_conflicts(self) -> None:
        self.forward(self.a, [self.m1, self.m2], [self.h])
        victim = self.sql("SELECT id FROM messages WHERE forwarded = 1 ORDER BY id DESC LIMIT 1")[0][0]
        self.sql("DELETE FROM messages WHERE id = ?", (victim,))
        self.fails("conflict", db_messages.msg_forward, self.a, [self.m1, self.m2], [self.h], "fwd-00000001")

    def refused(self, code: str, sources: Any, targets: Any) -> Any:
        """A forward that must fail with ``code`` and create nothing (all-or-nothing)."""
        before = self.count()
        err = self.fails(code, db_messages.msg_forward, self.a, sources, targets, "fwd-00000002")
        self.assertEqual(self.count(), before)
        return err

    def test_all_or_nothing_and_culprits(self) -> None:
        other = self.group(self.c, self.b, title="not mine")
        self.assertEqual(self.refused("not_member", [self.m1], [self.h, other]).chat_id, other)
        self.assertEqual(self.refused("not_found", [self.m1, 99999], [self.h]).message_id, 99999)
        hidden = self.mid(self.send(self.b, self.g, "will be hidden"))
        self.write(db_messages.msg_delete, self.a, hidden, "me")
        self.refused("not_found", [hidden], [self.h])
        elsewhere = self.mid(self.send(self.c, other, "in a chat a is not in"))
        self.refused("not_found", [elsewhere], [self.h])
        system = self.scalar("SELECT id FROM messages WHERE chat_id = ? AND kind = 'system'", (self.g,))
        self.assertEqual(self.refused("invalid_state", [self.m1, system], [self.h]).message_id, system)
        gone = self.mid(self.send(self.b, self.g, "deleted"))
        self.write(db_messages.msg_delete, self.b, gone, "everyone")
        self.assertEqual(self.refused("invalid_state", [gone], [self.h]).message_id, gone)

    def test_target_permissions(self) -> None:
        self.write(db_chats.chat_update, self.a, self.h, None, None, True)
        err = self.fails("forbidden", db_messages.msg_forward, self.b, [self.m1], [self.h], "fwd-00000003")
        self.assertEqual(err.chat_id, self.h)
        self.forward(self.a, [self.m1], [self.h], "fwd-00000004")  # a group admin may post
        direct = self.dm(self.b, self.c)
        self.send(self.b, direct, "open the chat")
        self.set_user(self.c, disabled=1)
        err = self.fails("invalid_state", db_messages.msg_forward, self.b, [self.m1], [direct], "fwd-00000005")
        self.assertEqual(err.chat_id, direct)
        # forwarding FROM an only_admins_post chat stays allowed for every member
        self.write(db_chats.chat_update, self.a, self.g, None, None, True)
        self.forward(self.b, [self.m1], [self.everyone], "fwd-00000006")

    def test_evaluation_order(self) -> None:
        other = self.group(self.c, self.b, title="not mine")
        # (5) a target the caller is not in beats (6) an unknown source
        self.fails("not_member", db_messages.msg_forward, self.a, [99999], [other], "fwd-00000007")
        # (6) unknown source beats (7) deleted source
        gone = self.mid(self.send(self.b, self.g, "deleted"))
        self.write(db_messages.msg_delete, self.b, gone, "everyone")
        self.fails("not_found", db_messages.msg_forward, self.a, [gone, 99999], [self.h], "fwd-00000007")
        # (7) invalid_state of a source beats (8) forbidden of a target
        self.write(db_chats.chat_update, self.a, self.h, None, None, True)
        self.fails("invalid_state", db_messages.msg_forward, self.b, [gone], [self.h], "fwd-00000007")
        # (1) shape beats everything
        self.fails("bad_request", db_messages.msg_forward, self.b, [], [other], "fwd-00000007")

    def test_shape_limits_and_deduplication(self) -> None:
        fwd = db_messages.msg_forward
        self.fails("bad_request", fwd, self.a, [], [self.h], "fwd-00000008")
        self.fails("bad_request", fwd, self.a, [self.m1], [], "fwd-00000008")
        self.fails("bad_request", fwd, self.a, list(range(1, 22)), [self.h], "fwd-00000008")
        self.fails("bad_request", fwd, self.a, [self.m1], list(range(1, 7)), "fwd-00000008")
        self.fails("bad_request", fwd, self.a, [self.m1], [self.h], "short")
        self.fails("bad_request", fwd, self.a, [self.m1, "2"], [self.h], "fwd-00000008")
        self.fails("bad_request", fwd, self.a, "1", [self.h], "fwd-00000008")
        out = self.forward(self.a, [self.m1, self.m1, self.m1], [self.h, self.h], "fwd-00000009")
        self.assertEqual(len(out["res"]["messages"]), 1)
        # 21 entries that collapse to 20 distinct ids pass the shape check
        self.fails("not_found", fwd, self.a, [self.m1] * 3 + list(range(1000, 1019)), [self.h], "fwd-00000010")

    def test_forwarding_into_a_chat_with_unread_messages_never_marks_them_read(self) -> None:
        for i in range(5):
            self.send(self.b, self.h, "unread %d" % i)
        before = self.me(self.a, self.h)
        out = self.forward(self.a, [self.m1], [self.h], "fwd-00000011")
        after = self.me(self.a, self.h)
        self.assertEqual((before["unread"], after["unread"]), (5, 5))
        self.assertEqual(before["last_read_id"], after["last_read_id"])
        self.assertEqual(out["read_sync"], {})
        member = {m["user_id"]: m for m in self.chat_of(self.a, self.h)["members"]}[self.a]
        self.assertEqual(member["read_up_to"], 0)

    def test_forwarded_copies_can_be_forwarded_again_and_carry_no_mentions(self) -> None:
        copy = self.forward(self.a, [self.m2], [self.h], "fwd-00000012")["res"]["messages"][0]
        again = self.write(db_messages.msg_forward, self.b, [copy["id"]], [self.g], "fwd-00000013")
        self.assertTrue(again["res"]["messages"][0]["forwarded"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM message_mentions WHERE message_id = ?", (copy["id"],)), 0)

    def test_late_joiner_cannot_forward_old_messages(self) -> None:
        d = self.user("dave")
        self.write(db_chats.chat_add_members, self.a, self.g, [d])
        self.fails("not_found", db_messages.msg_forward, d, [self.m1], [self.everyone], "fwd-00000014")


class InfoTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")
        self.g = self.group(self.a, self.b, self.c)

    def test_recipients_and_timestamps(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "info"))
        self.write(db_receipts.receipt_delivered, self.b, [{"chat_id": self.g, "up_to_id": mid}])
        self.write(db_receipts.receipt_read, self.c, self.g, mid)
        late = self.user("dave")
        self.write(db_chats.chat_add_members, self.a, self.g, [late])
        self.set_user(self.c, disabled=1)
        info = self.read(db_messages.msg_info, self.a, mid)
        self.assertEqual(info["message"]["id"], mid)
        by_user = {r["user_id"]: r for r in info["recipients"]}
        self.assertEqual(set(by_user), {self.b, self.c})  # sender and the late joiner are excluded; disabled stays
        self.assertEqual((by_user[self.b]["delivered"], by_user[self.b]["read"]), (True, False))
        self.assertIsNotNone(by_user[self.b]["delivered_at"])
        self.assertIsNone(by_user[self.b]["read_at"])
        self.assertEqual((by_user[self.c]["delivered"], by_user[self.c]["read"]), (True, True))
        self.assertGreaterEqual(by_user[self.c]["read_at"], by_user[self.c]["delivered_at"])

    def test_errors_in_order(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "mine"))
        system = self.scalar("SELECT id FROM messages WHERE chat_id = ? AND kind = 'system'", (self.g,))
        self.fails("forbidden", db_messages.msg_info, self.b, mid, reader=True)
        self.fails("invalid_state", db_messages.msg_info, self.a, system, reader=True)
        self.fails("invalid_state", db_messages.msg_info, self.b, system, reader=True)
        self.fails("not_found", db_messages.msg_info, self.a, 99999, reader=True)
        self.write(db_messages.msg_delete, self.a, mid, "everyone")
        self.fails("invalid_state", db_messages.msg_info, self.a, mid, reader=True)

    def test_large_chats_have_watermarks_but_no_timestamps(self) -> None:
        members: List[int] = []
        for i in range(50):
            members.append(self.user("big%02d" % i))
        big = self.group(self.a, *members)  # 51 members
        mid = self.mid(self.send(self.a, big, "to many"))
        self.write(db_receipts.receipt_read, members[0], big, mid)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM receipts WHERE message_id = ?", (mid,)), 0)
        info = self.read(db_messages.msg_info, self.a, mid)
        first = {r["user_id"]: r for r in info["recipients"]}[members[0]]
        self.assertEqual(
            (first["delivered"], first["read"], first["delivered_at"], first["read_at"]), (True, True, None, None)
        )


class HistoryTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.g = self.group(self.a, self.b)
        self.ids = [self.scalar("SELECT id FROM messages WHERE chat_id = ?", (self.g,))]
        for i in range(10):
            self.ids.append(self.mid(self.send(self.a, self.g, "m%d" % i)))

    def page(self, user: int, **kwargs: Any) -> Any:
        return self.read(
            db_messages.chat_history,
            user,
            self.g,
            kwargs.get("before_id"),
            kwargs.get("after_id"),
            kwargs.get("around_id"),
            kwargs.get("limit"),
        )

    @staticmethod
    def ids_of(page: Any) -> List[int]:
        return [m["id"] for m in page["messages"]]

    def test_no_cursor_newest_page(self) -> None:
        page = self.page(self.b, limit=3)
        self.assertEqual(self.ids_of(page), self.ids[-3:])
        self.assertEqual((page["has_more_before"], page["has_more_after"]), (True, False))
        everything = self.page(self.b)
        self.assertEqual(self.ids_of(everything), self.ids)
        self.assertEqual((everything["has_more_before"], everything["has_more_after"]), (False, False))

    def test_before_after_around(self) -> None:
        ids = self.ids
        before = self.page(self.b, before_id=ids[5], limit=2)
        self.assertEqual(
            (self.ids_of(before), before["has_more_before"], before["has_more_after"]), (ids[3:5], True, True)
        )
        after = self.page(self.b, after_id=ids[5], limit=2)
        self.assertEqual(
            (self.ids_of(after), after["has_more_before"], after["has_more_after"]), (ids[6:8], True, True)
        )
        start = self.page(self.b, after_id=0, limit=3)
        self.assertEqual(
            (self.ids_of(start), start["has_more_before"], start["has_more_after"]), (ids[:3], False, True)
        )
        around = self.page(self.b, around_id=ids[5], limit=4)
        self.assertEqual(
            (self.ids_of(around), around["has_more_before"], around["has_more_after"]), (ids[3:7], True, True)
        )
        wide = self.page(self.b, around_id=ids[5], limit=100)
        self.assertEqual((self.ids_of(wide), wide["has_more_before"], wide["has_more_after"]), (ids, False, False))
        near_start = self.page(self.b, around_id=ids[1], limit=6)
        self.assertEqual(self.ids_of(near_start), ids[0:4])  # no compensation when one side is short
        last = self.page(self.b, after_id=ids[-1])
        self.assertEqual((self.ids_of(last), last["has_more_before"], last["has_more_after"]), ([], True, False))

    def test_limit_clamping_and_validation(self) -> None:
        self.assertEqual(len(self.page(self.b, limit=0)["messages"]), 1)
        self.assertEqual(len(self.page(self.b, limit=-5)["messages"]), 1)
        self.assertEqual(len(self.page(self.b, limit=10**6)["messages"]), 11)
        for kwargs in (
            {"limit": "5"},
            {"limit": True},
            {"limit": 1.5},
            {"before_id": 0},
            {"around_id": 0},
            {"after_id": -1},
            {"before_id": 3, "after_id": 2},
            {"around_id": 3, "before_id": 4},
        ):
            self.fails(
                "bad_request",
                db_messages.chat_history,
                self.b,
                self.g,
                kwargs.get("before_id"),
                kwargs.get("after_id"),
                kwargs.get("around_id"),
                kwargs.get("limit"),
                reader=True,
            )

    def test_hidden_messages_never_create_phantom_pages(self) -> None:
        ids = self.ids
        for victim in ids[8:]:
            self.write(db_messages.msg_delete, self.b, victim, "me")
        page = self.page(self.b, after_id=ids[5], limit=3)
        self.assertEqual((self.ids_of(page), page["has_more_after"]), (ids[6:8], False))
        around = self.page(self.b, around_id=ids[7], limit=6)
        self.assertEqual(around["has_more_after"], False)
        self.fails("not_found", db_messages.chat_history, self.b, self.g, None, None, ids[8], None, reader=True)
        self.assertEqual(self.ids_of(self.page(self.a, limit=2)), ids[-2:])

    def test_visibility_window_of_a_late_joiner_and_after_clear(self) -> None:
        c = self.user("carol")
        self.write(db_chats.chat_add_members, self.a, self.g, [c])
        new = self.mid(self.send(self.a, self.g, "after c joined"))
        seen = self.ids_of(self.page(c))
        self.assertEqual(len(seen), 2)  # the 'added' notice and the new message
        self.assertEqual(seen[-1], new)
        self.fails("not_found", db_messages.chat_history, c, self.g, None, None, self.ids[3], None, reader=True)
        self.write(db_chats.chat_clear, self.b, self.g)
        self.assertEqual(self.page(self.b)["messages"][-2:], self.page(self.b)["messages"][-2:])
        self.assertEqual(self.ids_of(self.page(self.b)), [])
        self.send(self.a, self.g, "after the clear")
        self.assertEqual(len(self.page(self.b)["messages"]), 1)

    def test_not_member_and_disabled(self) -> None:
        outsider = self.add_user("outsider")
        self.fails("not_member", db_messages.chat_history, outsider, self.g, reader=True)
        self.set_user(self.b, disabled=1)
        self.fails("unauthorized", db_messages.chat_history, self.b, self.g, reader=True)


class SearchTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.g = self.group(self.a, self.b)

    def search(self, user: int, q: str, *args: Any) -> Any:
        return self.read(db_messages.msg_search, user, q, *args)

    def bodies(self, result: Any) -> List[str]:
        return [r["message"]["body"] for r in result["results"]]

    def test_case_folding_and_ordering(self) -> None:
        self.send(self.a, self.g, "Hello World")
        self.send(self.a, self.g, "Gruesse aus der Straße")
        self.send(self.a, self.g, "say HELLO again")
        self.assertEqual(self.bodies(self.search(self.b, "hello")), ["say HELLO again", "Hello World"])
        self.assertEqual(self.bodies(self.search(self.b, "STRASSE")), ["Gruesse aus der Straße"])
        self.assertEqual(self.bodies(self.search(self.b, "  hello  ")), ["say HELLO again", "Hello World"])
        result = self.search(self.b, "hello")["results"][0]
        self.assertEqual(result["chat_id"], self.g)

    def test_like_metacharacters_are_escaped(self) -> None:
        for body in ("100% sure", "1000 sure", "a_b test", "axb test", "back\\slash", "backXslash"):
            self.send(self.a, self.g, body)
        self.assertEqual(self.bodies(self.search(self.b, "100%")), ["100% sure"])
        self.assertEqual(self.bodies(self.search(self.b, "a_b")), ["a_b test"])
        self.assertEqual(self.bodies(self.search(self.b, "k\\s")), ["back\\slash"])
        self.assertEqual(self.bodies(self.search(self.b, "%%")), [])

    def test_paging_total_and_limits(self) -> None:
        ids = [self.mid(self.send(self.a, self.g, "needle %d" % i)) for i in range(7)]
        self.send(self.a, self.g, "other")
        first = self.search(self.b, "needle", self.g, None, 3)
        self.assertEqual([r["message"]["id"] for r in first["results"]], ids[::-1][:3])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["total"], 7)
        second = self.search(self.b, "needle", self.g, ids[4], 3)
        self.assertEqual([r["message"]["id"] for r in second["results"]], ids[1:4][::-1])
        self.assertNotIn("total", second)
        self.assertNotIn("total", self.search(self.b, "needle", None, None, 3))
        self.assertEqual(len(self.search(self.b, "needle", None, None, 0)["results"]), 1)
        self.assertEqual(len(self.search(self.b, "needle", None, None, 10**6)["results"]), 7)

    def test_validation_and_membership(self) -> None:
        for q in ("a", " a ", "x" * 65, "", 5):
            self.fails("bad_request", db_messages.msg_search, self.b, q, reader=True)
        self.search(self.b, "x" * 64)
        self.fails("bad_request", db_messages.msg_search, self.b, "ab", None, None, "5", reader=True)
        outsider = self.add_user("outsider")
        self.fails("not_member", db_messages.msg_search, outsider, "ab", self.g, reader=True)
        self.fails("not_member", db_messages.msg_search, self.b, "ab", 99999, reader=True)

    def test_only_visible_non_deleted_non_system_messages_are_found(self) -> None:
        old = self.mid(self.send(self.a, self.g, "secret old"))
        gone = self.mid(self.send(self.a, self.g, "secret deleted"))
        self.write(db_messages.msg_delete, self.a, gone, "everyone")
        mine = self.mid(self.send(self.a, self.g, "secret hidden by bob"))
        self.write(db_messages.msg_delete, self.b, mine, "me")
        late = self.user("carol")
        self.write(db_chats.chat_add_members, self.a, self.g, [late])
        self.send(self.a, self.g, "secret new")
        self.assertEqual(self.bodies(self.search(late, "secret")), ["secret new"])
        self.assertEqual(self.bodies(self.search(self.b, "secret")), ["secret new", "secret old"])
        self.assertEqual(self.search(self.b, "welcome")["results"], [])
        self.assertEqual(self.search(self.b, "created group")["results"], [])
        self.write(db_chats.chat_clear, self.b, self.g)
        self.assertEqual(self.search(self.b, "secret")["results"], [])
        self.assertIsNotNone(old)

    def test_searches_across_chats_of_the_member_only(self) -> None:
        other = self.group(self.b, self.user("carol"), title="no alice")
        self.send(self.b, other, "needle in other")
        self.send(self.a, self.g, "needle here")
        self.assertEqual(self.bodies(self.search(self.b, "needle")), ["needle here", "needle in other"][::-1][::-1])
        self.assertEqual(self.bodies(self.search(self.a, "needle")), ["needle here"])

    def test_interrupted_query_answers_server_busy(self) -> None:
        self.send(self.a, self.g, "needle")
        self.reader.execute("BEGIN")
        self.addCleanup(self.reader.execute, "COMMIT")
        state = {"like": False}
        self.reader.set_trace_callback(lambda statement: state.update(like="LIKE" in statement))
        self.reader.set_progress_handler(lambda: 1 if state["like"] else 0, 1)
        self.addCleanup(self.reader.set_progress_handler, None, 0)
        self.addCleanup(self.reader.set_trace_callback, None)
        with self.assertRaises(db_chats.ChatError) as caught:
            db_messages.msg_search(self.reader, self.b, "needle")
        self.assertEqual((caught.exception.code, caught.exception.retry_after), ("server_busy", 2.0))


class SharedTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.g = self.group(self.a, self.b)

    def test_kinds(self) -> None:
        image = self.mid(self.send(self.a, self.g, "pic", attachment_id=self.add_attachment(self.a, "image")))
        video = self.mid(self.send(self.a, self.g, "", attachment_id=self.add_attachment(self.a, "video")))
        doc = self.mid(self.send(self.a, self.g, "doc", attachment_id=self.add_attachment(self.a, "file")))
        audio = self.mid(self.send(self.a, self.g, "", attachment_id=self.add_attachment(self.a, "audio")))
        link = self.mid(self.send(self.a, self.g, "see HTTPS://example.org/x"))
        plain = self.mid(self.send(self.a, self.g, "no link here"))
        gone = self.mid(self.send(self.a, self.g, "http://deleted.example"))
        self.write(db_messages.msg_delete, self.a, gone, "everyone")

        def ids(kind: str, *args: Any) -> List[int]:
            return [m["id"] for m in self.read(db_messages.msg_shared, self.b, self.g, kind, *args)["messages"]]

        self.assertEqual(ids("media"), [video, image])
        self.assertEqual(ids("files"), [audio, doc])
        self.assertEqual(ids("links"), [link])
        self.assertNotIn(plain, ids("links"))
        page = self.read(db_messages.msg_shared, self.b, self.g, "media", None, 1)
        self.assertEqual(([m["id"] for m in page["messages"]], page["has_more"]), ([video], True))
        older = self.read(db_messages.msg_shared, self.b, self.g, "media", video, 1)
        self.assertEqual(([m["id"] for m in older["messages"]], older["has_more"]), ([image], False))

    def test_errors(self) -> None:
        self.fails("bad_request", db_messages.msg_shared, self.b, self.g, "docs", reader=True)
        outsider = self.add_user("outsider")
        self.fails("not_member", db_messages.msg_shared, outsider, self.g, "media", reader=True)


class ChatBuilderTest(support.DbChatCase):
    def test_build_chats_matches_per_chat_builds(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        g = self.group(a, b)
        self.send(a, g, "hello")
        self.send(b, g, "reply")
        built = self.read(
            db_chats.build_chats, [(a, g), (b, g), (a, self.everyone), (b, 99999), (self.add_user("zed"), g)]
        )
        self.assertEqual(set(built), {(a, g), (b, g), (a, self.everyone)})
        self.assertEqual(built[(a, g)], self.read(db_chats.build_chat, g, a))
        self.assertIs(built[(a, g)]["members"], built[(a, g)]["members"])
        self.assertEqual(built[(a, g)]["last_message"]["body"], "reply")
        self.assertEqual(built[(b, g)]["last_message"]["status"], "sent")
        self.assertIsNone(built[(a, g)]["last_message"]["status"])
        self.assertIsNone(self.read(db_chats.build_chat, g, 99999))


if __name__ == "__main__":
    unittest.main()
