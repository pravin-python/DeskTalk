"""``profile.update`` and the ``admin.*`` requests: results, events, audit, kicks (SPEC 7.3, 7.4, 4.3)."""

from __future__ import annotations

import unittest
from typing import Any

try:
    from . import test_hub_support as support
except ImportError:
    import test_hub_support as support

PASSWORD = support.PASSWORD


class ProfileTests(support.HubTestCase):
    def test_profile_update_goes_to_everybody_and_ev_me_to_the_actor_tabs_only(self) -> None:
        a, b = self.new_user(), self.new_user()
        tab = self.another_tab(a)
        marks = {s.name: s.mark() for s in (a, b, tab)}
        res = a.request(
            "profile.update", {"display_name": "  Renamed   Person ", "status_text": "at lunch", "read_receipts": False}
        )
        self.assertTrue(res["ok"], res)
        me = res["d"]["me"]
        self.assertEqual(
            (me["display_name"], me["status_text"], me["read_receipts"]), ("Renamed Person", "at lunch", False)
        )
        self.assertIn("show_last_seen", me)
        self.assertIn("must_change_password", me)
        for session in (a, b, tab):
            update = session.wait_event(
                "ev.user_update", lambda f: f["d"]["user"]["id"] == a.me["id"], since=marks[session.name]
            )
            self.assertEqual(update["d"]["user"]["display_name"], "Renamed Person")
            self.assertTrue(update["d"]["user"]["online"])
            self.assertNotIn("show_last_seen", update["d"]["user"])  # private fields stay in ev.me
        for session in (a, tab):
            self.assertEqual(
                session.wait_event("ev.me", since=marks[session.name])["d"]["me"]["display_name"], "Renamed Person"
            )
        self.barrier(b)
        b.expect_none("ev.me", since=marks[b.name])
        self.assertLess(a.wait_event("ev.user_update", since=marks[a.name]).seq, res.seq)

    def test_noop_conflicts_and_shape(self) -> None:
        a, b = self.new_user(), self.new_user()
        self.assertTrue(a.request("profile.update", {"status_text": "same"})["ok"])
        mark = b.mark()
        self.assertTrue(a.request("profile.update", {"status_text": "same"})["ok"])
        self.barrier(b)
        b.expect_none("ev.user_update", since=mark)
        err = self.assertErr(a.request("profile.update", {"display_name": b.me["display_name"]}), "conflict")
        self.assertEqual(err["reason"], "name_taken")
        self.assertErr(a.request("profile.update", {"display_name": "Admin"}), "conflict", "name_taken")
        self.assertErr(a.request("profile.update", {"read_receipts": "no"}), "bad_request")
        self.assertErr(a.request("profile.update", {"display_name": "x" * 41}), "bad_request")
        self.assertErr(a.request("profile.update", {"status_text": "x" * 141}), "bad_request")


