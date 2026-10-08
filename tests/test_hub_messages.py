"""Message and receipt requests over the real server: matrix, ordering, idempotence, cancel-safety (SPEC 7.3 - 7.6)."""

from __future__ import annotations

import base64
import os
import time
import unittest
from typing import Any

try:
    from . import test_hub_support as support
except ImportError:
    import test_hub_support as support

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def of_chat(chat_id: int) -> Any:
    return lambda f: (
        chat_id in (f["d"].get("chat_id"), f["d"].get("message", {}).get("chat_id"), f["d"].get("chat", {}).get("id"))
    )


class SendTests(support.HubTestCase):
    def test_send_fans_out_before_the_res_and_only_to_members(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        marks = {s.name: s.mark() for s in (a, b, c)}
        res = self.send(a, chat_id, "hello group")
        message = res["d"]["message"]
        self.assertEqual(message["sender_id"], a.me["id"])
        self.assertEqual(message["status"], "sent")
        for member in (a, b):
            ev = member.wait_event(
                "ev.message", lambda f: f["d"]["message"]["body"] == "hello group", since=marks[member.name]
            )
            self.assertLess(ev.seq, res.seq, "events precede the res (requester included)")
            self.assertEqual(ev["d"]["message"]["id"], message["id"])
        own = a.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "hello group")
        other = b.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "hello group")
        self.assertEqual(own["d"]["message"]["client_id"], message["client_id"])
        self.assertIsNone(other["d"]["message"]["client_id"])
        self.assertIsNone(other["d"]["message"]["status"])
        # the sender's own marks moved: receipt + read_sync to the sender, ordered after the message and before the res
        receipt = a.wait_event("ev.receipt", of_chat(chat_id), since=marks[a.name])
        sync = a.wait_event("ev.read_sync", of_chat(chat_id), since=marks[a.name])
        self.assertLess(own.seq, receipt.seq)
        self.assertLess(receipt.seq, sync.seq)
        self.assertLess(sync.seq, res.seq)
        self.assertEqual(set(receipt["d"]), {"chat_id", "user_id", "delivered_up_to", "read_up_to"})
        c.expect_none(None, of_chat(chat_id), since=marks[c.name])

    def test_two_quick_sends_from_one_connection_get_ascending_ids_in_order(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        ids = [
            a.request_nowait("msg.send", {"chat_id": chat_id, "client_id": "fifo-%04d-%d" % (i, i), "body": "m%d" % i})
            for i in range(25)
        ]
        results = [a.wait_res(rid) for rid in ids]
        message_ids = [r["d"]["message"]["id"] for r in results]
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(message_ids, sorted(message_ids))
        self.assertEqual([r["d"]["message"]["body"] for r in results], ["m%d" % i for i in range(25)])
        self.barrier(b)
        arrived = [
            e["d"]["message"]["id"]
            for _, e in b.events
            if e["t"] == "ev.message" and e["d"]["message"]["chat_id"] == chat_id
        ]
        self.assertEqual(arrived[-25:], message_ids)

    def test_two_connections_racing_the_same_client_id_create_one_message(self) -> None:
        a, b = self.new_user(), self.new_user()
        tab = self.another_tab(a)
        chat_id = self.make_group(a, b)
        for round_ in range(10):
            cid = "race-%04d-xyz" % round_
            first = a.request_nowait("msg.send", {"chat_id": chat_id, "client_id": cid, "body": "race %d" % round_})
            second = tab.request_nowait("msg.send", {"chat_id": chat_id, "client_id": cid, "body": "race %d" % round_})
            r1, r2 = a.wait_res(first), tab.wait_res(second)
            self.assertTrue(r1["ok"] and r2["ok"], (r1, r2))
            self.assertEqual(r1["d"]["message"]["id"], r2["d"]["message"]["id"])
        self.barrier(b)
        bodies = [
            e["d"]["message"]["body"]
            for _, e in b.events
            if e["t"] == "ev.message"
            and e["d"]["message"]["chat_id"] == chat_id
            and e["d"]["message"]["kind"] == "text"
        ]
        self.assertEqual(sorted(bodies), sorted("race %d" % i for i in range(10)))

    def test_a_reused_client_id_in_another_chat_conflicts(self) -> None:
        a, b = self.new_user(), self.new_user()
        one, two = self.make_group(a, b, title="One"), self.make_group(a, b, title="Two")
        cid = "reuse-0001-aaaa"
        self.assertTrue(a.request("msg.send", {"chat_id": one, "client_id": cid, "body": "x"})["ok"])
        self.assertErr(a.request("msg.send", {"chat_id": two, "client_id": cid, "body": "x"}), "conflict")

    def test_the_request_survives_a_closing_socket(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        a.request_nowait("msg.send", {"chat_id": chat_id, "client_id": "cancel-safe-1", "body": "sent then gone"})
        a.abort()
        ev = b.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "sent then gone")
        history = b.request("chat.history", {"chat_id": chat_id})
        self.assertEqual(
            [m["id"] for m in history["d"]["messages"] if m["body"] == "sent then gone"], [ev["d"]["message"]["id"]]
        )
        # a retry after the lost res returns the same message and emits nothing
        again = self.another_tab(a)
        mark = b.mark()
        res = again.request("msg.send", {"chat_id": chat_id, "client_id": "cancel-safe-1", "body": "sent then gone"})
        self.assertEqual(res["d"]["message"]["id"], ev["d"]["message"]["id"])
        self.barrier(b)
        b.expect_none("ev.message", since=mark)

    def test_send_errors_follow_the_evaluation_order(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        self.assertErr(
            c.request("msg.send", {"chat_id": chat_id, "client_id": "err-000001", "body": "x"}), "not_member"
        )
        self.assertErr(a.request("msg.send", {"chat_id": 999999, "client_id": "err-000002", "body": "x"}), "not_member")
        self.assertErr(
            a.request("msg.send", {"chat_id": chat_id, "client_id": "err-000003", "body": "   "}), "bad_request"
        )
        self.assertErr(
            a.request("msg.send", {"chat_id": chat_id, "client_id": "err-000004", "body": "x" * 9000}), "too_large"
        )
        self.assertErr(
            a.request("msg.send", {"chat_id": chat_id, "client_id": "err-000005", "body": "x", "reply_to_id": 999999}),
            "not_found",
        )
        self.assertErr(
            a.request(
                "msg.send", {"chat_id": chat_id, "client_id": "err-000006", "body": "x", "attachment_id": "0" * 32}
            ),
            "not_found",
        )
        # not_member beats a bad body; a bad shape beats a missing chat
        self.assertErr(
            c.request("msg.send", {"chat_id": chat_id, "client_id": "err-000007", "body": "   "}), "not_member"
        )
        self.assertErr(c.request("msg.send", {"chat_id": chat_id, "client_id": "short", "body": "x"}), "bad_request")
        self.assertTrue(a.request("chat.update", {"chat_id": chat_id, "only_admins_post": True})["ok"])
        self.assertErr(b.request("msg.send", {"chat_id": chat_id, "client_id": "err-000008", "body": "x"}), "forbidden")
        self.assertTrue(
            a.request("msg.send", {"chat_id": chat_id, "client_id": "err-000009", "body": "admins may"})["ok"]
        )

    def test_an_attachment_message_and_the_release_of_its_file_on_delete(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        uploaded = a.upload("pixel.png", PNG)
        self.assertEqual(uploaded.status, 201, uploaded)
        attachment = uploaded.json["attachment"]
        res = a.request("msg.send", {"chat_id": chat_id, "client_id": "attach-0001", "attachment_id": attachment["id"]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["d"]["message"]["kind"], "image")
        self.assertEqual(res["d"]["message"]["attachment"]["id"], attachment["id"])
        path = os.path.join(self.srv.data_dir, "uploads", attachment["id"][:2], attachment["id"])
        self.assertTrue(os.path.exists(path))
        self.assertErr(
            b.request("msg.send", {"chat_id": chat_id, "client_id": "attach-0002", "attachment_id": attachment["id"]}),
            "not_found",
        )
        deleted = a.request("msg.delete", {"message_id": res["d"]["message"]["id"], "scope": "everyone"})
        self.assertTrue(deleted["ok"], deleted)
        self.assertTrue(deleted["d"]["message"]["deleted"])
        deadline = time.monotonic() + 5
        while os.path.exists(path) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(os.path.exists(path), "the file is deleted after COMMIT")


class MutationTests(support.HubTestCase):
    def group_with_message(self, *others: Any) -> Any:
        a = self.new_user()
        chat_id = self.make_group(a, *others)
        message = self.send(a, chat_id, "original text")["d"]["message"]
        return a, chat_id, message

    def test_edit_reaches_every_member_and_noop_emits_nothing(self) -> None:
        b, c = self.new_user(), self.new_user()
        a, chat_id, message = self.group_with_message(b, c)
        marks = {s.name: s.mark() for s in (a, b, c)}
        res = a.request("msg.edit", {"message_id": message["id"], "body": "edited text"})
        self.assertTrue(res["ok"], res)
        self.assertIsNotNone(res["d"]["message"]["edited_at"])
        for member in (a, b, c):
            ev = member.wait_event("ev.message_update", since=marks[member.name])
            self.assertEqual(ev["d"]["message"]["body"], "edited text")
        self.assertLess(a.wait_event("ev.message_update", since=marks[a.name]).seq, res.seq)
        mark = b.mark()
        same = a.request("msg.edit", {"message_id": message["id"], "body": "edited text"})
        self.assertEqual(same["d"]["message"]["edited_at"], res["d"]["message"]["edited_at"])
        self.barrier(b)
        b.expect_none("ev.message_update", since=mark)
        self.assertErr(b.request("msg.edit", {"message_id": message["id"], "body": "mine now"}), "forbidden")
        self.assertErr(a.request("msg.edit", {"message_id": 999999, "body": "x"}), "not_found")
        outsider = self.new_user()
        self.assertErr(outsider.request("msg.edit", {"message_id": message["id"], "body": "x"}), "not_found")

    def test_system_messages_cannot_be_edited_deleted_or_inspected(self) -> None:
        a = self.new_user()
        chat_id = self.make_group(a)
        system = a.request("chat.get", {"chat_id": chat_id})["d"]["chat"]["last_message"]
        self.assertEqual(system["kind"], "system")
        self.assertErr(a.request("msg.edit", {"message_id": system["id"], "body": "x"}), "invalid_state")
        self.assertErr(a.request("msg.info", {"message_id": system["id"]}), "invalid_state")
        self.assertErr(a.request("msg.delete", {"message_id": system["id"], "scope": "everyone"}), "invalid_state")
        self.assertErr(a.request("msg.delete", {"message_id": system["id"], "scope": "me"}), "invalid_state")
        self.assertErr(a.request("msg.react", {"message_id": system["id"], "emoji": "\U0001f44d"}), "invalid_state")

    def test_delete_for_everyone_and_for_me(self) -> None:
        b, c = self.new_user(), self.new_user()
        a, chat_id, message = self.group_with_message(b, c)
        marks = {s.name: s.mark() for s in (a, b, c)}
        res = b.request("msg.delete", {"message_id": message["id"], "scope": "me"})
        self.assertEqual(res["d"], {})
        removed = b.wait_event("ev.message_removed", since=marks[b.name])
        self.assertEqual(removed["d"], {"chat_id": chat_id, "message_ids": [message["id"]]})
        self.barrier(a, c)
        a.expect_none("ev.message_removed", since=marks[a.name])
        c.expect_none("ev.message_removed", since=marks[c.name])
        self.assertEqual(b.request("msg.delete", {"message_id": message["id"], "scope": "me"})["d"], {})  # idempotent
        # c has it unread: delete for everyone re-sends c's counters
        marks = {s.name: s.mark() for s in (a, b, c)}
        res = a.request("msg.delete", {"message_id": message["id"], "scope": "everyone"})
        self.assertTrue(res["d"]["message"]["deleted"])
        self.assertEqual(res["d"]["message"]["body"], "")
        sync = c.wait_event("ev.read_sync", of_chat(chat_id), since=marks[c.name])
        self.assertEqual(sync["d"]["unread"], 0)
        c.wait_event("ev.message_update", lambda f: f["d"]["message"]["deleted"], since=marks[c.name])
        b.expect_none("ev.message_update", since=marks[b.name])  # b hid it: not visible, so no update
        again = a.request("msg.delete", {"message_id": message["id"], "scope": "everyone"})
        self.assertTrue(again["ok"])
        self.assertErr(b.request("msg.delete", {"message_id": message["id"], "scope": "everyone"}), "not_found")

    def test_react_set_semantics_and_validation(self) -> None:
        b = self.new_user()
        a, chat_id, message = self.group_with_message(b)
        mark = b.mark()
        thumbs = "\U0001f44d"
        res = b.request("msg.react", {"message_id": message["id"], "emoji": thumbs})
        self.assertEqual(res["d"]["message"]["reactions"], [{"emoji": thumbs, "user_ids": [b.me["id"]]}])
        a.wait_event("ev.message_update", lambda f: f["d"]["message"]["reactions"], since=0)
        mark = a.mark()
        again = b.request("msg.react", {"message_id": message["id"], "emoji": thumbs})  # same emoji: no-op
        self.assertTrue(again["ok"])
        self.barrier(a)
        a.expect_none("ev.message_update", since=mark)
        cleared = b.request("msg.react", {"message_id": message["id"], "emoji": None})
        self.assertEqual(cleared["d"]["message"]["reactions"], [])
        self.assertErr(b.request("msg.react", {"message_id": message["id"], "emoji": "hello"}), "bad_request")
        self.assertErr(b.request("msg.react", {"message_id": message["id"], "emoji": ""}), "bad_request")

    def test_star_is_private(self) -> None:
        b = self.new_user()
        a, chat_id, message = self.group_with_message(b)
        tab = self.another_tab(b)
        marks = {s.name: s.mark() for s in (a, b, tab)}
        res = b.request("msg.star", {"message_id": message["id"], "starred": True})
        self.assertTrue(res["d"]["message"]["starred"])
        tab.wait_event("ev.message_update", lambda f: f["d"]["message"]["starred"], since=marks[tab.name])
        self.barrier(a)
        a.expect_none("ev.message_update", since=marks[a.name])
        listed = b.request("msg.starred", {})
        self.assertIn(message["id"], [m["id"] for m in listed["d"]["messages"]])
        self.assertEqual(a.request("msg.starred", {"chat_id": chat_id})["d"]["messages"], [])
        self.assertErr(a.request("msg.starred", {"chat_id": 999999}), "not_member")

    def test_pin_event_order_and_limits(self) -> None:
        b = self.new_user()
        a, chat_id, message = self.group_with_message(b)
        mark = b.mark()
        res = a.request("msg.pin", {"chat_id": chat_id, "message_id": message["id"], "pinned": True})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["d"]["chat"]["pinned_message_ids"], [message["id"]])
        update = b.wait_event("ev.message_update", since=mark)
        chat_update = b.wait_event("ev.chat_update", of_chat(chat_id), since=mark)
        system = b.wait_event(
            "ev.message",
            lambda f: f["d"]["message"]["system"] and f["d"]["message"]["system"]["event"] == "pinned",
            since=mark,
        )
        self.assertTrue(update.seq < chat_update.seq < system.seq)
        self.assertEqual(system["d"]["message"]["system"]["message_id"], message["id"])
        mark = b.mark()
        self.assertTrue(
            a.request("msg.pin", {"chat_id": chat_id, "message_id": message["id"], "pinned": True})["ok"]
        )  # no-op
        self.barrier(b)
        b.expect_none(None, lambda f: f["t"] != "ev.presence", since=mark)
        other = self.make_group(a, b, title="Other")
        elsewhere = self.send(a, other, "other chat")["d"]["message"]
        self.assertErr(
            a.request("msg.pin", {"chat_id": chat_id, "message_id": elsewhere["id"], "pinned": True}), "not_found"
        )
        for i in range(5):
            extra = self.send(a, chat_id, "pin me %d" % i)["d"]["message"]
            res = a.request("msg.pin", {"chat_id": chat_id, "message_id": extra["id"], "pinned": True})
            if i < 4:
                self.assertTrue(res["ok"], res)
            else:
                self.assertErr(res, "invalid_state", "pin_limit")

    def test_a_late_joiner_gets_no_update_for_a_message_from_before_the_join(self) -> None:
        b, late = self.new_user(), self.new_user()
        a, chat_id, message = self.group_with_message(b)
        self.assertTrue(a.request("chat.add_members", {"chat_id": chat_id, "user_ids": [late.me["id"]]})["ok"])
        mark = late.mark()
        self.assertTrue(b.request("msg.react", {"message_id": message["id"], "emoji": "\U0001f44d"})["ok"])
        self.assertTrue(a.request("msg.edit", {"message_id": message["id"], "body": "edited later"})["ok"])
        a.wait_event("ev.message_update", lambda f: f["d"]["message"]["body"] == "edited later")
        self.barrier(late)
        late.expect_none("ev.message_update", since=mark)
        self.assertErr(late.request("msg.info", {"message_id": message["id"]}), "not_found")

    def test_forward_is_atomic_idempotent_and_reports_the_culprit(self) -> None:
        b = self.new_user()
        a, source_chat, message = self.group_with_message(b)
        one, two = self.make_group(a, title="F1"), self.make_group(a, b, title="F2")
        marks = {s.name: s.mark() for s in (a, b)}
        res = a.request(
            "msg.forward", {"message_ids": [message["id"]], "chat_ids": [two, one], "client_id": "fwd-0000001"}
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual([m["chat_id"] for m in res["d"]["messages"]], [two, one])  # creation order = chat_ids order
        self.assertTrue(all(m["forwarded"] for m in res["d"]["messages"]))
        b.wait_event("ev.message", lambda f: f["d"]["message"]["forwarded"], since=marks[b.name])
        mark = b.mark()
        retry = a.request(
            "msg.forward", {"message_ids": [message["id"]], "chat_ids": [two, one], "client_id": "fwd-0000001"}
        )
        self.assertEqual([m["id"] for m in retry["d"]["messages"]], [m["id"] for m in res["d"]["messages"]])
        self.barrier(b)
        b.expect_none("ev.message", since=mark)
        outsider_chat = self.make_group(self.new_user(), title="Foreign")
        err = self.assertErr(
            a.request(
                "msg.forward",
                {"message_ids": [message["id"]], "chat_ids": [two, outsider_chat], "client_id": "fwd-0000002"},
            ),
            "not_member",
        )
        self.assertEqual(err["chat_id"], outsider_chat)
        err = self.assertErr(
            a.request(
                "msg.forward", {"message_ids": [message["id"], 999999], "chat_ids": [two], "client_id": "fwd-0000003"}
            ),
            "not_found",
        )
        self.assertEqual(err["message_id"], 999999)
        self.barrier(b)
        history = b.request("chat.history", {"chat_id": two})["d"]["messages"]
        self.assertEqual(len([m for m in history if m["forwarded"]]), 1)  # nothing created by the failed requests

    def test_concurrent_forwards_to_the_same_chats_in_opposite_order_do_not_deadlock(self) -> None:
        a, b = self.new_user(), self.new_user()
        one, two = self.make_group(a, b, title="L1"), self.make_group(a, b, title="L2")
        message = self.send(a, one, "to forward")["d"]["message"]
        tab = self.another_tab(a)
        pending = []
        for i in range(8):
            pending.append(
                (
                    a,
                    a.request_nowait(
                        "msg.forward",
                        {"message_ids": [message["id"]], "chat_ids": [one, two], "client_id": "dl-a-%05d" % i},
                    ),
                )
            )
            pending.append(
                (
                    tab,
                    tab.request_nowait(
                        "msg.forward",
                        {"message_ids": [message["id"]], "chat_ids": [two, one], "client_id": "dl-b-%05d" % i},
                    ),
                )
            )
        for session, rid in pending:
            self.assertTrue(session.wait_res(rid, timeout=10)["ok"])

    def test_info_search_and_shared(self) -> None:
        b = self.new_user()
        a, chat_id, message = self.group_with_message(b)
        self.send(a, chat_id, "look at https://example.org/page now")
        self.send(a, chat_id, "100% sure_thing")
        info = a.request("msg.info", {"message_id": message["id"]})
        self.assertEqual([r["user_id"] for r in info["d"]["recipients"]], [b.me["id"]])
        self.assertErr(b.request("msg.info", {"message_id": message["id"]}), "forbidden")
        found = b.request("msg.search", {"q": "ORIGINAL", "chat_id": chat_id})
        self.assertEqual([r["message"]["id"] for r in found["d"]["results"]], [message["id"]])
        self.assertEqual(found["d"]["total"], 1)
        escaped = b.request("msg.search", {"q": "100%", "chat_id": chat_id})
        self.assertEqual(len(escaped["d"]["results"]), 1)
        self.assertEqual(len(b.request("msg.search", {"q": "%%", "chat_id": chat_id})["d"]["results"]), 0)
        self.assertErr(b.request("msg.search", {"q": "x"}), "bad_request")
        self.assertErr(b.request("msg.search", {"q": "okay", "chat_id": 999999}), "not_member")
        links = b.request("msg.shared", {"chat_id": chat_id, "kind": "links"})
        self.assertEqual(len(links["d"]["messages"]), 1)
        self.assertErr(self.new_user().request("msg.shared", {"chat_id": chat_id, "kind": "media"}), "not_member")

    def test_history_paging(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        sent = [self.send(a, chat_id, "page %02d" % i)["d"]["message"]["id"] for i in range(12)]
        newest = a.request("chat.history", {"chat_id": chat_id, "limit": 5})["d"]
        self.assertEqual([m["id"] for m in newest["messages"]], sent[-5:])
        self.assertTrue(newest["has_more_before"])
        self.assertFalse(newest["has_more_after"])
        older = a.request("chat.history", {"chat_id": chat_id, "before_id": sent[-5], "limit": 5})["d"]
        self.assertEqual([m["id"] for m in older["messages"]], sent[-10:-5])
        after = a.request("chat.history", {"chat_id": chat_id, "after_id": 0, "limit": 100})["d"]
        self.assertEqual([m["id"] for m in after["messages"]][-12:], sent)
        around = a.request("chat.history", {"chat_id": chat_id, "around_id": sent[6], "limit": 5})["d"]
        self.assertEqual([m["id"] for m in around["messages"]], sent[4:9])
        self.assertErr(a.request("chat.history", {"chat_id": chat_id, "around_id": 999999}), "not_found")
        self.assertErr(self.new_user().request("chat.history", {"chat_id": chat_id}), "not_member")


class ReceiptTests(support.HubTestCase):
    def test_receipts_are_routed_to_authors_only_and_precede_the_res(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        chat_id = self.make_group(a, b, c)
        message = self.send(a, chat_id, "who reads this")["d"]["message"]
        self.send(c, chat_id, "a message of c")  # c is an author too
        marks = {s.name: s.mark() for s in (a, b, c)}
        res = b.request("receipt.read", {"chat_id": chat_id, "up_to_id": message["id"]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["d"]["chat_id"], chat_id)
        self.assertEqual(res["d"]["last_read_id"], message["id"])
        mine = b.wait_event("ev.receipt", of_chat(chat_id), since=marks[b.name])
        theirs = a.wait_event("ev.receipt", of_chat(chat_id), since=marks[a.name])
        self.assertLess(mine.seq, res.seq)
        self.assertEqual(
            theirs["d"],
            {"chat_id": chat_id, "user_id": b.me["id"], "delivered_up_to": message["id"], "read_up_to": message["id"]},
        )
        self.barrier(c)
        c.expect_none("ev.receipt", since=marks[c.name])  # c authored nothing in the advanced range
        sync = b.wait_event("ev.read_sync", of_chat(chat_id), since=marks[b.name])
        self.assertEqual(sync["d"]["unread"], res["d"]["unread"])
        # nothing advanced: no events
        mark = a.mark()
        again = b.request("receipt.read", {"chat_id": chat_id, "up_to_id": message["id"]})
        self.assertTrue(again["ok"])
        self.barrier(a)
        a.expect_none("ev.receipt", since=mark)
        # a client can never pre-read the future
        future = b.request("receipt.read", {"chat_id": chat_id, "up_to_id": 10**9})
        self.assertTrue(future["ok"])
        self.assertLessEqual(future["d"]["last_read_id"], future["d"]["last_message_id"])

    def test_delivered_single_and_batched_and_the_not_member_chat_id(self) -> None:
        a, b = self.new_user(), self.new_user()
        one, two = self.make_group(a, b, title="D1"), self.make_group(a, b, title="D2")
        m1, m2 = self.send(a, one, "one")["d"]["message"], self.send(a, two, "two")["d"]["message"]
        mark = a.mark()
        res = b.request(
            "receipt.delivered",
            {"items": [{"chat_id": one, "up_to_id": m1["id"]}, {"chat_id": two, "up_to_id": m2["id"]}]},
        )
        self.assertEqual(res["d"], {})
        events = (
            a.wait_event("ev.receipt", of_chat(one), since=mark),
            a.wait_event("ev.receipt", of_chat(two), since=mark),
        )
        self.assertEqual([e["d"]["delivered_up_to"] for e in events], [m1["id"], m2["id"]])
        self.assertTrue(all(e["d"]["read_up_to"] < e["d"]["delivered_up_to"] for e in events))
        stranger = self.new_user()
        err = self.assertErr(
            stranger.request("receipt.delivered", {"items": [{"chat_id": one, "up_to_id": 1}]}), "not_member"
        )
        self.assertEqual(err["chat_id"], one)
        err = self.assertErr(stranger.request("receipt.read", {"chat_id": two, "up_to_id": 1}), "not_member")
        self.assertEqual(err["chat_id"], two)

    def test_the_status_of_a_message_goes_sent_delivered_read(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        b.abort()
        message = self.send(a, chat_id, "while b is offline")["d"]["message"]
        self.assertEqual(message["status"], "sent")
        again = b.clone("b2")
        self.addCleanup(again.abort)
        again.connect()
        chat = next(c for c in again.ready["chats"] if c["id"] == chat_id)
        self.assertEqual(chat["me"]["unread"], 1)
        mark = a.mark()
        self.assertTrue(again.request("receipt.delivered", {"chat_id": chat_id, "up_to_id": message["id"]})["ok"])
        a.wait_event(
            "ev.receipt",
            lambda f: f["d"]["user_id"] == b.me["id"] and f["d"]["delivered_up_to"] == message["id"],
            since=mark,
        )
        self.assertTrue(again.request("receipt.read", {"chat_id": chat_id, "up_to_id": message["id"]})["ok"])
        a.wait_event(
            "ev.receipt",
            lambda f: f["d"]["user_id"] == b.me["id"] and f["d"]["read_up_to"] == message["id"],
            since=mark,
        )


class ConcurrencyTests(support.HubTestCase):
    scale = 0.1

    def test_a_second_concurrent_search_is_rate_limited_and_waiting_is_bounded(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        self.send(a, chat_id, "searchable words")
        sem = self.hub._search_sem
        self.srv.call(sem.acquire())  # an unrelated search holds the global semaphore
        try:
            first = a.request_nowait("msg.search", {"q": "searchable"})
            time.sleep(0.05)
            err = self.assertErr(a.request("msg.search", {"q": "searchable"}), "rate_limited")
            self.assertEqual(err["retry_after"], 1.0)
            busy = a.wait_res(first, timeout=5)  # waiting for the semaphore is bounded (5 s * 0.1)
            err = self.assertErr(busy, "server_busy")
            self.assertEqual(err["retry_after"], 2.0)
        finally:
            self.srv.run(sem.release)
        self.assertTrue(a.request("msg.search", {"q": "searchable"})["ok"])


if __name__ == "__main__":
    unittest.main()
