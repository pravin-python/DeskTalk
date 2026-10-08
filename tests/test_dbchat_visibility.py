"""Visibility predicate, per-recipient variants and the write-result record (SPEC 3.1, 7.2, 7.6(6))."""

from __future__ import annotations

import unittest
from typing import Any, Dict, Set

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db_chats, db_messages, db_receipts


class VisibilityBase(support.DbChatCase):
    """a (author, admin), b (member), c (late joiner) and d (clears the chat) around one old message."""

    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.d = self.user("dave")
        self.g = self.group(self.a, self.b, self.d)
        self.old = self.mid(self.send(self.a, self.g, "an old message"))
        self.write(db_chats.chat_clear, self.d, self.g)
        self.c = self.user("carol")
        self.write(db_chats.chat_add_members, self.a, self.g, [self.c])
        self.hider = self.b
        self.write(db_messages.msg_delete, self.hider, self.old, "me")

    def receivers(self, out: Dict[str, Any], t: str = "ev.message_update") -> Set[int]:
        return self.audience(out, t)


class AudienceTest(VisibilityBase):
    def test_react_edit_pin_and_delete_skip_late_joiner_cleared_and_hiding_members(self) -> None:
        # a is the only member for whom the old message is visible: b hid it, d cleared the chat, c joined later
        react = self.write(db_messages.msg_react, self.a, self.old, "\U0001f44d")
        self.assertEqual(self.receivers(react), {self.a})
        self.assertEqual(react["hidden"], {self.b})
        edit = self.write(db_messages.msg_edit, self.a, self.old, "edited old message")
        self.assertEqual(self.receivers(edit), {self.a})
        pin = self.write(db_messages.msg_pin, self.a, self.g, self.old, True)
        self.assertEqual(self.receivers(pin), {self.a})
        # the chat_update of a pin goes to every member, each built per viewer: nobody else sees the old pin
        self.assertEqual(self.audience(pin, "ev.chat_update"), {self.a, self.b, self.c, self.d})
        for viewer in (self.b, self.c, self.d):
            self.assertEqual(self.payloads(pin, "ev.chat_update", viewer)[0]["chat"]["pinned_message_ids"], [])
        self.assertEqual(self.payloads(pin, "ev.chat_update", self.a)[0]["chat"]["pinned_message_ids"], [self.old])
        delete = self.write(db_messages.msg_delete, self.a, self.old, "everyone")
        self.assertEqual(self.receivers(delete), {self.a})

    def test_new_messages_reach_everybody_for_whom_they_are_visible(self) -> None:
        out = self.send(self.a, self.g, "fresh")
        self.assertEqual(self.receivers(out, "ev.message"), {self.a, self.b, self.c, self.d})
        mid = self.mid(out)
        self.write(db_messages.msg_delete, self.b, mid, "me")
        react = self.write(db_messages.msg_react, self.a, mid, "\U0001f44d")
        self.assertEqual(self.receivers(react), {self.a, self.c, self.d})

    def test_record_contents(self) -> None:
        out = self.write(db_messages.msg_react, self.a, self.old, "\U0001f44d")
        recipients = {r["user_id"]: r for r in out["recipients"]}
        self.assertEqual(set(recipients), {self.a, self.b, self.c, self.d})
        self.assertEqual(
            set(recipients[self.a]),
            {
                "user_id",
                "history_from_id",
                "cleared_before_id",
                "last_read_id",
                "delivered_id",
                "read_receipt_id",
                "disabled",
                "activated",
                "read_receipts",
                "role",
            },
        )
        self.assertEqual(recipients[self.d]["cleared_before_id"], self.old)
        self.assertGreaterEqual(recipients[self.c]["history_from_id"], self.old)
        self.assertEqual(
            (out["starred"], out["quote_hidden"], out["quoted"], out["deduped"]), (set(), set(), None, False)
        )
        self.assertEqual(out["sender_status"], "sent")


