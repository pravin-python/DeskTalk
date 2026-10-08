"""Tests of ``chatd.util`` (db-core): text rules, JSON, logging, file retry, pending deletes, instance lock."""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chatd import util  # noqa: E402


class TextRulesTest(unittest.TestCase):
    def test_normalize_text_strips_format_and_control_characters(self) -> None:
        self.assertEqual(util.normalize_text("  Ravi \u200b  Kumar\u202e\x00 "), "Ravi Kumar")
        self.assertEqual(util.normalize_text("a\u2028b\u2029c"), "abc")  # Zl and Zp are removed, not turned into spaces
        self.assertEqual(util.normalize_text("a\ud800b"), "ab")  # a lone surrogate (Cs)
        self.assertEqual(util.normalize_text("\ue000x"), "x")  # private use (Co)

    def test_normalize_text_applies_nfkc_and_collapses_whitespace(self) -> None:
        self.assertEqual(util.normalize_text("\uff32avi\u00a0\u3000K"), "Ravi K")
        self.assertEqual(util.normalize_text("a   b\t\tc"), "a bc")  # a tab is a control character: removed first
        self.assertEqual(util.normalize_text("   "), "")

    def test_newline_survives_only_when_allowed(self) -> None:
        self.assertEqual(util.normalize_text("a\nb"), "ab")
        self.assertEqual(util.normalize_text("a \r\n  b\n\n c", allow_newline=True), "a\nb\n\nc")
        self.assertEqual(util.normalize_text("\n\n x \n", allow_newline=True), "x")

    def test_display_key_is_casefolded_nfkc(self) -> None:
        self.assertEqual(util.display_key("\uff32AVI \u00df"), "ravi ss")
        self.assertEqual(util.display_key("Ravi"), util.display_key("rAVI"))

    def test_fold_matches_casefold_and_passes_non_text(self) -> None:
        self.assertEqual(util.fold("Stra\u00dfe"), "strasse")
        self.assertIsNone(util.fold(None))
        self.assertEqual(util.fold(5), 5)

    def test_login_key_and_log_username(self) -> None:
        self.assertEqual(util.login_key("Ravi.K"), "ravi.k")
        self.assertEqual(util.login_key("ab"), "?")
        self.assertEqual(util.login_key("abc\n"), "?")  # no trailing-newline match
        self.assertEqual(util.login_key("\u212aevin"), "?")  # the Kelvin sign must not alias "kevin"
        self.assertEqual(util.login_key(None), "?")
        self.assertEqual(util.log_username("ravi"), "ravi")
        self.assertEqual(util.log_username("my password 123"), "<invalid>")

    def test_username_regex_and_reserved_names(self) -> None:
        self.assertTrue(util.USERNAME_RE.match("a.b-c_9"))
        self.assertIsNone(util.USERNAME_RE.match("abc\n"))
        self.assertIsNone(util.USERNAME_RE.match("Abc"))
        self.assertEqual(
            util.RESERVED_USERNAMES,
            frozenset(
                ["admin", "administrator", "root", "system", "support", "helpdesk", "it", "hr", "everyone", "desktalk"]
            ),
        )

    def test_safe_log_value_is_ascii_and_truncated(self) -> None:
        self.assertEqual(util.safe_log_value("a\nb"), "'a\\nb'")
        self.assertEqual(util.safe_log_value("\u202e"), "'\\u202e'")
        long_value = util.safe_log_value("x" * 500)
        self.assertEqual(len(long_value), 100)
        self.assertTrue(long_value.endswith("..."))


class JsonTest(unittest.TestCase):
    def test_strict_parser_accepts_plain_documents(self) -> None:
        self.assertEqual(
            util.json_loads_strict('{"a":[1,2.5,{"b":null}],"c":"x"}'), {"a": [1, 2.5, {"b": None}], "c": "x"}
        )

    def test_strict_parser_rejects_the_documented_inputs(self) -> None:
        for text in (
            "NaN",
            "[Infinity]",
            "[-Infinity]",
            '{"a":1,"a":2}',
            "[1234567890123456789]",
            "[1e999]",
            "[" * 9 + "]" * 9,
            "[" * 200000,
            "{",
            "",
        ):
            with self.assertRaises(ValueError, msg=text[:20]):
                util.json_loads_strict(text)

    def test_depth_limit_is_in_containers(self) -> None:
        util.json_loads_strict("[" * 8 + "]" * 8)
        with self.assertRaises(ValueError):
            util.json_loads_strict("[" * 9 + "]" * 9)

    def test_dumps_is_compact_ascii_and_refuses_nan(self) -> None:
        self.assertEqual(util.json_dumps({"a": [1, "\u00e9"]}), '{"a":[1,"\\u00e9"]}')
        self.assertEqual(util.json_dumps("\u00e9", ensure_ascii=False), '"\u00e9"')
        with self.assertRaises(ValueError):
            util.json_dumps(float("nan"))


