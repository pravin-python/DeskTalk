"""Interpreter discovery, health probe, LAN discovery, TLS leaf generation and elevation of service/install_service.py.

Everything runs against fakes or a throw-away temp dir: no service, task, firewall rule or registry key is touched
(the registry is a dictionary, openssl only writes into a temp dir, ShellExecuteExW is replaced by a fake).
"""

from __future__ import annotations

import contextlib
import http.server
import importlib
import importlib.util
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_installer() -> Any:
    """Import service/install_service.py once per test process (it is a script, not a package)."""
    name = "desktalk_install_service"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "service", "install_service.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inst = load_installer()


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def quiet() -> Any:
    """Swallow the installer's stdout."""
    return contextlib.redirect_stdout(io.StringIO())


# ---------------------------------------------------------------------------------------------------------------------
# Interpreter discovery (SPEC 10.1) with fake registry data
# ---------------------------------------------------------------------------------------------------------------------


class FakeRegistry:
    """PEP 514 registry as a dictionary: ``{(hive, key path): {value name: data}}``; sub-keys are derived."""

    def __init__(self, values: Dict[Tuple[str, str], Dict[str, str]]) -> None:
        self.values = values

    def subkeys(self, hive: str, path: str) -> List[str]:
        prefix = path + "\\"
        found: List[str] = []
        for key_hive, key_path in self.values:
            if key_hive == hive and key_path.startswith(prefix):
                name = key_path[len(prefix) :].split("\\")[0]
                if name not in found:
                    found.append(name)
        return found

    def value(self, hive: str, path: str, name: str) -> Optional[str]:
        return self.values.get((hive, path), {}).get(name) or None


CORE = "SOFTWARE\\Python\\PythonCore"
WOW = "SOFTWARE\\WOW6432Node\\Python\\PythonCore"
PF = "C:\\Program Files\\"
REGISTRY = FakeRegistry(
    {
        ("HKLM", CORE + "\\3.12\\InstallPath"): {"ExecutablePath": PF + "Python312\\python.exe"},
        ("HKLM", CORE + "\\3.13\\InstallPath"): {
            "": PF + "Python313\\"
        },  # no ExecutablePath: default value + python.exe
        ("HKLM", CORE + "\\3.12-32\\InstallPath"): {"ExecutablePath": "C:\\Python312-32\\python.exe"},
        ("HKLM", CORE + "\\3.9\\Help"): {},  # a key without InstallPath is ignored
        ("HKLM", WOW + "\\3.11\\InstallPath"): {"ExecutablePath": "C:\\Python311\\python.exe"},
        ("HKCU", CORE + "\\3.13\\InstallPath"): {
            "ExecutablePath": "C:\\Users\\bob\\AppData\\Local\\Programs\\Python\\Python313\\python.exe"
        },
        ("HKCU", CORE + "\\3.8\\InstallPath"): {"": "D:\\Tools\\py38"},
    }
)


class ScriptedContext(inst.Context):
    """A Windows-on-Windows context whose files and processes are dictionaries; nothing real is touched."""

    def __init__(self, files: Dict[str, int], good: List[str], target: str = "windows") -> None:
        super().__init__(
            target,
            host=target,
            env={},
            interactive=False,
            app_root="C:\\DeskTalk" if target == "windows" else "/opt/dt",
        )
        self.files = files
        self.good = good
        self.commands: List[List[str]] = []
        self.launcher_output = ""
        self.on_path: Dict[str, str] = {}
        self.exists_fn = lambda path: path in self.files
        self.size_fn = self._size
        self.realpath_fn = lambda path: path

    def _size(self, path: str) -> int:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def which(self, name: str) -> Optional[str]:
        return self.on_path.get(name)

    def run(self, argv: Any, **kwargs: Any) -> Any:
        command = [str(a) for a in argv]
        self.commands.append(command)
        if command[0] == "py":
            return inst.Result(0, self.launcher_output, "")
        if command[0] not in self.good:
            return inst.Result(1, "", "ModuleNotFoundError: No module named 'sqlite3'")
        return inst.Result(0, "", "")


class Pep514Tests(unittest.TestCase):
    def test_order_machine_wide_first_newest_first_64_bit_first(self) -> None:
        found = inst.pep514_executables(REGISTRY)
        self.assertEqual(
            [(hive, version) for hive, version, _exe in found],
            [
                ("HKLM", "3.13"),
                ("HKLM", "3.12"),
                ("HKLM", "3.12-32"),
                ("HKLM", "3.11"),
                ("HKCU", "3.13"),
                ("HKCU", "3.8"),
            ],
        )

    def test_executable_path_wins_else_default_value_plus_python_exe(self) -> None:
        exes = {version: exe for hive, version, exe in inst.pep514_executables(REGISTRY) if hive == "HKLM"}
        self.assertEqual(exes["3.12"], PF + "Python312\\python.exe")
        self.assertEqual(exes["3.13"], PF + "Python313\\python.exe")
        per_user = {version: exe for hive, version, exe in inst.pep514_executables(REGISTRY) if hive == "HKCU"}
        self.assertEqual(per_user["3.8"], "D:\\Tools\\py38\\python.exe")

    def test_empty_registry(self) -> None:
        self.assertEqual(inst.pep514_executables(FakeRegistry({})), [])

    def test_py_launcher_output(self) -> None:
        text = (
            " -V:3.13 *        C:\\Python313\\python.exe\r\n"
            " -V:3.12          C:\\Program Files\\Python312\\python.exe\r\n"
            " -3.11-64         C:\\Python311\\python.exe\r\n"
            "garbage line\r\n"
            " -V:Store         C:\\Users\\x\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe\r\n"
        )
        self.assertEqual(
            inst.parse_py_launcher(text),
            ["C:\\Python313\\python.exe", "C:\\Program Files\\Python312\\python.exe", "C:\\Python311\\python.exe",
             "C:\\Users\\x\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe"],
        )  # fmt: skip

    def test_windows_candidates_order(self) -> None:
        ctx = ScriptedContext({}, [])
        ctx.on_path = {"py": "C:\\Windows\\py.exe", "python": "C:\\onpath\\python.exe"}
        ctx.launcher_output = " -V:3.10 * C:\\Python310\\python.exe\r\n"
        found = inst.windows_candidates(ctx, REGISTRY)
        self.assertEqual(found[0], PF + "Python313\\python.exe")
        self.assertEqual(found[-2:], ["C:\\Python310\\python.exe", "C:\\onpath\\python.exe"])
        self.assertIn(["py", "-0p"], ctx.commands)

    def test_py_launcher_is_skipped_when_missing(self) -> None:
        ctx = ScriptedContext({}, [])
        self.assertEqual(inst.windows_candidates(ctx, FakeRegistry({})), [])
        self.assertEqual(ctx.commands, [])


