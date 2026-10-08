"""Typing, presence, the pending-connection backlog, control-poll changes and shutdown at ``test_scale`` 0.1.

At that scale the typing TTL is 0.7 s, the typing sweep and the control poll run every 0.1 s / 0.2 s and the presence
grace period is 0.8 s (SPEC 2.1: every in-process timer goes through ``util.scaled``).
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any
from unittest import mock

try:
    from . import test_hub_support as support
except ImportError:
    import test_hub_support as support

from chatd import db_chats, maintenance


def typing_of(chat_id: int, user_id: int) -> Any:
    return lambda f: f["d"]["chat_id"] == chat_id and f["d"]["user_id"] == user_id


class TypingTests(support.HubTestCase):
    scale = 0.1

    def test_typing_reaches_the_other_members_and_never_the_typist(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        tab = self.another_tab(a)
        chat_id = self.make_group(a, b)
        marks = {s.name: s.mark() for s in (a, b, c, tab)}
        a.send("typing", {"chat_id": chat_id, "state": "typing"})
        ev = b.wait_event("ev.typing", since=marks[b.name])
        self.assertEqual(ev["d"], {"chat_id": chat_id, "user_id": a.me["id"], "state": "typing"})
        for silent in (a, tab, c):
            self.barrier(silent)
            silent.expect_none("ev.typing", since=marks[silent.name])
        mark = b.mark()
        a.send("typing", {"chat_id": chat_id, "state": "stop"})
        self.assertEqual(b.wait_event("ev.typing", since=mark)["d"]["state"], "stop")

    def test_refreshes_are_relayed_at_most_once_per_second_scaled(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        mark = b.mark()
        for _ in range(5):
            a.send("typing", {"chat_id": chat_id, "state": "typing"})
        self.barrier(a, b)
        self.assertEqual(len(b.events_matching("ev.typing", since=mark)), 1)
        time.sleep(0.15)  # scaled(1.0) = 0.1 s: the next refresh is relayed again
        a.send("typing", {"chat_id": chat_id, "state": "typing"})
        deadline = time.monotonic() + 3
        while len(b.events_matching("ev.typing", since=mark)) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(b.events_matching("ev.typing", since=mark)), 2)

    def test_an_entry_expires_after_seven_scaled_seconds_and_stop_is_relayed(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        mark = b.mark()
        started = time.monotonic()
        a.send("typing", {"chat_id": chat_id, "state": "recording"})
        self.assertEqual(b.wait_event("ev.typing", since=mark)["d"]["state"], "recording")
        stop = b.wait_event("ev.typing", lambda f: f["d"]["state"] == "stop", since=mark, timeout=5)
        self.assertGreater(time.monotonic() - started, 0.5)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(stop["d"]["user_id"], a.me["id"])

    def test_one_stop_of_two_tabs_does_not_stop_the_user(self) -> None:
        a, b = self.new_user(), self.new_user()
        tab = self.another_tab(a)
        chat_id = self.make_group(a, b)
        mark = b.mark()
        a.send("typing", {"chat_id": chat_id, "state": "typing"})
        tab.send("typing", {"chat_id": chat_id, "state": "recording"})
        b.wait_event("ev.typing", lambda f: f["d"]["state"] == "recording", since=mark)  # recording wins
        mark = b.mark()
        tab.send("typing", {"chat_id": chat_id, "state": "stop"})
        self.assertEqual(b.wait_event("ev.typing", since=mark)["d"]["state"], "typing")  # tab one still types
        mark = b.mark()
        a.send("typing", {"chat_id": chat_id, "state": "stop"})
        self.assertEqual(b.wait_event("ev.typing", since=mark)["d"]["state"], "stop")

    def test_closing_a_connection_drops_its_entries(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        mark = b.mark()
        a.send("typing", {"chat_id": chat_id, "state": "typing"})
        b.wait_event("ev.typing", since=mark)
        mark = b.mark()
        a.abort()
        self.assertEqual(b.wait_event("ev.typing", since=mark)["d"]["state"], "stop")

    def test_typing_from_non_members_unlisted_peers_and_non_admins_is_dropped(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        mark = a.mark()
        c.send("typing", {"chat_id": chat_id, "state": "typing"})  # not a member
        c.send("typing", {"chat_id": 999999, "state": "typing"})
        dormant = a.request("chat.open_direct", {"user_id": c.me["id"]})["d"]["chat"]["id"]
        c.send("typing", {"chat_id": dormant, "state": "typing"})  # the unlisted peer of a dormant chat
        self.assertTrue(a.request("chat.update", {"chat_id": chat_id, "only_admins_post": True})["ok"])
        mark_a = a.mark()
        b.send("typing", {"chat_id": chat_id, "state": "typing"})  # a non-admin in an only_admins_post chat
        for session in (a, c):
            self.barrier(session)
        a.expect_none("ev.typing", since=mark)
        self.assertEqual(len(a.events_matching("ev.typing", since=mark_a)), 0)
        mark = b.mark()
        a.send("typing", {"chat_id": chat_id, "state": "typing"})  # admins may
        self.assertEqual(b.wait_event("ev.typing", since=mark)["d"]["user_id"], a.me["id"])

    def test_typing_works_in_a_fresh_group_and_in_everyone_for_a_fresh_registration(self) -> None:
        newcomer = self.new_user()
        everyone = self.everyone_id()
        mark = self.admin.mark()
        newcomer.send("typing", {"chat_id": everyone, "state": "typing"})
        self.admin.wait_event("ev.typing", typing_of(everyone, newcomer.me["id"]), since=mark)
        other = self.new_user()
        group = self.make_group(other, newcomer)
        mark = newcomer.mark()
        other.send("typing", {"chat_id": group, "state": "typing"})
        newcomer.wait_event("ev.typing", typing_of(group, other.me["id"]), since=mark)

    def test_a_request_with_an_id_is_answered_even_for_dropped_typing(self) -> None:
        a = self.new_user()
        res = a.request("typing", {"chat_id": 999999, "state": "typing"})
        self.assertEqual(res["d"], {})
        self.assertErr(a.request("typing", {"chat_id": 1, "state": "sleeping"}), "bad_request")


class PresenceTests(support.HubTestCase):
    scale = 0.1

    def presence(self, observer: Any, user_id: int, since: int, online: bool, timeout: float = 5.0) -> Any:
        return observer.wait_event(
            "ev.presence",
            lambda f: f["d"]["user_id"] == user_id and f["d"]["online"] == online,
            since=since,
            timeout=timeout,
        )

    def test_online_is_broadcast_once_and_offline_after_the_grace_with_the_real_last_seen(self) -> None:
        observer = self.new_user("obs")
        user = self.new_user("pres", connect=False)
        mark = observer.mark()
        user.connect()
        online = self.presence(observer, user.me["id"], mark, True)
        self.assertEqual(online["d"], {"user_id": user.me["id"], "online": True, "last_seen": None})
        mark = observer.mark()
        tab = self.another_tab(user)
        tab.request("ping", {})
        self.barrier(observer)
        observer.expect_none(
            "ev.presence", lambda f: f["d"]["user_id"] == user.me["id"], since=mark
        )  # second tab: silence
        tab.abort()
        time.sleep(0.3)
        self.barrier(observer)
        observer.expect_none("ev.presence", lambda f: f["d"]["user_id"] == user.me["id"], since=mark)  # one tab left
        self.assertTrue(user.request("ping", {})["ok"])
        last_frame = time.time()
        user.abort()
        offline = self.presence(observer, user.me["id"], mark, False)
        self.assertLess(abs(offline["d"]["last_seen"] - last_frame), 2.0)

    def test_a_reconnect_inside_the_grace_period_broadcasts_nothing(self) -> None:
        observer = self.new_user("obs")
        user = self.new_user("flap")
        mark = observer.mark()
        user.abort()
        again = user.clone("again")
        self.addCleanup(again.abort)
        again.connect()
        time.sleep(1.3)  # well past scaled(8) = 0.8 s
        self.barrier(observer)
        observer.expect_none("ev.presence", lambda f: f["d"]["user_id"] == user.me["id"], since=mark)

    def test_last_seen_is_hidden_when_the_user_asks_for_it(self) -> None:
        observer = self.new_user("obs")
        user = self.new_user("shy")
        self.assertTrue(user.request("profile.update", {"show_last_seen": False})["ok"])
        mark = observer.mark()
        user.abort()
        offline = self.presence(observer, user.me["id"], mark, False)
        self.assertIsNone(offline["d"]["last_seen"])
        late = self.new_user("late")
        entry = next(u for u in late.ready["users"] if u["id"] == user.me["id"])
        self.assertFalse(entry["online"])
        self.assertIsNone(entry["last_seen"])

    def test_ev_ready_marks_who_is_online(self) -> None:
        a, b = self.new_user(), self.new_user()
        b.abort()
        time.sleep(1.2)
        late = self.new_user()
        flags = {u["id"]: u["online"] for u in late.ready["users"]}
        self.assertTrue(flags[a.me["id"]])
        self.assertFalse(flags[b.me["id"]])
        self.assertIn(a.me["id"], self.hub.online_user_ids())
        self.assertNotIn(b.me["id"], self.hub.online_user_ids())


class BacklogTests(support.HubTestCase):
    def test_events_during_the_snapshot_are_replayed_after_ev_ready_receipts_last(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        authored = self.send(b, chat_id, "b wrote this")["d"]["message"]
        b.abort()
        original = db_chats.build_ready

        def slow(conn: Any, *args: Any, **kwargs: Any) -> Any:
            snapshot = original(conn, *args, **kwargs)
            time.sleep(0.6)  # the connection stays pending while this snapshot ages
            return snapshot

        again = b.clone("b-again")
        self.addCleanup(again.abort)
        with mock.patch.object(db_chats, "build_ready", slow):
            worker = threading.Thread(target=again.connect, daemon=True)
            worker.start()
            time.sleep(0.25)
            late = self.send(a, chat_id, "while b is pending")["d"]["message"]
            self.assertTrue(a.request("receipt.read", {"chat_id": chat_id, "up_to_id": authored["id"]})["ok"])
            worker.join(10)
        self.assertEqual(again.frames[0]["t"], "ev.ready")
        message = again.wait_event("ev.message", lambda f: f["d"]["message"]["id"] == late["id"])
        receipt = again.wait_event(
            "ev.receipt", lambda f: f["d"]["chat_id"] == chat_id and f["d"]["user_id"] == a.me["id"]
        )
        self.assertGreater(message.seq, again.frames[0].seq)
        self.assertGreater(receipt.seq, message.seq)  # backlog_receipts follow the durable backlog
        snapshot = next(c for c in again.ready["chats"] if c["id"] == chat_id)
        self.assertLess(snapshot["last_message_id"], late["id"])  # it really was newer than the snapshot


class ExternalChangeTests(support.HubTestCase):
    scale = 0.1

    def test_cli_create_admin_emits_what_the_same_action_through_the_hub_emits(self) -> None:
        a, b = self.new_user(), self.new_user()
        everyone = self.everyone_id()
        marks = {s.name: s.mark() for s in (a, b)}
        before = self.hub.counters["external_change_calls"]
        user = maintenance.create_admin(self.srv.data_dir, "cli.admin", "cli admin password", "Cli Admin")
        for session in (a, b):
            update = session.wait_event(
                "ev.user_update", lambda f: f["d"]["user"]["id"] == user["id"], since=marks[session.name]
            )
            added = session.wait_event(
                "ev.chat_members",
                lambda f: [m["user_id"] for m in f["d"]["added"]] == [user["id"]],
                since=marks[session.name],
            )
            joined = session.wait_event(
                "ev.message",
                lambda f: f["d"]["message"]["system"] and f["d"]["message"]["system"]["target_ids"] == [user["id"]],
                since=marks[session.name],
            )
            self.assertTrue(update.seq < added.seq < joined.seq)
            self.assertEqual(added["d"]["chat_id"], everyone)
            self.assertEqual(joined["d"]["message"]["system"]["event"], "joined")
            self.assertFalse(update["d"]["user"]["activated"])
            self.assertFalse(update["d"]["user"]["online"])
            self.barrier(session)
            session.expect_none("ev.chat_update", lambda f: f["d"]["chat"]["id"] == everyone, since=marks[session.name])
        self.assertGreater(self.hub.counters["external_change_calls"], before)
        # it is not announced a second time by later polls
        self.srv.call(self.hub.external_change())
        self.barrier(a)
        self.assertEqual(len(a.events_matching("ev.user_update", lambda f: f["d"]["user"]["id"] == user["id"])), 1)

    def test_cli_promotion_updates_the_role_and_ends_the_sessions(self) -> None:
        a, target = self.new_user(), self.new_user()
        marks = {s.name: s.mark() for s in (a, target)}
        maintenance.create_admin(self.srv.data_dir, target.me["username"], "promoted password")
        update = a.wait_event("ev.user_update", lambda f: f["d"]["user"]["id"] == target.me["id"], since=marks[a.name])
        members = a.wait_event("ev.chat_members", lambda f: f["d"]["updated"], since=marks[a.name])
        self.assertEqual(update["d"]["user"]["role"], "admin")
        self.assertEqual([(m["user_id"], m["role"]) for m in members["d"]["updated"]], [(target.me["id"], "admin")])
        self.assertLess(update.seq, members.seq)
        self.assertEqual(target.wait_closed(), 4001)  # the CLI deleted the sessions
        self.assertEqual(target.wait_event("ev.kicked")["d"], {"reason": "revoked"})

    def test_cli_reset_password_kicks_the_user(self) -> None:
        a = self.new_user()
        maintenance.reset_password(self.srv.data_dir, a.me["username"], "reset by the cli", must_change=True)
        self.assertEqual(a.wait_closed(), 4001)
        kicked = a.wait_event("ev.kicked")
        self.assertEqual(kicked["d"], {"reason": "revoked"})
        self.assertLess(kicked.seq, a.close_seq)

    def test_revalidate_all_kicks_vanished_sessions(self) -> None:
        a, b = self.new_user(), self.new_user()
        before = self.hub.counters["revalidate_calls"]
        self.srv.server.db.run_sync(lambda conn: conn.execute("DELETE FROM sessions WHERE user_id = ?", (a.me["id"],)))
        self.srv.call(self.hub.revalidate_all())
        self.assertEqual(a.wait_event("ev.kicked")["d"], {"reason": "revoked"})
        self.assertEqual(a.wait_closed(), 4001)
        self.assertGreater(self.hub.counters["revalidate_calls"], before)
        self.assertTrue(b.request("ping", {})["ok"])


class NoRevalidationTests(support.HubTestCase):
    scale = 0.1

    def test_committing_many_messages_triggers_no_control_poll_work(self) -> None:
        a, b = self.new_user(), self.new_user()
        chat_id = self.make_group(a, b)
        time.sleep(0.8)  # let the polls of the setup settle
        before = dict(self.hub.counters)
        for start in range(0, 300, 30):  # batches stay far below the 64 requests in flight
            ids = [
                a.request_nowait("msg.send", {"chat_id": chat_id, "client_id": "bulk-%06d" % i, "body": "m%d" % i})
                for i in range(start, start + 30)
            ]
            for rid in ids:
                self.assertTrue(a.wait_res(rid, timeout=30)["ok"])
        time.sleep(1.0)  # five polls
        self.assertEqual(self.hub.counters["external_change_calls"], before["external_change_calls"])
        self.assertEqual(self.hub.counters["revalidate_calls"], before["revalidate_calls"])


class ShutdownTests(support.HubTestCase):
    def test_a_stopping_hub_refuses_new_requests_with_server_error(self) -> None:
        a = self.new_user()
        self.srv.run(lambda: setattr(self.hub, "stopping", True))
        try:
            err = self.assertErr(a.request("chat.get", {"chat_id": 1}), "server_error")
            self.assertIn("restart", err["msg"])
        finally:
            self.srv.run(lambda: setattr(self.hub, "stopping", False))
        self.assertTrue(a.request("ping", {})["ok"])


if __name__ == "__main__":
    unittest.main()
