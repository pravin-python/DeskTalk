"""msg.send of ``chatd/db_messages.py``: algorithm order, dedupe, mentions, sender marks, archived reset (SPEC 7.4)."""

from __future__ import annotations

import threading
import unittest
from typing import Any, List
from unittest import mock

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db, db_chats, db_messages, db_receipts


class SendTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")
        self.g = self.group(self.a, self.b, self.c)

    def test_variants_and_event_order(self) -> None:
        out = self.send(self.a, self.g, "  hello  ", client_id="client-0001")
        message = out["res"]["message"]
        self.assertEqual(
            (message["body"], message["kind"], message["client_id"], message["status"]),
            ("hello", "text", "client-0001", "sent"),
        )
        self.assertEqual(self.types(out), ["ev.message", "ev.receipt", "ev.read_sync"])
        seen_by_b = self.payloads(out, "ev.message", self.b)[0]["message"]
        self.assertIsNone(seen_by_b["client_id"])
        self.assertIsNone(seen_by_b["status"])
        self.assertEqual(self.payloads(out, "ev.message", self.a)[0]["message"]["client_id"], "client-0001")
        groups = out["events"][0]["groups"]
        self.assertEqual(sorted(sorted(g["user_ids"]) for g in groups), [[self.a], [self.b, self.c]])
        self.assertEqual({(r["user_id"]) for r in out["recipients"]}, {self.a, self.b, self.c})
        self.assertEqual(out["sender_status"], "sent")
        self.assertIsNone(out["quoted"])
        self.assertFalse(out["deduped"])
        self.assertEqual(out["read_sync"][self.a]["unread"], 0)
        self.assertEqual(out["receipt"]["audience"], [self.a])
        self.assertEqual(out["receipt"]["delivered_up_to"], message["id"])
        # the same message through the variant helper the hub uses
        variants = db_messages.serialize_message_variants(
            out["parts"], [(True, False, "none"), (False, False, "none")], "sent"
        )
        self.assertEqual(variants[(True, False, "none")]["client_id"], "client-0001")
        self.assertIsNone(variants[(False, False, "none")]["client_id"])

    def test_ids_and_created_at_are_monotonic_even_if_the_clock_steps_back(self) -> None:
        first = self.send(self.a, self.g, "one")["res"]["message"]
        self.clock.advance(-1000)
        second = self.send(self.a, self.g, "two")["res"]["message"]
        self.assertGreater(second["id"], first["id"])
        self.assertGreaterEqual(second["created_at"], first["created_at"])

    def test_dedupe_returns_the_existing_message_without_events(self) -> None:
        first = self.send(self.a, self.g, "once", client_id="client-dupe1")
        again = self.send(self.a, self.g, "different body", client_id="client-dupe1")
        self.assertTrue(again["deduped"])
        self.assertEqual(again["events"], [])
        self.assertEqual(again["res"]["message"]["id"], first["res"]["message"]["id"])
        self.assertEqual(again["res"]["message"]["body"], "once")
        self.assertEqual(again["res"]["message"]["client_id"], "client-dupe1")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM messages WHERE client_id = 'client-dupe1'"), 1)

    def test_dedupe_after_the_message_was_deleted_still_answers(self) -> None:
        first = self.send(self.a, self.g, "once", client_id="client-dupe2")
        self.write(db_messages.msg_delete, self.a, self.mid(first), "everyone")
        again = self.send(self.a, self.g, "once", client_id="client-dupe2")
        self.assertTrue(again["deduped"])
        self.assertTrue(again["res"]["message"]["deleted"])

    def test_client_id_for_another_chat_conflicts(self) -> None:
        self.send(self.a, self.g, "once", client_id="client-dupe3")
        other = self.group(self.a, self.b)
        self.fails("conflict", db_messages.msg_send, self.a, other, "client-dupe3", "x")
        # another user may reuse the same client_id
        self.send(self.b, self.g, "mine", client_id="client-dupe3")

    def test_unique_index_race_is_answered_with_the_existing_row(self) -> None:
        first = self.send(self.a, self.g, "once", client_id="client-race1")
        real = db_messages._existing_by_client_id
        calls = []

        def blind_first(conn: Any, sender: int, client_id: str) -> Any:
            calls.append(client_id)
            return None if len(calls) == 1 else real(conn, sender, client_id)

        with mock.patch.object(db_messages, "_existing_by_client_id", blind_first):
            again = self.send(self.a, self.g, "once", client_id="client-race1")
        self.assertTrue(again["deduped"])
        self.assertEqual(again["res"]["message"]["id"], first["res"]["message"]["id"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM messages WHERE client_id = 'client-race1'"), 1)

    def test_second_connection_sees_the_committed_message(self) -> None:
        first = self.send(self.a, self.g, "once", client_id="client-race2")
        other = db.open_connection(self.path, "extra")
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        out = db_messages.msg_send(other, self.a, self.g, "client-race2", "again")
        other.execute("COMMIT")
        self.assertTrue(out["deduped"])
        self.assertEqual(out["res"]["message"]["id"], first["res"]["message"]["id"])

    def test_validation_and_evaluation_order(self) -> None:
        send = db_messages.msg_send
        self.fails("bad_request", send, self.a, self.g, "short", "x")
        self.fails("bad_request", send, self.a, self.g, "bad id!!!!", "x")
        self.fails("bad_request", send, self.a, self.g, "client-0002", 5)
        self.fails("bad_request", send, self.a, True, "client-0002", "x")
        self.fails("bad_request", send, self.a, self.g, "client-0002", "x", 5)
        self.fails("bad_request", send, self.a, self.g, "client-0002", "x", None, "0")
        self.fails("bad_request", send, self.a, self.g, "client-0002", "x", None, None, -1)
        self.fails("bad_request", send, self.a, self.g, "client-0002", "   ")
        self.fails("bad_request", send, self.a, self.g, "client-0002", "\x00\x01\r")
        self.fails("bad_request", send, self.a, self.g, "client-0002", "x\ud800")
        self.fails("too_large", send, self.a, self.g, "client-0002", "y" * 8001)
        # not_member (step 2) precedes the body checks (step 7)
        self.fails("not_member", send, self.a, 9999, "client-0002", "y" * 9000)
        self.set_user(self.a, disabled=1)
        self.fails("unauthorized", send, self.a, 9999, "client-0002", "x")

    def test_body_normalisation(self) -> None:
        body = "\x00 hi\x07 there\r\n\tsecond line \x1b\n"
        message = self.send(self.a, self.g, body)["res"]["message"]
        self.assertEqual(message["body"], "hi there\n\tsecond line")
        db_chats.configure_limits(max_body_chars=5)
        self.send(self.a, self.g, "12345")
        self.fails("too_large", db_messages.msg_send, self.a, self.g, "client-0003", "123456")

    def test_only_admins_post_and_disabled_peer(self) -> None:
        self.write(db_chats.chat_update, self.a, self.g, None, None, True)
        self.fails("forbidden", db_messages.msg_send, self.b, self.g, "client-0004", "x")
        self.send(self.a, self.g, "admins may post")
        direct = self.dm(self.a, self.b)
        self.send(self.a, direct, "hi")
        self.set_user(self.b, disabled=1)
        self.fails("invalid_state", db_messages.msg_send, self.a, direct, "client-0005", "x")

    def test_reply_rules(self) -> None:
        target = self.mid(self.send(self.b, self.g, "x" * 250))
        reply = self.send(self.a, self.g, "re", reply_to_id=target)["res"]["message"]
        self.assertEqual(reply["reply_to"]["id"], target)
        self.assertEqual(reply["reply_to"]["body"], "x" * 200)
        self.assertEqual((reply["reply_to"]["deleted"], reply["reply_to"]["unavailable"]), (False, False))
        self.assertEqual(reply["reply_to"]["sender_id"], self.b)
        other = self.mid(self.send(self.a, self.group(self.a, self.b), "elsewhere"))
        self.fails("not_found", db_messages.msg_send, self.a, self.g, "client-0006", "x", None, other)
        self.fails("not_found", db_messages.msg_send, self.a, self.g, "client-0006", "x", None, 99999)
        system = self.scalar("SELECT id FROM messages WHERE chat_id = ? AND kind = 'system'", (self.g,))
        self.fails("invalid_state", db_messages.msg_send, self.a, self.g, "client-0006", "x", None, system)
        self.write(db_messages.msg_delete, self.b, target, "everyone")
        self.fails("invalid_state", db_messages.msg_send, self.a, self.g, "client-0006", "x", None, target)
        # a message the sender hid is not visible: not_found
        hidden = self.mid(self.send(self.b, self.g, "hide me"))
        self.write(db_messages.msg_delete, self.a, hidden, "me")
        self.fails("not_found", db_messages.msg_send, self.a, self.g, "client-0006", "x", None, hidden)

    def test_reply_preview_names_the_quoted_attachment(self) -> None:
        photo = self.mid(self.send(self.b, self.g, "", attachment_id=self.add_attachment(self.b, "image", "cat.png")))
        reply = self.send(self.a, self.g, "cute", reply_to_id=photo)["res"]["message"]["reply_to"]
        self.assertEqual((reply["kind"], reply["body"], reply["attachment_name"]), ("image", "", "cat.png"))

    def test_attachment_rules(self) -> None:
        mine = self.add_attachment(self.a, "image")
        theirs = self.add_attachment(self.b, "file", "doc.pdf")
        out = self.send(self.a, self.g, "", attachment_id=mine)
        message = out["res"]["message"]
        self.assertEqual(
            (message["kind"], message["attachment"]["id"], message["attachment"]["url"]),
            ("image", mine, "/files/" + mine),
        )
        self.assertEqual(message["body"], "")
        self.fails("not_found", db_messages.msg_send, self.a, self.g, "client-0007", "x", mine)
        self.fails("not_found", db_messages.msg_send, self.a, self.g, "client-0007", "x", theirs)
        self.fails("not_found", db_messages.msg_send, self.a, self.g, "client-0007", "x", "f" * 32)
        caption = self.send(self.b, self.g, "see attached", attachment_id=theirs)["res"]["message"]
        self.assertEqual((caption["kind"], caption["body"]), ("file", "see attached"))

    def test_mentions_grammar(self) -> None:
        d = self.user("dave.x")
        self.write(db_chats.chat_add_members, self.a, self.g, [d])
        bodies = {
            "@bob hi": [self.b],
            "hey @BOB and @Carol!": [self.b, self.c],
            "@bob. done": [self.b],
            "(@bob)": [self.b],
            "@bob-": [self.b],
            "mail alice@bob.example": [],
            "@dave.x.": [d],
            "@alice @alice": [],
            "@nobody @zz": [],
            "@bobby": [],
        }
        for body, expected in bodies.items():
            message = self.send(self.a, self.g, body)["res"]["message"]
            self.assertEqual(message["mentions"], expected, body)
        # stored in message_mentions and mirrored in messages.mentions
        last = self.send(self.a, self.g, "@bob @carol")["res"]["message"]
        self.assertEqual(
            self.sql("SELECT user_id FROM message_mentions WHERE message_id = ? ORDER BY user_id", (last["id"],)),
            [(self.b,), (self.c,)],
        )
        self.assertEqual(
            self.scalar("SELECT mentions FROM messages WHERE id = ?", (last["id"],)), "[%d,%d]" % (self.b, self.c)
        )

    def test_mentions_ignore_disabled_nonmembers_and_direct_chats(self) -> None:
        outsider = self.add_user("outsider")
        self.set_user(self.c, disabled=1)
        message = self.send(self.a, self.g, "@bob @carol @outsider")["res"]["message"]
        self.assertEqual(message["mentions"], [self.b])
        direct = self.dm(self.a, self.b)
        self.assertEqual(self.send(self.a, direct, "@bob")["res"]["message"]["mentions"], [])
        self.assertIsNotNone(outsider)

    def test_at_most_twenty_distinct_mentions(self) -> None:
        names = []
        for i in range(25):
            names.append(self.user("member%02d" % i))
        self.write(db_chats.chat_add_members, self.a, self.g, names[:25])
        body = " ".join("@member%02d" % i for i in range(25))
        message = self.send(self.a, self.g, body)["res"]["message"]
        self.assertEqual(len(message["mentions"]), 20)
        self.assertEqual(message["mentions"], sorted(names[:20]))

    def test_mentions_feed_the_counters(self) -> None:
        first = self.send(self.a, self.g, "@bob look")["res"]["message"]
        self.send(self.a, self.g, "no mention")
        me = self.me(self.b, self.g)
        self.assertEqual((me["unread"], me["unread_mentions"], me["first_unread_mention_id"]), (2, 1, first["id"]))
        self.assertEqual(self.me(self.c, self.g)["unread_mentions"], 0)

    def test_sender_marks_never_mark_unseen_messages_read(self) -> None:
        for i in range(60):
            self.send(self.b, self.g, "m%d" % i)
        chat = self.chat_of(self.a, self.g)
        self.assertEqual(chat["me"]["unread"], 60)
        out = self.send(self.a, self.g, "reply without seen_up_to_id")
        self.assertEqual(self.me(self.a, self.g)["unread"], 60)
        member = {m["user_id"]: m for m in self.chat_of(self.a, self.g)["members"]}[self.a]
        self.assertEqual(member["read_up_to"], 0)
        self.assertEqual(member["delivered_up_to"], out["res"]["message"]["id"])
        self.assertEqual(out["read_sync"], {})
        self.assertEqual(self.types(out), ["ev.message", "ev.receipt"])
        # the receipt reaches the authors of the delivered range (b) and the actor
        self.assertEqual(self.audience(out, "ev.receipt"), {self.a, self.b})

    def test_seen_up_to_id_marks_the_seen_prefix(self) -> None:
        ids = [self.mid(self.send(self.b, self.g, "m%d" % i)) for i in range(5)]
        out = self.send(self.a, self.g, "partial", seen_up_to_id=ids[2])
        me = self.me(self.a, self.g)
        self.assertEqual(me["last_read_id"], ids[2])
        self.assertEqual(me["unread"], 2)
        self.assertEqual(out["read_sync"][self.a]["unread"], 2)
        full = self.send(self.a, self.g, "everything", seen_up_to_id=ids[4])
        self.assertEqual(self.me(self.a, self.g)["unread"], 0)
        self.assertEqual(self.me(self.a, self.g)["last_read_id"], full["res"]["message"]["id"])
        # seen_up_to_id beyond the new id is clamped to new_id - 1
        again = self.send(self.a, self.g, "clamp", seen_up_to_id=10**9)
        self.assertEqual(self.me(self.a, self.g)["last_read_id"], again["res"]["message"]["id"])

    def test_sender_marks_respect_read_receipts_off(self) -> None:
        self.set_user(self.a, read_receipts=0)
        out = self.send(self.a, self.g, "private")
        member = {m["user_id"]: m for m in self.chat_of(self.a, self.g)["members"]}[self.a]
        self.assertEqual(member["read_up_to"], 0)
        self.assertEqual(self.me(self.a, self.g)["last_read_id"], out["res"]["message"]["id"])
        self.assertEqual(out["receipt"]["read_up_to"], 0)

    def test_archived_chats_reappear_unless_muted(self) -> None:
        self.write(db_chats.chat_prefs, self.b, self.g, None, None, True)
        self.write(db_chats.chat_prefs, self.c, self.g, 4102444800, None, True)
        out = self.send(self.a, self.g, "wake up")
        self.assertEqual(out["events"][-1]["t"], "ev.chat_update")
        self.assertEqual(self.audience(out, "ev.chat_update"), {self.b})
        self.assertFalse(self.me(self.b, self.g)["archived"])
        self.assertTrue(self.me(self.c, self.g)["archived"])
        self.write(db_chats.chat_prefs, self.a, self.g, None, None, True)
        self.send(self.a, self.g, "my own message")
        self.assertTrue(self.me(self.a, self.g)["archived"])

    def test_receipts_rows_for_the_sender_marks(self) -> None:
        first = self.mid(self.send(self.b, self.g, "from b"))
        self.send(self.a, self.g, "reply", seen_up_to_id=first)
        row = self.sql(
            "SELECT delivered_at, read_at FROM receipts WHERE message_id = ? AND user_id = ?", (first, self.a)
        )
        self.assertTrue(row and row[0][0] is not None and row[0][1] is not None)


class ConcurrencyTest(support.DbChatCase):
    def test_two_connections_racing_the_same_client_id_create_one_message(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        chat_id = self.dm(a, b)
        barrier = threading.Barrier(2)
        results: List[Any] = []

        def racer() -> None:
            conn = db.open_connection(self.path, "extra")
            try:
                barrier.wait(timeout=5)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    results.append(db_messages.msg_send(conn, a, chat_id, "client-thread", "same"))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

        threads = [threading.Thread(target=racer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(r["deduped"] for r in results), [False, True])
        self.assertEqual(results[0]["res"]["message"]["id"], results[1]["res"]["message"]["id"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM messages WHERE client_id = 'client-thread'"), 1)


class DeliveredStatusTest(support.DbChatCase):
    def test_status_progression(self) -> None:
        a = self.user("alice")
        b = self.user("bob")
        chat_id = self.dm(a, b)
        sent = self.send(a, chat_id, "ping")["res"]["message"]
        self.assertEqual(sent["status"], "sent")
        self.write(db_receipts.receipt_delivered, b, [{"chat_id": chat_id, "up_to_id": sent["id"]}])
        self.assertEqual(self.read(db_messages.chat_history, a, chat_id)["messages"][0]["status"], "delivered")
        self.write(db_receipts.receipt_read, b, chat_id, sent["id"])
        self.assertEqual(self.read(db_messages.chat_history, a, chat_id)["messages"][0]["status"], "read")
        self.assertIsNone(self.read(db_messages.chat_history, b, chat_id)["messages"][0]["status"])


if __name__ == "__main__":
    unittest.main()
