"""msg.edit / delete / react / star / pin of ``chatd/db_messages.py`` (SPEC 7.4, 7.3 recipient matrix)."""

from __future__ import annotations

import unittest

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db_chats, db_messages, db_receipts


class MutationBase(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")
        self.g = self.group(self.a, self.b, self.c)
        self.dm_ab = self.dm(self.a, self.b)


class EditTest(MutationBase):
    def test_edit_happy_path(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "first"))
        self.clock.advance(5)
        out = self.write(db_messages.msg_edit, self.a, mid, " second \x00")
        message = out["res"]["message"]
        self.assertEqual(message["body"], "second")
        self.assertIsNotNone(message["edited_at"])
        self.assertEqual(self.types(out), ["ev.message_update"])
        self.assertEqual(self.audience(out, "ev.message_update"), {self.a, self.b, self.c})
        self.assertEqual(
            self.payloads(out, "ev.message_update", self.b)[0]["message"]["edited_at"], message["edited_at"]
        )

    def test_unchanged_body_is_a_noop_and_keeps_edited_at(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "same"))
        out = self.write(db_messages.msg_edit, self.a, mid, "  same ")
        self.assertTrue(out["noop"])
        self.assertEqual(out["events"], [])
        self.assertIsNone(out["res"]["message"]["edited_at"])
        self.assertIsNone(self.scalar("SELECT edited_at FROM messages WHERE id = ?", (mid,)))

    def test_error_codes_and_order(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "mine"))
        system = self.scalar("SELECT id FROM messages WHERE chat_id = ? AND kind = 'system'", (self.g,))
        edit = db_messages.msg_edit
        self.fails("not_found", edit, self.a, 99999, "x")
        self.fails("forbidden", edit, self.b, mid, "x")
        self.fails("invalid_state", edit, self.a, system, "x")
        # a system message has no owner: invalid_state, never forbidden
        self.fails("invalid_state", edit, self.b, system, "x")
        # rule 7 checks come after the object state and before the window
        self.fails("bad_request", edit, self.a, mid, "   ")
        self.fails("too_large", edit, self.a, mid, "y" * 8001)
        self.fails("bad_request", edit, self.a, mid, 5)
        self.clock.advance(901)
        self.fails("window_expired", edit, self.a, mid, "late")
        # window beats a too-large body? no: body checks run first
        self.fails("too_large", edit, self.a, mid, "y" * 8001)
        outsider = self.add_user("outsider")
        self.fails("not_found", edit, outsider, mid, "x")

    def test_non_text_forwarded_and_deleted_messages_cannot_be_edited(self) -> None:
        image = self.mid(self.send(self.a, self.g, "cap", attachment_id=self.add_attachment(self.a)))
        self.fails("invalid_state", db_messages.msg_edit, self.a, image, "new")
        source = self.mid(self.send(self.b, self.g, "src"))
        copy = self.write(db_messages.msg_forward, self.a, [source], [self.dm_ab], "fwd-00000001")["res"]["messages"][0]
        self.fails("invalid_state", db_messages.msg_edit, self.a, copy["id"], "new")
        gone = self.mid(self.send(self.a, self.g, "gone"))
        self.write(db_messages.msg_delete, self.a, gone, "everyone")
        self.fails("invalid_state", db_messages.msg_edit, self.a, gone, "new")

    def test_edit_recomputes_mentions_and_syncs_only_unread_users(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "plain"))
        self.write(db_receipts.receipt_read, self.c, self.g, mid)
        out = self.write(db_messages.msg_edit, self.a, mid, "now @bob and @carol")
        self.assertEqual(out["res"]["message"]["mentions"], [self.b, self.c])
        self.assertEqual(set(out["read_sync"]), {self.b})  # carol already read it
        self.assertEqual(out["read_sync"][self.b]["unread_mentions"], 1)
        self.assertEqual(self.audience(out, "ev.read_sync"), {self.b})
        self.assertEqual(self.me(self.b, self.g)["first_unread_mention_id"], mid)
        gone = self.write(db_messages.msg_edit, self.a, mid, "no mention now")
        self.assertEqual(gone["res"]["message"]["mentions"], [])
        self.assertEqual(set(gone["read_sync"]), {self.b})
        self.assertEqual(gone["read_sync"][self.b]["unread_mentions"], 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM message_mentions WHERE message_id = ?", (mid,)), 0)


class DeleteTest(MutationBase):
    def test_delete_everyone_by_the_sender(self) -> None:
        first = self.mid(self.send(self.b, self.g, "reacted"))
        mid = self.mid(self.send(self.a, self.g, "oops @bob"))
        reply = self.mid(self.send(self.b, self.g, "reply", reply_to_id=mid))
        self.write(db_messages.msg_react, self.b, mid, "\U0001f44d")
        self.write(db_messages.msg_star, self.c, mid, True)
        self.write(db_messages.msg_pin, self.a, self.g, mid, True)
        out = self.write(db_messages.msg_delete, self.a, mid, "everyone")
        message = out["res"]["message"]
        self.assertEqual(
            (message["deleted"], message["body"], message["attachment"], message["reactions"], message["pinned"]),
            (True, "", None, [], False),
        )
        self.assertEqual(
            (message["mentions"], message["starred"], message["kind"], message["sender_id"]),
            ([], False, "text", self.a),
        )
        self.assertEqual(self.types(out), ["ev.message_update", "ev.chat_update", "ev.read_sync"])
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.a, self.b, self.c})
        self.assertEqual(self.audience(out, "ev.read_sync"), {self.b, self.c})
        for table in ("reactions", "stars", "pins", "message_mentions"):
            self.assertEqual(self.scalar("SELECT COUNT(*) FROM %s WHERE message_id = ?" % table, (mid,)), 0, table)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM messages WHERE id = ?", (mid,)), 1)
        # the quote of the deleted message shows a deleted original
        history = {m["id"]: m for m in self.read(db_messages.chat_history, self.c, self.g)["messages"]}
        self.assertEqual((history[reply]["reply_to"]["deleted"], history[reply]["reply_to"]["body"]), (True, ""))
        self.assertIn(first, history)
        # idempotent
        again = self.write(db_messages.msg_delete, self.a, mid, "everyone")
        self.assertTrue(again["noop"])
        self.assertEqual(again["events"], [])
        self.assertTrue(again["res"]["message"]["deleted"])

    def test_counters_change_only_for_members_who_had_it_unread(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "unread for bob"))
        self.write(db_receipts.receipt_read, self.c, self.g, mid)
        out = self.write(db_messages.msg_delete, self.a, mid, "everyone")
        self.assertEqual(set(out["read_sync"]), {self.b})
        self.assertEqual(out["read_sync"][self.b]["unread"], 0)
        self.assertEqual(self.me(self.b, self.g)["unread"], 0)

    def test_delete_window_and_admin_moderation(self) -> None:
        mid = self.mid(self.send(self.b, self.g, "old"))
        self.clock.advance(172801)
        self.fails("window_expired", db_messages.msg_delete, self.b, mid, "everyone")
        self.fails("forbidden", db_messages.msg_delete, self.c, mid, "everyone")
        # a group admin may delete anybody's message at any time
        out = self.write(db_messages.msg_delete, self.a, mid, "everyone")
        self.assertTrue(out["res"]["message"]["deleted"])

    def test_direct_chats_only_allow_deleting_your_own_messages(self) -> None:
        self.send(self.a, self.dm_ab, "first message lists the peer")
        mid = self.mid(self.send(self.b, self.dm_ab, "from bob"))
        self.fails("forbidden", db_messages.msg_delete, self.a, mid, "everyone")
        self.write(db_messages.msg_delete, self.b, mid, "everyone")

    def test_system_messages_cannot_be_deleted_in_either_scope(self) -> None:
        system = self.scalar("SELECT id FROM messages WHERE chat_id = ? AND kind = 'system'", (self.g,))
        for scope in ("me", "everyone"):
            self.fails("invalid_state", db_messages.msg_delete, self.a, system, scope)
            self.fails("invalid_state", db_messages.msg_delete, self.b, system, scope)

    def test_validation(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "x"))
        self.fails("bad_request", db_messages.msg_delete, self.a, mid, "all")
        self.fails("bad_request", db_messages.msg_delete, self.a, "1", "me")
        self.fails("not_found", db_messages.msg_delete, self.a, 99999, "me")
        outsider = self.add_user("outsider")
        self.fails("not_found", db_messages.msg_delete, outsider, mid, "me")

    def test_attachment_is_released_only_when_unreferenced(self) -> None:
        attachment = self.add_attachment(self.a, "file", "doc.pdf")
        original = self.mid(self.send(self.a, self.g, "doc", attachment_id=attachment))
        copy = self.write(db_messages.msg_forward, self.a, [original], [self.dm_ab], "fwd-00000002")["res"]["messages"][
            0
        ]
        first = self.write(db_messages.msg_delete, self.a, original, "everyone")
        self.assertEqual(first["released_attachments"], [])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM attachments WHERE id = ?", (attachment,)), 1)
        self.assertEqual(
            self.read(db_messages.chat_history, self.b, self.dm_ab)["messages"][0]["attachment"]["id"], attachment
        )
        second = self.write(db_messages.msg_delete, self.a, copy["id"], "everyone")
        self.assertEqual([r["id"] for r in second["released_attachments"]], [attachment])
        self.assertEqual(second["released_attachments"][0]["path"], "aa/" + attachment)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM attachments WHERE id = ?", (attachment,)), 0)

    def test_delete_for_me(self) -> None:
        old = self.mid(self.send(self.b, self.g, "older"))
        newest = self.mid(self.send(self.b, self.g, "newest"))
        out = self.write(db_messages.msg_delete, self.a, newest, "me")
        self.assertEqual(out["res"], {})
        self.assertEqual(self.types(out), ["ev.message_removed", "ev.chat_update", "ev.read_sync"])
        self.assertEqual(self.audience(out, "ev.message_removed"), {self.a})
        self.assertEqual(
            self.payloads(out, "ev.message_removed", self.a)[0], {"chat_id": self.g, "message_ids": [newest]}
        )
        update = self.payloads(out, "ev.chat_update", self.a)[0]["chat"]
        self.assertEqual(update["last_message"]["id"], old)
        self.assertEqual(update["me"]["unread"], 1)
        self.assertEqual(out["read_sync"][self.a]["unread"], 1)
        # others still see it; the actor no longer does
        self.assertIn(newest, [m["id"] for m in self.read(db_messages.chat_history, self.b, self.g)["messages"]])
        self.assertNotIn(newest, [m["id"] for m in self.read(db_messages.chat_history, self.a, self.g)["messages"]])
        again = self.write(db_messages.msg_delete, self.a, newest, "me")
        self.assertTrue(again["noop"])
        self.assertEqual((again["res"], again["events"]), ({}, []))
        # a hidden message is invisible to every other request
        self.fails("not_found", db_messages.msg_react, self.a, newest, "\U0001f44d")
        self.fails("not_found", db_messages.msg_delete, self.a, newest, "everyone")

    def test_delete_for_me_without_visible_effect_sends_only_the_removal(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "mine"))
        self.send(self.b, self.g, "later")
        out = self.write(db_messages.msg_delete, self.a, mid, "me")
        self.assertEqual(self.types(out), ["ev.message_removed"])


