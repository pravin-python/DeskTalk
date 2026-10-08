"""Watermarks, receipts rows, routing, counters (1000 cap, as-of rule) and status aggregation (SPEC 3.3, 8.1, 8.2)."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db_chats, db_messages, db_receipts


def member(chat: Dict[str, Any], user: int) -> Dict[str, Any]:
    return {m["user_id"]: m for m in chat["members"]}[user]


class ReceiptBase(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")
        self.g = self.group(self.a, self.b, self.c)

    def delivered(self, user: int, up_to: int, chat_id: int = 0) -> Any:
        return self.write(db_receipts.receipt_delivered, user, [{"chat_id": chat_id or self.g, "up_to_id": up_to}])

    def read_up_to(self, user: int, up_to: int, chat_id: int = 0) -> Any:
        return self.write(db_receipts.receipt_read, user, chat_id or self.g, up_to)

    def watermarks(self, user: int, chat_id: int = 0) -> Dict[str, int]:
        row = self.sql(
            "SELECT delivered_id, read_receipt_id, last_read_id FROM chat_members WHERE chat_id = ? AND user_id = ?",
            (chat_id or self.g, user),
        )[0]
        return {"delivered": row[0], "read": row[1], "last_read": row[2]}


class DeliveredTest(ReceiptBase):
    def test_delivered_advances_clamps_and_never_moves_back(self) -> None:
        m1 = self.mid(self.send(self.a, self.g, "one"))
        m2 = self.mid(self.send(self.a, self.g, "two"))
        out = self.delivered(self.b, m1)
        self.assertEqual(self.watermarks(self.b)["delivered"], m1)
        payload = self.payloads(out, "ev.receipt", self.a)[0]
        self.assertEqual(payload, {"chat_id": self.g, "user_id": self.b, "delivered_up_to": m1, "read_up_to": 0})
        event = out["events"][0]
        self.assertEqual(
            (event["t"], event["durable"], event["key"]), ("ev.receipt", False, ("receipt", self.g, self.b))
        )
        self.assertEqual(out["receipt"]["audience"], [self.b, self.a])
        self.assertEqual(out["res"], {})
        self.delivered(self.b, 10**9)
        self.assertEqual(self.watermarks(self.b)["delivered"], m2)  # clamped to chats.last_message_id
        back = self.delivered(self.b, m1)
        self.assertTrue(back["noop"])
        self.assertEqual(back["events"], [])
        self.assertEqual(self.watermarks(self.b)["delivered"], m2)

    def test_batched_items_are_one_transaction(self) -> None:
        other = self.group(self.a, self.b, title="other")
        m1 = self.mid(self.send(self.a, self.g, "one"))
        m2 = self.mid(self.send(self.a, other, "two"))
        out = self.write(
            db_receipts.receipt_delivered,
            self.b,
            [{"chat_id": self.g, "up_to_id": m1}, {"chat_id": other, "up_to_id": m2}],
        )
        self.assertEqual(len(out["receipts"]), 2)
        self.assertIsNone(out["receipt"])
        outsider_chat = self.group(self.c, self.a, title="b is not in")
        before = self.watermarks(self.b)
        err = self.fails(
            "not_member",
            db_receipts.receipt_delivered,
            self.b,
            [{"chat_id": self.g, "up_to_id": m1 + 5}, {"chat_id": outsider_chat, "up_to_id": 1}],
        )
        self.assertEqual(err.chat_id, outsider_chat)
        self.assertEqual(self.watermarks(self.b), before)

    def test_items_validation(self) -> None:
        for items in (
            [],
            "x",
            None,
            [{"chat_id": 0, "up_to_id": 1}],
            [{"chat_id": self.g, "up_to_id": -1}],
            [{"chat_id": self.g}],
            [{"chat_id": self.g, "up_to_id": True}],
            [5],
            [{"chat_id": self.g, "up_to_id": 1}] * 51,
        ):
            self.fails("bad_request", db_receipts.receipt_delivered, self.b, items)
        self.write(db_receipts.receipt_delivered, self.b, [{"chat_id": self.g, "up_to_id": 0}] * 50)

    def test_audience_is_the_actor_plus_current_authors_of_the_advanced_range(self) -> None:
        m1 = self.mid(self.send(self.a, self.g, "by a"))
        m2 = self.mid(self.send(self.b, self.g, "by b"))
        out = self.delivered(self.c, m2)
        self.assertEqual(out["receipt"]["audience"], [self.c, self.a, self.b])
        m3 = self.mid(self.send(self.a, self.g, "again by a"))
        out = self.delivered(self.c, m3)
        self.assertEqual(out["receipt"]["audience"], [self.c, self.a])
        # an author who has left gets nothing
        m4 = self.mid(self.send(self.b, self.g, "by b, who then leaves"))
        self.write(db_chats.chat_leave, self.b, self.g)
        out = self.delivered(self.c, m4 + 100)
        self.assertEqual(out["receipt"]["audience"], [self.c])
        self.assertIsNotNone(m1)

    def test_rows_only_for_other_peoples_messages_after_the_join_point(self) -> None:
        m1 = self.mid(self.send(self.a, self.g, "one"))
        m2 = self.mid(self.send(self.b, self.g, "mine"))
        self.delivered(self.b, m2)
        rows = self.sql("SELECT message_id FROM receipts WHERE user_id = ? ORDER BY message_id", (self.b,))
        self.assertEqual(rows, [(m1,)])  # not the system message, not b's own message
        late = self.user("dave")
        self.write(db_chats.chat_add_members, self.a, self.g, [late])
        m3 = self.mid(self.send(self.a, self.g, "after dave joined"))
        self.delivered(late, m3)
        self.assertEqual(self.sql("SELECT message_id FROM receipts WHERE user_id = ?", (late,)), [(m3,)])

    def test_system_messages_get_no_receipts_and_nothing_to_deliver_is_a_noop(self) -> None:
        out = self.delivered(self.b, 100)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM receipts"), [(0,)])
        self.assertFalse(out["noop"])  # the 'created' notice still advances delivered_id
        again = self.delivered(self.b, 100)
        self.assertTrue(again["noop"])


class ReadTest(ReceiptBase):
    def test_read_advances_everything_and_answers_counters(self) -> None:
        ids = [self.mid(self.send(self.a, self.g, "m%d" % i)) for i in range(3)]
        out = self.read_up_to(self.b, ids[1])
        self.assertEqual(
            out["res"],
            {
                "chat_id": self.g,
                "last_read_id": ids[1],
                "last_message_id": ids[2],
                "unread": 1,
                "unread_mentions": 0,
                "first_unread_mention_id": None,
            },
        )
        self.assertEqual(self.watermarks(self.b), {"delivered": ids[1], "read": ids[1], "last_read": ids[1]})
        self.assertEqual(self.types(out), ["ev.receipt", "ev.read_sync"])
        self.assertEqual(self.audience(out, "ev.read_sync"), {self.b})
        self.assertEqual(out["read_sync"][self.b], out["res"])
        self.assertEqual(self.payloads(out, "ev.receipt", self.a)[0]["read_up_to"], ids[1])
        rows = self.sql(
            "SELECT message_id, read_at IS NOT NULL FROM receipts WHERE user_id = ? ORDER BY message_id", (self.b,)
        )
        self.assertEqual(rows, [(ids[0], 1), (ids[1], 1)])

    def test_read_is_clamped_and_unchanged_values_emit_nothing(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "only"))
        out = self.read_up_to(self.b, mid + 1000)
        self.assertEqual(out["res"]["last_read_id"], mid)
        again = self.read_up_to(self.b, mid)
        self.assertEqual((again["events"], again["noop"]), ([], True))
        self.assertEqual(again["res"]["unread"], 0)
        for bad in (-1, "5", None, 1.5, True):
            self.fails("bad_request", db_receipts.receipt_read, self.b, self.g, bad)
        self.fails("bad_request", db_receipts.receipt_read, self.b, 0, 1)
        self.fails("not_member", db_receipts.receipt_read, self.b, 99999, 1)
        self.set_user(self.b, disabled=1)
        self.fails("unauthorized", db_receipts.receipt_read, self.b, self.g, 1)

    def test_receipts_off_freezes_the_public_read_mark_only(self) -> None:
        self.set_user(self.b, read_receipts=0)
        mid = self.mid(self.send(self.a, self.g, "private"))
        out = self.read_up_to(self.b, mid)
        self.assertEqual(self.watermarks(self.b), {"delivered": mid, "read": 0, "last_read": mid})
        self.assertEqual(self.types(out), ["ev.receipt", "ev.read_sync"])  # delivered still advanced
        self.assertEqual(out["receipt"]["read_up_to"], 0)
        self.assertEqual(self.sql("SELECT read_at FROM receipts WHERE user_id = ?", (self.b,))[0][0], None)
        # switching receipts back on publishes nothing by itself; the next read reveals the whole range
        self.set_user(self.b, read_receipts=1)
        revealed = self.read_up_to(self.b, mid)
        self.assertEqual(self.types(revealed), ["ev.receipt"])  # last_read_id did not advance: no read_sync
        self.assertEqual(self.watermarks(self.b)["read"], mid)
        self.assertIsNotNone(self.sql("SELECT read_at FROM receipts WHERE user_id = ?", (self.b,))[0][0])

    def test_invariants_after_mixed_updates(self) -> None:
        ids = [self.mid(self.send(self.a, self.g, "m%d" % i)) for i in range(4)]
        self.delivered(self.b, ids[3])
        self.read_up_to(self.b, ids[1])
        self.write(db_chats.chat_clear, self.b, self.g)
        marks = self.watermarks(self.b)
        self.assertGreaterEqual(marks["delivered"], marks["read"])
        self.assertGreaterEqual(marks["last_read"], marks["read"])
        self.assertEqual(marks["read"], ids[1])

    def test_large_chats_write_no_receipt_rows(self) -> None:
        many = [self.user("big%02d" % i) for i in range(50)]
        big = self.group(self.a, *many)
        mid = self.mid(self.send(self.a, big, "to many"))
        self.read_up_to(many[0], mid, big)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM receipts"), 0)
        self.assertEqual(self.watermarks(many[0], big)["read"], mid)


class CounterTest(ReceiptBase):
    def test_what_counts_as_unread(self) -> None:
        theirs = [self.mid(self.send(self.b, self.g, "b%d" % i)) for i in range(5)]
        self.send(self.a, self.g, "mine does not count")
        self.write(db_messages.msg_delete, self.b, theirs[0], "everyone")
        self.write(db_messages.msg_delete, self.a, theirs[1], "me")
        counters = self.me(self.a, self.g)
        self.assertEqual(counters["unread"], 3)
        self.assertEqual(self.me(self.c, self.g)["unread"], 5 - 1 + 1)  # b's 4 live messages + a's message
        late = self.user("dave")
        self.write(db_chats.chat_add_members, self.a, self.g, [late])
        self.assertEqual(self.me(late, self.g)["unread"], 0)  # history before the join never counts

    def test_cap_of_1000_and_configurable_cap(self) -> None:
        def seed(conn: Any) -> None:
            base = conn.execute("SELECT last_message_id FROM chats WHERE id = ?", (self.g,)).fetchone()[0]
            conn.executemany(
                "INSERT INTO messages(chat_id, sender_id, kind, body, created_at) VALUES (?, ?, 'text', 'x', 1.0)",
                [(self.g, self.b)] * 1001,
            )
            last = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
            conn.execute("UPDATE chats SET last_message_id = ? WHERE id = ?", (last, self.g))
            assert last > base

        self.write(seed)
        self.assertEqual(self.me(self.a, self.g)["unread"], 1000)
        db_chats.configure_limits(unread_cap=5)
        self.assertEqual(self.me(self.a, self.g)["unread"], 5)
        self.assertEqual(self.read(db_receipts.counters, self.a, self.g)["unread"], 5)
        db_chats.configure_limits(unread_cap=1000)

    def test_mentions_counters_and_first_unread_mention(self) -> None:
        self.send(self.b, self.g, "plain")
        first = self.mid(self.send(self.b, self.g, "@alice one"))
        second = self.mid(self.send(self.c, self.g, "@alice two"))
        counters = self.read(db_receipts.counters, self.a, self.g)
        self.assertEqual(
            (counters["unread"], counters["unread_mentions"], counters["first_unread_mention_id"]), (3, 2, first)
        )
        self.write(db_messages.msg_delete, self.b, first, "everyone")
        counters = self.read(db_receipts.counters, self.a, self.g)
        self.assertEqual((counters["unread_mentions"], counters["first_unread_mention_id"]), (1, second))
        self.write(db_messages.msg_delete, self.a, second, "me")
        self.assertIsNone(self.read(db_receipts.counters, self.a, self.g)["first_unread_mention_id"])

    def test_counters_are_as_of_last_message_id(self) -> None:
        mid = self.mid(self.send(self.b, self.g, "one"))
        counters = self.read(db_receipts.counters, self.a, self.g)
        self.assertEqual(counters["last_message_id"], mid)
        chat = self.chat_of(self.a, self.g)
        self.assertEqual(chat["last_message_id"], counters["last_message_id"])
        self.assertEqual(
            {k: chat["me"][k] for k in ("unread", "unread_mentions", "first_unread_mention_id", "last_read_id")},
            {k: counters[k] for k in ("unread", "unread_mentions", "first_unread_mention_id", "last_read_id")},
        )

    def test_counters_for_viewer_is_set_based_and_skips_unlisted(self) -> None:
        dormant = self.dm(self.a, self.b)
        all_counters = self.read(db_receipts.counters_for_viewer, self.b)
        self.assertEqual(set(all_counters), {self.everyone, self.g})
        self.assertNotIn(dormant, all_counters)
        self.assertEqual(set(self.read(db_receipts.counters_for_viewer, self.b, [self.g, 99999])), {self.g})


class StatusTest(ReceiptBase):
    def status(self, mid: int) -> Any:
        messages = self.read(db_messages.chat_history, self.a, self.g)["messages"]
        return {m["id"]: m for m in messages}[mid]["status"]

    def test_group_aggregation(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "tick"))
        self.assertEqual(self.status(mid), "sent")
        self.delivered(self.c, mid)
        self.assertEqual(self.status(mid), "sent")  # bob has not received it
        self.delivered(self.b, mid)
        self.assertEqual(self.status(mid), "delivered")
        self.read_up_to(self.b, mid)
        self.assertEqual(self.status(mid), "delivered")  # carol has not read it
        self.read_up_to(self.c, mid)
        self.assertEqual(self.status(mid), "read")

    def test_disabled_never_activated_and_receipts_off_members(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "tick"))
        self.delivered(self.c, mid)
        self.read_up_to(self.b, mid)
        self.assertEqual(self.status(mid), "delivered")  # carol received it but has not read it
        self.set_user(self.c, disabled=1)  # a disabled member never blocks
        self.assertEqual(self.status(mid), "read")
        self.set_user(self.c, disabled=0, last_login_at=None)  # neither does one who never logged in
        self.assertEqual(self.status(mid), "read")
        self.set_user(self.c, last_login_at=1.0, read_receipts=0)  # receipts off: blocks nothing, adds no blue
        self.assertEqual(self.status(mid), "read")
        self.set_user(self.b, read_receipts=0)  # nobody left to turn it blue
        self.assertEqual(self.status(mid), "delivered")

    def test_empty_recipient_set_is_sent_and_direct_chat_with_receipts_off_peer_never_blue(self) -> None:
        solo = self.group(self.a)
        solo_mid = self.mid(self.send(self.a, solo, "alone"))
        self.assertEqual(self.read(db_messages.chat_history, self.a, solo)["messages"][-1]["status"], "sent")
        self.assertIsNotNone(solo_mid)
        self.set_user(self.b, read_receipts=0)
        direct = self.dm(self.a, self.b)
        dmid = self.mid(self.send(self.a, direct, "hi"))
        self.read_up_to(self.b, dmid, direct)
        self.assertEqual(self.read(db_messages.chat_history, self.a, direct)["messages"][-1]["status"], "delivered")

    def test_status_for_is_pure(self) -> None:
        rows: List[Dict[str, Any]] = [
            {
                "user_id": 1,
                "delivered_id": 9,
                "read_receipt_id": 9,
                "disabled": False,
                "activated": True,
                "read_receipts": True,
            },
            {
                "user_id": 2,
                "delivered_id": 5,
                "read_receipt_id": 3,
                "disabled": False,
                "activated": True,
                "read_receipts": True,
            },
            {
                "user_id": 3,
                "delivered_id": 0,
                "read_receipt_id": 0,
                "disabled": True,
                "activated": True,
                "read_receipts": True,
            },
        ]
        self.assertEqual(db_receipts.status_for(rows, 1, 6), "sent")
        self.assertEqual(db_receipts.status_for(rows, 1, 5), "delivered")
        self.assertEqual(db_receipts.status_for(rows, 1, 3), "read")
        self.assertIsNone(db_receipts.status_for(rows, 1, 3, self_chat=True))
        self.assertEqual(db_receipts.status_for(rows[:1], 1, 3), "sent")  # R is empty

    def test_system_messages_and_self_chat_have_no_status(self) -> None:
        history = self.read(db_messages.chat_history, self.a, self.g)["messages"]
        self.assertEqual({m["kind"]: m["status"] for m in history}, {"system": None})
        me = self.write(db_chats.chat_open_direct, self.a, self.a)["res"]["chat"]["id"]
        self.assertIsNone(self.send(self.a, me, "self")["res"]["message"]["status"])


if __name__ == "__main__":
    unittest.main()
