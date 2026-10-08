"""Configuration wiring of the hub: windows, body limit, unread cap, ``ev.ready.limits`` (SPEC 2.1, 7.3, 7.4)."""

from __future__ import annotations

import time
import unittest

try:
    from . import test_hub_support as support
except ImportError:
    import test_hub_support as support


class WindowTests(support.HubTestCase):
    overrides = {"edit_window_s": 1, "delete_window_s": 1, "max_body_chars": 50}

    def test_ready_advertises_the_configured_limits(self) -> None:
        limits = self.admin.ready["limits"]
        self.assertEqual((limits["edit_window_s"], limits["delete_window_s"], limits["max_body_chars"]), (1, 1, 50))
        self.assertEqual(limits["max_upload_bytes"], self.srv.cfg.max_upload_bytes)

    def test_body_limit_edit_window_and_delete_window(self) -> None:
        admin_of_group, member = self.new_user(), self.new_user()
        chat_id = self.make_group(admin_of_group, member)
        self.assertEqual(self.send(member, chat_id, "x" * 50)["ok"], True)
        self.assertErr(
            member.request("msg.send", {"chat_id": chat_id, "client_id": "toolarge-01", "body": "x" * 51}), "too_large"
        )
        mine = self.send(member, chat_id, "will expire")["d"]["message"]
        theirs = self.send(member, chat_id, "moderated")["d"]["message"]
        time.sleep(1.3)
        self.assertErr(member.request("msg.edit", {"message_id": mine["id"], "body": "late edit"}), "window_expired")
        self.assertErr(member.request("msg.delete", {"message_id": mine["id"], "scope": "everyone"}), "window_expired")
        # delete for me has no window; a group admin may moderate at any time
        self.assertEqual(member.request("msg.delete", {"message_id": mine["id"], "scope": "me"})["d"], {})
        res = admin_of_group.request("msg.delete", {"message_id": theirs["id"], "scope": "everyone"})
        self.assertTrue(res["d"]["message"]["deleted"], res)


class UnreadCapTests(support.HubTestCase):
    limits = {"unread_cap": 3}

    def test_unread_counters_are_capped(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        for i in range(6):
            self.send(a, chat_id, "unread %d" % i)
        chat = b.request("chat.get", {"chat_id": chat_id})["d"]["chat"]
        self.assertEqual(chat["me"]["unread"], 3)
        read = b.request("receipt.read", {"chat_id": chat_id, "up_to_id": chat["last_message_id"] - 3})
        self.assertEqual(read["d"]["unread"], 3)


class TypingNeverViolatesTests(support.HubTestCase):
    limits = {"typing": [1, 1000.0]}

    def test_dropped_typing_frames_never_count_as_violations(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        for _ in range(40):
            a.send("typing", {"chat_id": chat_id, "state": "typing"})
        self.assertTrue(a.request("ping", {})["ok"])
        self.assertIsNone(a.close_code)


if __name__ == "__main__":
    unittest.main()
