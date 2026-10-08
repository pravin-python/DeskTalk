"""The real process: ``python -X utf8 -I server.py serve`` with a stub hub, signals, stop.request, exit codes."""

from __future__ import annotations

import http.client
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from typing import Any, List, Optional

try:  # `unittest discover -s tests -t .` imports this module as part of the `tests` package
    from . import test_transport_support as support
except ImportError:  # `unittest discover -s tests` or running from inside tests/
    import test_transport_support as support

from chatd import util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# hub.py / api.py belong to another owner: the launcher drops minimal stand-ins into sys.modules and then runs the
# genuine command line, exactly as server.py does.
LAUNCHER = """
import sys, types
root, data = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
hub = types.ModuleType("chatd.hub")
class Hub:
    def __init__(self, db, cfg):
        self.stopping = False
        self.counters = {"external_change_calls": 0, "revalidate_calls": 0, "dropped_ephemeral": 0}
    async def start(self):
        pass
    def sweep_typing(self):
        pass
    async def serve(self, ws, session):
        while await ws.recv() is not None:
            pass
    async def shutdown(self):
        self.stopping = True
    def online_user_ids(self):
        return []
    async def revalidate_all(self):
        pass
    async def external_change(self):
        pass
hub.Hub = Hub
api = types.ModuleType("chatd.api")
api.register_routes = lambda router, hub, db, cfg: None
sys.modules["chatd.hub"] = hub
sys.modules["chatd.api"] = api
from chatd.__main__ import main
sys.exit(main(["serve", "--host", "127.0.0.1", "--port", "0", "--data-dir", data] + sys.argv[3:]))
"""


class ProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dt-proc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        self.procs: List[subprocess.Popen] = []
        self.addCleanup(self._kill_all)

    def _kill_all(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def launch(self, *extra: str) -> "subprocess.Popen[bytes]":
        env = dict(os.environ, PYTHONUNBUFFERED="1", DESKTALK_TEST="1", DESKTALK_SCRYPT_N="1024")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-I", "-c", LAUNCHER, ROOT, self.data, *extra],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=flags,
        )
        self.procs.append(proc)
        return proc

    def wait_for_port(self, proc: "subprocess.Popen[bytes]") -> int:
        def port() -> Optional[int]:
            info = util.read_lock_info(self.data) or {}
            value = info.get("port")
            return value if isinstance(value, int) and value > 0 and proc.poll() is None else None

        self.assertTrue(support.wait_until(lambda: port() is not None, 60), "the server did not come up")
        result = port()
        assert result is not None
        return result

    def healthz(self, port: int) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def graceful_signal(self, proc: "subprocess.Popen[bytes]") -> None:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.send_signal(signal.SIGTERM)

    def port_closed(self, port: int) -> bool:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            conn.request("GET", "/healthz")
            conn.getresponse()
            return False
        except OSError:
            return True
        finally:
            conn.close()

    def test_signal_stops_the_process_gracefully_with_exit_0(self) -> None:
        proc = self.launch()
        port = self.wait_for_port(proc)
        self.assertEqual(self.healthz(port), (200, b"ok"))
        self.assertTrue(os.path.exists(os.path.join(self.data, "setup_code.txt")))
        self.graceful_signal(proc)
        self.assertEqual(proc.wait(30), 0, proc.stderr.read().decode("utf-8", "replace") if proc.stderr else "")
        self.assertTrue(self.port_closed(port))
        self.assertIsNone(util.read_lock_info(self.data))
        wal = os.path.join(self.data, "chat.db-wal")
        self.assertTrue(not os.path.exists(wal) or os.path.getsize(wal) == 0)
        with open(os.path.join(self.data, "logs", "desktalk.log"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("shutting down", log)
        self.assertNotIn("Traceback", log)

    def test_stop_request_file_stops_the_process_within_a_few_seconds(self) -> None:
        proc = self.launch()
        port = self.wait_for_port(proc)
        request = os.path.join(self.data, "control", "stop.request")
        with open(request, "w", encoding="utf-8") as fh:
            fh.write("stop")
        self.assertEqual(proc.wait(30), 0)
        self.assertTrue(self.port_closed(port))
        self.assertFalse(os.path.exists(request))

    def test_a_stale_stop_request_does_not_stop_a_new_process(self) -> None:
        os.makedirs(os.path.join(self.data, "control"))
        request = os.path.join(self.data, "control", "stop.request")
        with open(request, "w", encoding="utf-8") as fh:
            fh.write("stop")
        proc = self.launch()
        port = self.wait_for_port(proc)
        self.assertFalse(os.path.exists(request))
        self.assertFalse(support.wait_until(lambda: proc.poll() is not None, 3))
        self.assertEqual(self.healthz(port)[0], 200)

    def test_second_serve_prints_the_pid_and_exits_73(self) -> None:
        first = self.launch()
        port = self.wait_for_port(first)
        second = self.launch()
        _, err = second.communicate(timeout=60)
        self.assertEqual(second.returncode, 73)
        text = err.decode("utf-8", "replace")
        self.assertIn("already running", text)
        self.assertIn("pid %d" % first.pid, text)
        self.assertIn("port %d" % port, text)
        self.assertEqual(self.healthz(port)[0], 200)

    def test_restore_is_refused_while_the_server_runs(self) -> None:
        proc = self.launch()
        self.wait_for_port(proc)
        script = os.path.join(ROOT, "server.py")

        def cli(*args: str) -> "subprocess.CompletedProcess[bytes]":
            return subprocess.run(
                [sys.executable, "-I", script, *args, "--data-dir", self.data],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )

        backup = cli("backup", "--out", os.path.join(self.tmp, "snapshots"))
        self.assertEqual(backup.returncode, 0, backup.stderr)  # a backup may run next to the server
        restore = cli("restore", backup.stdout.decode("utf-8").strip())
        self.assertEqual(restore.returncode, 73)
        self.assertIn("running", restore.stderr.decode("utf-8", "replace"))
        self.assertIn("pid %d" % proc.pid, restore.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
