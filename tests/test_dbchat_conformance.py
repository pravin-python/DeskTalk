"""Conformance points of SPEC 7.1/7.1.1/7.6(6) that cut across requests: error class, invalid text, record shape."""

from __future__ import annotations

import unittest

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db, db_chats, db_messages, db_receipts

SURROGATE = "x\ud800y"


class ErrorClassTest(support.DbChatCase):
    def test_chat_error_is_a_request_error_with_the_same_wire_form(self) -> None:
        a = self.user("alice")
        self.assertTrue(issubclass(db_chats.ChatError, db.RequestError))
        self.assertIs(db.ChatError, db_chats.ChatError)
        with self.assertRaises(db.RequestError) as caught:
            self.write(db_chats.chat_get, a, 9999)
        self.assertEqual(
            caught.exception.to_err(), {"code": "not_member", "msg": "not a member of this chat", "chat_id": 9999}
        )
        self.assertEqual(str(caught.exception), "not_member: not a member of this chat")

    def test_error_fields_survive_to_err(self) -> None:
        err = db_chats.ChatError("server_busy", "slow", reason="x", retry_after=2.0, chat_id=3, message_id=4)
        self.assertEqual(
            err.to_err(),
            {"code": "server_busy", "msg": "slow", "reason": "x", "retry_after": 2.0, "chat_id": 3, "message_id": 4},
        )


class InvalidTextTest(support.DbChatCase):
    """A lone surrogate is ``bad_request`` / ``invalid_text`` in every text parameter (SPEC 7.1)."""

    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.g = self.group(self.a, self.b)
        self.mid_ = self.mid(self.send(self.a, self.g, "text"))

    def bad(self, fn, *args, reader: bool = False) -> None:
        self.fails("bad_request", fn, *args, reason="invalid_text", reader=reader)

    def test_every_text_parameter(self) -> None:
        a, g, mid = self.a, self.g, self.mid_
        self.bad(db_messages.msg_send, a, g, "client-0001", SURROGATE)
        self.bad(db_messages.msg_send, a, g, "client\ud800-1", "ok")
        self.bad(db_messages.msg_send, a, g, "client-0001", "ok", "a\ud800")
        self.bad(db_messages.msg_edit, a, mid, SURROGATE)
        self.bad(db_messages.msg_react, a, mid, "\ud83d")
        self.bad(db_messages.msg_delete, a, mid, SURROGATE)
        self.bad(db_messages.msg_forward, a, [mid], [g], "fwd-\ud800-0001")
        self.bad(db_messages.msg_search, a, SURROGATE, reader=True)
        self.bad(db_messages.msg_shared, a, g, SURROGATE, reader=True)
        self.bad(db_chats.chat_create_group, a, SURROGATE, [])
        self.bad(db_chats.chat_create_group, a, "Fine", [], SURROGATE)
        self.bad(db_chats.chat_update, a, g, SURROGATE)
        self.bad(db_chats.admin_update_user, a, self.b, None, None, SURROGATE)

    def test_invalid_text_is_a_shape_error_checked_before_authorisation_and_targets(self) -> None:
        self.set_user(self.a, disabled=1)
        self.bad(db_messages.msg_send, self.a, 99999, "client-0002", SURROGATE)
        self.bad(db_messages.msg_edit, self.a, 99999, SURROGATE)

    def test_nothing_was_written(self) -> None:
        before = self.scalar("SELECT COUNT(*) FROM messages")
        self.bad(db_messages.msg_send, self.a, self.g, "client-0003", SURROGATE)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM messages"), before)


class ReceiptShapeTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.g = self.group(self.a, self.b)
        self.mid_ = self.mid(self.send(self.a, self.g, "hello"))

    def test_single_and_batched_shapes_are_equivalent(self) -> None:
        single = self.write(db_receipts.receipt_delivered, self.b, None, self.g, self.mid_)
        self.assertEqual(single["receipt"]["delivered_up_to"], self.mid_)
        self.assertEqual(single["receipt"]["audience"], [self.b, self.a])
        self.assertEqual(single["receipt"]["authors"], [self.a])
        again = self.write(db_receipts.receipt_delivered, self.b, [{"chat_id": self.g, "up_to_id": self.mid_}])
        self.assertTrue(again["noop"])

    def test_exactly_one_shape(self) -> None:
        deliver = db_receipts.receipt_delivered
        item = [{"chat_id": self.g, "up_to_id": 1}]
        self.fails("bad_request", deliver, self.b)
        self.fails("bad_request", deliver, self.b, [])
        self.fails("bad_request", deliver, self.b, item, self.g, 1)
        self.fails("bad_request", deliver, self.b, None, self.g)
        self.fails("bad_request", deliver, self.b, None, None, 1)
        self.fails("bad_request", deliver, self.b, None, self.g, -1)
        self.fails("bad_request", deliver, self.b, None, True, 1)


class RecordShapeTest(support.DbChatCase):
    """Every write function returns the §7.6(6) keys (with the member list of its transaction)."""

    KEYS = {
        "res",
        "events",
        "index",
        "noop",
        "recipients",
        "hidden",
        "starred",
        "quote_hidden",
        "quoted",
        "sender_status",
        "deduped",
        "read_sync",
        "receipt",
        "receipts",
        "chats",
    }

    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.g = self.group(self.a, self.b)

    def test_chat_scoped_and_receipt_writes_carry_the_member_list(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "x"))
        results = [
            self.write(db_chats.chat_prefs, self.b, self.g, None, True),
            self.write(db_receipts.receipt_read, self.b, self.g, mid),
            self.write(db_receipts.receipt_delivered, self.b, None, self.g, mid),
            self.write(db_messages.msg_star, self.b, mid, True),
            self.write(db_messages.msg_delete, self.b, mid, "me"),
            self.write(db_chats.chat_clear, self.b, self.g),
            self.write(db_chats.chat_update, self.a, self.g, "Renamed"),
            self.write(db_chats.chat_set_admin, self.a, self.g, self.b, True),
        ]
        for out in results:
            self.assertTrue(set(out) >= self.KEYS, sorted(self.KEYS - set(out)))
            self.assertEqual({r["user_id"] for r in out["recipients"]}, {self.a, self.b})

    def test_event_frame_classes(self) -> None:
        out = self.send(self.a, self.g, "classes")
        classes = {e["t"]: e["cls"] for e in out["events"]}
        self.assertEqual(classes["ev.message"], "durable")
        delivered = self.write(db_receipts.receipt_delivered, self.b, None, self.g, self.mid(out))
        receipt = delivered["events"][0]
        self.assertEqual((receipt["t"], receipt["cls"], receipt["key"][0]), ("ev.receipt", "keyed", "receipt"))
        self.assertEqual(db_chats.event("ev.typing", [], durable=False)["cls"], "ephemeral")


if __name__ == "__main__":
    unittest.main()
