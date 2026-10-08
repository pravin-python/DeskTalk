"""Per-bucket rate limits of SPEC 7.5: every bucket, all-or-nothing tokens, the dedupe refund, exemptions."""

from __future__ import annotations

import contextlib
import unittest
from typing import Any, Dict, Iterator, Tuple

try:
    from . import test_hub_support as support
except ImportError:
    import test_hub_support as support


class LimitCase(support.HubTestCase):
    @contextlib.contextmanager
    def limit(self, **buckets: Tuple[int, float]) -> Iterator[None]:
        """Tighten buckets for users and connections that first charge them inside the block."""
        limits = self.srv.cfg.test_limits
        saved: Dict[str, Any] = {}
        for name, value in buckets.items():
            key = name.replace("_dot_", ".")
            saved[key] = limits.get(key)
            limits[key] = [value[0], value[1]]
        try:
            yield
        finally:
            for key, old in saved.items():
                if old is None:
                    limits.pop(key, None)
                else:
                    limits[key] = old

    def assertLimited(self, res: Any) -> Dict[str, Any]:
        err = self.assertErr(res, "rate_limited")
        self.assertIsInstance(err["retry_after"], (int, float))
        self.assertGreater(err["retry_after"], 0)
        return err

    def second_admin(self) -> Any:
        user = self.new_user("adm")
        self.assertTrue(self.admin.request("admin.update_user", {"user_id": user.me["id"], "role": "admin"})["ok"])
        return user