class ReactTest(MutationBase):
    def test_set_semantics(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "react to me"))
        thumbs, heart = "\U0001f44d", "❤️"
        out = self.write(db_messages.msg_react, self.b, mid, thumbs)
        self.assertEqual(out["res"]["message"]["reactions"], [{"emoji": thumbs, "user_ids": [self.b]}])
        self.assertEqual(self.types(out), ["ev.message_update"])
        self.assertEqual(self.audience(out, "ev.message_update"), {self.a, self.b, self.c})
        same = self.write(db_messages.msg_react, self.b, mid, thumbs)
        self.assertTrue(same["noop"])
        self.assertEqual(same["events"], [])
        switched = self.write(db_messages.msg_react, self.b, mid, heart)
        self.assertEqual(switched["res"]["message"]["reactions"], [{"emoji": heart, "user_ids": [self.b]}])
        self.write(db_messages.msg_react, self.c, mid, heart)
        message = self.read(db_messages.chat_history, self.a, self.g)["messages"][-1]
        self.assertEqual(message["reactions"], [{"emoji": heart, "user_ids": [self.b, self.c]}])
        removed = self.write(db_messages.msg_react, self.b, mid, None)
        self.assertEqual(removed["res"]["message"]["reactions"], [{"emoji": heart, "user_ids": [self.c]}])
        nothing = self.write(db_messages.msg_react, self.b, mid, None)
        self.assertTrue(nothing["noop"])

    def test_reaction_order(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "order"))
        first, second = "\U0001f602", "\U0001f44d"
        self.write(db_messages.msg_react, self.c, mid, first)
        self.write(db_messages.msg_react, self.a, mid, second)
        self.write(db_messages.msg_react, self.b, mid, first)
        message = self.read(db_messages.chat_history, self.a, self.g)["messages"][-1]
        self.assertEqual(
            message["reactions"],
            [
                {"emoji": first, "user_ids": [self.c, self.b]},
                {"emoji": second, "user_ids": [self.a]},
            ],
        )

    def test_emoji_validation(self) -> None:
        valid = [
            "\U0001f44d",
            "❤️",
            "\U0001f468‍\U0001f469‍\U0001f467",
            "\U0001f1ee\U0001f1f3",
            "1️⃣",
            "#⃣",
            "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
            "☀",
            "\U0001f3fd",
        ]
        invalid = [
            "",
            "a",
            "7",
            "\U0001f44da",
            " \U0001f44d",
            "\U0001f44d\n",
            "ab",
            "‍",
            "️",
            "1\U0001f44d",
            "\U0001f44d" * 17,
            "x⃣",
            5,
            ["\U0001f44d"],
        ]
        mid = self.mid(self.send(self.a, self.g, "emoji"))
        for emoji in valid:
            self.write(db_messages.msg_react, self.b, mid, emoji)
        for emoji in invalid:
            self.fails("bad_request", db_messages.msg_react, self.b, mid, emoji)
        self.write(db_messages.msg_react, self.b, mid, "\U0001f44d" * 16)

    def test_state_errors(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "x"))
        system = self.scalar("SELECT id FROM messages WHERE chat_id = ? AND kind = 'system'", (self.g,))
        self.fails("invalid_state", db_messages.msg_react, self.b, system, "\U0001f44d")
        self.write(db_messages.msg_delete, self.a, mid, "everyone")
        self.fails("invalid_state", db_messages.msg_react, self.b, mid, "\U0001f44d")
        self.fails("not_found", db_messages.msg_react, self.b, 99999, "\U0001f44d")
        direct = self.mid(self.send(self.a, self.dm_ab, "hi"))
        self.set_user(self.b, disabled=1)
        self.fails("invalid_state", db_messages.msg_react, self.a, direct, "\U0001f44d")

    def test_only_admins_post_still_allows_reactions_and_stars(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "announcement"))
        self.write(db_chats.chat_update, self.a, self.g, None, None, True)
        self.write(db_messages.msg_react, self.b, mid, "\U0001f44d")
        self.write(db_messages.msg_star, self.b, mid, True)
        self.fails("forbidden", db_messages.msg_pin, self.b, self.g, mid, True)


