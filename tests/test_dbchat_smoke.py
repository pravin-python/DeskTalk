"""Smoke test of the db-chat modules against a real database (grows into the per-request suites)."""

from __future__ import annotations

import unittest

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db_chats, db_messages, db_receipts, db_users


class SmokeTest(support.DbChatCase):
    def test_direct_conversation(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        chat_id = self.dm(a, b)
        out = self.send(a, chat_id, "hi bob")
        self.assertEqual(out["res"]["message"]["body"], "hi bob")
        self.assertEqual(self.types(out), ["ev.chat_update", "ev.message", "ev.receipt", "ev.read_sync"])
        history = self.read(db_messages.chat_history, b, chat_id)
        self.assertEqual([m["body"] for m in history["messages"]], ["hi bob"])
        self.assertEqual(self.me(b, chat_id)["unread"], 1)
        read = self.write(db_receipts.receipt_read, b, chat_id, out["res"]["message"]["id"])
        self.assertEqual(read["res"]["unread"], 0)

    def test_ready(self) -> None:
        a = self.user("alice")
        self.user("bob")
        ready = self.read(db_chats.build_ready, a)
        self.assertEqual(len(ready["chats"]), 1)
        self.assertEqual(ready["me"]["id"], a)


class RegistrationIntegrationTest(support.DbChatCase):
    """The three cross-contract functions as db-core's ``register_user`` uses them."""

    def register(self, name: str, **extra: object) -> dict:
        spec = {"username": name, "display_name": name.title(), "pw_hash": "x", "activated": True}
        spec.update(extra)
        return self.write(db_users.register_user, spec)

    def test_everyone_chat_and_the_join_point_rules(self) -> None:
        first = self.register("alice")
        everyone = first["chat_id"]
        alice = first["user"]["id"]
        self.send(alice, everyone, "hello team")
        before = self.scalar("SELECT last_activity_at FROM chats WHERE id = ?", (everyone,))
        second = self.register("bob")
        bob = second["user"]["id"]
        self.assertEqual(second["event"], "joined")
        self.assertEqual(self.scalar("SELECT last_activity_at FROM chats WHERE id = ?", (everyone,)), before)
        chat = self.chat_of(bob, everyone)
        self.assertTrue(chat["is_default"])
        self.assertEqual(chat["created_by"], alice)
        self.assertEqual(chat["last_message"]["system"]["event"], "joined")
        self.assertEqual(chat["last_message_id"], second["message_id"])
        self.assertEqual(chat["me"]["unread"], 0)
        history = self.read(db_messages.chat_history, bob, everyone)["messages"]
        self.assertEqual([m["system"]["event"] for m in history], ["joined"])
        self.assertEqual([m["role"] for m in chat["members"]], ["admin", "member"])
        member = second["member"]
        self.assertEqual({m["user_id"]: m for m in chat["members"]}[bob], member)
        # the joined message goes to every member as one variant
        event = self.read(db_messages.system_message_event, everyone, second["message_id"])
        self.assertEqual(event["groups"][0]["user_ids"], [alice, bob])
        self.assertEqual(event["groups"][0]["d"]["message"]["system"]["event"], "joined")

    def test_never_activated_users_do_not_block_ticks_until_they_log_in(self) -> None:
        alice = self.register("alice")["user"]["id"]
        everyone = self.scalar("SELECT id FROM chats WHERE is_default = 1")
        bob = self.register("bob")["user"]["id"]
        ghost = self.register("ghost", activated=False)["user"]["id"]
        sent = self.send(alice, everyone, "anyone there?")
        mid = sent["res"]["message"]["id"]
        self.assertEqual(sent["sender_status"], "sent")
        self.write(db_receipts.receipt_delivered, bob, [{"chat_id": everyone, "up_to_id": mid}])
        history = self.read(db_messages.chat_history, alice, everyone)["messages"]
        self.assertEqual({m["id"]: m["status"] for m in history}[mid], "delivered")
        self.set_user(ghost, last_login_at=1.0)  # their first login: the drop to 'sent' is truthful
        history = self.read(db_messages.chat_history, alice, everyone)["messages"]
        self.assertEqual({m["id"]: m["status"] for m in history}[mid], "sent")


if __name__ == "__main__":
    unittest.main()
