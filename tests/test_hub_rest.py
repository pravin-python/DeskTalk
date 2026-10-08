"""REST flows of ``api.py``: setup, register, login, logout, me, password, sessions (SPEC 4.1 - 4.3)."""

from __future__ import annotations

import os
import threading
import unittest
from typing import Any, Dict, Optional

try:
    from . import test_hub_support as support
    from .wsclient import ChatSession
except ImportError:
    import test_hub_support as support
    from wsclient import ChatSession  # type: ignore[no-redef]

PASSWORD = support.PASSWORD


class FreshServerCase(unittest.TestCase):
    """A new server per test: the first-run flows consume the setup code."""

    limits: Dict[str, Any] = {}
    scale = 1.0

    def setUp(self) -> None:
        self.srv = support.HubServer(self.scale, self.limits).start()
        self.addCleanup(self.srv.stop)
        self.sessions = []

    def session(self, name: str = "") -> ChatSession:
        s = ChatSession(self.srv.port, name=name)
        self.sessions.append(s)
        self.addCleanup(s.abort)
        return s

    def first_admin(self) -> ChatSession:
        admin = self.session("admin")
        res = admin.register("root1", "Root One", PASSWORD, setup_code=self.srv.setup_code())
        self.assertEqual(res.status, 201, res)
        return admin

    def open_registration(self, admin: ChatSession) -> str:
        admin.connect()
        res = admin.request("admin.settings", {"registration_open": True})
        self.assertTrue(res["ok"], res)
        return res["d"]["workspace"]["join_code"]

    def assertHttp(self, res: Any, status: int, code: Optional[str] = None, reason: Optional[str] = None) -> None:
        self.assertEqual(res.status, status, res)
        if code is not None:
            self.assertEqual(res.code, code, res)
        if reason is not None:
            self.assertEqual(res.error.get("reason"), reason, res)


class SetupTests(FreshServerCase):
    def test_info_before_and_after_setup(self) -> None:
        anon = self.session()
        info = anon.get("/api/info")
        self.assertEqual(info.status, 200)
        self.assertEqual(set(info.json), {"name", "registration_open", "needs_setup", "tls"})
        self.assertTrue(info.json["needs_setup"])
        self.assertFalse(info.json["registration_open"])
        self.assertFalse(info.json["tls"])
        self.first_admin()
        info = anon.get("/api/info")
        self.assertFalse(info.json["needs_setup"])
        self.assertFalse(os.path.exists(os.path.join(self.srv.data_dir, "setup_code.txt")))
        self.assertNotIn("version", info.json)

    def test_the_setup_code_is_required_and_checked(self) -> None:
        anon = self.session()
        self.assertHttp(anon.register("root1", "Root One", PASSWORD), 403, "setup_code_required")
        self.assertHttp(anon.register("root1", "Root One", PASSWORD, setup_code="wrongcode"), 403, "bad_setup_code")
        res = anon.register("root1", "Root One", PASSWORD, setup_code=self.srv.setup_code())
        self.assertHttp(res, 201)
        me = res.json["me"]
        self.assertEqual(me["role"], "admin")
        self.assertTrue(me["activated"])
        self.assertFalse(me["must_change_password"])
        cookie = res.set_cookies[0]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=%d" % (30 * 86400), cookie)
        self.assertNotIn("Secure", cookie)

    def test_wrong_setup_codes_are_throttled_per_ip(self) -> None:
        anon = self.session()
        for _ in range(5):
            self.assertHttp(
                anon.register("root1", "Root One", PASSWORD, setup_code="wrong-code"), 403, "bad_setup_code"
            )
        res = anon.register("root1", "Root One", PASSWORD, setup_code=self.srv.setup_code())
        self.assertHttp(res, 429, "rate_limited")
        self.assertGreater(res.error["retry_after"], 0)
        self.assertIn("retry-after", res.headers)

    def test_two_racing_first_registrations_make_exactly_one_admin(self) -> None:
        results = []
        code = self.srv.setup_code()

        def attempt(n: int) -> None:
            results.append(
                self.session("racer%d" % n).register("racer%d" % n, "Racer %d" % n, PASSWORD, setup_code=code)
            )

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(r.status for r in results), [201, 403, 403, 403], results)
        for loser in (r for r in results if r.status != 201):
            self.assertIn(loser.code, ("registration_closed", "setup_code_required", "bad_setup_code"))
        winner = next(r for r in results if r.status == 201)
        self.assertEqual(winner.json["me"]["role"], "admin")