class StarTest(MutationBase):
    def test_star_is_actor_only(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "star me"))
        out = self.write(db_messages.msg_star, self.b, mid, True)
        self.assertTrue(out["res"]["message"]["starred"])
        self.assertEqual(self.types(out), ["ev.message_update"])
        self.assertEqual(self.audience(out, "ev.message_update"), {self.b})
        self.assertTrue(self.write(db_messages.msg_star, self.b, mid, True)["noop"])
        history_a = self.read(db_messages.chat_history, self.a, self.g)["messages"][-1]
        history_b = self.read(db_messages.chat_history, self.b, self.g)["messages"][-1]
        self.assertEqual((history_a["starred"], history_b["starred"]), (False, True))
        starred = self.read(db_messages.msg_starred, self.b)
        self.assertEqual([m["id"] for m in starred["messages"]], [mid])
        self.assertEqual(self.read(db_messages.msg_starred, self.a)["messages"], [])
        off = self.write(db_messages.msg_star, self.b, mid, False)
        self.assertFalse(off["res"]["message"]["starred"])

    def test_errors(self) -> None:
        system = self.scalar("SELECT id FROM messages WHERE chat_id = ? AND kind = 'system'", (self.g,))
        self.fails("invalid_state", db_messages.msg_star, self.a, system, True)
        self.fails("bad_request", db_messages.msg_star, self.a, system, "yes")
        mid = self.mid(self.send(self.a, self.g, "x"))
        self.write(db_messages.msg_delete, self.a, mid, "everyone")
        self.fails("invalid_state", db_messages.msg_star, self.a, mid, True)

    def test_starred_listing(self) -> None:
        ids = [self.mid(self.send(self.a, self.g, "m%d" % i)) for i in range(4)]
        other = self.mid(self.send(self.a, self.dm_ab, "dm"))
        for mid in ids + [other]:
            self.write(db_messages.msg_star, self.b, mid, True)
        everything = self.read(db_messages.msg_starred, self.b)
        self.assertEqual([m["id"] for m in everything["messages"]], [other] + ids[::-1])
        scoped = self.read(db_messages.msg_starred, self.b, self.g, None, 2)
        self.assertEqual([m["id"] for m in scoped["messages"]], ids[::-1][:2])
        self.assertTrue(scoped["has_more"])
        page = self.read(db_messages.msg_starred, self.b, self.g, ids[2], 50)
        self.assertEqual([m["id"] for m in page["messages"]], ids[:2][::-1])
        self.assertFalse(page["has_more"])
        # a deleted message and a message of a chat the user left drop out
        self.write(db_messages.msg_delete, self.a, ids[3], "everyone")
        self.write(db_chats.chat_remove_member, self.a, self.g, self.b)
        remaining = self.read(db_messages.msg_starred, self.b)
        self.assertEqual([m["id"] for m in remaining["messages"]], [other])
        self.fails("not_member", db_messages.msg_starred, self.b, self.g, reader=True)
        self.fails("bad_request", db_messages.msg_starred, self.b, None, None, "ten", reader=True)


