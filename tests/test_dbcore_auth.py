"""Tests of ``chatd.auth`` (db-core): hashing, policy, tokens, cookies, throttles, setup and join codes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from typing import Any, List
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chatd import auth, db, util  # noqa: E402

N = 1024  # tests lower the scrypt cost, as SPEC §2.1 allows


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class HashTest(unittest.TestCase):
    def test_scrypt_format_and_roundtrip(self) -> None:
        stored = auth.hash_password_sync("correct horse battery", N)
        self.assertRegex(stored, r"^scrypt\$1024\$8\$1\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$")
        parts = stored.split("$")
        self.assertEqual(len(base64.b64decode(parts[4])), 16)
        self.assertEqual(len(base64.b64decode(parts[5])), 32)
        self.assertEqual(auth.verify_password_sync("correct horse battery", stored, N), (True, False))
        self.assertEqual(auth.verify_password_sync("wrong", stored, N), (False, False))
        self.assertNotEqual(stored, auth.hash_password_sync("correct horse battery", N))  # a random salt per hash

    def test_default_cost_is_the_spec_value(self) -> None:
        self.assertEqual((auth.SCRYPT_N, auth.SCRYPT_R, auth.SCRYPT_P, auth.SCRYPT_DKLEN), (65536, 8, 1, 32))
        self.assertEqual(auth.PBKDF2_ITERATIONS, 600_000)
        self.assertEqual(auth.SCRYPT_MAXMEM, 128 * 1024 * 1024)
        stored = auth.hash_password_sync("pw-for-default-cost")
        self.assertTrue(stored.startswith("scrypt$65536$8$1$"))
        self.assertEqual(auth.verify_password_sync("pw-for-default-cost", stored), (True, False))

    def test_parameters_come_from_the_stored_string_and_trigger_a_rehash(self) -> None:
        stored = auth.hash_password_sync("secret-password", 2048)
        self.assertEqual(auth.verify_password_sync("secret-password", stored, 2048), (True, False))
        self.assertEqual(auth.verify_password_sync("secret-password", stored, N), (True, True))  # policy n changed
        self.assertEqual(auth.verify_password_sync("nope", stored, N), (False, False))

    def test_passwords_are_nfkc_normalised_before_hashing(self) -> None:
        stored = auth.hash_password_sync("p\u00e4ssword-x", N)  # precomposed a-umlaut
        self.assertEqual(auth.verify_password_sync("pa\u0308ssword-x", stored, N), (True, False))  # decomposed
        full_width = auth.hash_password_sync("\uff50assword-x1", N)
        self.assertEqual(auth.verify_password_sync("password-x1", full_width, N), (True, False))

    def test_pbkdf2_fallback_format_and_verification(self) -> None:
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", b"legacy-pass", salt, 1000)
        stored = "pbkdf2$1000$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(digest).decode())
        self.assertEqual(auth.verify_password_sync("legacy-pass", stored, N), (True, True))  # scrypt is the policy
        self.assertEqual(auth.verify_password_sync("other", stored, N), (False, False))
        with mock.patch.object(auth, "have_scrypt", return_value=False):
            made = auth.hash_password_sync("legacy-pass", N)
            self.assertRegex(made, r"^pbkdf2\$600000\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$")
            self.assertEqual(auth.verify_password_sync("legacy-pass", made, N), (True, False))
            self.assertEqual(auth.verify_password_sync("legacy-pass", stored, N), (True, True))  # not 600000
            scrypt_hash = auth.hash_password_sync  # an scrypt string cannot be verified without scrypt
        self.assertTrue(callable(scrypt_hash))
        with mock.patch.object(auth, "have_scrypt", return_value=False):
            self.assertEqual(
                auth.verify_password_sync("x", "scrypt$1024$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 43 + "=", N),
                (False, False),
            )

    def test_over_long_passwords_are_refused_without_hashing(self) -> None:
        stored = auth.hash_password_sync("a perfectly fine password", N)
        with mock.patch.object(auth.hashlib, "scrypt") as scrypt:
            self.assertEqual(auth.verify_password_sync("x" * 129, stored, N), (False, False))
            self.assertEqual(auth.verify_password_sync("x" * 100000, stored, N), (False, False))
        scrypt.assert_not_called()
        longest = "aB3" * 42 + "xy"  # exactly 128 characters
        self.assertEqual(auth.verify_password_sync(longest, auth.hash_password_sync(longest, N), N), (True, False))

    def test_same_password_compares_after_nfkc(self) -> None:
        self.assertTrue(auth.same_password("p\u00e4ssword-1", "pa\u0308ssword-1"))
        self.assertTrue(auth.same_password("same-password", "same-password"))
        self.assertFalse(auth.same_password("same-password", "Same-password"))
        self.assertFalse(auth.same_password("same-password", "same-password "))

    def test_malformed_stored_values_never_verify(self) -> None:
        good_salt = base64.b64encode(b"s" * 16).decode()
        good_hash = base64.b64encode(b"h" * 32).decode()
        for stored in (
            "",
            "plain",
            "scrypt$1024$8$1$%s" % good_salt,
            "scrypt$x$8$1$%s$%s" % (good_salt, good_hash),
            "scrypt$1000$8$1$%s$%s" % (good_salt, good_hash),  # not a power of two
            "scrypt$%d$8$1$%s$%s" % (2**21, good_salt, good_hash),  # beyond the accepted cost
            "scrypt$1024$0$1$%s$%s" % (good_salt, good_hash),
            "scrypt$1024$8$99$%s$%s" % (good_salt, good_hash),
            "scrypt$1024$8$1$!!!$%s" % good_hash,
            "scrypt$1024$8$1$%s$%s" % (good_salt, base64.b64encode(b"x").decode()),
            "pbkdf2$0$%s$%s" % (good_salt, good_hash),
            "pbkdf2$999999999$%s$%s" % (good_salt, good_hash),
            "pbkdf2$abc$%s$%s" % (good_salt, good_hash),
            "md5$1$a$b",
        ):
            self.assertEqual(auth.verify_password_sync("password", stored, N), (False, False), stored)


class HasherTest(unittest.IsolatedAsyncioTestCase):
    async def test_hash_and_verify_run_off_the_loop(self) -> None:
        hasher = auth.PasswordHasher(N)
        try:
            stored = await hasher.hash("an-ordinary-password")
            self.assertTrue(stored.startswith("scrypt$1024$"))
            self.assertEqual(await hasher.verify("an-ordinary-password", stored), (True, False))
            self.assertEqual(await hasher.verify("different", stored), (False, False))
        finally:
            hasher.shutdown()

    async def test_unknown_user_verifies_a_dummy_hash(self) -> None:
        hasher = auth.PasswordHasher(N)
        try:
            self.assertIsNone(hasher._dummy)
            self.assertEqual(await hasher.verify("whatever", None), (False, False))
            dummy = hasher._dummy
            self.assertIsNotNone(dummy)
            self.assertTrue(dummy.startswith("scrypt$1024$"))  # the same cost as a real hash
            await hasher.verify("again", None)
            self.assertEqual(hasher._dummy, dummy)  # created once
        finally:
            hasher.shutdown()

    async def test_the_queue_is_bounded(self) -> None:
        hasher = auth.PasswordHasher(N, workers=2, queue=3)
        gate = threading.Event()
        try:
            jobs = [asyncio.ensure_future(hasher._submit(gate.wait, 10)) for _ in range(5)]  # 2 running + 3 queued
            await asyncio.sleep(0.05)
            with self.assertRaises(db.ServerBusy) as raised:
                await hasher.hash("x" * 10)
            self.assertEqual(raised.exception.retry_after, 1.0)
            gate.set()
            await asyncio.gather(*jobs)
            await asyncio.sleep(0.05)
            self.assertEqual(hasher._outstanding, 0)
            self.assertTrue((await hasher.hash("ok-again-ok")).startswith("scrypt$"))
        finally:
            gate.set()
            hasher.shutdown()

    async def test_the_spec_bounds(self) -> None:
        hasher = auth.PasswordHasher()
        self.assertEqual((hasher._workers, hasher._capacity), (2, 10))  # ThreadPoolExecutor(2) and 8 queued
        hasher.shutdown()

    async def test_a_cancelled_waiter_still_frees_its_slot(self) -> None:
        hasher = auth.PasswordHasher(N, workers=1, queue=0)
        gate = threading.Event()
        try:
            task = asyncio.ensure_future(hasher._submit(gate.wait, 10))
            await asyncio.sleep(0.05)
            task.cancel()
            gate.set()
            await asyncio.sleep(0.1)
            self.assertEqual(hasher._outstanding, 0)
        finally:
            gate.set()
            hasher.shutdown()


class PolicyTest(unittest.TestCase):
    def test_length_limits_count_characters_after_nfkc(self) -> None:
        self.assertIsNone(auth.weak_password("a" * 8 + "Z9"))
        self.assertIn("at least 8", auth.weak_password("Ab1!xyz"))
        self.assertIn("at most 128", auth.weak_password("aB3" * 43))
        self.assertIsNone(auth.weak_password("aB3" * 42 + "xy"))  # exactly 128
        self.assertIn("at most 128", auth.weak_password("aB3" * 42 + "xyz"))
        self.assertIsNone(auth.weak_password("\u00e5" * 8))  # eight characters even though UTF-8 is longer
        self.assertIsNone(auth.weak_password("\uff21" * 8 + "b9"))
        self.assertIn("at least 12", auth.weak_password("longenough", min_len=12))

    def test_not_the_username_or_display_name(self) -> None:
        self.assertIn("username or name", auth.weak_password("Ravi.Kumar99", "ravi.kumar99", ""))
        self.assertIn("username or name", auth.weak_password("RAVI KUMAR!!", "", "Ravi Kumar!!"))
        self.assertIsNone(auth.weak_password("Ravi.Kumar99", "ravi.kumar98", "Someone else"))
        self.assertIsNone(auth.weak_password("Different-1", "", ""))

    def test_common_passwords(self) -> None:
        self.assertGreaterEqual(len(set(auth.COMMON_PASSWORDS)), 200)
        self.assertIn("too common", auth.weak_password("password"))
        self.assertIn("too common", auth.weak_password("PassWord123"))
        self.assertIn("too common", auth.weak_password("12345678"))
        self.assertIn("too common", auth.weak_password("qwertyuiop"))
        for entry in auth.COMMON_PASSWORDS:
            if len(entry) >= 8:
                self.assertIsNotNone(auth.weak_password(entry), entry)
        self.assertTrue(all(entry == entry.casefold() for entry in auth.COMMON_PASSWORDS))

    def test_non_text_is_weak(self) -> None:
        for value in (None, 12345678, b"password1", ["a"]):
            self.assertIsNotNone(auth.weak_password(value))

    def test_configured_minimum_length(self) -> None:
        auth.configure(scrypt_n=N, min_password_len=10)
        try:
            self.assertIn("at least 10", auth.check_password_policy("Abcdef9!x"))
            self.assertIsNone(auth.check_password_policy("Abcdef9!xy"))
        finally:
            auth.configure(scrypt_n=N)


class TokenAndCookieTest(unittest.TestCase):
    def test_tokens(self) -> None:
        token, digest = auth.new_session_token()
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")  # secrets.token_urlsafe(32)
        self.assertEqual(digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(auth.token_hash(token), digest)
        self.assertNotEqual(token, auth.new_session_token()[0])
        self.assertEqual(auth.session_public_id(digest), digest[:16])

    def test_cookie_headers(self) -> None:
        token = "A" * 43
        self.assertEqual(
            auth.cookie_header(token, False, 2592000),
            "fc_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000" % token,
        )
        self.assertEqual(
            auth.cookie_header(token, True, 60),
            "fc_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=60; Secure" % token,
        )
        self.assertEqual(auth.clear_cookie_header(False), "fc_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        self.assertTrue(auth.clear_cookie_header(True).endswith("; Max-Age=0; Secure"))

    def test_parse_cookie(self) -> None:
        token = "abc_DEF-" * 5 + "xyz"
        self.assertEqual(auth.parse_cookie("fc_session=" + token), token)
        self.assertEqual(auth.parse_cookie("a=b; fc_session=%s; c=d" % token), token)
        self.assertEqual(auth.parse_cookie('  fc_session="%s"  ' % token), token)
        for header in (
            None,
            "",
            "a=b",
            "fc_session=",
            "fc_session=short",
            "fc_session=%s=x" % token,
            "xfc_session=" + token,
        ):
            self.assertIsNone(auth.parse_cookie(header), header)
        self.assertIsNone(auth.parse_cookie("fc_session=%s; fc_session=%s" % (token, token)))  # ambiguous: refuse
        self.assertIsNone(auth.parse_cookie("fc_session=" + "A" * 200))  # too long to be ours


class LoginThrottleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.throttle = auth.LoginThrottle(self.clock)

    def tearDown(self) -> None:
        util.set_test_scale(1.0)

    def fail(self, times: int, ip: str = "1.1.1.1", username: str = "ravi", counters: str = "abc") -> None:
        for _ in range(times):
            self.throttle.record_failure(ip, username, counters)

    def test_counter_a_allows_five_failures_then_demands_exponential_pauses(self) -> None:
        self.fail(5)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 0.0)
        expected = [5, 10, 20, 40, 80, 160, 320, 640, 900, 900, 900]
        for n, delay in enumerate(expected, start=1):
            self.fail(1)
            self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), delay, "failure %d" % (5 + n))

    def test_retry_after_is_reported_in_whole_seconds(self) -> None:
        self.fail(6)
        self.clock.advance(0.2)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 5.0)  # 4.8 s left
        self.clock.advance(4.0)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 1.0)  # 0.8 s left
        self.clock.advance(0.9)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 0.0)

    def test_the_pause_counts_down_and_lifts(self) -> None:
        self.fail(6)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 5)
        self.clock.advance(3)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 2)
        self.clock.advance(2)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 0.0)
        self.fail(1)  # a further failure doubles it: it never locks, it only delays
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 10)

    def test_failures_leave_the_window(self) -> None:
        self.fail(6)
        self.clock.advance(300)  # counter (a) is a 5 minute window
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi", "a"), 0.0)
        self.fail(5, counters="a")
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi", "a"), 0.0)

    def test_counter_b_is_per_ip_across_usernames(self) -> None:
        for index in range(20):
            self.throttle.record_failure("2.2.2.2", "user%02d" % index)
        self.assertEqual(self.throttle.check("2.2.2.2", "fresh.name"), 0.0)
        self.throttle.record_failure("2.2.2.2", "another")
        self.assertEqual(self.throttle.check("2.2.2.2", "fresh.name"), 5)
        self.assertEqual(self.throttle.check("3.3.3.3", "fresh.name"), 0.0)
        self.clock.advance(599)
        self.assertGreater(self.throttle.check("2.2.2.2", "x" * 5), 0.0 - 1)  # the pause has long passed ...
        self.clock.advance(1)
        self.assertEqual(self.throttle.check("2.2.2.2", "fresh.name"), 0.0)  # ... and the window is over

    def test_counter_c_is_per_username_across_ips(self) -> None:
        for index in range(30):
            self.throttle.record_failure("10.0.0.%d" % index, "victim")
        self.assertEqual(self.throttle.check("10.9.9.9", "victim"), 0.0)
        self.throttle.record_failure("10.0.1.1", "victim")
        self.assertEqual(self.throttle.check("10.9.9.9", "victim"), 5)
        self.assertEqual(self.throttle.check("10.9.9.9", "bystander"), 0.0)
        self.clock.advance(900)
        self.assertEqual(self.throttle.check("10.9.9.9", "victim"), 0.0)

    def test_largest_pause_wins(self) -> None:
        self.fail(8, "4.4.4.4", "ravi", "a")  # (a): n=3 -> 20 s
        for index in range(22):
            self.throttle.record_failure("4.4.4.4", "u%d" % index, "b")  # (b): n=2 -> 10 s
        self.assertEqual(self.throttle.check("4.4.4.4", "ravi"), 20)
        self.assertEqual(self.throttle.check("4.4.4.4", "ravi", "b"), 10)

    def test_known_and_unknown_usernames_are_counted_identically(self) -> None:
        self.fail(6, username="ravi")
        self.fail(6, ip="1.1.1.2", username="nobody-such")
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), self.throttle.check("1.1.1.2", "nobody-such"))

    def test_invalid_usernames_share_the_fixed_key(self) -> None:
        for index, name in enumerate(("my pass word 1", "x y", "", "a" * 40, "A\nB", None)):
            self.throttle.record_failure("5.5.5.5", name, "a")
        self.assertEqual(self.throttle.check("5.5.5.5", "also invalid!", "a"), 5)  # 6 failures under the key (ip, '?')
        self.assertEqual(self.throttle.check("5.5.5.5", "validname", "a"), 0.0)
        self.assertEqual(self.throttle.check("5.5.5.5", "RAVI", "a"), self.throttle.check("5.5.5.5", "ravi", "a"))

    def test_usernames_are_case_insensitive(self) -> None:
        self.fail(6, username="Ravi")
        self.assertEqual(self.throttle.check("1.1.1.1", "rAVI", "a"), 5)

    def test_success_clears_counter_a_only(self) -> None:
        self.fail(6)
        self.throttle.record_success("1.1.1.1", "ravi")
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi", "a"), 0.0)
        for index in range(15):
            self.throttle.record_failure("1.1.1.1", "u%d" % index)
        self.assertGreater(self.throttle.check("1.1.1.1", "ravi", "b"), 0)  # (b) 6 + 15 = 21 failures: n=1
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi", "c"), 0.0)

    def test_password_change_failures_use_counter_a_only(self) -> None:
        self.fail(6, counters="a")
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi", "a"), 5)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi", "bc"), 0.0)
        self.assertEqual(self.throttle.sizes()["b"], 0)

    def test_maps_are_capped_with_lru_eviction(self) -> None:
        throttle = auth.LoginThrottle(self.clock, cap=5)
        for index in range(40):
            throttle.record_failure("9.9.9.%d" % index, "name%d" % index)
            throttle.record_failure("9.9.9.0", "name0")  # keep one key hot
        self.assertTrue(all(count <= 5 for count in throttle.sizes().values()))
        self.assertGreater(len(throttle._maps["a"]), 0)
        self.assertGreater(throttle.check("9.9.9.0", "name0", "a"), 0)  # the hot key survived the eviction
        self.assertEqual(throttle.check("9.9.9.39", "name39", "a"), 0.0)  # a cold key lost nothing it needed

    def test_memory_per_key_is_bounded(self) -> None:
        for _ in range(500):
            self.throttle.record_failure("1.1.1.1", "ravi")
        times = self.throttle._maps["a"].peek(("1.1.1.1", "ravi"), 300, self.clock())
        self.assertLessEqual(len(times), 5 + 24)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 900)

    def test_test_limits_override_counts_and_windows(self) -> None:
        throttle = auth.LoginThrottle(self.clock, {"login_a": [2, 10], "login_b": [100000, 1], "login_c": [100000, 1]})
        for _ in range(3):
            throttle.record_failure("1.1.1.1", "ravi")
        self.assertEqual(throttle.check("1.1.1.1", "ravi"), 5)
        self.clock.advance(10)
        self.assertEqual(throttle.check("1.1.1.1", "ravi"), 0.0)

    def test_defaults_follow_the_test_scale(self) -> None:
        util.set_test_scale(0.1)
        self.fail(6)
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi"), 1.0)  # 0.5 s, reported as whole seconds
        self.clock.advance(30)  # the 5 minute window is 30 s at scale 0.1
        self.assertEqual(self.throttle.check("1.1.1.1", "ravi", "a"), 0.0)


class GuessLimiterTest(unittest.TestCase):
    def tearDown(self) -> None:
        util.set_test_scale(1.0)

    def test_five_wrong_guesses_per_ip_then_blocked_until_the_oldest_expires(self) -> None:
        clock = FakeClock()
        limiter = auth.GuessLimiter(clock)
        for _ in range(4):
            limiter.record_failure("1.1.1.1")
            clock.advance(10)
        self.assertEqual(limiter.check("1.1.1.1"), 0.0)
        limiter.record_failure("1.1.1.1")
        self.assertEqual(limiter.check("1.1.1.1"), 600 - 40)
        self.assertEqual(limiter.check("2.2.2.2"), 0.0)
        clock.advance(560)
        self.assertEqual(limiter.check("1.1.1.1"), 0.0)

    def test_follows_the_test_scale(self) -> None:
        util.set_test_scale(0.1)
        clock = FakeClock()
        limiter = auth.GuessLimiter(clock)
        for _ in range(5):
            limiter.record_failure("1.1.1.1")
        self.assertAlmostEqual(limiter.check("1.1.1.1"), 60)


class RegistrationLimiterTest(unittest.TestCase):
    def test_five_per_ip_per_hour(self) -> None:
        clock = FakeClock()
        limiter = auth.RegistrationLimiter(clock)
        for _ in range(5):
            self.assertEqual(limiter.check("1.1.1.1"), 0.0)
            limiter.record("1.1.1.1")
            clock.advance(60)
        self.assertEqual(limiter.check("1.1.1.1"), 3600 - 300)
        self.assertEqual(limiter.check("2.2.2.2"), 0.0)
        clock.advance(3300)
        self.assertEqual(limiter.check("1.1.1.1"), 0.0)

    def test_sixty_per_hour_globally(self) -> None:
        clock = FakeClock()
        limiter = auth.RegistrationLimiter(clock)
        for index in range(60):
            limiter.record("10.0.%d.%d" % (index // 200, index % 200))
        self.assertEqual(limiter.check("172.16.0.1"), 3600)
        clock.advance(3600)
        self.assertEqual(limiter.check("172.16.0.1"), 0.0)

    def test_test_limits(self) -> None:
        clock = FakeClock()
        limiter = auth.RegistrationLimiter(clock, {"reg_per_ip_hour": 2, "reg_global_hour": 3})
        for ip in ("a", "a", "b"):
            limiter.record(ip)
        self.assertGreater(limiter.check("a"), 0)  # per ip
        self.assertGreater(limiter.check("c"), 0)  # global
        lifted = auth.RegistrationLimiter(clock, {"reg_per_ip_hour": 100000, "reg_global_hour": 100000})
        for _ in range(500):
            lifted.record("a")
        self.assertEqual(lifted.check("a"), 0.0)


class AuthenticateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.database = db.Database(os.path.join(self.data, "chat.db"))
        self.database.open()
        auth.configure(scrypt_n=N)
        spec = {"username": "ravi", "display_name": "Ravi", "pw_hash": "h", "activated": True}
        await self.database.run(db.register_user, spec, 2000, self.data)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        auth.hasher.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def new_session(self, ts: float) -> Any:
        token, digest = auth.new_session_token()
        await self.database.run(db.create_session, 1, digest, "1.2.3.4", "UA", 30, ts)
        return token, digest

    async def row(self, digest: str) -> Any:
        return await self.database.run_read(
            lambda c: c.execute(
                "SELECT last_used_at, expires_at FROM sessions WHERE token_hash = ?", (digest,)
            ).fetchone()
        )

    async def test_valid_token_returns_the_session_dict(self) -> None:
        token, digest = await self.new_session(util.now())
        session = await auth.authenticate(self.database, token, "9.9.9.9", "Agent/1")
        self.assertEqual(
            session,
            {
                "user_id": 1,
                "token_hash": digest,
                "ip": "9.9.9.9",
                "user_agent": "Agent/1",
                "must_change_password": False,
                "reissue_cookie": False,
            },
        )

    async def test_bad_tokens_are_refused_without_a_database_trip(self) -> None:
        for token in (None, "", "short", "x" * 500, "has spaces " * 6, 12345, b"bytes" * 10, "\u00e9" * 40):
            self.assertIsNone(await auth.authenticate(self.database, token, "i", "u"))
        self.assertIsNone(await auth.authenticate(self.database, "A" * 43, "i", "u"))  # well formed but unknown

    async def test_expired_and_disabled(self) -> None:
        token, digest = await self.new_session(util.now() - 31 * 86400)
        self.assertIsNone(await auth.authenticate(self.database, token, "i", "u"))
        token, digest = await self.new_session(util.now())
        self.assertIsNotNone(await auth.authenticate(self.database, token, "i", "u"))
        await self.database.run(lambda c: c.execute("UPDATE users SET disabled = 1 WHERE id = 1"))
        self.assertIsNone(await auth.authenticate(self.database, token, "i", "u"))

    async def test_forced_password_change_is_reported(self) -> None:
        token, _ = await self.new_session(util.now())
        await self.database.run(lambda c: c.execute("UPDATE users SET must_change_password = 1 WHERE id = 1"))
        self.assertTrue((await auth.authenticate(self.database, token, "i", "u"))["must_change_password"])

    async def test_last_used_slides_at_most_every_ten_minutes(self) -> None:
        now = util.now()
        token, digest = await self.new_session(now - 60)
        first = await self.row(digest)
        self.assertFalse((await auth.authenticate(self.database, token, "i", "u"))["reissue_cookie"])
        self.assertEqual(await self.row(digest), first)  # one minute old: untouched
        token2, digest2 = await self.new_session(now - 1200)
        before = await self.row(digest2)
        session = await auth.authenticate(self.database, token2, "i", "u")
        after = await self.row(digest2)
        self.assertGreater(after[0], before[0])
        self.assertAlmostEqual(after[1], util.now() + 30 * 86400, delta=30)
        self.assertFalse(session["reissue_cookie"])  # the expiry moved by 20 minutes only

    async def test_cookie_is_reissued_when_the_expiry_moved_by_more_than_a_day(self) -> None:
        token, digest = await self.new_session(util.now() - 3 * 86400)
        session = await auth.authenticate(self.database, token, "i", "u")
        self.assertTrue(session["reissue_cookie"])
        again = await auth.authenticate(self.database, token, "i", "u")
        self.assertFalse(again["reissue_cookie"])  # just touched

    async def test_session_days_comes_from_configure_or_the_argument(self) -> None:
        auth.configure(scrypt_n=N, session_days=7)
        try:
            token, digest = await self.new_session(util.now() - 1200)
            await auth.authenticate(self.database, token, "i", "u")
            self.assertAlmostEqual((await self.row(digest))[1], util.now() + 7 * 86400, delta=30)
            token, digest = await self.new_session(util.now() - 1200)
            await auth.authenticate(self.database, token, "i", "u", session_days=2)
            self.assertAlmostEqual((await self.row(digest))[1], util.now() + 2 * 86400, delta=30)
        finally:
            auth.configure(scrypt_n=N)

    async def test_revoked_between_lookup_and_touch(self) -> None:
        token, digest = await self.new_session(util.now() - 1200)
        original = db.touch_session

        def revoke_first(conn: Any, token_hash: str, days: float, ts: float) -> Any:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return original(conn, token_hash, days, ts)

        with mock.patch.object(auth.db_users, "touch_session", revoke_first):
            self.assertIsNone(await auth.authenticate(self.database, token, "i", "u"))


class SetupCodeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.database = db.Database(os.path.join(self.data, "chat.db"))
        self.database.open()
        self.file = os.path.join(self.data, "setup_code.txt")

    async def asyncTearDown(self) -> None:
        await self.database.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_created_while_there_are_no_users_and_reused_afterwards(self) -> None:
        code = await auth.ensure_setup_code(self.database, self.data)
        self.assertRegex(code, r"^[A-Za-z0-9_-]{8}$")
        with open(self.file, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), code)
        if os.name != "nt":
            self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertEqual(await auth.ensure_setup_code(self.database, self.data), code)
        auth._setup_codes.clear()  # a restart: the code is read back from the file
        self.assertEqual(await auth.ensure_setup_code(self.database, self.data), code)

    async def test_verify(self) -> None:
        code = await auth.ensure_setup_code(self.database, self.data)
        self.assertTrue(auth.verify_setup_code(self.data, code))
        for wrong in ("WRONGCODE", "", code + "x", code.swapcase() if code.swapcase() != code else "zzzzzzzz", None, 5):
            self.assertFalse(auth.verify_setup_code(self.data, wrong), wrong)
        other = os.path.join(self.tmp, "other")
        self.assertFalse(auth.verify_setup_code(other, code))  # no code in that data dir

    async def test_a_damaged_file_is_replaced(self) -> None:
        os.makedirs(self.data, exist_ok=True)
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write("not a code at all\n")
        code = await auth.ensure_setup_code(self.database, self.data)
        self.assertRegex(code, r"^[A-Za-z0-9_-]{8}$")
        with open(self.file, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), code)

    async def test_removed_once_a_user_exists(self) -> None:
        code = await auth.ensure_setup_code(self.database, self.data)
        spec = {"username": "ravi", "display_name": "Ravi", "pw_hash": "h", "activated": True}
        await self.database.run(db.register_user, spec, 2000, self.data)
        self.assertIsNone(await auth.ensure_setup_code(self.database, self.data))
        self.assertFalse(os.path.exists(self.file))
        self.assertFalse(auth.verify_setup_code(self.data, code))

    async def test_clear_is_idempotent(self) -> None:
        auth.clear_setup_code(self.data)
        await auth.ensure_setup_code(self.database, self.data)
        auth.clear_setup_code(self.data)
        auth.clear_setup_code(self.data)
        self.assertFalse(os.path.exists(self.file))

    async def test_join_code_check(self) -> None:
        self.assertFalse(await auth.check_join_code(self.database, "anything"))
        spec = {"username": "ravi", "display_name": "Ravi", "pw_hash": "h", "activated": True}
        await self.database.run(db.register_user, spec, 2000, self.data)
        code = await self.database.run_read(db.get_meta, "join_code")
        self.assertTrue(await auth.check_join_code(self.database, code))
        for wrong in ("", None, code + "x", 7):
            self.assertFalse(await auth.check_join_code(self.database, wrong))


class ConfigureTest(unittest.TestCase):
    def tearDown(self) -> None:
        auth.configure(scrypt_n=N)

    def test_configure_installs_fresh_state(self) -> None:
        auth.configure(
            scrypt_n=2048, session_days=7, min_password_len=9, test_limits={"login_a": [1, 5], "reg_per_ip_hour": 1}
        )
        self.assertEqual(auth.hasher.scrypt_n, 2048)
        auth.login_throttle.record_failure("i", "ravi")
        auth.login_throttle.record_failure("i", "ravi")
        self.assertGreater(auth.login_throttle.check("i", "ravi", "a"), 0)
        auth.registration_limiter.record("i")
        self.assertGreater(auth.registration_limiter.check("i"), 0)
        auth.configure(scrypt_n=N)
        self.assertEqual(auth.login_throttle.check("i", "ravi", "a"), 0.0)
        self.assertEqual(auth.registration_limiter.check("i"), 0.0)

    def test_hash_and_verify_use_the_configured_cost(self) -> None:
        auth.configure(scrypt_n=2048)

        async def go() -> List[Any]:
            stored = await auth.hash_password("some-long-password")
            return [
                stored,
                await auth.verify_password("some-long-password", stored),
                await auth.verify_password("x", None),
            ]

        stored, good, dummy = asyncio.run(go())
        self.assertTrue(stored.startswith("scrypt$2048$"))
        self.assertEqual(good, (True, False))
        self.assertEqual(dummy, (False, False))
        self.assertTrue(re.match(r"^scrypt\$2048\$", auth.hasher._dummy))


if __name__ == "__main__":
    unittest.main()