class RegistrationTests(FreshServerCase):
    def test_closed_by_default_then_join_code(self) -> None:
        admin = self.first_admin()
        anon = self.session()
        self.assertHttp(anon.register("newbie", "New Bie", PASSWORD), 403, "registration_closed")
        join = self.open_registration(admin)
        self.assertHttp(anon.register("newbie", "New Bie", PASSWORD), 403, "bad_join_code")
        self.assertHttp(anon.register("newbie", "New Bie", PASSWORD, join_code="nope"), 403, "bad_join_code")
        res = anon.register("newbie", "New Bie", PASSWORD, join_code=join)
        self.assertHttp(res, 201)
        self.assertEqual(res.json["me"]["role"], "member")
        # the new member is announced to the admin: user_update -> chat_members {added} -> `joined`
        events = [e["t"] for _, e in admin.events if e["t"] in ("ev.user_update", "ev.chat_members", "ev.message")]
        admin.wait_event(
            "ev.message", lambda f: f["d"]["message"]["system"] and f["d"]["message"]["system"]["event"] == "joined"
        )
        tail = [e["t"] for _, e in admin.events if e["t"] in ("ev.user_update", "ev.chat_members", "ev.message")][-3:]
        self.assertEqual(tail, ["ev.user_update", "ev.chat_members", "ev.message"], events)
        added = admin.wait_event("ev.chat_members")
        self.assertEqual([m["user_id"] for m in added["d"]["added"]], [res.json["me"]["id"]])
        joined = admin.wait_event("ev.user_update", lambda f: f["d"]["user"]["username"] == "newbie")
        self.assertTrue(joined["d"]["user"]["activated"])
        self.assertFalse(joined["d"]["user"]["online"])

    def test_identity_rules(self) -> None:
        admin = self.first_admin()
        join = self.open_registration(admin)
        anon = self.session()
        self.assertHttp(anon.register("ROOT1", "Other Name", PASSWORD, join_code=join), 409, "username_taken")
        self.assertHttp(anon.register("root1x", "root   one", PASSWORD, join_code=join), 409, "name_taken")
        self.assertHttp(anon.register("admin", "Fine Name", PASSWORD, join_code=join), 409, "username_taken")
        self.assertHttp(anon.register("fine.user", "Admin", PASSWORD, join_code=join), 409, "name_taken")
        self.assertHttp(anon.register("root one", "Fine Name", PASSWORD, join_code=join), 400, "bad_request")
        self.assertHttp(anon.register("ab", "Fine Name", PASSWORD, join_code=join), 400, "bad_request")
        self.assertHttp(anon.register("fine.user", "   ", PASSWORD, join_code=join), 400, "bad_request")
        self.assertHttp(anon.register("fine.user", "Fine Name", "short", join_code=join), 400, "weak_password")
        self.assertHttp(anon.register("fine.user", "Fine Name", "password123", join_code=join), 400, "weak_password")
        self.assertHttp(anon.register("fine.user", "Fine Name", "fine.user", join_code=join), 400, "weak_password")
        self.assertHttp(anon.register("fine.user", "Fine Name", "x" * 200, join_code=join), 400, "weak_password")
        res = anon.http(
            "POST", "/api/register", body={"username": "fine.user", "display_name": "\ud800", "password": PASSWORD}
        )
        self.assertHttp(res, 400, "bad_request", "invalid_text")
        self.assertHttp(
            anon.http("POST", "/api/register", body={"username": 5, "display_name": "x", "password": PASSWORD}),
            400,
            "bad_request",
        )
        self.assertHttp(anon.register("fine.user", "Fine Name", PASSWORD, join_code=join), 201)

    def test_csrf_and_content_type_checks(self) -> None:
        anon = self.session()
        body = {"username": "x1x", "password": "whatever"}
        self.assertHttp(
            anon.http("POST", "/api/login", body=body, headers={"X-Requested-With": None}), 403, "forbidden"
        )
        self.assertHttp(
            anon.http("POST", "/api/login", data=b"{}", headers={"Content-Type": "text/plain"}),
            415,
            "unsupported_media_type",
        )
        self.assertHttp(
            anon.http("POST", "/api/login", data=b"[]", headers={"Content-Type": "application/json"}),
            400,
            "bad_request",
        )
        self.assertHttp(anon.http("GET", "/api/login"), 405, "method_not_allowed")

    def test_max_users_closes_registration(self) -> None:
        self.srv.cfg.max_users = 2
        admin = self.first_admin()
        join = self.open_registration(admin)
        anon = self.session()
        self.assertHttp(anon.register("second1", "Second One", PASSWORD, join_code=join), 201)
        self.assertHttp(
            self.session().register("third1", "Third One", PASSWORD, join_code=join), 403, "registration_closed"
        )
        res = admin.request("admin.create_user", {"username": "forced", "display_name": "Forced", "password": PASSWORD})
        self.assertEqual(res["err"]["code"], "invalid_state")
        self.assertEqual(res["err"]["reason"], "max_users")