class PinTest(MutationBase):
    def test_pin_and_unpin(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "pin me"))
        out = self.write(db_messages.msg_pin, self.b, self.g, mid, True)
        self.assertEqual(self.types(out), ["ev.message_update", "ev.chat_update", "ev.message"])
        self.assertTrue(self.payloads(out, "ev.message_update", self.a)[0]["message"]["pinned"])
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.a, self.b, self.c})
        system = self.payloads(out, "ev.message", self.c)[0]["message"]["system"]
        self.assertEqual((system["event"], system["message_id"], system["actor_id"]), ("pinned", mid, self.b))
        chat = out["res"]["chat"]
        self.assertEqual(chat["pinned_message_ids"], [mid])
        self.assertEqual(chat["pinned_messages"][0]["body"], "pin me")
        self.assertEqual(set(out["chats"]), {(u, self.g) for u in (self.a, self.b, self.c)})
        again = self.write(db_messages.msg_pin, self.b, self.g, mid, True)
        self.assertTrue(again["noop"])
        self.assertEqual(again["events"], [])
        before = self.scalar("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (self.g,))
        off = self.write(db_messages.msg_pin, self.c, self.g, mid, False)
        self.assertEqual(self.types(off), ["ev.message_update", "ev.chat_update"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (self.g,)), before)
        self.assertTrue(self.write(db_messages.msg_pin, self.c, self.g, mid, False)["noop"])

    def test_limit_of_five_and_ordering(self) -> None:
        ids = [self.mid(self.send(self.a, self.g, "m%d" % i)) for i in range(6)]
        for mid in ids[:5]:
            self.write(db_messages.msg_pin, self.a, self.g, mid, True)
        self.fails("invalid_state", db_messages.msg_pin, self.a, self.g, ids[5], True, reason="pin_limit")
        self.assertEqual(self.chat_of(self.b, self.g)["pinned_message_ids"], ids[:5][::-1])

    def test_errors_and_order(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "x"))
        other_chat = self.group(self.a, self.b, title="Other")
        elsewhere = self.mid(self.send(self.a, other_chat, "in another chat"))
        self.fails("not_found", db_messages.msg_pin, self.a, self.g, elsewhere, True)
        self.fails("not_found", db_messages.msg_pin, self.a, self.g, elsewhere, False)
        self.fails("not_found", db_messages.msg_pin, self.a, self.g, 99999, True)
        self.fails("not_member", db_messages.msg_pin, self.a, 99999, mid, True)
        outsider = self.add_user("outsider")
        self.fails("not_member", db_messages.msg_pin, outsider, self.g, mid, True)
        system = self.scalar("SELECT id FROM messages WHERE chat_id = ? AND kind = 'system'", (self.g,))
        self.fails("invalid_state", db_messages.msg_pin, self.a, self.g, system, True)
        self.write(db_messages.msg_delete, self.a, mid, "everyone")
        self.fails("invalid_state", db_messages.msg_pin, self.a, self.g, mid, True)
        # only_admins_post: forbidden for a member (step 6) before the object-state checks (step 7)
        live = self.mid(self.send(self.a, self.g, "live"))
        self.write(db_chats.chat_update, self.a, self.g, None, None, True)
        self.fails("forbidden", db_messages.msg_pin, self.b, self.g, system, True)
        self.write(db_messages.msg_pin, self.a, self.g, live, True)

    def test_direct_chat_members_can_both_pin_and_a_disabled_peer_blocks(self) -> None:
        mid = self.mid(self.send(self.a, self.dm_ab, "x"))
        self.write(db_messages.msg_pin, self.b, self.dm_ab, mid, True)
        self.set_user(self.b, disabled=1)
        self.fails("invalid_state", db_messages.msg_pin, self.a, self.dm_ab, mid, False)

    def test_deleting_a_pinned_message_unpins_it_for_everyone(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "x"))
        self.write(db_messages.msg_pin, self.a, self.g, mid, True)
        self.write(db_messages.msg_delete, self.a, mid, "everyone")
        self.assertEqual(self.chat_of(self.c, self.g)["pinned_message_ids"], [])


if __name__ == "__main__":
    unittest.main()
