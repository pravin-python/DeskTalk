"""``python -m chatd doctor`` end to end in a subprocess (SPEC 2.2, round-3 tests of SPEC 12).

Each run uses its own temp data dir and a free loopback port; no service, firewall or registry state is changed.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from typing import List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "correct-horse-battery-staple"

# Python code that makes `import sqlite3` fail like a broken installation, then runs the real command line.
NO_SQLITE = (
    "import runpy, sys\n"
    "sys.modules['sqlite3'] = None\n"
    "sys.argv = ['chatd'] + sys.argv[1:]\n"
    "runpy.run_module('chatd', run_name='__main__')\n"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DoctorCliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dt-doctor-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DESKTALK_")}
        self.env.update({"DESKTALK_TEST": "1", "DESKTALK_SCRYPT_N": "1024", "PYTHONIOENCODING": "utf-8"})

    def chatd(self, *args: str, code: Optional[str] = None, stdin: str = "") -> Tuple[int, str, str]:
        command: List[str] = [sys.executable]
        command += ["-c", code] if code else ["-m", "chatd"]
        proc = subprocess.run(
            command + list(args),
            cwd=ROOT,
            env=self.env,
            input=stdin.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def doctor(self, *extra: str, code: Optional[str] = None) -> Tuple[int, str, str]:
        base = ["doctor", "--data-dir", self.data, "--host", "127.0.0.1", "--port", str(free_port())]
        return self.chatd(*(base + list(extra)), code=code)


class DoctorCommandTests(DoctorCliCase):
    def test_fresh_installation_can_run(self) -> None:
        code, out, err = self.doctor()
        self.assertEqual(code, 0, out + err)
        for heading in ("Environment", "Data", "Database", "Network", "TLS", "Effective configuration"):
            self.assertIn(heading, out)
        self.assertIn("[PASS] sqlite3", out)
        self.assertIn("[PASS] Port", out)
        self.assertIn("no database yet", out)
        self.assertIn("the server can run (exit code 0)", out)
        self.assertEqual(err, "")

    def test_effective_configuration_names_the_source_of_each_value(self) -> None:
        self.env["DESKTALK_MAX_UPLOAD_MB"] = "7"
        self.env["DESKTALK_NAME"] = "Team Chat"
        code, out, _err = self.doctor("--redirect-port", "0")
        self.assertEqual(code, 0, out)
        for key, value, source in (
            ("host", "127.0.0.1", "flag"),
            ("max_upload_mb", "7", "env"),
            ("workspace_name", "Team Chat", "env"),
            ("max_users", "2000", "default"),
        ):
            self.assertRegex(out, r"%s\s+%s\s+\(%s\)" % (key, value, source))

    def test_config_file_values_and_warnings(self) -> None:
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config.json"), "w", encoding="utf-8") as handle:
            handle.write('{"session_days": 7, "colour": "red", "test_scale": 0.1}')
        code, out, _err = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"session_days\s+7\s+\(file\)")
        self.assertIn("[WARN] config", out)
        self.assertIn("unknown key", out)
        del self.env["DESKTALK_TEST"]
        code, out, _err = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("tests-only", out)  # test_scale without DESKTALK_TEST=1 is flagged

    def test_database_is_reported(self) -> None:
        args = ("create-admin", "boss", "--password-stdin", "--data-dir", self.data)
        code, out, err = self.chatd(*args, stdin=PASSWORD + "\n")
        self.assertEqual(code, 0, out + err)
        code, out, _err = self.doctor("--name", "From the flag")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"\[PASS\] Schema\s+v\d+ matches this program")
        self.assertIn("[PASS] Integrity", out)
        self.assertIn("[PASS] Database file", out)

    def test_taken_port_is_exit_1_and_names_the_owner(self) -> None:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]
        code, out, _err = self.chatd("doctor", "--data-dir", self.data, "--host", "127.0.0.1", "--port", str(port))
        self.assertEqual(code, 1, out)
        self.assertIn("[FAIL] Port", out)
        self.assertIn("cannot be bound", out)
        self.assertIn("CANNOT run", out)

    def test_wal_hostile_location_is_exit_78(self) -> None:
        cloud = os.path.join(self.tmp, "OneDrive - Contoso", "chat")
        code, out, _err = self.chatd("doctor", "--data-dir", cloud, "--host", "127.0.0.1", "--port", str(free_port()))
        self.assertEqual(code, 78, out)
        self.assertIn("[FAIL] WAL location", out)
        self.assertIn("cloud-sync", out)


class DoctorWithoutSqliteTests(DoctorCliCase):
    """SPEC round-3 test: `doctor` and `--version` still run when sqlite3 is unusable; `serve` exits 78 cleanly."""

    def test_doctor_reports_fail_rows_and_exits_78_without_a_traceback(self) -> None:
        code, out, err = self.doctor(code=NO_SQLITE)
        self.assertEqual(code, 78, out + err)
        self.assertIn("[FAIL] sqlite3", out)
        self.assertIn("[FAIL] Database", out)
        self.assertIn("Effective configuration", out)  # the report still completes
        self.assertNotIn("Traceback", out + err)
        self.assertIn("CANNOT run", out)

    def test_version_still_works(self) -> None:
        code, out, err = self.chatd("--version", code=NO_SQLITE)
        self.assertEqual(code, 0, out + err)
        self.assertIn("DeskTalk", out)
        self.assertNotIn("Traceback", out + err)

    def test_serve_prints_the_message_and_exits_78(self) -> None:
        args = ("serve", "--data-dir", self.data, "--host", "127.0.0.1", "--port", "0")
        code, out, err = self.chatd(*args, code=NO_SQLITE)
        self.assertEqual(code, 78, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("doctor", err + out)


class DoctorEnvironmentTests(DoctorCliCase):
    def test_runs_from_any_working_directory_through_server_py(self) -> None:
        command = [sys.executable, os.path.join(ROOT, "server.py"), "doctor", "--data-dir", self.data]
        command += ["--host", "127.0.0.1", "--port", str(free_port())]
        proc = subprocess.run(
            command,
            cwd=self.tmp,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        out = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, out + proc.stderr.decode("utf-8", "replace"))
        self.assertIn("DeskTalk doctor - version", out)

    def test_doctor_is_read_only(self) -> None:
        os.makedirs(self.data)
        before = sorted(os.listdir(self.data))
        code, _out, _err = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(self.data)), before)

    def test_running_server_is_recognised_with_pid_and_version(self) -> None:
        port = free_port()
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "chatd",
                "serve",
                "--data-dir",
                self.data,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(server.wait, 15)
        self.addCleanup(server.kill)
        deadline = time.time() + 40
        started = False
        while time.time() < deadline and server.poll() is None and not started:
            with socket.socket() as probe:
                probe.settimeout(0.5)
                started = probe.connect_ex(("127.0.0.1", port)) == 0
            if not started:
                time.sleep(0.3)
        self.assertTrue(started, "the server did not start")
        time.sleep(1.0)  # let it publish the real port and pid in server.lock
        code, out, err = self.chatd("doctor", "--data-dir", self.data, "--host", "127.0.0.1", "--port", str(port))
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"DeskTalk is running \(version \S+, pid %d, http port %d\)" % (server.pid, port))
        self.assertIn("Setup code", out)  # no admin yet: the one-time code is shown for the first account


if __name__ == "__main__":
    unittest.main()