class InterpreterProblemTests(unittest.TestCase):
    def test_windows_rules_apply_even_for_a_foreign_target(self) -> None:
        ctx = inst.Context("windows", host="linux", env={}, interactive=False)
        cases = {
            "C:\\Users\\bob\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe": "Store alias",
            "c:\\users\\bob\\appdata\\local\\microsoft\\windowsapps\\python3.exe": "Store alias",
            "C:\\Python313\\pythonw.exe": "pythonw",
            "C:\\Users\\bob\\AppData\\Local\\Programs\\Python\\Python312\\python.exe": "all users",
            "D:\\USERS\\x\\python.exe": "all users",
        }
        for path, fragment in cases.items():
            self.assertIn(fragment, inst.interpreter_problem(ctx, path) or "", path)
        self.assertIsNone(inst.interpreter_problem(ctx, PF + "Python312\\python.exe"))
        self.assertIsNone(inst.interpreter_problem(ctx, "C:\\Python313\\python.exe"))

    def test_zero_byte_and_missing_files(self) -> None:
        ctx = ScriptedContext({PF + "p\\python.exe": 0, PF + "q\\python.exe": 5000}, [])
        self.assertIn("0-byte", inst.interpreter_problem(ctx, PF + "p\\python.exe"))
        self.assertIn("does not exist", inst.interpreter_problem(ctx, PF + "nope\\python.exe"))
        self.assertIsNone(inst.interpreter_problem(ctx, PF + "q\\python.exe"))

    def test_posix_has_no_windows_rules(self) -> None:
        ctx = ScriptedContext({"/Users/bob/py/bin/python3": 10}, [], target="linux")
        self.assertIsNone(inst.interpreter_problem(ctx, "/Users/bob/py/bin/python3"))


