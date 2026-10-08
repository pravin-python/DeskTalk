"""Chat requests over the real server: res, recipient matrix, event order and error codes (SPEC 7.3, 7.4, 7.6)."""

from __future__ import annotations

import unittest

try:
    from . import test_hub_support as support
except ImportError:
    import test_hub_support as support


class GroupTests(support.HubTestCase):
    def test_create_group_sends_chat_update_before_the_created_message(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        mark_b, mark_c = b.mark(), c.mark()
        res = a.request(
            "chat.create_group", {"title": "  Core   team ", "member_ids": [b.me["id"], a.me["id"], b.me["id"]]}
        )
        self.assertTrue(res["ok"], res)
        chat = res["d"]["chat"]
        self.assertEqual(chat["title"], "Core team")
        self.assertEqual({m["user_id"] for m in chat["members"]}, {a.me["id"], b.me["id"]})
        self.assertEqual(chat["last_message"]["kind"], "system")
        self.assertEqual(chat["last_message"]["system"]["event"], "created")
        # events before res (requester included), chat_update before the system message
        order = [
            e["t"]
            for _, e in a.events
            if e.get("d", {}).get("chat", {}).get("id") == chat["id"]
            or e.get("d", {}).get("message", {}).get("chat_id") == chat["id"]
        ]
        self.assertEqual(order, ["ev.chat_update", "ev.message"])
        first = a.wait_event("ev.chat_update", lambda f: f["d"]["chat"]["id"] == chat["id"])
        self.assertLess(first.seq, res.seq)
        for member in (b,):
            update = member.wait_event("ev.chat_update", lambda f: f["d"]["chat"]["id"] == chat["id"], since=mark_b)
            message = member.wait_event(
                "ev.message", lambda f: f["d"]["message"]["chat_id"] == chat["id"], since=mark_b
            )
            self.assertLess(update.seq, message.seq)
        self.barrier(c)
        c.expect_none(
            None,
            lambda f: chat["id"] in (f["d"].get("chat", {}).get("id"), f["d"].get("message", {}).get("chat_id")),
            since=mark_c,
        )

    def test_create_group_errors(self) -> None:
        a, b = self.new_user(), self.new_user()
        self.assertErr(a.request("chat.create_group", {"title": "   ", "member_ids": []}), "bad_request")
        self.assertErr(a.request("chat.create_group", {"title": "x" * 61, "member_ids": []}), "bad_request")
        self.assertErr(a.request("chat.create_group", {"title": "ok", "member_ids": [999999]}), "not_found")
        solo = a.request("chat.create_group", {"title": "Just me", "member_ids": []})
        self.assertTrue(solo["ok"])
        self.assertEqual(len(solo["d"]["chat"]["members"]), 1)
        self.assertTrue(b.request("ping", {})["ok"])

    def test_add_members_event_order_and_permissions(self) -> None:
        a, b, c, d = self.new_user(), self.new_user(), self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        marks = {s.name: s.mark() for s in (a, b, c, d)}
        res = a.request("chat.add_members", {"chat_id": chat_id, "user_ids": [c.me["id"], b.me["id"]]})
        self.assertTrue(res["ok"], res)
        # the new member: chat_update first, then the system message `added`
        update = c.wait_event("ev.chat_update", since=marks[c.name])
        message = c.wait_event(
            "ev.message", lambda f: f["d"]["message"]["system"]["event"] == "added", since=marks[c.name]
        )
        self.assertLess(update.seq, message.seq)
        self.assertEqual(update["d"]["chat"]["id"], chat_id)
        # existing members: chat_members {added} then the system message; no chat_update for them
        added = b.wait_event("ev.chat_members", since=marks[b.name])
        self.assertEqual([m["user_id"] for m in added["d"]["added"]], [c.me["id"]])
        self.assertLess(
            added.seq,
            b.wait_event(
                "ev.message", lambda f: f["d"]["message"]["system"]["event"] == "added", since=marks[b.name]
            ).seq,
        )
        b.expect_none("ev.chat_update", since=marks[b.name])
        c.expect_none("ev.chat_members", since=marks[c.name])  # never to the added user
        d.expect_none(
            None,
            lambda f: f["d"].get("chat_id") == chat_id or f["d"].get("message", {}).get("chat_id") == chat_id,
            since=marks[d.name],
        )
        # all already members: no-op, no events
        mark = b.mark()
        again = a.request("chat.add_members", {"chat_id": chat_id, "user_ids": [b.me["id"], c.me["id"]]})
        self.assertTrue(again["ok"])
        b.expect_none(None, lambda f: f["t"] != "ev.presence", since=mark)
        # errors in the order of SPEC 7.1.1
        self.assertErr(b.request("chat.add_members", {"chat_id": chat_id, "user_ids": [d.me["id"]]}), "forbidden")
        self.assertErr(a.request("chat.add_members", {"chat_id": chat_id, "user_ids": [999999]}), "not_found")
        self.assertErr(d.request("chat.add_members", {"chat_id": chat_id, "user_ids": [d.me["id"]]}), "not_member")
        self.assertErr(
            a.request("chat.add_members", {"chat_id": self.everyone_id(), "user_ids": [d.me["id"]]}), "invalid_state"
        )
        direct = a.request("chat.open_direct", {"user_id": b.me["id"]})["d"]["chat"]["id"]
        self.send(a, direct, "wakes the dormant chat")
        self.assertErr(b.request("chat.add_members", {"chat_id": direct, "user_ids": [c.me["id"]]}), "invalid_state")

    def test_remove_member_and_leave(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        chat_id = self.make_group(a, b, c)
        marks = {s.name: s.mark() for s in (a, b, c)}
        self.assertErr(
            a.request("chat.remove_member", {"chat_id": chat_id, "user_id": a.me["id"]}), "invalid_state", "use_leave"
        )
        self.assertErr(b.request("chat.remove_member", {"chat_id": chat_id, "user_id": c.me["id"]}), "forbidden")
        res = a.request("chat.remove_member", {"chat_id": chat_id, "user_id": b.me["id"]})
        self.assertTrue(res["ok"], res)
        removed = b.wait_event("ev.chat_removed", since=marks[b.name])
        self.assertEqual(removed["d"], {"chat_id": chat_id})
        members = c.wait_event("ev.chat_members", since=marks[c.name])
        self.assertEqual(members["d"]["removed"], [b.me["id"]])
        # a removed user receives nothing chat-scoped afterwards
        mark = b.mark()
        self.send(a, chat_id, "after removal")
        c.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "after removal")
        b.expect_none(None, lambda f: f["d"].get("message", {}).get("chat_id") == chat_id, since=mark)
        self.assertErr(b.request("chat.history", {"chat_id": chat_id}), "not_member")
        self.assertErr(
            b.request("msg.send", {"chat_id": chat_id, "client_id": "abcdefgh99", "body": "x"}), "not_member"
        )
        # leave: only the leaver gets chat_removed; the default group cannot be left
        left = c.request("chat.leave", {"chat_id": chat_id})
        self.assertEqual(left, {"t": "res", "id": left["id"], "ok": True, "d": {}})
        self.assertTrue(c.wait_event("ev.chat_removed", since=marks[c.name]))
        a.wait_event("ev.chat_members", lambda f: f["d"]["removed"] == [c.me["id"]])
        self.assertErr(a.request("chat.leave", {"chat_id": self.everyone_id()}), "invalid_state")
        self.assertErr(
            a.request("chat.remove_member", {"chat_id": self.everyone_id(), "user_id": c.me["id"]}), "invalid_state"
        )

    def test_promotion_after_the_last_admin_leaves_reaches_every_remaining_member(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        chat_id = self.make_group(a, b, c)
        marks = {s.name: s.mark() for s in (b, c)}
        self.assertTrue(a.request("chat.leave", {"chat_id": chat_id})["ok"])
        for member in (b, c):
            ev = member.wait_event("ev.chat_members", since=marks[member.name])
            self.assertEqual(ev["d"]["removed"], [a.me["id"]])
            self.assertEqual([m["user_id"] for m in ev["d"]["updated"]], [b.me["id"]])  # smallest joined_at, then id
            events = member.wait_event(
                "ev.message", lambda f: f["d"]["message"]["system"]["event"] == "promoted", since=marks[member.name]
            )
            self.assertEqual(events["d"]["message"]["system"]["target_ids"], [b.me["id"]])

    def test_set_admin_update_and_prefs(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        mark = b.mark()
        self.assertTrue(a.request("chat.set_admin", {"chat_id": chat_id, "user_id": b.me["id"], "admin": True})["ok"])
        ev = b.wait_event("ev.chat_members", since=mark)
        self.assertEqual(ev["d"]["updated"][0]["role"], "admin")
        self.assertEqual(
            b.wait_event("ev.message", lambda f: f["d"]["message"]["system"]["event"] == "promoted", since=mark).seq
            > ev.seq,
            True,
        )
        self.assertErr(
            a.request("chat.set_admin", {"chat_id": self.everyone_id(), "user_id": b.me["id"], "admin": True}),
            "invalid_state",
        )
        # update: broadcast to everyone, `renamed` only on a title change, no-op when equal
        mark = a.mark()
        res = b.request("chat.update", {"chat_id": chat_id, "title": "Renamed", "only_admins_post": True})
        self.assertTrue(res["ok"])
        a.wait_event("ev.chat_update", lambda f: f["d"]["chat"]["title"] == "Renamed", since=mark)
        a.wait_event("ev.message", lambda f: f["d"]["message"]["system"]["event"] == "renamed", since=mark)
        mark = a.mark()
        self.assertTrue(b.request("chat.update", {"chat_id": chat_id, "title": "Renamed"})["ok"])
        self.barrier(a)
        a.expect_none(None, lambda f: f["t"] != "ev.presence", since=mark)
        # prefs reach only the actor
        mark_a, mark_b = a.mark(), b.mark()
        res = b.request("chat.prefs", {"chat_id": chat_id, "pinned": True, "muted_until": 4102444800})
        self.assertTrue(res["ok"])
        self.assertTrue(res["d"]["chat"]["me"]["pinned"])
        b.wait_event("ev.chat_update", since=mark_b)
        a.expect_none("ev.chat_update", since=mark_a)
        self.assertErr(b.request("chat.prefs", {"chat_id": chat_id, "pinned": True, "archived": True}), "bad_request")
        self.assertErr(
            b.request("chat.prefs", {"chat_id": chat_id, "pinned": True, "archived": False, "muted_until": -1}),
            "bad_request",
        )

    def test_two_tabs_of_the_actor_both_get_the_events(self) -> None:
        a, b = self.new_user(), self.new_user()
        tab = self.another_tab(a)
        chat_id = self.make_group(a, b)
        mark = tab.mark()
        self.send(a, chat_id, "from tab one")
        tab.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "from tab one", since=mark)
        # client_id appears only in the sender's variant, in every tab of the sender
        mine = tab.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "from tab one")
        theirs = b.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "from tab one")
        self.assertIsNotNone(mine["d"]["message"]["client_id"])
        self.assertIsNone(theirs["d"]["message"]["client_id"])