class SurrogateTest(unittest.TestCase):
    LONE = chr(0xD800)

    def test_detects_lone_surrogates_anywhere(self) -> None:
        self.assertTrue(util.has_lone_surrogate("a" + self.LONE + "b"))
        self.assertTrue(util.has_lone_surrogate({"k": ["x", {"deep": chr(0xDFFF)}]}))
        self.assertTrue(util.has_lone_surrogate({chr(0xD83D): 1}))  # keys too
        self.assertTrue(util.has_lone_surrogate(("a", ["b" + chr(0xDC00)])))
        for clean in ("plain", "emoji " + chr(0x1F600), {"a": [1, 2.5, None, True, "x"]}, [], {}, 5, None):
            self.assertFalse(util.has_lone_surrogate(clean))

    def test_a_parsed_json_escape_is_caught(self) -> None:
        backslash = chr(92)
        lone = util.json_loads_strict('{"body":"' + backslash + 'ud800"}')
        self.assertTrue(util.has_lone_surrogate(lone))
        pair = util.json_loads_strict('{"body":"' + backslash + "ud83d" + backslash + 'ude00"}')
        self.assertFalse(util.has_lone_surrogate(pair))  # a valid pair becomes one real character

    def test_deep_nesting_does_not_recurse(self) -> None:
        value: object = "x"
        for _ in range(5000):
            value = [value]
        self.assertFalse(util.has_lone_surrogate(value))

    def test_normalize_text_removes_them(self) -> None:
        self.assertEqual(util.normalize_text("a" + self.LONE + "b"), "ab")


class ScaleTest(unittest.TestCase):
    def tearDown(self) -> None:
        util.set_test_scale(1.0)

    def test_scaled_multiplies(self) -> None:
        self.assertEqual(util.scaled(10), 10)
        util.set_test_scale(0.1)
        self.assertAlmostEqual(util.scaled(10), 1.0)

    def test_scale_must_be_positive_and_finite(self) -> None:
        for bad in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                util.set_test_scale(bad)

    def test_ids_and_codes(self) -> None:
        self.assertRegex(util.new_id(), r"^[0-9a-f]{32}$")
        self.assertNotEqual(util.new_id(), util.new_id())
        self.assertRegex(util.short_code(), r"^[A-Za-z0-9_-]{8}$")


class LanAddressesTest(unittest.TestCase):
    def test_returns_valid_addresses_without_raising(self) -> None:
        primary, others = util.lan_addresses()
        for address in ([primary] if primary else []) + others:
            self.assertFalse(address.startswith(("127.", "169.254.")))
        self.assertNotIn(primary, others)

    def test_discovery_rules_with_fakes(self) -> None:
        class FakeSocket:
            def __init__(self, *a: object) -> None:
                pass

            def __enter__(self) -> "FakeSocket":
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def connect(self, address: object) -> None:
                self.address = address

            def getsockname(self) -> tuple:
                return ("192.168.1.9", 5555)

        with mock.patch("socket.socket", FakeSocket), mock.patch(
            "socket.gethostbyname_ex", return_value=("h", [], ["10.0.0.5", "127.0.0.1", "169.254.1.1", "10.0.0.5"])
        ), mock.patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.9", 0)), (2, 1, 6, "", ("172.16.0.2", 0))]
        ):
            self.assertEqual(util.lan_addresses(), ("192.168.1.9", ["10.0.0.5", "172.16.0.2"]))

    def test_every_failure_is_swallowed(self) -> None:
        with mock.patch("socket.socket", side_effect=OSError), mock.patch(
            "socket.gethostbyname_ex", side_effect=OSError
        ):
            self.assertEqual(util.lan_addresses(), (None, []))


class LoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.root_handlers = list(logging.getLogger().handlers)
        self.root_level = logging.getLogger().level

    def tearDown(self) -> None:
        util.stop_logging()
        logging.getLogger().handlers[:] = self.root_handlers
        logging.getLogger().setLevel(self.root_level)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_formatter_escapes_crlf_in_the_message_only(self) -> None:
        formatter = util.SafeFormatter("%(levelname)s %(message)s")
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord("x", logging.ERROR, __file__, 1, "bad\r\nuser %s", ("a\nb",), sys.exc_info())
        text = formatter.format(record)
        first, _, rest = text.partition("\nTraceback")
        self.assertEqual(first, "ERROR bad\\r\\nuser a\\nb")
        self.assertIn("ValueError: boom", rest)  # the traceback keeps its real line breaks

    def test_setup_logging_writes_an_escaped_file_and_stop_detaches(self) -> None:
        util.setup_logging("INFO", self.tmp, True)
        logging.getLogger("chatd.test").info("line1\nFAKE ENTRY")
        logging.getLogger("chatd.test").debug("hidden")
        util.stop_logging()
        with open(os.path.join(self.tmp, "logs", "desktalk.log"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("line1\\nFAKE ENTRY", text)
        self.assertNotIn("hidden", text)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertEqual(logging.getLogger().handlers, self.root_handlers)

    def test_no_file_without_attach_file(self) -> None:
        util.setup_logging("INFO", self.tmp, False)
        logging.getLogger("chatd.test").info("x")
        util.stop_logging()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "logs")))

    def test_rotation_failure_is_swallowed_and_retried_after_another_chunk(self) -> None:
        path = os.path.join(self.tmp, "rot.log")
        handler = util.SafeRotatingFileHandler(path, maxBytes=300, backupCount=2, encoding="utf-8")
        handler.setFormatter(util.SafeFormatter("%(message)s"))
        logger = logging.getLogger("chatd.rot")
        logger.propagate = False
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            with mock.patch.object(util, "_ROTATE_GRACE", 1000), mock.patch.object(
                logging.handlers.RotatingFileHandler, "rotate", side_effect=PermissionError("locked")
            ) as rotate:
                for _ in range(8):
                    logger.info("x" * 100)  # 800 bytes: rollover is attempted once and fails
                self.assertEqual(rotate.call_count, 1)
                for _ in range(10):
                    logger.info("y" * 100)  # past maxBytes + grace: the next attempt
                self.assertGreaterEqual(rotate.call_count, 2)
            for _ in range(30):
                logger.info("z" * 100)  # the real rotation works again
            handler.flush()
            self.assertTrue(os.path.exists(path + ".1"))
        finally:
            logger.removeHandler(handler)
            handler.close()


class RetryFileOpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        util.set_test_scale(0.001)

    def tearDown(self) -> None:
        util.set_test_scale(1.0)
        util._pending_deletes = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_success_and_transient_failures(self) -> None:
        calls = []

        def flaky(path: str) -> None:
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError("busy")

        self.assertTrue(util.retry_file_op(flaky, "p"))
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_five_retries_and_reraises_for_non_deletes(self) -> None:
        calls = []

        def always(path: str) -> None:
            calls.append(path)
            raise PermissionError("busy")

        with self.assertRaises(PermissionError):
            util.retry_file_op(always, "p")
        self.assertEqual(len(calls), 6)  # the first attempt plus 5 retries

    def test_missing_source_is_not_retried(self) -> None:
        with self.assertRaises(FileNotFoundError):
            util.retry_file_op(os.replace, os.path.join(self.tmp, "nope"), os.path.join(self.tmp, "dst"))

    def test_deleting_a_missing_file_is_success(self) -> None:
        self.assertTrue(util.retry_file_op(os.remove, os.path.join(self.tmp, "nope")))

    def test_failed_delete_goes_to_the_persisted_pending_list(self) -> None:
        util.pending_delete_load(self.tmp)
        stuck = os.path.join(self.tmp, "uploads", "ab", "x" * 32)
        os.makedirs(stuck)  # os.remove on a directory fails with an OSError everywhere
        self.assertFalse(util.retry_file_op(os.remove, stuck))
        listing = os.path.join(self.tmp, "control", "pending-delete.txt")
        with open(listing, encoding="utf-8") as handle:
            self.assertEqual(handle.read().splitlines(), [os.path.join("uploads", "ab", "x" * 32)])
        self.assertEqual(util.pending_delete_sweep(), 1)
        shutil.rmtree(stuck)
        os.makedirs(os.path.dirname(stuck), exist_ok=True)
        with open(stuck, "w", encoding="utf-8") as handle:
            handle.write("data")
        util.pending_delete_load(self.tmp)  # a restart reloads the list
        self.assertEqual(util.pending_delete_sweep(), 0)
        self.assertFalse(os.path.exists(stuck))
        with open(listing, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "")

    def test_without_load_a_failed_delete_is_only_logged(self) -> None:
        stuck = os.path.join(self.tmp, "dir")
        os.makedirs(stuck)
        self.assertFalse(util.retry_file_op(os.remove, stuck))
        self.assertEqual(util.pending_delete_sweep(), 0)


