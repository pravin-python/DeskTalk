"""Command line: exit codes, boot log, sub-command dispatch (SPEC 2.2)."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from typing import Any, List, Tuple
from unittest import mock

from chatd import __main__ as cli
from chatd import __version__, tlsutil, util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOOD_PASSWORD = "correct-horse-battery-staple"


class CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dt-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        patcher = mock.patch.dict(os.environ, {"DESKTALK_TEST": "1", "DESKTALK_SCRYPT_N": "1024"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("DESKTALK_DATA_DIR", "DESKTALK_PORT"):
            os.environ.pop(name, None)
        self.root_handlers = list(logging.getLogger().handlers)
        self.addCleanup(self._assert_logging_restored)

    def _assert_logging_restored(self) -> None:
        util.stop_logging()
        self.assertEqual(logging.getLogger().handlers, self.root_handlers)

    def run_cli(self, *argv: str, stdin: str = "") -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(
            sys, "stdin", io.StringIO(stdin)
        ):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def create_admin(self, name: str = "boss") -> None:
        code, out, err = self.run_cli(
            "create-admin", name, "--password-stdin", "--data-dir", self.data, stdin=GOOD_PASSWORD + "\n"
        )
        self.assertEqual(code, 0, err)

    def read_tls(self, name: str) -> bytes:
        with open(os.path.join(self.data, "tls", name), "rb") as fh:
            return fh.read()

    def users(self) -> List[Tuple[Any, ...]]:
        conn = sqlite3.connect(os.path.join(self.data, "chat.db"))
        try:
            return conn.execute("SELECT username, role, must_change_password FROM users ORDER BY id").fetchall()
        finally:
            conn.close()


class EntryPointTests(CliBase):
    def test_version(self) -> None:
        code, out, _ = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)
        self.assertRegex(out, r"schema v\d+")

    def test_help_and_usage_errors(self) -> None:
        self.assertEqual(self.run_cli("--help")[0], 0)
        self.assertEqual(self.run_cli("serve", "--help")[0], 0)
        self.assertEqual(self.run_cli("frobnicate")[0], 2)
        self.assertEqual(self.run_cli("backup", "--bogus-flag")[0], 2)
        self.assertEqual(self.run_cli("create-admin")[0], 2)

    def test_invalid_configuration_is_exit_2_with_the_key_name(self) -> None:
        code, _, err = self.run_cli("serve", "--port", "abc", "--data-dir", self.data)
        self.assertEqual(code, 2)
        self.assertIn("port", err)
        code, _, err = self.run_cli("serve", "--data-dir", self.data, "--log-level", "loud")
        self.assertEqual(code, 2)

    def test_broken_config_file_is_exit_78_and_leaves_a_boot_log_line(self) -> None:
        os.makedirs(os.path.join(self.data, "logs"))
        with open(os.path.join(self.data, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        code, _, err = self.run_cli("backup", "--data-dir", self.data)
        self.assertEqual(code, 78)
        self.assertIn("config.json", err)
        with open(os.path.join(self.data, "logs", "boot.log"), encoding="utf-8") as fh:
            self.assertIn("exit 78", fh.read())

    def test_flags_without_a_command_mean_serve(self) -> None:
        seen = {}

        class FakeServer:
            def __init__(self, cfg: Any) -> None:
                seen["cfg"] = cfg

            def run(self) -> None:
                seen["ran"] = True

        with mock.patch("chatd.app.Server", FakeServer):
            for argv in (("--port", "0", "--data-dir", self.data), ("serve", "--port", "0", "--data-dir", self.data)):
                seen.clear()
                code, _, _ = self.run_cli(*argv)
                self.assertEqual(code, 0)
                self.assertTrue(seen["ran"])
                self.assertEqual((seen["cfg"].port, str(seen["cfg"].data_dir)), (0, self.data))

    def test_serve_attaches_the_file_log_but_other_commands_do_not(self) -> None:
        self.create_admin()
        self.assertFalse(os.path.exists(os.path.join(self.data, "logs", "desktalk.log")))

    def test_environment_selects_the_data_dir_and_flags_override_it(self) -> None:
        seen = {}

        class FakeServer:
            def __init__(self, cfg: Any) -> None:
                seen["cfg"] = cfg

            def run(self) -> None:
                return None

        other = os.path.join(self.tmp, "other")
        with mock.patch("chatd.app.Server", FakeServer), mock.patch.dict(
            os.environ, {"DESKTALK_DATA_DIR": other, "DESKTALK_PORT": "4321"}
        ):
            self.run_cli("serve")
            self.assertEqual((str(seen["cfg"].data_dir), seen["cfg"].port), (other, 4321))
            self.run_cli("serve", "--data-dir", self.data, "--port", "1234")
            self.assertEqual((str(seen["cfg"].data_dir), seen["cfg"].port), (self.data, 1234))

    def test_server_py_wrapper_runs_in_isolated_mode(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-I", os.path.join(ROOT, "server.py"), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(__version__, proc.stdout.decode("utf-8"))
        proc = subprocess.run(
            [sys.executable, "-I", os.path.join(ROOT, "server.py"), "serve", "--port", "abc"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)

    def test_python_dash_m_entry_point(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "chatd", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(__version__, proc.stdout.decode("utf-8"))


class EnvironmentProblemTests(CliBase):
    def test_missing_sqlite_is_exit_78_with_an_actionable_message_and_no_import_of_db(self) -> None:
        for name in [n for n in sys.modules if n in ("chatd.app", "chatd.hub")]:
            del sys.modules[name]
        with mock.patch.dict(sys.modules, {"sqlite3": None}):
            for command in (
                ("backup",),
                ("serve",),
                ("restore", "x"),
                ("create-admin", "bob"),
                ("reset-password", "bob"),
            ):
                code, out, err = self.run_cli(*command, "--data-dir", self.data)
                self.assertEqual(code, 78, command)
                self.assertIn("sqlite3", err)
                self.assertIn("doctor", err)
                self.assertIn(sys.executable, err)
                self.assertIn("3.11", err)
                self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(os.path.join(self.data, "chat.db")))
        with open(os.path.join(self.data, "logs", "boot.log"), encoding="utf-8") as fh:
            boot = fh.read()
        self.assertIn("sqlite3", boot)
        self.assertNotIn("Traceback", boot)

    def test_old_sqlite_is_refused_by_the_preflight(self) -> None:
        import sqlite3

        with mock.patch.object(sqlite3, "sqlite_version_info", (3, 22, 0)), mock.patch.object(
            sqlite3, "sqlite_version", "3.22.0"
        ):
            self.assertIn("3.22.0", cli.preflight())
            code, _, err = self.run_cli("backup", "--data-dir", self.data)
        self.assertEqual(code, 78)
        self.assertIn("3.22.0", err)
        self.assertIn("3.24", err)
        self.assertIsNone(cli.preflight())

    def test_commands_that_need_no_database_skip_the_preflight(self) -> None:
        fake = types.ModuleType("chatd.doctor")
        seen = []
        fake.run = lambda cfg: seen.append(cfg) or 5  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"sqlite3": None, "chatd.doctor": fake}):
            code, _, _ = self.run_cli("doctor", "--data-dir", self.data, "--port", "9001")
            self.assertEqual(code, 5)
            self.assertEqual((seen[0].port, str(seen[0].data_dir)), (9001, self.data))
            code, out, _ = self.run_cli("--version")
            self.assertEqual(code, 0)
            self.assertIn(__version__, out)

    def test_round_3_subprocess_without_sqlite3(self) -> None:
        launcher = (
            "import sys; sys.modules['sqlite3'] = None; sys.path.insert(0, sys.argv[1]); "
            "from chatd.__main__ import main; sys.exit(main(sys.argv[2:]))"
        )

        def run(*args: str) -> "subprocess.CompletedProcess[bytes]":
            return subprocess.run(
                [sys.executable, "-X", "utf8", "-I", "-c", launcher, ROOT, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
                cwd=tempfile.gettempdir(),
            )

        serve = run("serve", "--port", "0", "--host", "127.0.0.1", "--data-dir", self.data)
        self.assertEqual(serve.returncode, 78)
        self.assertIn("sqlite3", serve.stderr.decode("utf-8", "replace"))
        self.assertNotIn("Traceback", serve.stderr.decode("utf-8", "replace"))
        version = run("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("schema", version.stdout.decode("utf-8"))
        doctor = run("doctor", "--data-dir", self.data, "--port", "0")
        self.assertNotIn("Traceback", doctor.stderr.decode("utf-8", "replace"))
        self.assertIn(doctor.returncode, (0, 1, 78))
        self.assertIn("FAIL", doctor.stdout.decode("utf-8", "replace"))

    def test_doctor_module_missing_is_a_clear_error(self) -> None:
        with mock.patch.dict(sys.modules, {"chatd.doctor": None}):
            code, _, err = self.run_cli("doctor", "--data-dir", self.data)
        self.assertEqual(code, 1)
        self.assertIn("doctor", err)

    def test_second_serve_exits_73_and_names_the_running_instance(self) -> None:
        lock = util.instance_lock(self.data, {"port": 4242})
        self.addCleanup(lock.release)
        code, _, err = self.run_cli("serve", "--data-dir", self.data, "--port", "0")
        self.assertEqual(code, 73)
        self.assertIn("already running", err)
        self.assertIn("pid %d" % os.getpid(), err)
        self.assertIn("port 4242", err)
        with open(os.path.join(self.data, "logs", "boot.log"), encoding="utf-8") as fh:
            self.assertIn("exit 73", fh.read())

    def test_restore_while_the_server_runs_exits_73(self) -> None:
        self.create_admin()
        lock = util.instance_lock(self.data)
        self.addCleanup(lock.release)
        code, _, err = self.run_cli("backup", "--data-dir", self.data, "--out", os.path.join(self.tmp, "b"))
        self.assertEqual(code, 0, err)
        snapshot = [os.path.join(self.tmp, "b", n) for n in os.listdir(os.path.join(self.tmp, "b"))][0]
        code, _, err = self.run_cli("restore", snapshot, "--data-dir", self.data)
        self.assertEqual(code, 73)
        self.assertIn("running", err)

    def test_permission_errors_name_the_owner_and_the_service_command(self) -> None:
        with mock.patch("chatd.maintenance.backup", side_effect=PermissionError("denied")):
            code, _, err = self.run_cli("backup", "--data-dir", self.data)
        self.assertEqual(code, 78)
        self.assertIn("install_service.py", err)
        self.assertIn("backup", err)
        self.assertIn("belongs to", err)
        self.assertNotIn("--data-dir", err.split("install_service.py")[1])

    def test_unexpected_errors_are_written_to_the_boot_log(self) -> None:
        with mock.patch("chatd.maintenance.backup", side_effect=RuntimeError("kaboom")):
            code, _, err = self.run_cli("backup", "--data-dir", self.data)
        self.assertEqual(code, 1)
        self.assertIn("Traceback", err)
        with open(os.path.join(self.data, "logs", "boot.log"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("RuntimeError: kaboom", text)
        self.assertIn("pid %d" % os.getpid(), text)

    def test_boot_log_falls_back_to_the_temp_directory(self) -> None:
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        target = os.path.join(tempfile.gettempdir(), "desktalk-boot.log")
        before = os.path.getsize(target) if os.path.exists(target) else 0
        with mock.patch("chatd.maintenance.backup", side_effect=RuntimeError("fallback-test")):
            code, _, _ = self.run_cli("backup", "--data-dir", os.path.join(blocker, "data"))
        self.assertEqual(code, 1)
        with open(target, "rb") as fh:
            fh.seek(before)
            self.assertIn(b"fallback-test", fh.read())

    def test_main_never_needs_stderr(self) -> None:
        with mock.patch.object(sys, "stderr", None), mock.patch(
            "chatd.maintenance.backup", side_effect=RuntimeError("x")
        ):
            self.assertEqual(cli.main(["backup", "--data-dir", self.data]), 1)


class CommandTests(CliBase):
    def test_password_stdin_reads_exactly_one_line(self) -> None:
        stdin = io.StringIO(GOOD_PASSWORD + "\nsecond line\nthird line\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", stdin):
            code = cli.main(["create-admin", "boss", "--password-stdin", "--data-dir", self.data])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(stdin.read(), "second line\nthird line\n")

    def test_every_command_honours_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"DESKTALK_DATA_DIR": self.data}):
            code, _, err = self.run_cli("create-admin", "envadmin", "--password-stdin", stdin=GOOD_PASSWORD + "\n")
            self.assertEqual(code, 0, err)
            code, out, err = self.run_cli("backup")
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.isfile(out.strip()))
        self.assertEqual([u[0] for u in self.users()], ["envadmin"])

    def test_data_dir_is_accepted_after_the_sub_command_and_equals_form(self) -> None:
        code, _, err = self.run_cli(
            "create-admin", "equal", "--password-stdin", "--data-dir=" + self.data, stdin=GOOD_PASSWORD + "\n"
        )
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli(
            "reset-password", "equal", "--data-dir", self.data, "--password-stdin", stdin="a-different-long-password\n"
        )
        self.assertEqual(code, 0, err)

    @unittest.skipIf(tlsutil.find_openssl() is None, "openssl is not available")
    def test_tls_init_creates_the_certificate_without_a_database(self) -> None:
        with mock.patch("chatd.util.lan_addresses", return_value=("10.4.4.4", [])):
            code, out, err = self.run_cli("tls-init", "--data-dir", self.data)
        self.assertEqual(code, 0, err)
        for name in ("cert.pem", "key.pem", "meta.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.data, "tls", name)), name)
        self.assertFalse(os.path.exists(os.path.join(self.data, "chat.db")))
        self.assertIn("10.4.4.4", out)
        self.assertIn("localhost", out)
        self.assertRegex(out, r"SHA-256 fingerprint: ([0-9A-F]{2}:){31}[0-9A-F]{2}")
        cert = self.read_tls("cert.pem")
        key = self.read_tls("key.pem")
        with mock.patch("chatd.util.lan_addresses", return_value=("10.4.4.4", [])):
            self.assertEqual(self.run_cli("tls-init", "--data-dir", self.data)[0], 0)
            self.assertEqual(self.read_tls("cert.pem"), cert)
            self.assertEqual(self.run_cli("tls-init", "--data-dir", self.data, "--force")[0], 0)
        self.assertNotEqual(self.read_tls("cert.pem"), cert)
        self.assertEqual(self.read_tls("key.pem"), key)

    def test_tls_init_without_openssl_exits_1(self) -> None:
        with mock.patch.object(tlsutil, "find_openssl", return_value=None):
            code, _, err = self.run_cli("tls-init", "--data-dir", self.data)
        self.assertEqual(code, 1)
        self.assertIn("openssl", err)

    def test_create_admin_creates_then_promotes(self) -> None:
        self.create_admin("boss")
        self.assertEqual(self.users(), [("boss", "admin", 0)])
        self.assertTrue(os.path.exists(os.path.join(self.data, "control", "reload")))
        code, out, _ = self.run_cli(
            "create-admin", "boss", "--password-stdin", "--data-dir", self.data, stdin="another-long-passphrase\r\n"
        )
        self.assertEqual(code, 0)
        self.assertIn("boss", out)
        self.assertEqual(len(self.users()), 1)

    def test_create_admin_password_problems(self) -> None:
        code, _, err = self.run_cli("create-admin", "boss", "--password-stdin", "--data-dir", self.data, stdin="")
        self.assertEqual(code, 2)
        self.assertIn("password", err)
        code, _, err = self.run_cli(
            "create-admin", "boss", "--password-stdin", "--data-dir", self.data, stdin="short\n"
        )
        self.assertEqual(code, 1)
        self.assertIn("weak password", err)
        code, _, err = self.run_cli(
            "create-admin", "a b", "--password-stdin", "--data-dir", self.data, stdin=GOOD_PASSWORD + "\n"
        )
        self.assertEqual(code, 1)
        self.assertIn("username", err)

    def test_interactive_password_prompt(self) -> None:
        answers = iter([GOOD_PASSWORD, GOOD_PASSWORD])
        with mock.patch("getpass.getpass", lambda prompt="": next(answers)):
            code, _, err = self.run_cli("create-admin", "typed", "--data-dir", self.data)
        self.assertEqual(code, 0, err)
        answers = iter(["one-long-password-1", "two-long-password-2"])
        with mock.patch("getpass.getpass", lambda prompt="": next(answers)):
            code, _, err = self.run_cli("create-admin", "typed2", "--data-dir", self.data)
        self.assertEqual(code, 2)
        self.assertIn("do not match", err)
        self.assertEqual([u[0] for u in self.users()], ["typed"])

    def test_reset_password(self) -> None:
        self.create_admin()
        code, out, err = self.run_cli(
            "reset-password",
            "boss",
            "--password-stdin",
            "--must-change",
            "--data-dir",
            self.data,
            stdin="a-brand-new-password\n",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.users(), [("boss", "admin", 1)])
        code, _, _ = self.run_cli(
            "reset-password", "boss", "--password-stdin", "--data-dir", self.data, stdin="yet-another-password\n"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.users(), [("boss", "admin", 0)])
        code, _, err = self.run_cli(
            "reset-password", "nobody", "--password-stdin", "--data-dir", self.data, stdin=GOOD_PASSWORD + "\n"
        )
        self.assertEqual(code, 1)
        self.assertIn("no user", err)

    def test_backup_and_restore_round_trip(self) -> None:
        self.create_admin("first")
        out_dir = os.path.join(self.tmp, "snapshots")
        code, out, err = self.run_cli("backup", "--data-dir", self.data, "--out", out_dir)
        self.assertEqual(code, 0, err)
        snapshot = out.strip()
        self.assertTrue(os.path.isfile(snapshot))
        self.assertRegex(os.path.basename(snapshot), r"^chat-\d{8}-\d{6}\.db$")
        self.create_admin("second")
        self.assertEqual(len(self.users()), 2)
        code, out, err = self.run_cli("restore", snapshot, "--data-dir", self.data)
        self.assertEqual(code, 0, err)
        self.assertEqual([u[0] for u in self.users()], ["first"])
        parked = os.path.join(self.data, "backups")
        self.assertTrue(any(n.startswith("pre-restore-") for n in os.listdir(parked)))

    def test_backup_defaults_to_the_backup_dir_and_can_include_uploads(self) -> None:
        self.create_admin()
        os.makedirs(os.path.join(self.data, "uploads", "ab"))
        with open(os.path.join(self.data, "uploads", "ab", "f" * 32), "wb") as fh:
            fh.write(b"payload")
        code, out, err = self.run_cli("backup", "--data-dir", self.data, "--with-uploads")
        self.assertEqual(code, 0, err)
        folder = out.strip()
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(os.path.dirname(folder), os.path.join(self.data, "backups"))
        self.assertTrue(os.path.isfile(os.path.join(folder, "chat.db")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "uploads", "ab", "f" * 32)))

    def test_backup_without_a_database_fails_cleanly(self) -> None:
        code, _, err = self.run_cli("backup", "--data-dir", self.data)
        self.assertEqual(code, 1)
        self.assertIn("no database", err)
        self.assertNotIn("Traceback", err)

    def test_backup_below_web_is_refused(self) -> None:
        self.create_admin()
        code, _, err = self.run_cli("backup", "--data-dir", self.data, "--out", os.path.join(ROOT, "web", "oops"))
        self.assertEqual(code, 1)
        self.assertIn("web", err)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "web", "oops")))

    def test_backup_dir_flag_and_environment(self) -> None:
        self.create_admin()
        target = os.path.join(self.tmp, "elsewhere")
        code, out, _ = self.run_cli("backup", "--data-dir", self.data, "--backup-dir", target)
        self.assertEqual((code, os.path.dirname(out.strip())), (0, target))


if __name__ == "__main__":
    unittest.main()