class DirectChatTests(support.HubTestCase):
    def test_dormant_direct_chat_is_invisible_to_the_peer_until_the_first_message(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        mark_a, mark_b = a.mark(), b.mark()
        res = a.request("chat.open_direct", {"user_id": b.me["id"]})
        self.assertTrue(res["ok"], res)
        chat = res["d"]["chat"]
        self.assertEqual(chat["kind"], "direct")
        self.assertIsNone(chat["created_by"])
        self.assertIsNone(chat["title"])
        a.wait_event("ev.chat_update", lambda f: f["d"]["chat"]["id"] == chat["id"], since=mark_a)
        b.expect_none("ev.chat_update", since=mark_b)
        # every request of the unlisted peer answers not_member
        self.assertErr(b.request("chat.get", {"chat_id": chat["id"]}), "not_member")
        self.assertErr(b.request("chat.history", {"chat_id": chat["id"]}), "not_member")
        self.assertErr(
            b.request("msg.send", {"chat_id": chat["id"], "client_id": "abcdefgh01", "body": "hi"}), "not_member"
        )
        self.assertErr(b.request("receipt.read", {"chat_id": chat["id"], "up_to_id": 1}), "not_member")
        err = self.assertErr(b.request("receipt.delivered", {"chat_id": chat["id"], "up_to_id": 1}), "not_member")
        self.assertEqual(err["chat_id"], chat["id"])
        self.assertNotIn(chat["id"], [c_["id"] for c_ in self.another_tab(b).ready["chats"]])
        # idempotent for the creator, nothing emitted
        mark = a.mark()
        again = a.request("chat.open_direct", {"user_id": b.me["id"]})
        self.assertEqual(again["d"]["chat"]["id"], chat["id"])
        self.barrier(a)
        a.expect_none("ev.chat_update", since=mark)
        # the first message lists the chat for the peer: chat_update strictly before the message
        mark_b = b.mark()
        self.send(a, chat["id"], "hello b")
        update = b.wait_event("ev.chat_update", lambda f: f["d"]["chat"]["id"] == chat["id"], since=mark_b)
        message = b.wait_event("ev.message", lambda f: f["d"]["message"]["chat_id"] == chat["id"], since=mark_b)
        self.assertLess(update.seq, message.seq)
        self.assertTrue(b.request("chat.get", {"chat_id": chat["id"]})["ok"])
        # an outsider never learns anything
        self.assertErr(c.request("chat.get", {"chat_id": chat["id"]}), "not_member")

    def test_the_unlisted_peer_can_open_the_chat_for_itself_only(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = a.request("chat.open_direct", {"user_id": b.me["id"]})["d"]["chat"]["id"]
        mark_a, mark_b = a.mark(), b.mark()
        res = b.request("chat.open_direct", {"user_id": a.me["id"]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["d"]["chat"]["id"], chat_id)
        b.wait_event("ev.chat_update", lambda f: f["d"]["chat"]["id"] == chat_id, since=mark_b)
        self.barrier(a)
        a.expect_none("ev.chat_update", since=mark_a)

    def test_self_chat_and_unknown_user(self) -> None:
        a = self.new_user()
        tab = self.another_tab(a)
        res = a.request("chat.open_direct", {"user_id": a.me["id"]})
        self.assertTrue(res["ok"], res)
        chat = res["d"]["chat"]
        self.assertEqual(chat["peer_id"], a.me["id"])
        self.assertEqual(len(chat["members"]), 1)
        mark = tab.mark()
        self.send(a, chat["id"], "note to self")
        tab.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "note to self", since=mark)
        self.assertErr(a.request("chat.open_direct", {"user_id": 999999}), "not_found")

    def test_typing_is_ignored_in_the_self_chat(self) -> None:
        a = self.new_user()
        tab = self.another_tab(a)
        chat = a.request("chat.open_direct", {"user_id": a.me["id"]})["d"]["chat"]["id"]
        mark = tab.mark()
        a.send("typing", {"chat_id": chat, "state": "typing"})
        self.barrier(a, tab)
        tab.expect_none("ev.typing", since=mark)


if __name__ == "__main__":
    unittest.main()