class RegistrationLimitTests(FreshServerCase):
    limits = {"reg_per_ip_hour": 2}

    def test_five_registrations_per_ip_per_hour_become_two_here(self) -> None:
        admin = self.first_admin()  # the first registration counts too
        join = self.open_registration(admin)
        self.assertHttp(self.session().register("second1", "Second One", PASSWORD, join_code=join), 201)
        res = self.session().register("third1", "Third One", PASSWORD, join_code=join)
        self.assertHttp(res, 429, "rate_limited")
        self.assertIn("retry-after", res.headers)


class LoginTests(FreshServerCase):
    limits = {"login_a": [3, 300.0]}

    def setUp(self) -> None:
        super().setUp()
        self.admin = self.first_admin()
        self.join = self.open_registration(self.admin)
        self.anon = self.session()

    def test_wrong_credentials_are_generic(self) -> None:
        for name, password in (("root1", "wrong password"), ("nobody", PASSWORD), ("bad name!", PASSWORD)):
            res = self.session().login(name, password)
            self.assertHttp(res, 401, "bad_credentials")
        res = self.session().login("root1", PASSWORD)
        self.assertHttp(res, 200)
        self.assertEqual(res.json["me"]["username"], "root1")
        self.assertIn("HttpOnly", res.set_cookies[0])

    def test_login_failures_are_throttled_identically_for_known_and_unknown_names(self) -> None:
        for name in ("root1", "ghost"):
            s = self.session()
            for _ in range(
                4
            ):  # the limit is 3: the failure beyond it starts the delay (SPEC 4.1: n = failures - limit)
                self.assertHttp(s.login(name, "wrong password"), 401, "bad_credentials")
            res = s.login(name, PASSWORD)
            self.assertHttp(res, 429, "rate_limited")
            self.assertGreaterEqual(res.error["retry_after"], 1)
            self.assertEqual(res.headers["retry-after"], str(int(res.error["retry_after"])))

    def test_disabled_is_revealed_only_after_the_password_verified(self) -> None:
        res = self.session().register("victim", "Vic Tim", PASSWORD, join_code=self.join)
        self.assertEqual(res.status, 201)
        victim_id = res.json["me"]["id"]
        self.assertTrue(self.admin.request("admin.update_user", {"user_id": victim_id, "disabled": True})["ok"])
        self.assertHttp(self.session().login("victim", "wrong password"), 401, "bad_credentials")
        self.assertHttp(self.session().login("victim", PASSWORD), 403, "disabled")

    def test_the_first_login_of_a_never_activated_user_broadcasts_activated(self) -> None:
        created = self.admin.request(
            "admin.create_user", {"username": "temp.user", "display_name": "Temp User", "password": PASSWORD}
        )
        self.assertTrue(created["ok"], created)
        self.assertFalse(created["d"]["user"]["activated"])
        uid = created["d"]["user"]["id"]
        mark = self.admin.mark()
        res = self.session().login("temp.user", PASSWORD)
        self.assertHttp(res, 200)
        self.assertTrue(res.json["me"]["must_change_password"])
        ev = self.admin.wait_event("ev.user_update", lambda f: f["d"]["user"]["id"] == uid, since=mark)
        self.assertTrue(ev["d"]["user"]["activated"])
        mark = self.admin.mark()
        self.assertHttp(self.session().login("temp.user", PASSWORD), 200)  # a later login announces nothing
        self.assertTrue(self.admin.request("ping", {})["ok"])
        self.admin.expect_none("ev.user_update", lambda f: f["d"]["user"]["id"] == uid, since=mark)