class BucketTests(LimitCase):
    def test_every_request_bucket_refuses_with_retry_after(self) -> None:
        chat_id = self.everyone_id()
        cases = {
            "msg.send": ("msg.send", lambda i: {"chat_id": chat_id, "client_id": "bucket-%06d" % i, "body": "x"}),
            "msg.forward": (
                "msg.forward",
                lambda i: {"message_ids": [1], "chat_ids": [chat_id], "client_id": "fwd-%08d" % i},
            ),
            "msg.search": ("msg.search", lambda i: {"q": "zzzzz"}),
            "read": ("chat.history", lambda i: {"chat_id": chat_id}),
            "msg.react": ("msg.react", lambda i: {"message_id": 1, "emoji": None}),
            "msg.pin": ("msg.pin", lambda i: {"chat_id": chat_id, "message_id": 1, "pinned": True}),
            "chat.create_group": ("chat.create_group", lambda i: {"title": "g%d" % i, "member_ids": []}),
            "chat.add_members": ("chat.add_members", lambda i: {"chat_id": chat_id, "user_ids": [1]}),
            "chat.open_direct": ("chat.open_direct", lambda i: {"user_id": 1}),
            "profile.update": ("profile.update", lambda i: {"status_text": "s%d" % i}),
            "receipt": ("receipt.read", lambda i: {"chat_id": chat_id, "up_to_id": 0}),
            "global_user": ("chat.update", lambda i: {"chat_id": chat_id, "title": "t"}),
            "global_conn": ("chat.update", lambda i: {"chat_id": chat_id, "title": "t"}),
        }
        for bucket, (request, build) in cases.items():
            with self.subTest(bucket=bucket), self.limit(**{bucket: (2, 1000.0)}):
                user = self.new_user()
                for i in range(2):
                    res = user.request(request, build(i))
                    self.assertNotEqual(res.get("err", {}).get("code"), "rate_limited", (bucket, res))
                self.assertLimited(user.request(request, build(2)))
                self.assertTrue(user.request("ping", {})["ok"])  # ping is exempt from everything
                self.assertIsNone(user.close_code)  # one violation per window never closes the socket

    def test_admin_buckets(self) -> None:
        admin = self.second_admin()
        cases = {
            "admin.create_user": ("admin.create_user", {"username": "zz", "display_name": "Zed", "password": "x"}),
            "admin.reset_password": ("admin.reset_password", {"user_id": 999999, "new_password": "x"}),
            "admin_other": ("admin.stats", {}),
        }
        for bucket, (request, data) in cases.items():
            with self.subTest(bucket=bucket), self.limit(**{bucket: (2, 1000.0)}):
                user = self.second_admin()
                for _ in range(2):
                    self.assertNotEqual(user.request(request, data).get("err", {}).get("code"), "rate_limited")
                self.assertLimited(user.request(request, data))
        self.assertTrue(admin.request("ping", {})["ok"])

    def test_admin_requests_are_exempt_from_the_global_buckets(self) -> None:
        with self.limit(global_user=(1, 1000.0), global_conn=(1, 1000.0)):
            admin = self.second_admin()
            for _ in range(6):
                self.assertTrue(admin.request("admin.stats", {})["ok"])

    def test_receipt_ping_and_typing_are_exempt_from_the_global_buckets(self) -> None:
        with self.limit(global_user=(2, 1000.0), global_conn=(2, 1000.0)):
            a, b = self.new_user(), self.new_user()
            chat_id = self.everyone_id()
            for _ in range(2):
                self.assertTrue(a.request("chat.get", {"chat_id": chat_id})["ok"])
            self.assertLimited(a.request("chat.get", {"chat_id": chat_id}))
            for _ in range(25):
                self.assertTrue(a.request("receipt.delivered", {"chat_id": chat_id, "up_to_id": 1})["ok"])
                self.assertTrue(a.request("ping", {})["ok"])
            mark = b.mark()
            a.send("typing", {"chat_id": chat_id, "state": "typing"})
            b.wait_event("ev.typing", since=mark)

    def test_receipt_bucket_is_per_connection_and_typing_is_dropped_silently(self) -> None:
        with self.limit(receipt=(2, 1000.0), typing=(1, 1000.0)):
            a, b = self.new_user(), self.new_user()
            chat_id = self.make_group(a, b)
            tab = self.another_tab(a)
            for _ in range(2):
                self.assertTrue(a.request("receipt.delivered", {"chat_id": chat_id, "up_to_id": 1})["ok"])
            self.assertLimited(a.request("receipt.read", {"chat_id": chat_id, "up_to_id": 1}))
            self.assertTrue(tab.request("receipt.delivered", {"chat_id": chat_id, "up_to_id": 1})["ok"])  # own bucket
            mark = b.mark()
            a.send("typing", {"chat_id": chat_id, "state": "typing"})
            b.wait_event("ev.typing", since=mark)
            mark = b.mark()
            res = a.request("typing", {"chat_id": chat_id, "state": "stop"})  # over the limit: dropped, not an error
            self.assertEqual(res["d"], {})
            self.barrier(a, b)
            b.expect_none("ev.typing", since=mark)

    def test_user_buckets_are_shared_by_all_connections_of_the_user(self) -> None:
        with self.limit(**{"msg.send": (3, 1000.0)}):
            a, b = self.new_user(), self.new_user()
            tab = self.another_tab(a)
            chat_id = self.make_group(a, b)
            self.assertTrue(a.request("msg.send", {"chat_id": chat_id, "client_id": "share-000001", "body": "1"})["ok"])
            self.assertTrue(
                tab.request("msg.send", {"chat_id": chat_id, "client_id": "share-000002", "body": "2"})["ok"]
            )
            self.assertTrue(a.request("msg.send", {"chat_id": chat_id, "client_id": "share-000003", "body": "3"})["ok"])
            self.assertLimited(tab.request("msg.send", {"chat_id": chat_id, "client_id": "share-000004", "body": "4"}))

    def test_a_deduplicated_send_refunds_its_token_but_an_empty_bucket_refuses_even_a_dedupe(self) -> None:
        with self.limit(**{"msg.send": (3, 1000.0)}):
            a, b = self.new_user(), self.new_user()
            chat_id = self.make_group(a, b)
            first = a.request("msg.send", {"chat_id": chat_id, "client_id": "dedupe-00001", "body": "same"})
            self.assertTrue(first["ok"])
            for _ in range(10):
                again = a.request("msg.send", {"chat_id": chat_id, "client_id": "dedupe-00001", "body": "same"})
                self.assertEqual(again["d"]["message"]["id"], first["d"]["message"]["id"])
            self.assertTrue(a.request("msg.send", {"chat_id": chat_id, "client_id": "dedupe-00002", "body": "x"})["ok"])
            self.assertTrue(a.request("msg.send", {"chat_id": chat_id, "client_id": "dedupe-00003", "body": "x"})["ok"])
            self.assertLimited(a.request("msg.send", {"chat_id": chat_id, "client_id": "dedupe-00004", "body": "x"}))
            self.assertLimited(a.request("msg.send", {"chat_id": chat_id, "client_id": "dedupe-00001", "body": "same"}))

    def test_forward_takes_its_tokens_all_or_nothing(self) -> None:
        with self.limit(**{"msg.send": (3, 1000.0), "msg.forward": (5, 1000.0)}):
            a = self.new_user()
            chats = [self.make_group(a, title="Fwd%d" % i) for i in range(4)]
            source = self.send(a, chats[0], "to forward")  # takes one msg.send token
            mid = source["d"]["message"]["id"]
            res = a.request("msg.forward", {"message_ids": [mid], "chat_ids": chats, "client_id": "fwd-all-000001"})
            self.assertLimited(res)  # needs 4 msg.send tokens, 2 are left: nothing is consumed
            self.assertTrue(
                a.request("msg.send", {"chat_id": chats[1], "client_id": "fwd-all-000002", "body": "1"})["ok"]
            )
            self.assertTrue(
                a.request("msg.send", {"chat_id": chats[1], "client_id": "fwd-all-000003", "body": "2"})["ok"]
            )
            self.assertLimited(a.request("msg.send", {"chat_id": chats[1], "client_id": "fwd-all-000004", "body": "3"}))
            forwards = a.request(
                "msg.forward", {"message_ids": [mid], "chat_ids": chats[:1], "client_id": "fwd-all-000005"}
            )
            self.assertLimited(forwards)  # the msg.send bucket is empty, so even one chat is refused

    def test_a_deduplicated_forward_refunds_both_buckets(self) -> None:
        with self.limit(**{"msg.send": (6, 1000.0), "msg.forward": (2, 1000.0)}):
            a = self.new_user()
            one, two = self.make_group(a, title="R1"), self.make_group(a, title="R2")
            mid = self.send(a, one, "source")["d"]["message"]["id"]
            first = a.request(
                "msg.forward", {"message_ids": [mid], "chat_ids": [one, two], "client_id": "fwd-refund-001"}
            )
            self.assertTrue(first["ok"], first)
            for _ in range(6):  # a retry needs its 1 + 2 tokens at the door, then refunds them when it dedupes
                again = a.request(
                    "msg.forward", {"message_ids": [mid], "chat_ids": [one, two], "client_id": "fwd-refund-001"}
                )
                self.assertEqual([m["id"] for m in again["d"]["messages"]], [m["id"] for m in first["d"]["messages"]])


if __name__ == "__main__":
    unittest.main()