class AdminTests(support.HubTestCase):
    def test_every_admin_request_is_forbidden_for_members(self) -> None:
        a = self.new_user()
        for name, data in (
            ("admin.users", {}),
            ("admin.create_user", {"username": "sneaky", "display_name": "Sneaky", "password": PASSWORD}),
            ("admin.update_user", {"user_id": a.me["id"], "role": "admin"}),
            ("admin.reset_password", {"user_id": a.me["id"], "new_password": PASSWORD}),
            ("admin.settings", {}),
            ("admin.settings", {"registration_open": True}),
            ("admin.stats", {}),
            ("admin.audit", {}),
        ):
            self.assertErr(a.request(name, data), "forbidden")

    def test_users_stats_and_audit(self) -> None:
        a = self.new_user()
        users = self.admin.request("admin.users", {})["d"]["users"]
        mine = next(u for u in users if u["id"] == a.me["id"])
        self.assertEqual(
            set(mine),
            {
                "id",
                "username",
                "display_name",
                "status_text",
                "role",
                "online",
                "last_seen",
                "disabled",
                "activated",
                "read_receipts",
                "created_at",
                "last_login_at",
                "message_count",
                "must_change_password",
            },
        )
        self.assertTrue(mine["online"])
        stats = self.admin.request("admin.stats", {})["d"]
        for key in (
            "users",
            "online",
            "chats",
            "messages",
            "attachments",
            "storage_bytes",
            "db_bytes",
            "disk_free_bytes",
            "last_backup_at",
            "uptime_s",
            "python",
            "version",
            "urls",
        ):
            self.assertIn(key, stats)
        self.assertGreaterEqual(stats["users"], 2)
        self.assertGreater(stats["db_bytes"], 0)
        self.assertGreater(stats["disk_free_bytes"], 0)
        self.assertTrue(all(u.startswith("http://") and u.endswith("/") for u in stats["urls"]))
        entries = self.admin.request("admin.audit", {"limit": 3})["d"]["entries"]
        self.assertLessEqual(len(entries), 3)
        self.assertEqual(set(entries[0]), {"id", "ts", "actor_id", "action", "target_id", "ip"})
        self.assertErr(self.admin.request("admin.audit", {"limit": "3"}), "bad_request")

    def test_settings_read_writes_nothing_and_a_change_is_broadcast(self) -> None:
        a = self.new_user()
        before = len(self.admin.request("admin.audit", {})["d"]["entries"])
        mark_admin, mark_a = self.admin.mark(), a.mark()
        read = self.admin.request("admin.settings", {})
        self.assertEqual(read["d"]["workspace"]["join_code"], self.join_code)
        self.admin.expect_none("ev.workspace", since=mark_admin)
        a.expect_none("ev.workspace", since=mark_a)
        self.assertEqual(len(self.admin.request("admin.audit", {})["d"]["entries"]), before)  # a read appends nothing
        res = self.admin.request("admin.settings", {"workspace_name": "  Acme   HQ "})
        self.assertEqual(res["d"]["workspace"]["name"], "Acme HQ")
        for session in (self.admin, a):
            ev = session.wait_event(
                "ev.workspace", lambda f: f["d"]["name"] == "Acme HQ", since=mark_a if session is a else mark_admin
            )
            self.assertEqual(set(ev["d"]), {"name", "registration_open"})  # the join code is never broadcast
        mark = a.mark()
        again = self.admin.request("admin.settings", {"workspace_name": "Acme HQ"})
        self.assertTrue(again["ok"])
        self.barrier(a)
        a.expect_none("ev.workspace", since=mark)
        rotated = self.admin.request("admin.settings", {"rotate_join_code": True})["d"]["workspace"]["join_code"]
        self.assertNotEqual(rotated, self.join_code)
        self.admin.request("admin.settings", {"workspace_name": "DeskTalk"})
        type(self).join_code = rotated
        self.assertErr(self.admin.request("admin.settings", {"workspace_name": "x" * 41}), "bad_request")
        self.assertErr(self.admin.request("admin.settings", {"registration_open": "yes"}), "bad_request")

    def test_create_user_flow_and_errors(self) -> None:
        a = self.new_user()
        marks = {s.name: s.mark() for s in (self.admin, a)}
        res = self.admin.request(
            "admin.create_user", {"username": "Fresh.Hire", "display_name": "  Fresh   Hire ", "password": PASSWORD}
        )
        self.assertTrue(res["ok"], res)
        user = res["d"]["user"]
        self.assertEqual((user["username"], user["display_name"]), ("fresh.hire", "Fresh Hire"))
        self.assertFalse(user["activated"])
        self.assertEqual(user["role"], "member")
        # existing members: user_update -> chat_members {added} -> `joined`
        update = a.wait_event("ev.user_update", lambda f: f["d"]["user"]["id"] == user["id"], since=marks[a.name])
        added = a.wait_event(
            "ev.chat_members", lambda f: [m["user_id"] for m in f["d"]["added"]] == [user["id"]], since=marks[a.name]
        )
        joined = a.wait_event(
            "ev.message", lambda f: f["d"]["message"]["system"]["event"] == "joined", since=marks[a.name]
        )
        self.assertTrue(update.seq < added.seq < joined.seq)
        self.assertLess(
            self.admin.wait_event(
                "ev.message", lambda f: f["d"]["message"]["system"]["event"] == "joined", since=marks[self.admin.name]
            ).seq,
            res.seq,
        )
        self.assertErr(
            self.admin.request(
                "admin.create_user", {"username": "fresh.hire", "display_name": "Other One", "password": PASSWORD}
            ),
            "conflict",
            "username_taken",
        )
        self.assertErr(
            self.admin.request(
                "admin.create_user", {"username": "other.hire", "display_name": "fresh hire", "password": PASSWORD}
            ),
            "conflict",
            "name_taken",
        )
        self.assertErr(
            self.admin.request(
                "admin.create_user", {"username": "weak.hire", "display_name": "Weak Hire", "password": "short"}
            ),
            "bad_request",
            "weak_password",
        )
        self.assertErr(
            self.admin.request(
                "admin.create_user", {"username": "no", "display_name": "Weak Hire", "password": PASSWORD}
            ),
            "bad_request",
        )
        reserved = self.admin.request(
            "admin.create_user", {"username": "helpdesk", "display_name": "Help Desk", "password": PASSWORD}
        )
        self.assertTrue(reserved["ok"], reserved)  # reserved usernames are allowed for admins
        made_admin = self.admin.request(
            "admin.create_user",
            {"username": "second.root", "display_name": "Second Root", "password": PASSWORD, "role": "admin"},
        )
        self.assertEqual(made_admin["d"]["user"]["role"], "admin")

    def test_update_user_role_change_updates_everyone_and_the_member_entry(self) -> None:
        a, b = self.new_user(), self.new_user()
        marks = {s.name: s.mark() for s in (a, b)}
        res = self.admin.request("admin.update_user", {"user_id": b.me["id"], "role": "admin"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["d"]["user"]["role"], "admin")
        update = a.wait_event("ev.user_update", lambda f: f["d"]["user"]["id"] == b.me["id"], since=marks[a.name])
        members = a.wait_event("ev.chat_members", lambda f: f["d"]["updated"], since=marks[a.name])
        self.assertLess(update.seq, members.seq)
        self.assertEqual([(m["user_id"], m["role"]) for m in members["d"]["updated"]], [(b.me["id"], "admin")])
        self.assertEqual(update["d"]["user"]["role"], "admin")
        a.expect_none("ev.chat_update", since=marks[a.name])  # never a full chat_update of Everyone
        # back to member; no-op emits nothing
        self.assertTrue(self.admin.request("admin.update_user", {"user_id": b.me["id"], "role": "member"})["ok"])
        mark = a.mark()
        self.assertTrue(self.admin.request("admin.update_user", {"user_id": b.me["id"], "role": "member"})["ok"])
        self.barrier(a)
        a.expect_none("ev.user_update", since=mark)
        self.assertErr(self.admin.request("admin.update_user", {"user_id": 999999, "role": "admin"}), "not_found")
        self.assertErr(self.admin.request("admin.update_user", {"user_id": b.me["id"]}), "bad_request")
        self.assertErr(self.admin.request("admin.update_user", {"user_id": b.me["id"], "role": "owner"}), "bad_request")
        self.assertErr(
            self.admin.request("admin.update_user", {"user_id": b.me["id"], "display_name": a.me["display_name"]}),
            "conflict",
            "name_taken",
        )

    def test_disable_kicks_the_target_and_promotes_in_groups(self) -> None:
        a, b, c = self.new_user(), self.new_user(), self.new_user()
        tab = self.another_tab(b)
        group = self.make_group(b, a, c)  # b is the only admin of the group
        direct = a.request("chat.open_direct", {"user_id": b.me["id"]})["d"]["chat"]["id"]
        self.send(a, direct, "before the disable")
        self.assertTrue(self.admin.request("admin.update_user", {"user_id": b.me["id"], "role": "member"})["ok"])
        marks = {s.name: s.mark() for s in (a, b, c, tab)}
        res = self.admin.request("admin.update_user", {"user_id": b.me["id"], "disabled": True})
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["d"]["user"]["disabled"])
        for session in (b, tab):
            self.assertEqual(session.wait_closed(), 4001)
            kicked = session.wait_event("ev.kicked", since=marks[session.name])
            self.assertEqual(kicked["d"], {"reason": "disabled"})
            self.assertLess(kicked.seq, session.close_seq)
        # everybody else: user_update, then (for the group) chat_members {updated:[promoted]} then `promoted`
        update = c.wait_event("ev.user_update", lambda f: f["d"]["user"]["id"] == b.me["id"], since=marks[c.name])
        self.assertTrue(update["d"]["user"]["disabled"])
        members = c.wait_event(
            "ev.chat_members", lambda f: f["d"]["chat_id"] == group and f["d"]["updated"], since=marks[c.name]
        )
        promoted = c.wait_event(
            "ev.message",
            lambda f: f["d"]["message"]["system"] and f["d"]["message"]["system"]["event"] == "promoted",
            since=marks[c.name],
        )
        self.assertTrue(update.seq < members.seq < promoted.seq)
        self.assertEqual(members["d"]["updated"][0]["user_id"], a.me["id"])  # smallest joined_at among enabled members
        self.assertEqual(members["d"]["updated"][0]["role"], "admin")
        # a disabled user cannot log in and has no sessions left; chats with them stay listed but take no new messages
        self.assertEqual(self.login_status(b), 403)
        left = self.srv.server.db.run_sync(
            lambda conn: conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (b.me["id"],)).fetchone()[0]
        )
        self.assertEqual(left, 0)
        self.assertErr(
            a.request("msg.send", {"chat_id": direct, "client_id": "disabled-peer-1", "body": "hi"}), "invalid_state"
        )
        fresh = self.new_user()
        self.assertErr(fresh.request("chat.open_direct", {"user_id": b.me["id"]}), "invalid_state")

    def login_status(self, user: Any) -> int:
        probe = user.clone("probe")
        probe.token = None
        return probe.login(user.me["username"], PASSWORD).status

    def test_reset_password_kicks_the_target_and_forces_a_change(self) -> None:
        a = self.new_user()
        tab = self.another_tab(a)
        marks = {s.name: s.mark() for s in (a, tab)}
        self.assertErr(
            self.admin.request("admin.reset_password", {"user_id": 999999, "new_password": PASSWORD}), "not_found"
        )
        self.assertErr(
            self.admin.request("admin.reset_password", {"user_id": a.me["id"], "new_password": "short"}),
            "bad_request",
            "weak_password",
        )
        res = self.admin.request("admin.reset_password", {"user_id": a.me["id"], "new_password": "temporary secret pw"})
        self.assertEqual(res["d"], {})
        for session in (a, tab):
            self.assertEqual(session.wait_closed(), 4001)
            kicked = session.wait_event("ev.kicked", since=marks[session.name])
            self.assertEqual(kicked["d"], {"reason": "revoked"})
            self.assertLess(kicked.seq, session.close_seq)
        relog = a.clone("relog")
        relog.token = None
        self.assertEqual(relog.login(a.me["username"], PASSWORD).status, 401)
        res = relog.login(a.me["username"], "temporary secret pw")
        self.assertEqual(res.status, 200)
        self.assertTrue(res.json["me"]["must_change_password"])
        audit = self.admin.request("admin.audit", {"limit": 5})["d"]["entries"]
        self.assertTrue(any(e["action"] == "admin.reset_password" and e["target_id"] == a.me["id"] for e in audit))

    def test_a_disabled_but_not_yet_kicked_socket_cannot_act(self) -> None:
        a = self.new_user()
        chat_id = self.make_group(a)
        # simulate a change that bypassed hub.revoke (the 5-minute sweep is only a backstop)
        self.srv.server.db.run_sync(
            lambda conn: conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (a.me["id"],))
        )
        res = a.request("msg.send", {"chat_id": chat_id, "client_id": "ghost-0001", "body": "x"})
        self.assertErr(res, "unauthorized")
        self.assertEqual(a.wait_event("ev.kicked")["d"], {"reason": "disabled"})
        self.assertEqual(a.wait_closed(), 4001)


class LastAdminTests(support.HubTestCase):
    def test_the_last_enabled_admin_can_neither_be_demoted_nor_disabled(self) -> None:
        me = self.admin.me["id"]
        self.assertErr(
            self.admin.request("admin.update_user", {"user_id": me, "role": "member"}), "invalid_state", "last_admin"
        )
        self.assertErr(
            self.admin.request("admin.update_user", {"user_id": me, "disabled": True}), "invalid_state", "last_admin"
        )
        # with a second admin the first may step down, and then the second is the last one
        other = self.new_user()
        self.assertTrue(self.admin.request("admin.update_user", {"user_id": other.me["id"], "role": "admin"})["ok"])
        self.assertTrue(self.admin.request("admin.update_user", {"user_id": me, "role": "member"})["ok"])
        self.assertErr(
            other.request("admin.update_user", {"user_id": other.me["id"], "role": "member"}),
            "invalid_state",
            "last_admin",
        )
        self.assertErr(self.admin.request("admin.users", {}), "forbidden")  # the demoted admin lost the right at once


if __name__ == "__main__":
    unittest.main()