class ChooseInterpreterTests(unittest.TestCase):
    A = PF + "Python313\\python.exe"
    B = PF + "Python312\\python.exe"
    PER_USER = "C:\\Users\\bob\\AppData\\Local\\Programs\\Python\\Python313\\python.exe"

    def make(self, good: List[str]) -> ScriptedContext:
        known = (self.A, self.B, self.PER_USER, "C:\\Python312-32\\python.exe", "C:\\Python311\\python.exe")
        return ScriptedContext(dict.fromkeys(known, 1000), good)

    def test_explicit_valid(self) -> None:
        ctx = self.make([self.B])
        self.assertEqual(inst.choose_interpreter(ctx, self.B, None, REGISTRY), self.B)
        self.assertEqual(ctx.commands[0][1], "-c")
        self.assertEqual(ctx.commands[0][2], inst.PYTHON_CHECK_CODE)
        self.assertEqual(ctx.commands[1][2], inst.CHATD_CHECK_CODE)

    def test_explicit_invalid_aborts_instead_of_being_replaced(self) -> None:
        ctx = self.make([self.B])
        for bad, fragment in ((self.PER_USER, "all users"), (self.A, "sqlite3")):
            with self.assertRaises(inst.InstallerError) as caught:
                inst.choose_interpreter(ctx, bad, None, REGISTRY)
            self.assertEqual(caught.exception.code, inst.EXIT_USAGE)
            self.assertIn(fragment, str(caught.exception))

    def test_discovery_picks_the_newest_valid_machine_wide_interpreter(self) -> None:
        ctx = self.make([self.B, "C:\\Python311\\python.exe"])  # 3.13 is broken (no sqlite3), 3.12 works
        with mock.patch.object(sys, "executable", self.PER_USER):  # the running interpreter is per-user: refused
            chosen = inst.choose_interpreter(ctx, None, None, REGISTRY)
        self.assertEqual(chosen, self.B)
        tried = [c[0] for c in ctx.commands if c[1:2] == ["-c"]]
        self.assertEqual(tried, [self.A, self.B, self.B])  # 3.13 fails its first check, 3.12 passes both

    def test_saved_interpreter_comes_before_the_running_one(self) -> None:
        ctx = self.make([self.A, self.B])
        with mock.patch.object(sys, "executable", self.B):
            self.assertEqual(inst.choose_interpreter(ctx, None, self.A, REGISTRY), self.A)

    def test_missing_saved_interpreter_is_skipped(self) -> None:
        ctx = self.make([self.B])
        with mock.patch.object(sys, "executable", self.B):
            self.assertEqual(inst.choose_interpreter(ctx, None, "C:\\gone\\python.exe", REGISTRY), self.B)

    def test_nothing_usable_explains_every_candidate(self) -> None:
        ctx = self.make([])
        with mock.patch.object(sys, "executable", self.PER_USER), self.assertRaises(inst.InstallerError) as caught:
            inst.choose_interpreter(ctx, None, None, REGISTRY)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, inst.EXIT_USAGE)
        self.assertIn("no usable Python interpreter", message)
        self.assertIn(self.PER_USER, message)
        self.assertIn(self.A, message)
        self.assertIn("for all users", message)

    def test_store_stubs_pythonw_and_empty_files_are_never_tried(self) -> None:
        stub = "C:\\Users\\bob\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe"
        pythonw = PF + "Python311\\pythonw.exe"
        empty = PF + "Python310\\python.exe"
        ctx = ScriptedContext({stub: 0, pythonw: 100, empty: 0, self.B: 100}, [self.B, stub, pythonw, empty])
        ctx.on_path = {"python": stub}
        ctx.launcher_output = (
            " -V:3.11 C:\\Program Files\\Python311\\pythonw.exe\r\n -V:3.10 "
            + empty
            + "\r\n -V:3.12 "
            + self.B
            + "\r\n"
        )
        ctx.on_path["py"] = "C:\\Windows\\py.exe"
        with mock.patch.object(sys, "executable", "C:\\nowhere\\python.exe"):
            self.assertEqual(inst.choose_interpreter(ctx, None, None, FakeRegistry({})), self.B)
        executed = {c[0] for c in ctx.commands}
        self.assertTrue({stub, pythonw, empty}.isdisjoint(executed))

    def test_dry_run_executes_nothing_and_takes_the_first_acceptable_candidate(self) -> None:
        ctx = inst.Context("windows", host="linux", dry_run=True, env={}, interactive=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(inst.subprocess, "run", side_effect=AssertionError):
            chosen = inst.choose_interpreter(ctx, "C:\\Python313\\python.exe", None)
        self.assertEqual(chosen, "C:\\Python313\\python.exe")
        self.assertIn(
            '[dry-run] $ C:\\Python313\\python.exe -c "import sys,sqlite3,ssl,hashlib,asyncio;', out.getvalue()
        )
        self.assertIn('[dry-run] $ C:\\Python313\\python.exe -c "import chatd"', out.getvalue())


class PosixCandidateTests(unittest.TestCase):
    def test_linux_order_is_newest_first_and_only_existing_files(self) -> None:
        present = ["/usr/bin/python3.12", "/usr/bin/python3.9", "/usr/bin/python3", "/usr/local/bin/python3"]
        ctx = ScriptedContext(dict.fromkeys(present, 1000), [], target="linux")
        ctx.on_path = {
            "python3.12": "/usr/bin/python3.12",
            "python3.9": "/usr/bin/python3.9",
            "python3.7": "/usr/bin/python3.7",  # on PATH but not an existing file
            "python3": "/usr/bin/python3",
        }
        self.assertEqual(inst.posix_candidates(ctx), present)

    def test_linux_discovery_replaces_a_broken_running_interpreter(self) -> None:
        files = {"/opt/broken/python": 1000, "/usr/bin/python3.12": 1000, "/usr/bin/python3": 1000}
        ctx = ScriptedContext(files, ["/usr/bin/python3.12", "/usr/bin/python3"], target="linux")
        ctx.on_path = {"python3.12": "/usr/bin/python3.12", "python3": "/usr/bin/python3"}
        with mock.patch.object(sys, "executable", "/opt/broken/python"):
            self.assertEqual(inst.choose_interpreter(ctx, None, None), "/usr/bin/python3.12")

    def mac_candidates(self, xcode_tools: bool) -> List[str]:
        framework = "/Library/Frameworks/Python.framework/Versions/%s/bin/python3"
        files = {
            framework % "3.9": 1,
            framework % "3.12": 1,
            "/opt/homebrew/bin/python3.11": 1,
            "/opt/homebrew/bin/python3.13": 1,
            "/usr/local/bin/python3.10": 1,
            "/usr/bin/python3": 1,
        }
        ctx = ScriptedContext(files, ["xcode-select"] if xcode_tools else [], target="macos")
        ctx.on_path = {"python3": "/usr/bin/python3"}  # PATH finds the Command Line Tools shim first

        def fake_glob(pattern: str) -> List[str]:
            return {
                "/Library/Frameworks/Python.framework/Versions/3.*/bin/python3": [
                    framework % "3.9",
                    framework % "3.12",
                ],
                "/opt/homebrew/bin/python3.*": [
                    "/opt/homebrew/bin/python3.11",
                    "/opt/homebrew/bin/python3.11-config",  # not an interpreter
                    "/opt/homebrew/bin/python3.13",
                ],
                "/usr/local/bin/python3.*": ["/usr/local/bin/python3.10"],
            }[pattern]

        with mock.patch.object(inst.glob, "glob", fake_glob):
            return inst.posix_candidates(ctx)

    def test_macos_order_and_the_xcode_shim_rule(self) -> None:
        framework = "/Library/Frameworks/Python.framework/Versions/%s/bin/python3"
        with_tools = self.mac_candidates(xcode_tools=True)
        self.assertEqual(
            with_tools,
            [framework % "3.12", framework % "3.9", "/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3.11",
             "/usr/local/bin/python3.10", "/usr/bin/python3"],
        )  # fmt: skip
        without_tools = self.mac_candidates(xcode_tools=False)
        self.assertEqual(without_tools, with_tools[:-1])  # /usr/bin/python3 is never executed without CLT


class VenvResolutionTests(unittest.TestCase):
    def test_venv_launcher_resolves_to_the_base_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = os.path.join(folder, "base")
            venv_bin = os.path.join(folder, "venv", "bin")
            os.makedirs(base)
            os.makedirs(venv_bin)
            for path in (os.path.join(base, "pyx"), os.path.join(venv_bin, "pyx")):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("x")
            with open(os.path.join(folder, "venv", "pyvenv.cfg"), "w", encoding="utf-8") as handle:
                handle.write("home = %s\ninclude-system-site-packages = false\n" % base)
            self.assertEqual(inst.resolve_venv_home(os.path.join(venv_bin, "pyx")), os.path.join(base, "pyx"))

    def test_cfg_next_to_the_executable_and_missing_home(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            exe = os.path.join(folder, "pyx")
            with open(exe, "w", encoding="utf-8") as handle:
                handle.write("x")
            with open(os.path.join(folder, "pyvenv.cfg"), "w", encoding="utf-8") as handle:
                handle.write("home = %s\n" % os.path.join(folder, "does-not-exist"))
            self.assertEqual(inst.resolve_venv_home(exe), exe)  # unresolvable home: keep the path

    def test_plain_interpreter_is_unchanged(self) -> None:
        self.assertEqual(inst.resolve_venv_home(sys.executable), sys.executable)


# ---------------------------------------------------------------------------------------------------------------------
# Health probe (SPEC 10.1) against real local servers, http and https
# ---------------------------------------------------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: Dict[str, Tuple[int, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        status, body = self.routes.get(self.path, (404, b"not found"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return None


INFO = {"name": "DeskTalk", "registration_open": False, "needs_setup": True, "tls": False}


def serve(
    routes: Dict[str, Tuple[int, bytes]], tls: Optional[Tuple[str, str]] = None
) -> Tuple[http.server.HTTPServer, int]:
    handler = type("Handler", (_Handler,), {"routes": routes})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def healthy_routes() -> Dict[str, Tuple[int, bytes]]:
    return {"/healthz": (200, b"ok"), "/api/info": (200, json.dumps(INFO).encode())}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class HealthProbeTests(unittest.TestCase):
    def probe(self, routes: Dict[str, Tuple[int, bytes]]) -> Optional[Dict[str, Any]]:
        server, port = serve(routes)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return inst.health_probe("127.0.0.1", port, False, 3.0)

    def test_healthy_server_returns_the_info_dict(self) -> None:
        self.assertEqual(self.probe(healthy_routes()), INFO)

    def test_wildcard_bind_host_probes_loopback(self) -> None:
        server, port = serve(healthy_routes())
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for host in ("0.0.0.0", "::", "localhost", "127.0.0.1"):
            self.assertEqual(inst.health_probe(host, port, False, 3.0), INFO, host)
        self.assertEqual(inst.probe_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(inst.probe_host("127.5.5.5"), "127.0.0.1")
        self.assertEqual(inst.probe_host("192.168.1.9"), "192.168.1.9")

    def test_anything_unhealthy_is_none(self) -> None:
        good = healthy_routes()
        variants = {
            "healthz 500": {**good, "/healthz": (500, b"ok")},
            "healthz body": {**good, "/healthz": (200, b"fine")},
            "info 404": {"/healthz": (200, b"ok")},
            "info not json": {**good, "/api/info": (200, b"<html>")},
            "info list": {**good, "/api/info": (200, b"[1]")},
            "info 503": {**good, "/api/info": (503, b"{}")},
            "info bad utf8": {**good, "/api/info": (200, b"\xff\xfe")},
        }
        for label, routes in variants.items():
            self.assertIsNone(self.probe(routes), label)

    def test_nothing_listening_is_none(self) -> None:
        self.assertIsNone(inst.health_probe("127.0.0.1", free_port(), False, 1.0))

    def test_healthz_trailing_newline_is_accepted(self) -> None:
        self.assertEqual(self.probe({**healthy_routes(), "/healthz": (200, b"ok\n")}), INFO)

    def test_identical_to_chatd_doctor_when_available(self) -> None:
        try:
            doctor = importlib.import_module("chatd.doctor")
            reference = doctor.health_probe
        except Exception as exc:  # noqa: BLE001 - any import problem means "not written yet"
            self.skipTest("chatd.doctor.health_probe is not importable yet: %s" % exc)
        good = healthy_routes()
        fixtures = [
            good,
            {**good, "/healthz": (500, b"")},
            {**good, "/api/info": (200, b"nope")},
            {"/healthz": (200, b"ok")},
        ]
        for routes in fixtures:
            server, port = serve(routes)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.assertEqual(inst.health_probe("127.0.0.1", port, False, 3.0), reference("127.0.0.1", port, False, 3.0))
        closed = free_port()
        self.assertEqual(inst.health_probe("127.0.0.1", closed, False, 1.0), reference("127.0.0.1", closed, False, 1.0))


class PortAndWaitTests(unittest.TestCase):
    def test_port_is_open_and_wait_port_closed(self) -> None:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]
        ctx = inst.Context(inst.detect_host(), env={}, interactive=False)
        self.assertTrue(inst.port_is_open("127.0.0.1", port))
        self.assertFalse(inst.wait_port_closed(ctx, "0.0.0.0", port, 0.3))
        listener.close()
        self.assertFalse(inst.port_is_open("127.0.0.1", port))
        self.assertTrue(inst.wait_port_closed(ctx, "127.0.0.1", port, 1.0))

    def test_wait_for_health_polls_until_the_server_answers(self) -> None:
        ctx = inst.Context("linux", host="linux", env={}, interactive=False)
        answers: List[Optional[Dict[str, Any]]] = [None, None, INFO]
        ctx.health_fn = lambda *args: answers.pop(0)
        naps: List[float] = []
        ctx.sleep_fn = naps.append
        s = inst.Settings(target="linux", python="p", app_root="/a", data_dir="/d")
        self.assertEqual(inst.wait_for_health(ctx, s, timeout=30), INFO)
        self.assertEqual(naps, [0.5, 0.5])

    def test_wait_for_health_gives_up(self) -> None:
        ctx = inst.Context("linux", host="linux", env={}, interactive=False)
        ctx.health_fn = lambda *args: None
        ctx.sleep_fn = lambda seconds: None
        s = inst.Settings(target="linux", python="p", app_root="/a", data_dir="/d")
        self.assertIsNone(inst.wait_for_health(ctx, s, timeout=0))


# ---------------------------------------------------------------------------------------------------------------------
# LAN discovery (SPEC 5.8)
# ---------------------------------------------------------------------------------------------------------------------


class FakeUdpSocket:
    """Stands in for ``socket.socket(AF_INET, SOCK_DGRAM)``."""

    primary: Optional[str] = "192.168.1.20"

    def __init__(self, *args: Any) -> None:
        self.target: Optional[Tuple[str, int]] = None

    def __enter__(self) -> "FakeUdpSocket":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def connect(self, address: Tuple[str, int]) -> None:
        if self.primary is None:
            raise OSError("network unreachable")
        self.target = address

    def getsockname(self) -> Tuple[str, int]:
        return (str(self.primary), 50000)


@contextlib.contextmanager
def fake_network(primary: Optional[str], hostbyname: Any, addrinfo: Any) -> Any:
    udp = type("Udp", (FakeUdpSocket,), {"primary": primary})
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("socket.socket", udp))
        stack.enter_context(mock.patch("socket.gethostname", return_value="PC-01"))
        stack.enter_context(mock.patch("socket.gethostbyname_ex", hostbyname))
        stack.enter_context(mock.patch("socket.getaddrinfo", addrinfo))
        yield


class LanAddressTests(unittest.TestCase):
    def test_primary_others_filtered_and_deduplicated(self) -> None:
        by_name = mock.Mock(
            return_value=("PC-01", [], ["172.31.1.5", "127.0.0.1", "169.254.7.7", "192.168.1.20", "10.0.0.8"])
        )
        info = mock.Mock(
            return_value=[
                (2, 1, 6, "", ("10.0.0.8", 0)),
                (2, 1, 6, "", ("192.168.56.1", 0)),
                (2, 1, 6, "", ("0.0.0.0", 0)),
            ]
        )
        with fake_network("192.168.1.20", by_name, info):
            self.assertEqual(inst.lan_addresses(), ("192.168.1.20", ["172.31.1.5", "10.0.0.8", "192.168.56.1"]))

    def test_no_network_yields_none_and_empty(self) -> None:
        with fake_network(None, mock.Mock(side_effect=socket.gaierror("no")), mock.Mock(side_effect=OSError("no"))):
            self.assertEqual(inst.lan_addresses(), (None, []))

    def test_loopback_primary_is_discarded(self) -> None:
        with fake_network("127.0.0.1", mock.Mock(return_value=("h", [], [])), mock.Mock(return_value=[])):
            self.assertEqual(inst.lan_addresses(), (None, []))

    def test_identical_to_chatd_util_when_available(self) -> None:
        try:
            util = importlib.import_module("chatd.util")
            reference = util.lan_addresses
        except Exception as exc:  # noqa: BLE001 - any import problem means "not written yet"
            self.skipTest("chatd.util.lan_addresses is not importable yet: %s" % exc)
        scenarios = [
            (
                "192.168.1.20",
                ["172.31.1.5", "127.0.0.1", "169.254.7.7", "192.168.1.20", "10.0.0.8"],
                ["10.0.0.8", "192.168.56.1"],
            ),
            (None, [], []),
            ("10.1.2.3", ["10.1.2.3"], []),
            ("10.1.2.3", None, ["10.9.9.9"]),  # gethostbyname_ex fails: both copies stop searching
        ]
        for primary, names, infos in scenarios:
            by_name = (
                mock.Mock(return_value=("PC-01", [], list(names)))
                if names is not None
                else mock.Mock(side_effect=socket.gaierror("x"))
            )
            addr = mock.Mock(return_value=[(2, 1, 6, "", (ip, 0)) for ip in infos])
            with fake_network(primary, by_name, addr):
                mine = inst.lan_addresses()
            with fake_network(primary, by_name, addr):
                theirs = reference()
            self.assertEqual((mine[0], list(mine[1])), (theirs[0], list(theirs[1])), (primary, names, infos))


# ---------------------------------------------------------------------------------------------------------------------
# TLS leaf certificate (SPEC 5.7) with the real openssl, inside a temp dir
# ---------------------------------------------------------------------------------------------------------------------


def real_context(dry_run: bool = False) -> Any:
    ctx = inst.Context(inst.detect_host(), dry_run=dry_run, env=dict(os.environ), interactive=False)
    ctx.lan_fn = lambda: ("192.168.77.5", ["10.77.0.9"])
    return ctx


class TlsHelperTests(unittest.TestCase):
    def test_san_entries_are_unique_and_ordered(self) -> None:
        self.assertEqual(
            inst.tls_san_entries("PC-01", ["192.168.1.5", "127.0.0.1", "192.168.1.5"]),
            ["PC-01", "localhost", "127.0.0.1", "192.168.1.5"],
        )

    def test_is_current(self) -> None:
        now = 1_000_000.0
        meta = {"san": ["PC", "localhost", "127.0.0.1", "10.0.0.1"], "not_after": now + 100 * 86400, "created": now}
        self.assertTrue(inst.tls_is_current(meta, ["PC", "10.0.0.1"], now))
        self.assertFalse(inst.tls_is_current(meta, ["PC", "10.0.0.2"], now))  # a new LAN address
        self.assertFalse(inst.tls_is_current({**meta, "not_after": now + 29 * 86400}, ["PC"], now))  # < 30 days left
        self.assertTrue(inst.tls_is_current({**meta, "not_after": now + 30 * 86400}, ["PC"], now))
        for broken in (
            None,
            {},
            {"san": "PC", "not_after": now + 1e9},
            {"san": ["PC"], "not_after": "soon"},
            {"san": ["PC"], "not_after": True},
        ):
            self.assertFalse(inst.tls_is_current(broken, ["PC"], now))


@unittest.skipIf(
    inst.find_openssl(inst.Context(inst.detect_host(), env=dict(os.environ))) is None, "openssl not available"
)
class TlsGenerationTests(unittest.TestCase):
    def openssl_text(self, cert: str) -> str:
        exe = inst.find_openssl(real_context())
        return subprocess.run(
            [exe, "x509", "-in", cert, "-noout", "-text"], capture_output=True, text=True, check=True
        ).stdout

    def test_generate_reuse_regenerate(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            tls = os.path.join(data_dir, "tls")
            cert, key, meta_path = (os.path.join(tls, name) for name in ("cert.pem", "key.pem", "meta.json"))
            with quiet():
                inst.ensure_tls(real_context(), data_dir)
            self.assertFalse(os.path.exists(os.path.join(tls, "openssl.cnf")))  # the cnf is deleted again
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
            text = self.openssl_text(cert)
            self.assertIn("CA:FALSE", text)
            self.assertIn("TLS Web Server Authentication", text)
            for expected in (
                "IP Address:192.168.77.5",
                "IP Address:10.77.0.9",
                "IP Address:127.0.0.1",
                "DNS:localhost",
            ):
                self.assertIn(expected, text)
            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
            self.assertEqual(sorted(meta), ["created", "not_after", "san"])
            self.assertEqual(meta["san"][1:3], ["localhost", "127.0.0.1"])
            self.assertIn("192.168.77.5", meta["san"])
            self.assertAlmostEqual(meta["not_after"] - meta["created"], 825 * 86400, delta=5)
            if os.name == "posix":
                self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)

            first_cert, first_key = read_bytes(cert), read_bytes(key)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                inst.ensure_tls(real_context(), data_dir)
            self.assertIn("is current", out.getvalue())
            self.assertEqual(read_bytes(cert), first_cert)

            ctx = real_context()
            ctx.lan_fn = lambda: ("192.168.77.5", ["10.77.0.9", "10.88.0.1"])  # a new adapter appeared
            with quiet():
                inst.ensure_tls(ctx, data_dir)
            self.assertIn("IP Address:10.88.0.1", self.openssl_text(cert))
            self.assertEqual(read_bytes(key), first_key)  # the key is reused
            self.assertEqual(read_bytes(cert + ".old"), first_cert)  # the old leaf is kept

    def test_health_probe_over_https_with_the_generated_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            with quiet():
                inst.ensure_tls(real_context(), data_dir)
            pair = (os.path.join(data_dir, "tls", "cert.pem"), os.path.join(data_dir, "tls", "key.pem"))
            server, port = serve(healthy_routes(), tls=pair)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.assertEqual(inst.health_probe("127.0.0.1", port, True, 5.0), INFO)  # unverified context accepts it
            self.assertIsNone(inst.health_probe("127.0.0.1", port, False, 2.0))  # plain http to a TLS socket

    def test_https_probe_against_plain_http_is_none(self) -> None:
        server, port = serve(healthy_routes())
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertIsNone(inst.health_probe("127.0.0.1", port, True, 2.0))

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                inst.ensure_tls(real_context(dry_run=True), data_dir)
            self.assertEqual(os.listdir(data_dir), [])
            self.assertIn("[dry-run] $", out.getvalue())
            self.assertIn("basicConstraints=critical,CA:FALSE", out.getvalue())


class TlsFailureTests(unittest.TestCase):
    def test_missing_openssl_aborts_with_exit_1(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, mock.patch.object(inst, "find_openssl", return_value=None):
            with self.assertRaises(inst.InstallerError) as caught:
                inst.ensure_tls(real_context(), data_dir)
            self.assertEqual(caught.exception.code, inst.EXIT_ERROR)
            self.assertIn("openssl", str(caught.exception))
            self.assertEqual(os.listdir(data_dir), [])

    def test_openssl_failure_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            ctx = real_context()
            ctx.run = lambda argv, **kw: inst.Result(1, "", "unable to write 'random state'")  # type: ignore[method-assign]
            with mock.patch.object(inst, "find_openssl", return_value="openssl"), self.assertRaises(
                inst.InstallerError
            ):
                inst.ensure_tls(ctx, data_dir)
            self.assertEqual(os.listdir(os.path.join(data_dir, "tls")), [])  # openssl.cnf removed, nothing else left

    def test_openssl_conf_is_removed_from_the_child_environment(self) -> None:
        seen: Dict[str, Any] = {}
        with tempfile.TemporaryDirectory() as data_dir:
            ctx = real_context()
            ctx.env["OPENSSL_CONF"] = "C:\\evil\\openssl.cnf"
            ctx.env["openssl_conf"] = "lowercase twin"

            def capture(argv: Any, **kw: Any) -> Any:
                seen.update(kw)
                return inst.Result(1, "", "stop here")

            ctx.run = capture  # type: ignore[method-assign]
            with mock.patch.object(inst, "find_openssl", return_value="openssl"), self.assertRaises(
                inst.InstallerError
            ):
                inst.ensure_tls(ctx, data_dir)
        self.assertTrue(seen["env"])
        self.assertNotIn("OPENSSL_CONF", {k.upper() for k in seen["env"]})


# ---------------------------------------------------------------------------------------------------------------------
# Elevation and privilege messages (SPEC 10.1 "Privilege")
# ---------------------------------------------------------------------------------------------------------------------


class PrivilegeMessageTests(unittest.TestCase):
    def test_windows_steps_name_powershell_and_the_script(self) -> None:
        ctx = inst.Context("windows", host="windows", env={}, interactive=False, app_root="C:\\Dev Tools\\DeskTalk")
        text = "\n".join(inst.privilege_instructions(ctx, ["install", "--port", "9000", "--name", "Free Chat"]))
        self.assertIn("Run as administrator", text)
        self.assertIn('cd "C:\\Dev Tools\\DeskTalk"; & "', text)
        self.assertIn("install_service.py", text)
        self.assertIn('install --port 9000 --name "Free Chat"', text)
        self.assertIn("--elevate", text)

    def test_posix_steps_use_sudo_with_the_absolute_interpreter_and_script(self) -> None:
        ctx = inst.Context("linux", host="linux", env={}, interactive=False)
        lines = inst.privilege_instructions(ctx, ["install", "--name", "A B"])
        self.assertEqual(lines[0], "This command needs root. Run:")
        self.assertTrue(lines[1].startswith('  sudo "%s" "' % sys.executable))
        self.assertTrue(lines[1].endswith("install_service.py\" install --name 'A B'"))

    def test_ps_quote(self) -> None:
        self.assertEqual(inst.ps_quote("C:\\x y"), '"C:\\x y"')
        self.assertEqual(inst.ps_quote("a$b"), "'a$b'")
        self.assertEqual(inst.ps_quote("it's `x`"), "'it''s `x`'")

    def test_require_privilege_outcomes(self) -> None:
        ctx = inst.Context("linux", host="linux", env={}, interactive=False)
        ctx.admin_fn = lambda: False
        args = mock.Mock(elevate=False)
        with quiet():
            self.assertEqual(inst.require_privilege(ctx, args, ["install"]), inst.EXIT_USAGE)
        self.assertIsNone(inst.require_privilege(ctx, args, ["install"], needed=False))
        ctx.admin_fn = lambda: True
        self.assertIsNone(inst.require_privilege(ctx, args, ["install"]))
        ctx.admin_fn = lambda: False
        ctx.dry_run = True
        self.assertIsNone(inst.require_privilege(ctx, args, ["install"]))  # dry-run never needs privileges
        ctx.dry_run = False
        with self.assertRaises(inst.InstallerError) as caught:
            inst.require_privilege(ctx, mock.Mock(elevate=True), ["install", "--elevate"])
        self.assertEqual(caught.exception.code, inst.EXIT_USAGE)  # --elevate is Windows only


class TeeStreamTests(unittest.TestCase):
    def test_everything_reaches_both_targets(self) -> None:
        console, log = io.StringIO(), io.StringIO()
        tee = inst.TeeStream(console, log)
        tee.write("hello ")
        print("world", file=tee)
        tee.flush()
        self.assertEqual(console.getvalue(), "hello world\n")
        self.assertEqual(log.getvalue(), "hello world\n")
        self.assertFalse(tee.isatty())

    def test_a_dead_log_never_breaks_the_console(self) -> None:
        log = io.StringIO()
        log.close()
        console = io.StringIO()
        inst.TeeStream(console, log).write("still shown")
        self.assertEqual(console.getvalue(), "still shown")


@unittest.skipUnless(sys.platform == "win32", "ShellExecuteExW exists on Windows only")
class ElevationTests(unittest.TestCase):
    def test_structure_layout_matches_shellexecuteinfow(self) -> None:
        import ctypes

        info = inst.shell_execute_info_class()
        self.assertEqual(ctypes.sizeof(info), 112 if ctypes.sizeof(ctypes.c_void_p) == 8 else 60)
        offsets = {name: getattr(info, name).offset for name, _type in info._fields_}
        self.assertEqual(offsets["cbSize"], 0)
        self.assertEqual(offsets["fMask"], 4)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(
                (offsets["lpVerb"], offsets["lpFile"], offsets["nShow"], offsets["hProcess"]), (16, 24, 48, 104)
            )

    def run_elevation(
        self, shell_ok: bool, last_error: int, exit_code: int, argv: List[str]
    ) -> Tuple[Any, Dict[str, Any], str]:
        """Run ``elevate_and_wait`` against fake shell32/kernel32 and capture what ShellExecuteExW received."""
        import ctypes

        captured: Dict[str, Any] = {}

        class Fn:
            def __init__(self, impl: Any) -> None:
                self.impl = impl
                self.argtypes = None
                self.restype = None

            def __call__(self, *args: Any) -> Any:
                return self.impl(*args)

        def shell_execute(info: Any) -> int:
            for field in ("cbSize", "fMask", "lpVerb", "lpFile", "lpParameters", "lpDirectory", "nShow"):
                captured[field] = getattr(info, field)
            info.hProcess = 4321
            return 1 if shell_ok else 0

        def exit_code_of(handle: Any, code: Any) -> int:
            code.value = exit_code
            return 1

        shell32 = mock.Mock(ShellExecuteExW=Fn(shell_execute))
        kernel32 = mock.Mock(
            WaitForSingleObject=Fn(lambda handle, ms: 0),
            GetExitCodeProcess=Fn(exit_code_of),
            GetProcessId=Fn(lambda handle: 4242),
            CloseHandle=Fn(lambda handle: 1),
        )
        out = io.StringIO()
        ctx = inst.Context("windows", host="windows", env={}, interactive=False)
        with tempfile.TemporaryDirectory() as temp:
            log = os.path.join(temp, "child.log")
            with open(log, "w", encoding="utf-8") as handle:
                handle.write("step one\nstep two\n")
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(
                        ctypes, "WinDLL", side_effect=lambda name, **kw: shell32 if name == "shell32" else kernel32
                    )
                )
                stack.enter_context(mock.patch.object(ctypes, "byref", side_effect=lambda obj: obj))
                stack.enter_context(mock.patch.object(ctypes, "get_last_error", return_value=last_error))
                stack.enter_context(mock.patch.object(inst, "elevation_log_path", return_value=log))
                stack.enter_context(contextlib.redirect_stdout(out))
                try:
                    result: Any = inst.elevate_and_wait(ctx, argv)
                except inst.InstallerError as exc:
                    result = exc
        return result, captured, out.getvalue()

    def test_uac_accepted_runs_the_script_elevated_and_returns_the_child_exit_code(self) -> None:
        result, seen, output = self.run_elevation(True, 0, 3, ["install", "--elevate", "--port", "9000"])
        self.assertEqual(result, 3)
        self.assertEqual(seen["lpVerb"], "runas")
        self.assertEqual(seen["fMask"], 0x40 | 0x100)  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
        self.assertEqual(seen["nShow"], 1)  # SW_SHOWNORMAL
        self.assertEqual(seen["lpDirectory"], os.getcwd())
        self.assertTrue(seen["lpFile"].lower().endswith("python.exe") or seen["lpFile"].lower().endswith("python3.exe"))
        script = os.path.abspath(inst.__file__)
        self.assertEqual(
            seen["lpParameters"], subprocess.list2cmdline([script, "install", "--port", "9000", "--elevated-child"])
        )
        self.assertNotIn("--elevate ", seen["lpParameters"] + " ")
        self.assertIn("step two", output)  # the child's log tail is shown by the parent

    def test_uac_declined_is_exit_2(self) -> None:
        result, _seen, _out = self.run_elevation(False, 1223, 0, ["install", "--elevate"])
        self.assertIsInstance(result, inst.InstallerError)
        self.assertEqual(result.code, inst.EXIT_USAGE)
        self.assertIn("declined", str(result))

    def test_other_shellexecute_failure_is_exit_1(self) -> None:
        result, _seen, _out = self.run_elevation(False, 5, 0, ["install", "--elevate"])
        self.assertEqual(result.code, inst.EXIT_ERROR)

    def test_pythonw_is_mapped_to_python(self) -> None:
        with mock.patch.object(sys, "executable", "C:\\Python313\\pythonw.exe"):
            _result, seen, _out = self.run_elevation(True, 0, 0, ["install", "--elevate"])
        self.assertEqual(seen["lpFile"], "C:\\Python313\\python.exe")


# ---------------------------------------------------------------------------------------------------------------------
# The winreg wrapper (against a fake winreg module: the real registry is never read by the tests)
# ---------------------------------------------------------------------------------------------------------------------


class FakeWinreg:
    """Just enough of the ``winreg`` module: OpenKey / EnumKey / QueryValueEx over a dictionary."""

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"

    class Key:
        def __init__(self, hive: str, path: str) -> None:
            self.hive, self.path = hive, path

        def __enter__(self) -> "FakeWinreg.Key":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    def __init__(self, values: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
        self.values = values

    def _children(self, hive: str, path: str) -> List[str]:
        names: List[str] = []
        for key_hive, key_path in self.values:
            if key_hive == hive and key_path.startswith(path + "\\"):
                name = key_path[len(path) + 1 :].split("\\")[0]
                if name not in names:
                    names.append(name)
        return names

    def OpenKey(self, hive: str, path: str) -> "FakeWinreg.Key":  # noqa: N802 - winreg API
        if (hive, path) not in self.values and not self._children(hive, path):
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return FakeWinreg.Key(hive, path)

    def EnumKey(self, key: "FakeWinreg.Key", index: int) -> str:  # noqa: N802 - winreg API
        names = self._children(key.hive, key.path)
        if index >= len(names):
            raise OSError(259, "No more data is available")
        return names[index]

    def QueryValueEx(self, key: "FakeWinreg.Key", name: str) -> Tuple[Any, int]:  # noqa: N802 - winreg API
        data = self.values.get((key.hive, key.path), {})
        if name not in data:
            raise FileNotFoundError(2, "value not found")
        return data[name], 1


class WinRegistryTests(unittest.TestCase):
    def registry(self, values: Dict[Tuple[str, str], Dict[str, Any]]) -> Any:
        with mock.patch.dict(sys.modules, {"winreg": FakeWinreg(values)}):
            return inst.WinRegistry()

    def test_matches_the_dictionary_registry(self) -> None:
        real = self.registry(REGISTRY.values)
        self.assertEqual(inst.pep514_executables(real), inst.pep514_executables(REGISTRY))
        self.assertEqual(real.subkeys("HKLM", CORE), ["3.12", "3.13", "3.12-32", "3.9"])
        self.assertEqual(
            real.value("HKLM", CORE + "\\3.12\\InstallPath", "ExecutablePath"), PF + "Python312\\python.exe"
        )

    def test_missing_keys_and_values_are_empty(self) -> None:
        real = self.registry({("HKLM", CORE + "\\3.12\\InstallPath"): {"": 5, "ExecutablePath": ""}})
        self.assertEqual(real.subkeys("HKCU", CORE), [])
        self.assertEqual(real.subkeys("HKLM", WOW), [])
        self.assertIsNone(real.value("HKLM", CORE + "\\3.12\\InstallPath", "Nope"))
        self.assertIsNone(real.value("HKLM", CORE + "\\3.12\\InstallPath", ""))  # not a string
        self.assertIsNone(real.value("HKLM", CORE + "\\3.12\\InstallPath", "ExecutablePath"))  # empty string
        self.assertIsNone(real.value("HKCU", "SOFTWARE\\Nothing", ""))
        self.assertEqual(inst.pep514_executables(real), [])


# ---------------------------------------------------------------------------------------------------------------------
# Real (non dry-run) context operations on throw-away processes and a temp dir
# ---------------------------------------------------------------------------------------------------------------------


def real_ctx() -> Any:
    return inst.Context(inst.detect_host(), env=dict(os.environ), interactive=False)


class ContextRunTests(unittest.TestCase):
    def test_captures_output_and_feeds_stdin(self) -> None:
        code = "import sys; data = sys.stdin.read(); print(data.upper(), end=''); sys.stderr.write('warn')"
        result = real_ctx().run([sys.executable, "-c", code], input_text="abc")
        self.assertEqual((result.returncode, result.stdout, result.stderr, result.dry), (0, "ABC", "warn", False))

    def test_stdin_is_closed_when_nothing_is_sent(self) -> None:
        result = real_ctx().run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])
        self.assertEqual(result.stdout.strip(), "''")

    def test_cwd_and_environment_are_passed(self) -> None:
        env = dict(os.environ, DESKTALK_PROBE="42")
        with tempfile.TemporaryDirectory() as folder:
            code = "import os; print(os.environ['DESKTALK_PROBE']); print(os.getcwd())"
            result = real_ctx().run([sys.executable, "-c", code], cwd=folder, env=env)
        lines = result.stdout.split()
        self.assertEqual(lines[0], "42")
        self.assertEqual(
            os.path.normcase(os.path.realpath(" ".join(lines[1:]))), os.path.normcase(os.path.realpath(folder))
        )

    def test_missing_executable_and_timeout_do_not_raise(self) -> None:
        ctx = real_ctx()
        missing = ctx.run(["definitely-not-a-real-command-desktalk"])
        self.assertEqual(missing.returncode, 127)
        self.assertIn("cannot run", missing.stderr)
        slow = ctx.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertEqual(slow.returncode, 124)
        self.assertIn("timed out", slow.stderr)

    def test_check_raises_with_the_last_output_line(self) -> None:
        ctx = real_ctx()
        code = "import sys; sys.stderr.write('first\\nsecond problem\\n'); sys.exit(3)"
        with self.assertRaises(inst.InstallerError) as caught:
            ctx.run([sys.executable, "-c", code], check=True, what="the helper")
        self.assertEqual(str(caught.exception), "the helper failed (exit 3): second problem")
        self.assertEqual(caught.exception.code, inst.EXIT_ERROR)
        self.assertEqual(
            ctx.run([sys.executable, "-c", "import sys; sys.exit(1)"], check=True, ok=(0, 1)).returncode, 1
        )

    def test_stream_mode_returns_the_exit_status_only(self) -> None:
        result = real_ctx().run([sys.executable, "-c", "import sys; sys.exit(7)"], stream=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "", ""))

    def test_dry_run_prints_and_returns_a_placeholder(self) -> None:
        ctx = inst.Context("linux", host="linux", dry_run=True, env={}, interactive=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(inst.subprocess, "run", side_effect=AssertionError):
            result = ctx.run(["tool", "a b"], cwd="/srv", input_text="secret")
        self.assertEqual((result.returncode, result.dry), (0, True))
        self.assertEqual(out.getvalue(), "[dry-run] $ tool 'a b'   (cwd: /srv)   (stdin: secret withheld)\n")
        self.assertNotIn("secret", out.getvalue().replace("secret withheld", ""))

    def test_decode_output(self) -> None:
        self.assertEqual(inst.decode_output("caf\u00e9".encode("utf-8")), "caf\u00e9")
        self.assertIsInstance(inst.decode_output(b"\xff\xfe\x80"), str)  # undecodable bytes never raise


class ContextFileTests(unittest.TestCase):
    def test_write_file_is_atomic_and_creates_parents(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "a", "b", "unit.service")
            ctx = real_ctx()
            ctx.write_file(target, b"one", 0o640)
            ctx.write_file(target, b"two")
            self.assertEqual(read_bytes(target), b"two")
            self.assertEqual(sorted(os.listdir(os.path.dirname(target))), ["unit.service"])  # no .tmp left behind
            if os.name == "posix":
                ctx.write_file(target, b"three", 0o640)
                self.assertEqual(os.stat(target).st_mode & 0o777, 0o640)

    def test_makedirs_remove_file_and_remove_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ctx = real_ctx()
            deep = os.path.join(folder, "x", "y")
            ctx.makedirs(deep)
            ctx.makedirs(deep)  # idempotent
            self.assertTrue(os.path.isdir(deep))
            victim = os.path.join(deep, "f.txt")
            ctx.write_file(victim, b"data")
            ctx.remove_empty_dir(deep)
            self.assertTrue(os.path.isdir(deep))  # not empty: left alone
            ctx.remove_file(victim)
            ctx.remove_file(victim)  # missing file is fine
            ctx.remove_empty_dir(deep)
            self.assertFalse(os.path.exists(deep))
            ctx.remove_empty_dir(deep)  # missing directory is fine

    def test_dry_run_changes_nothing_and_prints_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ctx = inst.Context(inst.detect_host(), dry_run=True, env={}, interactive=False)
            out = io.StringIO()
            target = os.path.join(folder, "new", "unit.service")
            with contextlib.redirect_stdout(out):
                ctx.write_file(target, b"abc", 0o644, "line one\nline two\n")
                ctx.makedirs(os.path.join(folder, "other"))
                ctx.remove_file(target)
                ctx.remove_empty_dir(folder)
            self.assertEqual(os.listdir(folder), [])
            text = out.getvalue()
            self.assertIn("[dry-run] write %s (3 bytes, mode 644)" % target, text)
            self.assertIn("===== BEGIN %s =====\nline one\nline two\n===== END %s =====" % (target, target), text)
            self.assertIn("[dry-run] mkdir", text)
            self.assertIn("[dry-run] delete", text)
            self.assertIn("(only if empty)", text)


if __name__ == "__main__":
    unittest.main()
