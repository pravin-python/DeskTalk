"""Frame validation, ``ev.ready``, request ids, in-flight cap and the 4008 rule (SPEC 7.1, 7.1.1, 7.5, 7.6(7))."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from typing import Any, Dict

try:
    from . import test_hub_support as support
except ImportError:
    import test_hub_support as support

from chatd import hub as hubmod

try:
    from . import wsclient as support_wsclient
except ImportError:
    import wsclient as support_wsclient  # type: ignore[no-redef]


class FrameTests(support.HubTestCase):
    def raw_res(self, session: Any, text: str) -> Dict[str, Any]:
        """Send ``text`` verbatim and return the next ``res`` frame (answers to malformed frames carry ``id:null``)."""
        before = len(session.responses)
        session.raw(text)
        deadline = time.monotonic() + 5
        while len(session.responses) == before and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(len(session.responses), before, "no res for %r" % text[:60])
        return session.responses[before]

    def assertMalformed(self, session: Any, text: str) -> None:
        res = self.raw_res(session, text)
        self.assertIsNone(res["id"])
        self.assertErr(res, "bad_request")

    # ---- malformed frames ----------------------------------------------------------------------------------------

    def test_malformed_frames_get_an_id_null_res_and_never_kill_the_connection(self) -> None:
        user = self.new_user()
        for text in (
            "not json",
            "[1, 2]",
            "42",
            '"t"',
            "{}",
            '{"id":"x1","d":{}}',
            '{"t":5,"id":"x1"}',
            '{"t":"ping","id":"x1","d":NaN}',
            '{"t":"ping","id":"x1","d":{"a":Infinity}}',
            '{"t":"ping","id":"x1","d":{"a":-Infinity}}',
            '{"t":"ping","id":"x1","d":{"a":1234567890123456789}}',
            '{"t":"ping","id":"x1","d":{"a":1,"a":2}}',
            '{"t":"ping","id":"x1","t":"ping"}',
            '{"t":"ping","id":"x1","d":{"a":1e999}}',
            '{"t":"ping" "id":"x1"}',
        ):
            self.assertMalformed(user, text)
        self.assertTrue(user.request("ping", {})["ok"])

    def test_nesting_deeper_than_eight_is_malformed(self) -> None:
        user = self.new_user()
        deep = "[" * 9 + "]" * 9
        self.assertMalformed(user, '{"t":"ping","id":"x1","d":{"a":%s}}' % deep)
        deeper = "[" * 100000 + "]" * 100000  # raises RecursionError inside json.loads on most builds
        self.assertMalformed(user, '{"t":"ping","id":"x1","d":{"a":%s}}' % deeper)
        ok = "[" * 6 + "]" * 6  # d -> a -> six lists: depth 8 in total
        res = self.raw_res(user, '{"t":"ping","id":"x1","d":{"a":%s}}' % ok)
        self.assertTrue(res["ok"], res)

    def test_twenty_consecutive_malformed_frames_close_with_1008(self) -> None:
        user = self.new_user()
        for _ in range(19):
            user.raw("garbage")
        self.assertTrue(user.request("ping", {})["ok"])  # a good frame resets the counter
        for _ in range(20):
            user.raw("garbage")
        self.assertEqual(user.wait_closed(), 1008)

    # ---- ids and envelope ----------------------------------------------------------------------------------------

    def test_a_request_without_a_usable_id_never_gets_a_res(self) -> None:
        user = self.new_user()
        before = len(user.responses)
        user.raw(json.dumps({"t": "chat.get", "d": {"chat_id": "nope"}}))  # an error, but no id: no res
        user.raw(json.dumps({"t": "ping", "id": 7, "d": {}}))  # non-string id = absent
        user.raw(json.dumps({"t": "ping", "id": "", "d": {}}))
        user.raw(json.dumps({"t": "ping", "id": "x" * 65, "d": {}}))
        user.raw(json.dumps({"t": "ping", "id": None, "d": {}}))
        user.raw(json.dumps({"t": "nonsense"}))
        res = user.request("ping", {})  # the barrier
        self.assertTrue(res["ok"])
        self.assertEqual(len(user.responses), before + 1)

    def test_id_of_64_characters_is_echoed_and_a_missing_d_is_empty(self) -> None:
        user = self.new_user()
        rid = "r" * 64
        res = self.raw_res(user, json.dumps({"t": "ping", "id": rid}))
        self.assertTrue(res["ok"])
        self.assertEqual(res["id"], rid)
        self.assertIn("now", res["d"])

    def test_d_must_be_an_object_and_unknown_types_are_bad_requests(self) -> None:
        user = self.new_user()
        for d in ("[]", '"x"', "5", "null"):
            res = self.raw_res(user, '{"t":"ping","id":"d1","d":%s}' % d)
            self.assertEqual(res["id"], "d1")
            self.assertErr(res, "bad_request")
        self.assertErr(user.request("no.such.type", {}), "bad_request")
        self.assertErr(user.request("x" * 40, {}), "bad_request")

    def test_unknown_fields_are_ignored(self) -> None:
        user = self.new_user()
        res = user.request("ping", {"future": {"field": [1, 2, 3]}})
        self.assertTrue(res["ok"])

    def test_lone_surrogates_are_invalid_text(self) -> None:
        user = self.new_user()
        group = self.make_group(user)
        for text in (
            '{"t":"msg.send","id":"s1","d":{"chat_id":%d,"client_id":"abcdefgh12","body":"\\ud800"}}' % group,
            '{"t":"chat.update","id":"s1","d":{"chat_id":%d,"title":"a\\udfffb"}}' % group,
            '{"t":"ping","id":"s1","d":{"\\ud800":1}}',
        ):
            res = self.raw_res(user, text)
            self.assertEqual(res["id"], "s1")
            self.assertErr(res, "bad_request", "invalid_text")
        # a surrogate pair is valid text
        ok = self.raw_res(
            user,
            '{"t":"msg.send","id":"s2","d":{"chat_id":%d,"client_id":"abcdefgh13","body":"\\ud83d\\ude00"}}' % group,
        )
        self.assertTrue(ok["ok"], ok)

    def test_shape_errors_use_bad_request(self) -> None:
        user = self.new_user()
        cases = [
            ("chat.get", {}),
            ("chat.get", {"chat_id": True}),
            ("chat.get", {"chat_id": "1"}),
            ("chat.get", {"chat_id": 1.5}),
            ("chat.get", {"chat_id": 0}),
            ("chat.get", {"chat_id": 2**53}),
            ("chat.history", {"chat_id": 1, "before_id": 2, "after_id": 1}),
            ("chat.history", {"chat_id": 1, "limit": "10"}),
            ("msg.send", {"chat_id": 1, "client_id": "short", "body": "x"}),
            ("msg.send", {"chat_id": 1, "client_id": "has space here", "body": "x"}),
            ("msg.send", {"chat_id": 1, "client_id": "abcdefgh12", "body": 5}),
            ("msg.send", {"chat_id": 1, "client_id": "abcdefgh12", "seen_up_to_id": -1}),
            ("msg.react", {"message_id": 1}),
            ("msg.react", {"message_id": 1, "emoji": "abc"}),
            ("msg.delete", {"message_id": 1, "scope": "all"}),
            ("msg.forward", {"message_ids": [], "chat_ids": [1], "client_id": "abcdefgh12"}),
            ("msg.forward", {"message_ids": [1], "chat_ids": list(range(1, 7)), "client_id": "abcdefgh12"}),
            ("msg.forward", {"message_ids": list(range(1, 22)), "chat_ids": [1], "client_id": "abcdefgh12"}),
            ("receipt.delivered", {}),
            ("receipt.delivered", {"chat_id": 1, "up_to_id": 1, "items": [{"chat_id": 1, "up_to_id": 1}]}),
            ("receipt.delivered", {"items": []}),
            ("receipt.delivered", {"items": [{"chat_id": 1}]}),
            ("receipt.read", {"chat_id": 1, "up_to_id": -1}),
            ("chat.prefs", {"chat_id": 1}),
            ("chat.prefs", {"chat_id": 1, "pinned": True, "archived": True}),
            ("chat.create_group", {"title": "x"}),
            ("chat.create_group", {"title": "x", "member_ids": list(range(1, 202))}),
            ("chat.add_members", {"chat_id": 1, "user_ids": []}),
            ("chat.update", {"chat_id": 1}),
            ("profile.update", {}),
            ("msg.shared", {"chat_id": 1, "kind": "pictures"}),
            ("typing", {"chat_id": 1, "state": "dancing"}),
        ]
        for name, data in cases:
            res = user.request(name, data)
            self.assertErr(res, "bad_request")

    # ---- ev.ready ------------------------------------------------------------------------------------------------

    def test_ev_ready_is_the_first_frame_and_carries_the_snapshot(self) -> None:
        user = self.new_user(connect=False)
        user.connect()
        first = user.frames[0]
        self.assertEqual(first["t"], "ev.ready")
        d = first["d"]
        self.assertEqual(d["protocol"], 1)
        self.assertEqual(d["me"]["id"], user.me["id"])
        self.assertTrue(d["me"]["online"])
        self.assertEqual({"name", "registration_open"}, set(d["workspace"]))
        self.assertGreaterEqual(len(d["users"]), 2)
        self.assertTrue(any(c["is_default"] for c in d["chats"]))
        self.assertEqual(d["limits"]["max_forward_chats"], 5)
        self.assertEqual(d["limits"]["max_body_chars"], self.srv.cfg.max_body_chars)
        online = {u["id"]: u["online"] for u in d["users"]}
        self.assertTrue(online[self.admin.user_id])
        self.assertTrue(online[user.me["id"]])

    def test_ev_ready_comes_first_even_while_messages_are_being_sent(self) -> None:
        sender = self.new_user("snd")
        receiver = self.new_user("rcv", connect=False)
        group = self.make_group(sender, receiver)
        stop = threading.Event()
        sent = []

        def spam() -> None:
            n = 0
            while not stop.is_set() and n < 60:
                n += 1
                res = sender.request("msg.send", {"chat_id": group, "client_id": "spam-%06d" % n, "body": "m%d" % n})
                if res.get("ok"):
                    sent.append(res["d"]["message"]["id"])

        worker = threading.Thread(target=spam, daemon=True)
        worker.start()
        time.sleep(0.05)
        receiver.connect()
        worker.join(30)
        self.assertEqual(receiver.frames[0]["t"], "ev.ready")
        self.barrier(receiver, sender)
        snapshot = next(c for c in receiver.ready["chats"] if c["id"] == group)
        seen = {e["d"]["message"]["id"] for _, e in receiver.events if e["t"] == "ev.message"}
        # every message is in the snapshot (id <= last_message_id) or arrived as an event afterwards
        for mid in sent:
            self.assertTrue(mid <= (snapshot["last_message_id"] or 0) or mid in seen, mid)

    def test_requests_sent_before_ready_are_held_then_answered(self) -> None:
        user = self.new_user(connect=False)
        user.connect(wait_ready=False)
        rid = user.request_nowait("ping", {})
        res = user.wait_res(rid)
        self.assertTrue(res["ok"])
        self.assertEqual(user.frames[0]["t"], "ev.ready")
        self.assertLess(user.frames[0].seq, res.seq)

    # ---- in flight / 4008 ----------------------------------------------------------------------------------------

    def test_the_65th_request_in_flight_is_rate_limited_and_not_executed(self) -> None:
        user = self.new_user()
        gate = threading.Event()
        started = []

        async def slow(conn: Any, k: Dict[str, Any]) -> Dict[str, Any]:
            started.append(1)
            while not gate.is_set():
                await asyncio.sleep(0.01)
            return {}

        spec, original = self.hub._specs["chat.get"]
        self.hub._specs["chat.get"] = (spec, slow)
        try:
            ids = [user.request_nowait("chat.get", {"chat_id": 1}) for _ in range(64)]
            deadline = time.monotonic() + 5
            while len(started) < 64 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(started), 64)
            extra = user.request("chat.get", {"chat_id": 1})
            err = self.assertErr(extra, "rate_limited")
            self.assertEqual(err["retry_after"], 1.0)
            self.assertEqual(len(started), 64)  # the 65th never ran
        finally:
            gate.set()
            self.hub._specs["chat.get"] = (spec, original)
        for rid in ids:
            self.assertTrue(user.wait_res(rid)["ok"])


class LimitTests(support.HubTestCase):
    """The 3-violations rule at ``test_scale`` 0.1 (a window of 1 s is then 0.1 s, 60 s is 6 s)."""

    scale = 0.1
    limits = {"msg.send": [3, 1.0]}

    def test_three_violations_in_sixty_seconds_close_with_4008(self) -> None:
        user = self.new_user()
        group = self.make_group(user)
        # window 1: exhaust the bucket; the first refusal is violation 1, the rest of the window adds nothing
        results = [
            user.request("msg.send", {"chat_id": group, "client_id": "burst1-%03d" % i, "body": "x"}) for i in range(8)
        ]
        self.assertEqual([r["ok"] for r in results[:3]], [True, True, True])
        refused = [r for r in results[3:] if not r["ok"]]
        self.assertTrue(refused)
        for r in refused:
            self.assertEqual(r["err"]["code"], "rate_limited")
            self.assertGreater(r["err"]["retry_after"], 0)
        self.assertIsNone(user.close_code, "refusals inside one window are ONE violation")
        # windows 2 and 3
        for window in (2, 3):
            time.sleep(0.15)
            for i in range(8):
                rid = user.request_nowait(
                    "msg.send", {"chat_id": group, "client_id": "burst%d-%03d" % (window, i), "body": "x"}
                )
                try:
                    user.wait_res(rid)
                except support_wsclient.ConnectionClosedError:
                    break
        self.assertEqual(user.wait_closed(), 4008)
        last = user.responses[-1]
        self.assertEqual(last["err"]["code"], "rate_limited")  # the res of the third refusal was sent before the close


class BucketUnitTests(unittest.TestCase):
    def test_retry_after_is_rounded_up_to_two_decimals(self) -> None:
        bucket = hubmod._Bucket(3, 1.0, 0.0)
        for _ in range(3):
            self.assertEqual(bucket.shortfall(1, 0.0), 0.0)
            bucket.take(1)
        self.assertEqual(bucket.shortfall(1, 0.0), 0.34)  # ceil(1 / 3 * 100) / 100
        self.assertEqual(bucket.shortfall(1, 0.2), 0.14)  # 0.6 tokens refilled
        self.assertEqual(bucket.shortfall(1, 0.4), 0.0)  # 1.2 tokens
        bucket.take(1)
        bucket.refund(5, 0.4)
        self.assertEqual(bucket.shortfall(3, 0.4), 0.0)  # a refund never exceeds the capacity
        bucket.take(3)
        self.assertGreater(bucket.shortfall(1, 0.4), 0.0)

    def test_a_refused_request_consumes_nothing(self) -> None:
        low = hubmod._Bucket(1, 10.0, 0.0)
        high = hubmod._Bucket(5, 10.0, 0.0)
        low.take(1)
        needs = [(low, 1), (high, 1)]
        waits = [b.shortfall(n, 0.0) for b, n in needs]
        self.assertGreater(max(waits), 0.0)
        self.assertEqual(high.tokens, 5.0)


if __name__ == "__main__":
    unittest.main()