class SessionTests(FreshServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.first_admin()
        self.join = self.open_registration(self.admin)

    def member(self) -> ChatSession:
        s = self.session("member")
        self.assertEqual(s.register("member1", "Member One", PASSWORD, join_code=self.join).status, 201)
        return s

    def test_me_and_must_change_password(self) -> None:
        me = self.admin.get("/api/me")
        self.assertEqual(me.status, 200)
        self.assertEqual(me.json["me"]["username"], "root1")
        self.assertEqual(self.session().get("/api/me").status, 401)
        created = self.admin.request(
            "admin.create_user", {"username": "forced", "display_name": "Forced One", "password": PASSWORD}
        )
        self.assertTrue(created["ok"])
        forced = self.session("forced")
        self.assertHttp(forced.login("forced", PASSWORD), 200)
        self.assertTrue(forced.get("/api/me").json["me"]["must_change_password"])
        self.assertHttp(forced.get("/api/sessions"), 403, "password_change_required")
        with self.assertRaises(Exception) as caught:
            forced.connect(wait_ready=False)
        self.assertEqual(getattr(caught.exception, "status", None), 403)
        self.assertHttp(forced.change_password(PASSWORD, "a brand new secret"), 204)
        self.assertFalse(forced.get("/api/me").json["me"]["must_change_password"])
        forced.connect()  # now allowed

    def test_logout_closes_that_sessions_sockets_after_ev_kicked(self) -> None:
        member = self.member()
        member.connect()
        other_tab = member.clone("other-tab")
        other = self.session("second-login")
        self.assertHttp(other.login("member1", PASSWORD), 200)
        other.connect()
        self.addCleanup(other_tab.abort)
        other_tab.connect()
        res = member.logout()
        self.assertEqual(res.status, 204)
        self.assertIn("Max-Age=0", res.set_cookies[0])
        self.assertEqual(member.wait_closed(), 4001)
        kicked = member.wait_event("ev.kicked")
        self.assertEqual(kicked["d"], {"reason": "logout"})
        self.assertLess(kicked.seq, member.close_seq)
        self.assertEqual(other_tab.wait_closed(), 4001)  # the same session on another tab
        self.assertTrue(other.request("ping", {})["ok"])  # another session of the same user lives on
        self.assertEqual(member.get("/api/me").status, 401)

    def test_change_password_rules_and_kicking_the_other_sessions(self) -> None:
        member = self.member()
        member.connect()
        other = self.session("other")
        other.login("member1", PASSWORD)
        other.connect()
        res = member.change_password("not the password", "another fine secret")
        self.assertHttp(res, 403, "forbidden", "bad_old_password")
        self.assertEqual(member.get("/api/me").status, 200)  # the session survives
        self.assertHttp(member.change_password(PASSWORD, PASSWORD), 400, "weak_password", "same_as_old")
        self.assertHttp(member.change_password(PASSWORD, "short"), 400, "weak_password")
        self.assertHttp(member.change_password(PASSWORD, "password123"), 400, "weak_password")
        res = member.change_password(PASSWORD, "another fine secret")
        self.assertEqual(res.status, 204, res)
        self.assertEqual(other.wait_closed(), 4001)
        self.assertEqual(other.wait_event("ev.kicked")["d"], {"reason": "password_changed"})
        self.assertTrue(member.request("ping", {})["ok"])  # the current session keeps working
        self.assertEqual(self.session().login("member1", PASSWORD).status, 401)
        self.assertEqual(self.session().login("member1", "another fine secret").status, 200)

    def test_sessions_list_and_revoke(self) -> None:
        member = self.member()
        member.connect()
        other = self.session("other")
        other.login("member1", PASSWORD)
        other.connect()
        listing = member.get("/api/sessions")
        self.assertEqual(listing.status, 200)
        sessions = listing.json["sessions"]
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(1 for s in sessions if s["current"]), 1)
        self.assertEqual(set(sessions[0]), {"id", "created_at", "last_used_at", "ip", "user_agent", "current"})
        theirs = next(s for s in sessions if not s["current"])
        self.assertHttp(member.post("/api/sessions/revoke", {"id": "0" * 16}), 404, "not_found")
        self.assertHttp(member.post("/api/sessions/revoke", {"id": "zz"}), 404, "not_found")
        self.assertHttp(member.post("/api/sessions/revoke", {}), 400, "bad_request")
        self.assertHttp(
            member.post("/api/sessions/revoke", {"id": theirs["id"], "all_others": True}), 400, "bad_request"
        )
        admin_session = self.admin.get("/api/sessions").json["sessions"][0]
        self.assertHttp(member.post("/api/sessions/revoke", {"id": admin_session["id"]}), 404, "not_found")  # not yours
        self.assertEqual(member.post("/api/sessions/revoke", {"id": theirs["id"]}).status, 204)
        self.assertEqual(other.wait_closed(), 4001)
        self.assertEqual(other.wait_event("ev.kicked")["d"], {"reason": "revoked"})
        self.assertTrue(member.request("ping", {})["ok"])
        # all_others, then revoking the current session behaves like logout
        third = self.session("third")
        third.login("member1", PASSWORD)
        third.connect()
        self.assertEqual(member.post("/api/sessions/revoke", {"all_others": True}).status, 204)
        self.assertEqual(third.wait_closed(), 4001)
        mine = next(s for s in member.get("/api/sessions").json["sessions"] if s["current"])
        res = member.post("/api/sessions/revoke", {"id": mine["id"]})
        self.assertEqual(res.status, 204)
        self.assertIn("Max-Age=0", res.set_cookies[0])
        self.assertEqual(member.wait_closed(), 4001)
        self.assertEqual(member.wait_event("ev.kicked")["d"], {"reason": "logout"})


class PasswordThrottleTests(FreshServerCase):
    limits = {"login_a": [5, 300.0]}

    def test_wrong_old_passwords_count_against_the_throttle(self) -> None:
        admin = self.first_admin()
        join = self.open_registration(admin)
        member = self.session("member")
        self.assertEqual(member.register("member1", "Member One", PASSWORD, join_code=join).status, 201)
        for _ in range(6):  # the 6th failure exceeds the limit of 5; the next attempt is delayed
            self.assertHttp(member.change_password("bad guess", "another fine secret"), 403, "forbidden")
        res = member.change_password("bad guess", "another fine secret")
        self.assertHttp(res, 429, "rate_limited")
        self.assertEqual(member.get("/api/me").status, 200)  # throttled, not signed out


if __name__ == "__main__":
    unittest.main()