class QuoteTest(VisibilityBase):
    def setUp(self) -> None:
        super().setUp()
        self.reply = self.send(self.a, self.g, "replying to the old one", reply_to_id=self.old)

    def test_quote_of_a_pre_join_message_is_unavailable_for_late_joiner_and_cleared_and_hiding_members(self) -> None:
        out = self.reply
        states = {
            u: self.payloads(out, "ev.message", u)[0]["message"]["reply_to"] for u in (self.a, self.b, self.c, self.d)
        }
        self.assertEqual(states[self.a]["body"], "an old message")
        self.assertFalse(states[self.a]["unavailable"])
        for viewer in (self.b, self.c, self.d):
            self.assertEqual(
                states[viewer],
                {
                    "id": self.old,
                    "sender_id": None,
                    "kind": "text",
                    "body": "",
                    "attachment_name": None,
                    "deleted": False,
                    "unavailable": True,
                },
            )
        self.assertEqual(out["quote_hidden"], {self.b})
        self.assertEqual(
            out["quoted"],
            {
                "id": self.old,
                "sender_id": self.a,
                "kind": "text",
                "body": "an old message",
                "attachment_name": None,
                "deleted": False,
            },
        )

    def test_unavailable_in_message_update_and_history_too(self) -> None:
        reply_id = self.mid(self.reply)
        edit = self.write(db_messages.msg_edit, self.a, reply_id, "edited reply")
        self.assertTrue(self.payloads(edit, "ev.message_update", self.c)[0]["message"]["reply_to"]["unavailable"])
        self.assertFalse(self.payloads(edit, "ev.message_update", self.a)[0]["message"]["reply_to"]["unavailable"])
        history_c = {m["id"]: m for m in self.read(db_messages.chat_history, self.c, self.g)["messages"]}
        self.assertTrue(history_c[reply_id]["reply_to"]["unavailable"])
        history_a = {m["id"]: m for m in self.read(db_messages.chat_history, self.a, self.g)["messages"]}
        self.assertEqual(history_a[reply_id]["reply_to"]["body"], "an old message")
        self.assertNotIn(self.old, history_c)

    def test_deleted_original_is_shown_as_deleted(self) -> None:
        reply_id = self.mid(self.reply)
        self.write(db_messages.msg_delete, self.a, self.old, "everyone")
        history_a = {m["id"]: m for m in self.read(db_messages.chat_history, self.a, self.g)["messages"]}
        self.assertEqual(
            (history_a[reply_id]["reply_to"]["deleted"], history_a[reply_id]["reply_to"]["body"]), (True, "")
        )
        # unavailable still wins for a viewer who cannot see the original at all
        history_c = {m["id"]: m for m in self.read(db_messages.chat_history, self.c, self.g)["messages"]}
        self.assertTrue(history_c[reply_id]["reply_to"]["unavailable"])


class VariantTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")
        self.g = self.group(self.a, self.b, self.c)

    def test_client_id_and_status_only_in_the_senders_variant_and_star_is_the_recipients_own(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "mine", client_id="client-variant"))
        self.write(db_messages.msg_star, self.b, mid, True)
        out = self.write(db_messages.msg_react, self.c, mid, "\U0001f44d")
        views = {u: self.payloads(out, "ev.message_update", u)[0]["message"] for u in (self.a, self.b, self.c)}
        self.assertEqual((views[self.a]["client_id"], views[self.a]["status"]), ("client-variant", "sent"))
        for viewer in (self.b, self.c):
            self.assertEqual((views[viewer]["client_id"], views[viewer]["status"]), (None, None))
        self.assertEqual({u: v["starred"] for u, v in views.items()}, {self.a: False, self.b: True, self.c: False})
        self.assertEqual(out["starred"], {self.b})
        # the three audience groups are the three distinct variants
        self.assertEqual(sorted(len(g["user_ids"]) for g in out["events"][0]["groups"]), [1, 1, 1])

    def test_variant_serialisation_is_cheap_and_shares_nested_objects(self) -> None:
        out = self.send(self.a, self.g, "same body")
        parts = out["parts"]
        variants = [(False, False, "none"), (False, True, "none"), (True, False, "none")]
        rendered = db_messages.serialize_message_variants(parts, variants + variants, "delivered")
        self.assertEqual(len(rendered), 3)
        self.assertEqual(rendered[(True, False, "none")]["status"], "delivered")
        self.assertTrue(rendered[(False, True, "none")]["starred"])
        self.assertIs(rendered[(False, False, "none")]["reactions"], rendered[(True, False, "none")]["reactions"])

    def test_events_can_be_rebuilt_from_the_record_alone(self) -> None:
        sent = self.send(self.a, self.g, "rebuild me")
        self.assertEqual(db_messages.message_event("ev.message", sent["parts"], sent), sent["events"][0])
        mid = self.mid(sent)
        for out in (
            self.write(db_messages.msg_react, self.b, mid, "👍"),
            self.write(db_messages.msg_edit, self.a, mid, "rebuilt"),
        ):
            self.assertEqual(db_messages.message_event("ev.message_update", out["parts"], out), out["events"][0])
        parts, record = self.read(db_messages.message_record, mid)
        self.assertEqual(record["sender_status"], "sent")
        self.assertEqual(db_messages.message_event("ev.message_update", parts, record), out["events"][0])

    def test_message_shape(self) -> None:
        attachment = self.add_attachment(self.a, "image", "photo.png")
        message = self.send(self.a, self.g, "look @bob", attachment_id=attachment)["res"]["message"]
        self.assertEqual(
            set(message),
            {
                "id",
                "chat_id",
                "sender_id",
                "client_id",
                "kind",
                "body",
                "created_at",
                "edited_at",
                "deleted",
                "forwarded",
                "mentions",
                "reply_to",
                "attachment",
                "reactions",
                "starred",
                "pinned",
                "status",
                "system",
            },
        )
        self.assertEqual(
            message["attachment"],
            {
                "id": attachment,
                "name": "photo.png",
                "mime": "image/png",
                "size": 10,
                "kind": "image",
                "url": "/files/" + attachment,
                "width": None,
                "height": None,
                "duration": None,
            },
        )
        system = self.read(db_messages.chat_history, self.a, self.g)["messages"][0]
        self.assertEqual(set(system["system"]), {"event", "actor_id", "target_ids", "title", "message_id"})
        self.assertIsNone(system["client_id"])


