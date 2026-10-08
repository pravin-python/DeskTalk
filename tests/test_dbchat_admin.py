"""admin.update_user of ``chatd/db_chats.py``: role mirror, disable, promotions, last-admin invariants (SPEC 7.4)."""

from __future__ import annotations

import unittest

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db_chats


class AdminUpdateUserTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.user("root2")  # first user: workspace admin and Everyone admin
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")

    def update(self, actor: int, user: int, **kwargs: object) -> dict:
        return self.write(
            db_chats.admin_update_user,
            actor,
            user,
            kwargs.get("role"),
            kwargs.get("disabled"),
            kwargs.get("display_name"),
            kwargs.get("ip"),
        )

    def test_role_change_mirrors_the_default_group(self) -> None:
        out = self.update(self.root, self.a, role="admin", ip="10.0.0.5")
        self.assertEqual(out["res"]["user"]["role"], "admin")
        self.assertEqual(self.types(out), ["ev.user_update", "ev.chat_members"])
        self.assertIsNone(out["events"][0]["groups"][0]["user_ids"])  # everybody
        members = out["events"][1]["groups"][0]
        self.assertEqual(
            members["d"]["updated"],
            [
                {
                    "user_id": self.a,
                    "role": "admin",
                    "delivered_up_to": members["d"]["updated"][0]["delivered_up_to"],
                    "read_up_to": members["d"]["updated"][0]["read_up_to"],
                }
            ],
        )
        self.assertEqual(sorted(members["user_ids"]), [self.root, self.a, self.b, self.c])
        self.assertEqual(
            self.scalar("SELECT role FROM chat_members WHERE chat_id = ? AND user_id = ?", (self.everyone, self.a)),
            "admin",
        )
        self.assertEqual(out["touched_chats"], [self.everyone])
        audit = self.sql("SELECT actor_id, action, target_id, ip FROM audit_log")
        self.assertEqual(audit, [(self.root, "admin.update_user", self.a, "10.0.0.5")])
        # no implicit power in other groups: group admin roles are untouched
        group = self.group(self.b, self.a)
        self.assertEqual(
            self.scalar("SELECT role FROM chat_members WHERE chat_id = ? AND user_id = ?", (group, self.a)), "member"
        )

    def test_noop_writes_nothing(self) -> None:
        out = self.update(self.root, self.a, role="member", disabled=False, display_name="Alice")
        self.assertTrue(out["noop"])
        self.assertEqual(out["events"], [])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_log"), 0)

    def test_display_name(self) -> None:
        out = self.update(self.root, self.a, display_name="  Alice   Smith ")
        self.assertEqual(out["res"]["user"]["display_name"], "Alice Smith")
        self.assertEqual(self.scalar("SELECT display_key FROM users WHERE id = ?", (self.a,)), "alice smith")
        self.assertEqual(self.types(out), ["ev.user_update"])
        self.fails(
            "conflict", db_chats.admin_update_user, self.root, self.b, None, None, "ALICE smith", reason="name_taken"
        )
        self.fails("conflict", db_chats.admin_update_user, self.root, self.b, None, None, "carol", reason="name_taken")
        self.fails("bad_request", db_chats.admin_update_user, self.root, self.b, None, None, "   ")
        self.fails("bad_request", db_chats.admin_update_user, self.root, self.b, None, None, "x" * 41)
        # a workspace admin may use a reserved display name
        self.update(self.root, self.b, display_name="Admin")

    def test_self_update_also_sends_ev_me(self) -> None:
        out = self.update(self.root, self.root, display_name="Boss")
        self.assertEqual(self.types(out), ["ev.user_update", "ev.me"])
        self.assertEqual(self.audience(out, "ev.me"), {self.root})
        self.assertEqual(out["events"][1]["groups"][0]["d"]["me"]["display_name"], "Boss")

    def test_last_admin_invariants(self) -> None:
        self.fails("invalid_state", db_chats.admin_update_user, self.root, self.root, "member", reason="last_admin")
        self.fails("invalid_state", db_chats.admin_update_user, self.root, self.root, None, True, reason="last_admin")
        self.update(self.root, self.a, role="admin")
        self.update(self.root, self.root, role="member")  # alice is still an active admin
        self.fails("invalid_state", db_chats.admin_update_user, self.a, self.a, None, True, reason="last_admin")
        self.fails("invalid_state", db_chats.admin_update_user, self.a, self.a, "member", reason="last_admin")
        self.update(self.a, self.b, role="admin")
        self.update(self.a, self.a, role="member")  # bob can take over
        self.fails("invalid_state", db_chats.admin_update_user, self.b, self.b, None, True)

    def test_errors_and_order(self) -> None:
        self.fails("bad_request", db_chats.admin_update_user, self.root, self.a)
        self.fails("bad_request", db_chats.admin_update_user, self.root, self.a, "owner")
        self.fails("bad_request", db_chats.admin_update_user, self.root, self.a, None, "yes")
        self.fails("forbidden", db_chats.admin_update_user, self.b, self.a, "admin")
        self.fails("not_found", db_chats.admin_update_user, self.root, 99999, "admin")
        self.fails("not_found", db_chats.admin_update_user, self.b, 99999, "admin")  # resolve before the role check
        self.set_user(self.root, disabled=1)
        self.fails("unauthorized", db_chats.admin_update_user, self.root, self.a, "admin")

    def test_disable_promotes_in_every_affected_group_in_ascending_order(self) -> None:
        first = self.group(self.a, self.b, self.c, title="first")
        second = self.group(self.b, self.a, self.c, title="second")  # alice is a plain member here
        third = self.group(self.a, self.c, title="third")
        self.write(db_chats.chat_add_members, self.a, third, [self.b])  # carol joined earlier than bob
        self.write(db_chats.chat_set_admin, self.b, second, self.c, True)
        self.assertEqual(self.read(db_chats.admin_user_chat_ids, self.a), [self.everyone, first, third])
        out = self.update(self.root, self.a, disabled=True)
        types = self.types(out)
        self.assertEqual(types[0], "ev.user_update")
        self.assertEqual(out["events"][1]["t"], "hub.revoke")
        self.assertEqual((out["events"][1]["user_id"], out["events"][1]["reason"]), (self.a, "disabled"))
        self.assertEqual(types[2:], ["ev.chat_members", "ev.message", "ev.chat_members", "ev.message"])
        updated = [
            (e["groups"][0]["d"]["chat_id"], [m["user_id"] for m in e["groups"][0]["d"]["updated"]])
            for e in out["events"]
            if e["t"] == "ev.chat_members"
        ]
        self.assertEqual(updated, [(first, [self.b]), (third, [self.c])])  # smallest joined_at, then lowest user id
        self.assertEqual(out["touched_chats"], [first, third])
        systems = [g["d"]["message"]["system"] for e in out["events"] if e["t"] == "ev.message" for g in e["groups"]]
        self.assertEqual(
            [(s["event"], s["actor_id"], s["target_ids"]) for s in systems],
            [("promoted", None, [self.b]), ("promoted", None, [self.c])],
        )
        self.assertEqual(
            self.scalar("SELECT role FROM chat_members WHERE chat_id = ? AND user_id = ?", (second, self.a)), "member"
        )
        self.assertEqual(
            self.scalar("SELECT role FROM chat_members WHERE chat_id = ? AND user_id = ?", (first, self.a)), "admin"
        )

    def test_disable_keeps_groups_that_still_have_an_enabled_admin_and_updates_direct_index(self) -> None:
        group = self.group(self.a, self.b, self.c)
        self.write(db_chats.chat_set_admin, self.a, group, self.b, True)
        direct = self.dm(self.a, self.c)
        out = self.update(self.root, self.a, disabled=True)
        self.assertEqual(out["touched_chats"], [])
        self.assertNotIn("ev.chat_members", self.types(out))
        by_chat = {entry["chat_id"]: entry for entry in out["index"]}
        self.assertTrue(by_chat[direct]["peer_disabled"])
        self.assertTrue(self.read(db_chats.load_membership_index)[direct]["peer_disabled"])

    def test_reenable(self) -> None:
        self.update(self.root, self.a, disabled=True)
        out = self.update(self.root, self.a, disabled=False)
        self.assertEqual(self.types(out), ["ev.user_update"])
        self.assertFalse(out["res"]["user"]["disabled"])

    def test_disabled_users_are_blocked_everywhere_after_the_update(self) -> None:
        self.update(self.root, self.a, disabled=True)
        self.fails("unauthorized", db_chats.chat_get, self.a, self.everyone, reader=True)
        self.fails("unauthorized", db_chats.chat_open_direct, self.a, self.b)
        self.fails("unauthorized", db_chats.chat_create_group, self.a, "T", [])


if __name__ == "__main__":
    unittest.main()