class WritePrivateFileTest(unittest.TestCase):
    def test_writes_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "code.txt")
            util.write_private_file(target, "caf\u00e9\n")
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "caf\u00e9\n")
            if os.name != "nt":
                self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)


HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from chatd import util
lock = util.instance_lock(sys.argv[2], {"port": 4321, "tls": False, "version": "t"})
print("locked", flush=True)
sys.stdin.readline()
lock.release()
print("released", flush=True)
"""

PROBE = """
import sys
sys.path.insert(0, sys.argv[1])
from chatd import util
try:
    util.instance_lock(sys.argv[2])
except util.AlreadyRunning as exc:
    print("already", exc.pid, exc.port, exc.code, exc)
else:
    print("got it")
"""


class InstanceLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_lock_in_the_same_process_fails_with_the_info(self) -> None:
        lock = util.instance_lock(self.tmp, {"port": 8765, "tls": False, "version": "2.0.0"})
        try:
            self.assertEqual(lock.info["pid"], os.getpid())
            with self.assertRaises(util.AlreadyRunning) as raised:
                util.instance_lock(self.tmp)
            self.assertEqual(raised.exception.pid, os.getpid())
            self.assertEqual(raised.exception.port, 8765)
            self.assertEqual(raised.exception.code, 73)
            self.assertIn("pid %d, port 8765" % os.getpid(), str(raised.exception))
            info = util.read_lock_info(self.tmp)
            self.assertEqual((info["pid"], info["port"], info["version"]), (os.getpid(), 8765, "2.0.0"))
            lock.update({"port": 9000})
            self.assertEqual(util.read_lock_info(self.tmp)["port"], 9000)
        finally:
            lock.release()
        lock.release()  # idempotent
        self.assertIsNone(util.read_lock_info(self.tmp))  # blanked on release
        util.instance_lock(self.tmp).release()

    def test_info_must_stay_small(self) -> None:
        lock = util.instance_lock(self.tmp)
        try:
            with self.assertRaises(ValueError):
                lock.update({"junk": "x" * 600})
        finally:
            lock.release()

    def test_read_lock_info_degrades_to_none(self) -> None:
        self.assertIsNone(util.read_lock_info(self.tmp))
        os.makedirs(os.path.join(self.tmp, "control"))
        with open(os.path.join(self.tmp, "control", "server.lock"), "wb") as handle:
            handle.write(b"{not json")
        self.assertIsNone(util.read_lock_info(self.tmp))

    def test_unavailable_details_are_reported_as_such(self) -> None:
        self.assertIn("details unavailable", str(util.AlreadyRunning(None)))

    def test_a_second_process_sees_the_pid_while_the_first_holds_the_lock(self) -> None:
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, ROOT, self.tmp],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaises(util.AlreadyRunning) as raised:
                util.instance_lock(self.tmp)
            self.assertEqual(raised.exception.pid, holder.pid)
            self.assertEqual(raised.exception.port, 4321)
            probe = subprocess.run(
                [sys.executable, "-c", PROBE, ROOT, self.tmp], capture_output=True, text=True, timeout=60
            )
            self.assertIn("already %d 4321 73" % holder.pid, probe.stdout)
            holder.stdin.write("\n")
            holder.stdin.flush()
            self.assertEqual(holder.stdout.readline().strip(), "released")
            holder.wait(timeout=30)
            util.instance_lock(self.tmp).release()  # free again
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.stdout.close()
            holder.stdin.close()
            holder.wait()

    def test_a_killed_holder_leaves_the_lock_free(self) -> None:
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, ROOT, self.tmp], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            holder.kill()
            holder.wait()
        finally:
            holder.stdout.close()
            holder.stdin.close()
        lock = util.instance_lock(self.tmp)  # the stale JSON does not keep the lock
        self.assertEqual(lock.info["pid"], os.getpid())
        lock.release()


class FatalErrorTest(unittest.TestCase):
    def test_default_code_is_78(self) -> None:
        self.assertEqual(util.FatalError("x").code, 78)
        self.assertEqual(util.FatalError("x", 5).code, 5)
        self.assertTrue(issubclass(util.AlreadyRunning, util.FatalError))


if __name__ == "__main__":
    unittest.main()