class ChatViewTest(support.DbChatCase):
    def test_last_message_follows_the_visibility_predicate(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        g = self.group(a, b)
        first = self.mid(self.send(a, g, "first"))
        newest = self.mid(self.send(a, g, "newest"))
        self.write(db_messages.msg_delete, b, newest, "me")
        chat_b = self.chat_of(b, g)
        self.assertEqual(chat_b["last_message"]["id"], first)
        self.assertEqual(chat_b["last_message_id"], newest)  # the raw newest id, even though b cannot see it
        self.assertEqual(self.chat_of(a, g)["last_message"]["id"], newest)
        late = self.user("carol")
        self.write(db_chats.chat_add_members, a, g, [late])
        chat_late = self.chat_of(late, g)
        self.assertEqual(chat_late["last_message"]["system"]["event"], "added")
        self.assertEqual(chat_late["pinned_messages"], [])

    def test_ready_snapshot(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        c = self.user("carol")
        g = self.group(a, b)
        self.dm(b, c)  # dormant: only b lists it
        self.send(a, g, "hi @bob")
        ready_b = self.read(db_chats.build_ready, b)
        self.assertEqual({chat["id"] for chat in ready_b["chats"]}, {self.everyone, g, self.dm(b, c)})
        ready_c = self.read(db_chats.build_ready, c)
        self.assertEqual({chat["id"] for chat in ready_c["chats"]}, {self.everyone})
        self.assertEqual(ready_b["me"]["id"], b)
        self.assertEqual([u["id"] for u in ready_b["users"]], [a, b, c])
        self.assertEqual(ready_b["workspace"], {"name": "DeskTalk", "registration_open": False})
        self.assertTrue(ready_b["instance_id"])
        self.assertIsInstance(ready_b["server_time"], float)
        by_id = {chat["id"]: chat for chat in ready_b["chats"]}
        self.assertEqual((by_id[g]["me"]["unread"], by_id[g]["me"]["unread_mentions"]), (1, 1))
        self.assertEqual(by_id[g]["last_message"]["body"], "hi @bob")
        self.set_user(b, disabled=1)
        self.fails("unauthorized", db_chats.build_ready, b, reader=True)


class DbFacadeTest(unittest.TestCase):
    def test_facade_exports_the_hub_api(self) -> None:
        from chatd import db

        for name in (
            "ChatError",
            "msg_send",
            "msg_forward",
            "chat_history",
            "build_chats",
            "build_ready",
            "receipt_read",
            "receipt_delivered",
            "load_membership_index",
            "chat_create_group",
            "admin_update_user",
            "insert_system_message",
            "create_everyone_chat",
            "add_member_row",
            "status_for",
        ):
            self.assertTrue(hasattr(db, name), name)
        self.assertIs(db.ChatError, db_chats.ChatError)
        self.assertIs(db_messages.insert_system_message, db.insert_system_message)
        self.assertTrue(callable(db_receipts.counters))


if __name__ == "__main__":
    unittest.main()
