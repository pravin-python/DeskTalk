"""Command flows of service/install_service.py: dry-run output, exit codes, the order of every command it runs.

Real-mode flows run against a scripted in-memory machine (``Lab``): ``subprocess.run`` is replaced by a recorder that
answers from a script, files and directories are recorded instead of written. So the *sequence* of operations
(SPEC 10.1-10.4: stop sequence, state diff, first-admin, firewall, launchctl order) is asserted without touching the
real machine.
"""

from __future__ import annotations

import codecs
import configparser
import contextlib
import importlib.util
import io
import json
import ntpath
import os
import plistlib
import posixpath
import re
import socket
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "service", "install_service.py")


def load_installer() -> Any:
    """Import service/install_service.py once per test process (it is a script, not a package)."""
    name = "desktalk_install_service"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inst = load_installer()
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
SAFE = "O:BAG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;LS)(A;OICI;0x1200a9;;;BU)"
DRIVE_ROOT = "D:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICIIO;0x1301bf;;;AU)(A;OICI;0x1200a9;;;BU)"
INFO = {"name": "DeskTalk", "registration_open": False, "needs_setup": False, "tls": False}
PASSWORD = "S3cret-pass-phrase"
SPEC_LITERAL = "A & B\\Free Chat 100%\\"

PATHS = {
    "windows": {
        "app": "C:\\DeskTalk",
        "python": "C:\\Program Files\\Python313\\python.exe",
        "data": "C:\\ProgramData\\DeskTalk\\data",
    },
    "linux": {"app": "/opt/desktalk", "python": "/usr/bin/python3", "data": "/var/lib/desktalk"},
    "macos": {
        "app": "/usr/local/desktalk",
        "python": "/usr/local/bin/python3",
        "data": "/Library/Application Support/DeskTalk/data",
    },
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LabContext(inst.Context):
    """A context whose file writes and environment probes go to a ``Lab`` instead of the machine."""

    def __init__(self, lab: Lab, target: str, env: Dict[str, str]) -> None:
        super().__init__(target, host=target, dry_run=False, env=env, interactive=False, app_root=lab.paths["app"])
        self.lab = lab
        self.exists_fn = lab.exists
        self.size_fn = lab.size
        self.realpath_fn = lambda path: path
        self.health_fn = lab.health
        self.lan_fn = lambda: ("192.168.1.5", ["10.0.0.7"])
        self.sleep_fn = lambda seconds: None
        self.admin_fn = lambda: lab.admin

    def which(self, name: str) -> Optional[str]:
        return self.lab.binaries.get(name)

    def write_file(self, path: str, data: bytes, mode: Optional[int] = None, text: Optional[str] = None) -> None:
        self.lab.files[path] = data
        self.lab.events.append("write: " + path)

    def makedirs(self, path: str, mode: int = 0o755) -> None:
        self.lab.present.add(path)
        self.lab.events.append("mkdir: " + path)

    def remove_empty_dir(self, path: str) -> None:
        self.lab.events.append("rmdir: " + path)

    def remove_file(self, path: str) -> None:
        self.lab.files.pop(path, None)
        self.lab.events.append("remove: " + path)


class Lab:
    """A fake machine for one target OS: scripted command replies, in-memory files, an ordered event log."""

    def __init__(self, target: str, admin: bool = True, env: Optional[Dict[str, str]] = None) -> None:
        self.target = target
        self.paths = PATHS[target]
        self.mod = ntpath if target == "windows" else posixpath
        self.admin = admin
        self.events: List[str] = []
        self.runs: List[Dict[str, Any]] = []
        self.files: Dict[str, bytes] = {}
        self.replies: List[Tuple[str, Tuple[int, str, str]]] = []
        self.sddl: Dict[str, str] = {}
        self.binaries = {
            "runuser": "/usr/sbin/runuser",
            "systemd-inhibit": "/usr/bin/systemd-inhibit",
            "nologin": "/usr/sbin/nologin",
            "ufw": "/usr/sbin/ufw",
            "firewall-cmd": "/usr/bin/firewall-cmd",
        }
        self.present = {
            self.paths["python"],
            self.mod.join(self.paths["app"], "server.py"),
            self.mod.join(self.paths["app"], "chatd"),
        }
        self.port_open = False
        self.health_answer: Optional[Dict[str, Any]] = dict(INFO)
        self.port = free_port()
        self.ctx = LabContext(self, target, env or {})
        self.backend = inst.make_backend(self.ctx)

    # -- the fake machine ---------------------------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.present

    def size(self, path: str) -> int:
        if not self.exists(path):
            raise FileNotFoundError(path)
        return 1000

    def health(self, *args: Any) -> Optional[Dict[str, Any]]:
        return self.health_answer

    def reply(self, needle: str, rc: int = 0, out: str = "", err: str = "") -> None:
        """Answer every later command whose joined command line contains ``needle`` (latest rule wins)."""
        self.replies.append((needle, (rc, out, err)))

    def fake_run(self, args: Any, **kwargs: Any) -> Any:
        argv = [str(a) for a in args]
        self.runs.append(
            {
                "argv": argv,
                "cwd": kwargs.get("cwd"),
                "env": kwargs.get("env"),
                "input": kwargs.get("input"),
                "stream": "stdout" not in kwargs,
            }
        )
        self.events.append("run: " + " ".join(argv))
        if argv[0] == "icacls" and "/save" in argv:
            text = "name\r\n" + self.sddl.get(argv[1], SAFE) + "\r\n"  # real icacls: UTF-16 LE, no BOM
            with open(argv[3], "wb") as handle:
                handle.write(text.encode("utf-16-le"))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        line = " ".join(argv)
        rc, out, err = 0, "", ""
        for needle, answer in reversed(self.replies):
            if needle in line:
                rc, out, err = answer
                break
        return subprocess.CompletedProcess(argv, rc, out.encode(), err.encode())

    def read_state(self, path: str) -> Optional[Dict[str, Any]]:
        return json.loads(self.files[path].decode("utf-8")) if path in self.files else None

    @contextlib.contextmanager
    def patched(self, stdin: str = "") -> Iterator[None]:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(inst.subprocess, "run", self.fake_run))
            stack.enter_context(mock.patch.object(inst, "read_state", self.read_state))
            stack.enter_context(mock.patch.object(inst, "port_is_open", lambda host, port, timeout=1.0: self.port_open))
            stack.enter_context(mock.patch.object(inst, "STOP_WAIT_S", 0.0))
            stack.enter_context(mock.patch.object(inst, "STOP_RECHECK_S", 0.0))
            stack.enter_context(mock.patch.object(inst, "HEALTH_WAIT_S", 0.0))
            stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(stdin)))
            yield

    # -- driving it ---------------------------------------------------------------------------------------------

    def invoke(self, argv: List[str], stdin: str = "") -> Tuple[int, str]:
        """Run one command line; returns ``(exit code, stdout)``. ``InstallerError`` becomes its exit code."""
        args, chatd_args = inst.parse_args(argv)
        out = io.StringIO()
        with self.patched(stdin), contextlib.redirect_stdout(out):
            try:
                code = inst.dispatch(self.ctx, self.backend, args, chatd_args, argv)
            except inst.InstallerError as exc:
                print("ERROR: %s" % exc)
                code = exc.code
        return code, out.getvalue()

    def install_args(self, *extra: str) -> List[str]:
        return ["install", "--port", str(self.port), "--python", self.paths["python"], *extra]

    def state(self) -> Dict[str, Any]:
        return json.loads(self.files[self.backend.state_path()].decode("utf-8"))

    def commands(self) -> List[str]:
        return [" ".join(run["argv"]) for run in self.runs]

    def find_run(self, needle: str) -> Dict[str, Any]:
        for run in self.runs:
            if needle in " ".join(run["argv"]):
                return run
        raise AssertionError("no command containing %r among:\n%s" % (needle, "\n".join(self.commands())))


class LabTestCase(unittest.TestCase):
    def assert_order(self, lab: Lab, *needles: str) -> None:
        """Every needle occurs in ``lab.events``, each after the previous one."""
        position = 0
        for needle in needles:
            for index in range(position, len(lab.events)):
                if needle in lab.events[index]:
                    position = index + 1
                    break
            else:
                self.fail("%r not found after event %d in:\n%s" % (needle, position, "\n".join(lab.events)))

    def assert_absent(self, lab: Lab, needle: str) -> None:
        hits = [event for event in lab.events if needle in event]
        self.assertEqual(hits, [], "unexpected event(s) containing %r" % needle)


# ---------------------------------------------------------------------------------------------------------------------
# Windows: install, stop sequence, uninstall
# ---------------------------------------------------------------------------------------------------------------------


class WindowsInstallTests(LabTestCase):
    def setUp(self) -> None:
        self.lab = Lab("windows")
        self.lab.reply("schtasks /Query", 1, "", "ERROR: The system cannot find the file specified.")
        self.lab.reply("firewall delete rule", 1)  # no old rule yet: netsh answers 1

    def test_first_install_runs_every_step_in_order(self) -> None:
        lab = self.lab
        code, out = lab.invoke(
            lab.install_args("--name", "Free Chat", "--admin", "bob", "--password-stdin"), PASSWORD + "\n"
        )
        self.assertEqual(code, 0, out)
        data, root, py_dir = lab.paths["data"], lab.paths["app"], "C:\\Program Files\\Python313"
        state_dir = "C:\\ProgramData\\DeskTalk"
        self.assert_order(
            lab,
            "sqlite3",  # interpreter validation: python -c "import sys,sqlite3,..."
            "-c import chatd",
            "icacls %s /save" % root,
            "icacls %s /save" % py_dir,
            "mkdir: " + state_dir,
            "icacls %s /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F *S-1-5-32-545:(OI)(CI)RX"
            % state_dir,
            "mkdir: " + data,
            "icacls %s /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F *S-1-5-19:(OI)(CI)M" % data,
            "icacls %s /save" % data,
            "-m chatd create-admin bob --password-stdin --data-dir " + data,
            "schtasks /Query /TN DeskTalk",
            "write: " + state_dir + "\\DeskTalk.task.xml",
            "schtasks /Create /TN DeskTalk /XML " + state_dir + "\\DeskTalk.task.xml /F",
            "remove: " + data + "\\control\\stop.request",
            "schtasks /Change /TN DeskTalk /ENABLE",
            "schtasks /Run /TN DeskTalk",
            "netsh advfirewall firewall delete rule name=DeskTalk",
            "netsh advfirewall firewall add rule name=DeskTalk dir=in action=allow protocol=TCP localport=%d"
            % lab.port,
            "write: " + state_dir + "\\install.json",
        )
        self.assert_absent(lab, "schtasks /End")  # nothing to stop on a first install
        self.assert_absent(lab, "/DISABLE")
        self.assertIn("starts at boot, before anyone logs in", out)
        self.assertIn("No saved installation found", out)
        self.assertIn("Share this link: http://192.168.1.5:%d/" % lab.port, out)
        self.assertIn("powercfg /change standby-timeout-ac 0", out)
        self.assertIn("powercfg /change hibernate-timeout-ac 0", out)
        self.assertIn("Plain HTTP: anyone on this network can read passwords and messages", out)
        self.assertIn("python service/install_service.py stop", out)  # the upgrade recipe

    def test_the_password_only_travels_on_stdin_of_create_admin(self) -> None:
        lab = self.lab
        code, out = lab.invoke(lab.install_args("--admin", "bob", "--password-stdin"), PASSWORD + "\n")
        self.assertEqual(code, 0, out)
        run = lab.find_run("create-admin")
        self.assertEqual(run["input"], (PASSWORD + "\n").encode("utf-8"))
        self.assertEqual(run["cwd"], lab.paths["app"])
        self.assertEqual(run["argv"][:6], [lab.paths["python"], "-X", "utf8", "-m", "chatd", "create-admin"])
        self.assertEqual(run["env"]["DESKTALK_PORT"], str(lab.port))
        everything = "\n".join(lab.events) + out + "".join(v.decode("utf-8", "replace") for v in lab.files.values())
        everything += "".join(" ".join(r["argv"]) for r in lab.runs)
        self.assertNotIn(PASSWORD, everything)

    def test_written_task_and_state_file(self) -> None:
        lab = self.lab
        code, _out = lab.invoke(
            lab.install_args("--name", "Free Chat", "--allowed-host", "chat.corp", "--redirect-port", "80")
        )
        self.assertEqual(code, 0)
        root = ET.fromstring(lab.files["C:\\ProgramData\\DeskTalk\\DeskTalk.task.xml"])
        arguments = root.findtext("%sActions/%sExec/%sArguments" % (NS, NS, NS))
        self.assertIn('--name "Free Chat"', arguments)
        self.assertIn("--redirect-port 80 --allowed-host chat.corp", arguments)
        self.assertTrue(lab.files["C:\\ProgramData\\DeskTalk\\DeskTalk.task.xml"].startswith(codecs.BOM_UTF16_LE))
        state = lab.state()
        self.assertEqual(tuple(state), inst.STATE_KEYS)
        self.assertEqual(state["python"], lab.paths["python"])
        self.assertEqual(state["data_dir"], lab.paths["data"])
        self.assertEqual(state["user"], "NT AUTHORITY\\LOCAL SERVICE")
        self.assertEqual(state["firewall"], {"port": lab.port, "scope": "localsubnet", "profile": "private,domain"})
        netsh = lab.find_run("add rule")["argv"]
        self.assertIn("localport=%d,80" % lab.port, netsh)
        self.assertIn("profile=private,domain", netsh)
        self.assertIn("remoteip=localsubnet", netsh)

    def test_reinstall_prints_the_diff_stops_first_and_keeps_saved_options(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args("--name", "Free Chat"))[0], 0)
        lab.events.clear()
        lab.replies.clear()
        lab.reply("schtasks /Query", 0)  # the task exists now
        new_port = free_port()
        code, out = lab.invoke(["install", "--port", str(new_port), "--tls"])
        self.assertEqual(code, 0, out)
        self.assertIn("Changes versus the saved installation:", out)
        self.assertIn("  port: %d -> %d" % (lab.port, new_port), out)
        self.assertIn("  tls: false -> true", out)
        self.assertNotIn("  name:", out)  # unchanged keys are not listed
        self.assert_order(
            lab,
            "schtasks /Query /TN DeskTalk",
            "schtasks /Change /TN DeskTalk /DISABLE",
            "write: C:\\ProgramData\\DeskTalk\\data\\control\\stop.request",
            "schtasks /End /TN DeskTalk",
            "schtasks /Create",
            "remove: C:\\ProgramData\\DeskTalk\\data\\control\\stop.request",
            "schtasks /Change /TN DeskTalk /ENABLE",
            "schtasks /Run /TN DeskTalk",
        )
        self.assertEqual(lab.state()["name"], "Free Chat")  # not given again: taken from the state file
        self.assertEqual(lab.state()["python"], lab.paths["python"])
        self.assertTrue(lab.state()["tls"])
        self.assert_order(lab, "-m chatd tls-init --data-dir " + lab.paths["data"], "schtasks /Create")

    def test_reinstall_with_a_new_port_and_data_dir_stops_the_old_server_where_it_runs(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        old_port, old_data = lab.port, lab.paths["data"]
        lab.events.clear()
        lab.replies.clear()
        lab.reply("schtasks /Query", 0)
        waited: List[int] = []

        def closed(ctx: Any, host: str, port: int, timeout: float) -> bool:
            waited.append(port)
            return True

        new_port = free_port()
        with mock.patch.object(inst, "wait_port_closed", side_effect=closed):
            code, out = lab.invoke(["install", "--port", str(new_port), "--data-dir", "C:\\Chat2\\data"])
        self.assertEqual(code, 0, out)
        self.assertEqual(waited, [old_port, old_port])  # graceful wait, then the re-check after /End
        self.assert_order(
            lab,
            "write: " + old_data + "\\control\\stop.request",  # the running server polls ITS data dir
            "schtasks /End /TN DeskTalk",
            "schtasks /Create",
            "remove: C:\\Chat2\\data\\control\\stop.request",  # a stale request must not stop the new process
            "schtasks /Change /TN DeskTalk /ENABLE",
        )
        self.assert_absent(lab, "write: C:\\Chat2\\data\\control\\stop.request")
        self.assertEqual(lab.state()["data_dir"], "C:\\Chat2\\data")

    def test_without_administrator_rights_it_prints_the_steps_and_changes_nothing(self) -> None:
        lab = Lab("windows", admin=False)
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 2)
        self.assertIn("Run as administrator", out)
        self.assertIn("install_service.py", out)
        self.assertEqual((lab.events, lab.runs, lab.files), ([], [], {}))

    def test_elevate_hands_over_to_the_uac_flow(self) -> None:
        lab = Lab("windows", admin=False)
        argv = lab.install_args("--elevate")
        with mock.patch.object(inst, "elevate_and_wait", return_value=7) as elevate:
            code, _out = lab.invoke(argv)
        self.assertEqual(code, 7)
        self.assertEqual(elevate.call_args[0][1], argv)
        self.assertEqual(lab.runs, [])

    def test_unsafe_acl_refuses_before_changing_anything(self) -> None:
        lab = self.lab
        lab.sddl[lab.paths["app"]] = DRIVE_ROOT
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 2)
        self.assertIn("UNSAFE: C:\\DeskTalk: Authenticated Users can write", out)
        self.assertIn("--harden", out)
        self.assertIn("icacls C:\\DeskTalk /inheritance:r /grant:r", out)
        for needle in ("mkdir:", "schtasks /Create", "write:", "/grant:r"):
            self.assert_absent(lab, needle)

    def test_harden_fixes_the_failing_path_and_continues(self) -> None:
        lab = self.lab
        lab.sddl[lab.paths["app"]] = DRIVE_ROOT
        original = lab.fake_run

        def healing(args: Any, **kwargs: Any) -> Any:
            if list(args)[:2] == ["icacls", lab.paths["app"]] and "/grant:r" in list(args):
                lab.sddl[lab.paths["app"]] = SAFE
            return original(args, **kwargs)

        with mock.patch.object(lab, "fake_run", healing):
            code, out = lab.invoke(lab.install_args("--harden"))
        self.assertEqual(code, 0, out)
        hardened = [c for c in lab.commands() if c.startswith("icacls C:\\DeskTalk /inheritance:r")]
        self.assertEqual(len(hardened), 1)
        self.assertIn("*S-1-5-19:(OI)(CI)RX *S-1-5-32-545:(OI)(CI)RX", hardened[0])
        self.assert_order(lab, "icacls C:\\DeskTalk /inheritance:r", "schtasks /Create")

    def test_per_user_python_is_refused_with_an_explanation(self) -> None:
        lab = self.lab
        per_user = "C:\\Users\\bob\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
        lab.present.add(per_user)
        code, out = lab.invoke(["install", "--port", str(lab.port), "--python", per_user])
        self.assertEqual(code, 2)
        self.assertIn("install Python for all users", out)
        self.assertEqual(lab.runs, [])

    def test_run_as_system(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args("--run-as-system"))[0], 0)
        root = ET.fromstring(lab.files["C:\\ProgramData\\DeskTalk\\DeskTalk.task.xml"])
        principal = root.find("%sPrincipals/%sPrincipal" % (NS, NS))
        self.assertEqual(principal.findtext(NS + "UserId"), "S-1-5-18")
        self.assertEqual(principal.findtext(NS + "RunLevel"), "HighestAvailable")
        data_acl = [c for c in lab.commands() if c.startswith("icacls %s /inheritance:r" % lab.paths["data"])][0]
        self.assertNotIn("S-1-5-19", data_acl)
        self.assertEqual(lab.state()["user"], "NT AUTHORITY\\SYSTEM")
        lab.events.clear()
        self.assertEqual(lab.invoke(["install", "--port", str(lab.port)])[0], 0)  # the choice is remembered
        self.assertEqual(lab.state()["user"], "NT AUTHORITY\\SYSTEM")

    def test_firewall_options_and_no_firewall(self) -> None:
        lab = self.lab
        self.assertEqual(
            lab.invoke(lab.install_args("--allow-from", "10.0.0.0/24,10.1.0.0/16", "--firewall-profile", "any"))[0], 0
        )
        netsh = lab.find_run("add rule")["argv"]
        self.assertIn("remoteip=10.0.0.0/24,10.1.0.0/16", netsh)
        self.assertIn("profile=any", netsh)
        self.assertEqual(lab.state()["firewall"]["scope"], "10.0.0.0/24,10.1.0.0/16")
        lab.events.clear()
        self.assertEqual(lab.invoke(["install", "--port", str(lab.port), "--no-firewall"])[0], 0)
        self.assert_absent(lab, "netsh")
        self.assertEqual(
            lab.state()["firewall"]["profile"], "any"
        )  # the recorded rule stays recorded (uninstall removes it)

    def test_no_firewall_on_a_fresh_install_records_none(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args("--no-firewall"))[0], 0)
        self.assert_absent(lab, "netsh")
        self.assertIsNone(lab.state()["firewall"])

    def test_netsh_failure_other_than_rule_absent_is_an_error(self) -> None:
        lab = self.lab
        lab.reply("firewall delete rule", 5, "", "Access is denied")
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 1)
        self.assertIn("netsh could not delete the old firewall rule", out)

    def test_unhealthy_server_exits_3_after_the_state_was_written(self) -> None:
        lab = self.lab
        lab.health_answer = None
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 3)
        self.assertIn("did not answer /healthz", out)
        self.assertIn("logs", out)
        self.assertIn(lab.backend.state_path(), lab.files)

    def test_setup_code_and_next_step_while_needs_setup(self) -> None:
        lab = self.lab
        lab.health_answer = {**INFO, "needs_setup": True}
        with mock.patch.object(inst, "read_setup_code", return_value="ABCD1234"):
            code, out = lab.invoke(lab.install_args("--tls"))
        self.assertEqual(code, 0)
        self.assertIn("Setup code: ABCD1234", out)
        self.assertIn("next step: open https://127.0.0.1:%d/ and create the admin account." % lab.port, out)
        self.assertIn("cli -- create-admin <username>", out)
        self.assertIn("Share this link: https://192.168.1.5:%d/" % lab.port, out)

    def test_tls_option_runs_tls_init_before_the_service_exists_and_serves_https(self) -> None:
        lab = self.lab
        lab.reply("tls-init", 0, "TLS certificate ready: C:\\x\\cert.pem\nSHA-256 fingerprint: AA:BB\n")
        code, out = lab.invoke(lab.install_args("--tls"))
        self.assertEqual(code, 0, out)
        self.assert_order(lab, "mkdir: " + lab.paths["data"], "-m chatd tls-init --data-dir", "schtasks /Create")
        run = lab.find_run("tls-init")
        self.assertEqual(run["argv"][:6], [lab.paths["python"], "-X", "utf8", "-m", "chatd", "tls-init"])
        self.assertEqual(run["cwd"], lab.paths["app"])
        self.assertEqual(run["env"]["DESKTALK_TLS"], "1")  # the installed options reach the command
        self.assertIn("SHA-256 fingerprint: AA:BB", out)  # what tls-init printed is shown
        self.assertNotIn("Plain HTTP", out)
        task = ET.fromstring(lab.files["C:\\ProgramData\\DeskTalk\\DeskTalk.task.xml"])
        self.assertIn("--tls", task.findtext("%sActions/%sExec/%sArguments" % (NS, NS, NS)))
        self.assertEqual(len([c for c in lab.commands() if "tls-init" in c]), 1)

    def test_a_failing_tls_init_aborts_before_anything_is_registered(self) -> None:
        lab = self.lab
        lab.reply("tls-init", 1, "", "no certificate could be produced: openssl was not found")
        code, out = lab.invoke(lab.install_args("--tls", "--admin", "bob", "--password-stdin"), PASSWORD + "\n")
        self.assertEqual(code, 1)
        self.assertIn("TLS was requested but no certificate could be created (tls-init exit 1)", out)
        self.assertIn("openssl was not found", out)
        for needle in ("create-admin", "schtasks /Create", "write: C:\\ProgramData\\DeskTalk\\install.json"):
            self.assert_absent(lab, needle)

    def test_without_tls_there_is_no_certificate_step(self) -> None:
        lab = self.lab
        for flag in ("--no-tls", "--allow-sleep"):
            self.assertEqual(lab.invoke(lab.install_args(flag))[0], 0)
        self.assert_absent(lab, "tls-init")

    def test_explicit_no_tls_silences_the_warning(self) -> None:
        lab = self.lab
        self.assertNotIn("Plain HTTP", lab.invoke(lab.install_args("--no-tls"))[1])

    def test_user_option_is_refused(self) -> None:
        code, out = self.lab.invoke(self.lab.install_args("--user", "bob"))
        self.assertEqual(code, 2)
        self.assertIn("--user exists on Linux/macOS only", out)

    def test_create_admin_failure_aborts_before_the_service_exists(self) -> None:
        lab = self.lab
        lab.reply("create-admin", 1, "", "weak_password: too short")
        code, out = lab.invoke(lab.install_args("--admin", "bob", "--password-stdin"), "x\n")
        self.assertEqual(code, 1)
        self.assertIn("weak_password", out)
        self.assert_absent(lab, "schtasks /Create")

    def test_admin_needs_a_password_source(self) -> None:
        lab = self.lab
        code, out = lab.invoke(lab.install_args("--admin", "bob"))
        self.assertEqual(code, 2)
        self.assertIn("--password-stdin", out)
        self.assert_absent(lab, "schtasks /Create")

    def test_interactive_first_admin_question(self) -> None:
        lab = self.lab
        lab.ctx.interactive = True
        answers = ["", "carol"]
        lab.ctx.input_fn = lambda prompt: answers.pop(0)
        lab.ctx.getpass_fn = lambda prompt: PASSWORD
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 0, out)
        self.assertEqual(lab.find_run("create-admin")["input"], (PASSWORD + "\n").encode("utf-8"))
        self.assertIn("create-admin carol --password-stdin", " ".join(lab.find_run("create-admin")["argv"]))
        self.assertNotIn(PASSWORD, out)

    def test_interactive_question_is_skipped_when_a_database_exists_or_declined(self) -> None:
        lab = self.lab
        lab.ctx.interactive = True
        lab.present.add(lab.paths["data"] + "\\chat.db")
        lab.ctx.input_fn = lambda prompt: self.fail("must not ask when chat.db exists")
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        lab.present.clear()
        lab.present.update({lab.paths["python"], "C:\\DeskTalk\\server.py", "C:\\DeskTalk\\chatd"})
        lab.ctx.input_fn = lambda prompt: "n"
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        self.assertEqual([c for c in lab.commands() if "create-admin" in c], [])

    def test_interactive_retry_after_a_weak_password(self) -> None:
        lab = self.lab
        lab.ctx.interactive = True
        passwords = iter(["short", "short", PASSWORD, PASSWORD])
        lab.ctx.getpass_fn = lambda prompt: next(passwords)
        outcomes = iter([(1, "", "weak_password"), (0, "", "")])
        original = lab.fake_run

        def scripted(args: Any, **kwargs: Any) -> Any:
            if "create-admin" in list(args):
                rc, out, err = next(outcomes)
                lab.runs.append({"argv": [str(a) for a in args], "input": kwargs.get("input"), "env": kwargs.get("env"),
                                 "cwd": kwargs.get("cwd"), "stream": False})  # fmt: skip
                return subprocess.CompletedProcess(args, rc, out.encode(), err.encode())
            return original(args, **kwargs)

        with mock.patch.object(lab, "fake_run", scripted):
            code, out = lab.invoke(lab.install_args("--admin", "dave"))
        self.assertEqual(code, 0, out)
        self.assertIn("create-admin failed: weak_password", out)
        inputs = [r["input"] for r in lab.runs if "create-admin" in r["argv"]]
        self.assertEqual(inputs, [b"short\n", (PASSWORD + "\n").encode("utf-8")])

    def test_missing_checkout_files_are_an_error(self) -> None:
        lab = self.lab
        lab.present.discard("C:\\DeskTalk\\server.py")
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 2)
        self.assertIn("server.py is missing", out)

    def test_data_dir_inside_the_app_tree_warns(self) -> None:
        lab = self.lab
        code, out = lab.invoke(lab.install_args("--data-dir", "C:\\DeskTalk\\data"))
        self.assertEqual(code, 0)
        self.assertIn("the data directory lies inside the application directory", out)


class WindowsControlTests(LabTestCase):
    def setUp(self) -> None:
        self.lab = Lab("windows")
        self.lab.invoke(self.lab.install_args("--no-firewall"))
        self.lab.events.clear()
        self.lab.runs.clear()
        self.control = "C:\\ProgramData\\DeskTalk\\data\\control\\stop.request"

    def test_stop_sequence(self) -> None:
        code, out = self.lab.invoke(["stop"])
        self.assertEqual(code, 0, out)
        self.assert_order(
            self.lab,
            "schtasks /Change /TN DeskTalk /DISABLE",
            "write: " + self.control,
            "schtasks /End /TN DeskTalk",
        )
        self.assertIn("DeskTalk stopped.", out)

    def test_stop_hard_ends_the_task_and_fails_when_the_port_stays_open(self) -> None:
        self.lab.port_open = True
        code, out = self.lab.invoke(["stop"])
        self.assertEqual(code, 1)
        self.assertIn("still listening", out)
        self.assertIn("still open after ending the task", out)
        self.assert_order(self.lab, "/DISABLE", "stop.request", "schtasks /End")

    def test_stop_continues_when_the_task_cannot_be_disabled(self) -> None:
        self.lab.reply("/DISABLE", 1, "", "ERROR: The system cannot find the file specified.")
        code, out = self.lab.invoke(["stop"])
        self.assertEqual(code, 0)
        self.assertIn("could not disable the task", out)
        self.assert_order(self.lab, "/DISABLE", "write: " + self.control, "/End")

    def test_start_deletes_the_stale_stop_request_before_enabling(self) -> None:
        code, out = self.lab.invoke(["start"])
        self.assertEqual(code, 0, out)
        self.assert_order(
            self.lab, "remove: " + self.control, "schtasks /Change /TN DeskTalk /ENABLE", "schtasks /Run /TN DeskTalk"
        )
        self.assertIn("DeskTalk is running", out)

    def test_start_failure_and_unhealthy(self) -> None:
        self.lab.reply("/Run", 1, "", "ERROR: Access is denied.")
        code, out = self.lab.invoke(["start"])
        self.assertEqual(code, 1)
        self.assertIn("schtasks /Run failed (exit 1)", out)
        self.lab.replies.clear()
        self.lab.health_answer = None
        self.assertEqual(self.lab.invoke(["start"])[0], 3)

    def test_restart_is_stop_then_start(self) -> None:
        code, _out = self.lab.invoke(["restart"])
        self.assertEqual(code, 0)
        self.assert_order(
            self.lab, "/DISABLE", "write: " + self.control, "/End", "remove: " + self.control, "/ENABLE", "/Run"
        )

    def test_commands_need_privileges(self) -> None:
        self.lab.admin = False
        for command in ("start", "stop", "restart", "uninstall"):
            code, out = self.lab.invoke([command])
            self.assertEqual(code, 2, command)
            self.assertIn("Run as administrator", out)
        self.assertEqual(self.lab.runs, [])

    def test_status_parses_schtasks_and_reports_health(self) -> None:
        columns = ["PC", "\\DeskTalk", "N/A", "Running", "Interactive/Background", "1/1/2026 9:00:00 AM", "267009"]
        columns += ["SYSTEM", "python.exe", "C:\\x", "N/A", "Enabled"]
        self.lab.reply("/Query", 0, ",".join('"%s"' % column for column in columns) + "\r\n")
        self.lab.admin = False  # status never needs elevation
        code, out = self.lab.invoke(["status"])
        self.assertEqual(code, 0, out)
        self.assertIn("status=Running state=Enabled last result=267009", out)
        self.assertIn("health: OK", out)
        self.lab.health_answer = None
        code, out = self.lab.invoke(["status"])
        self.assertEqual(code, 3)
        self.assertIn("NOT healthy", out)

    def test_status_when_schtasks_is_denied_falls_back_to_health(self) -> None:
        self.lab.reply("/Query", 1, "", "ERROR: Access is denied.")
        code, out = self.lab.invoke(["status"])
        self.assertEqual(code, 0)
        self.assertIn("task: unavailable", out)
        self.assertIn("health: OK", out)

    def test_status_reports_a_missing_interpreter(self) -> None:
        self.lab.present.discard(self.lab.paths["python"])
        code, out = self.lab.invoke(["status"])
        self.assertIn("interpreter missing: %s" % self.lab.paths["python"], out)
        self.assertEqual(code, 0)

    def test_status_without_state_file_uses_defaults(self) -> None:
        lab = Lab("windows")
        code, out = lab.invoke(["status"])
        self.assertEqual(code, 0)
        self.assertIn("No install state file at C:\\ProgramData\\DeskTalk\\install.json", out)

    def test_uninstall_removes_exactly_what_was_installed(self) -> None:
        lab = Lab("windows")
        lab.reply("schtasks /Query", 1)
        lab.reply("firewall delete rule", 1)
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        lab.events.clear()
        code, out = lab.invoke(["uninstall"])
        self.assertEqual(code, 0, out)
        self.assert_order(
            lab,
            "/DISABLE",
            "stop.request",
            "schtasks /End",
            "schtasks /Delete /TN DeskTalk /F",
            "remove: C:\\ProgramData\\DeskTalk\\DeskTalk.task.xml",
            "netsh advfirewall firewall delete rule name=DeskTalk",
            "remove: C:\\ProgramData\\DeskTalk\\install.json",
        )
        self.assertNotIn(lab.backend.state_path(), lab.files)
        self.assertIn("Your data was NOT deleted", out)
        self.assertIn('rmdir /s /q "C:\\ProgramData\\DeskTalk\\data"', out)

    def test_uninstall_keep_firewall_and_unmanaged_firewall(self) -> None:
        lab = Lab("windows")
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        lab.events.clear()
        code, out = lab.invoke(["uninstall", "--keep-firewall"])
        self.assertEqual(code, 0)
        self.assert_absent(lab, "netsh")
        self.assertIn("Firewall rule kept", out)
        other = Lab("windows")
        self.assertEqual(other.invoke(other.install_args("--no-firewall"))[0], 0)
        other.events.clear()
        self.assertEqual(other.invoke(["uninstall"])[0], 0)
        self.assert_absent(other, "netsh")  # a rule the installer never created is never deleted

    def test_uninstall_without_state_warns(self) -> None:
        lab = Lab("windows")
        code, out = lab.invoke(["uninstall"])
        self.assertEqual(code, 0)
        self.assertIn("no install state file found", out)
        self.assert_absent(lab, "netsh")

    def test_cli_passthrough_runs_chatd_with_the_installed_options(self) -> None:
        lab = self.lab
        lab.reply("-m chatd", 73)
        code, _out = lab.invoke(["cli", "--", "create-admin", "alice"])
        self.assertEqual(code, 73)  # the exit status of chatd is passed through
        run = lab.find_run("-m chatd")
        self.assertEqual(
            run["argv"],
            [
                lab.paths["python"],
                "-X",
                "utf8",
                "-m",
                "chatd",
                "create-admin",
                "alice",
                "--data-dir",
                lab.paths["data"],
            ],
        )
        self.assertEqual(run["cwd"], lab.paths["app"])
        self.assertEqual(run["env"]["DESKTALK_PORT"], str(lab.port))
        self.assertEqual(run["env"]["DESKTALK_DATA_DIR"], lab.paths["data"])
        self.assertTrue(run["stream"])

    def test_cli_does_not_duplicate_an_explicit_data_dir_and_needs_elevation(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(["cli", "--", "doctor", "--data-dir=D:\\other"])[0], 0)
        self.assertEqual(lab.find_run("doctor")["argv"].count("--data-dir"), 0)
        lab.admin = False
        lab.runs.clear()
        code, out = lab.invoke(["cli", "--", "doctor"])
        self.assertEqual(code, 2)
        self.assertIn("Run as administrator", out)
        self.assertEqual(lab.runs, [])

    def test_cli_without_install_state_is_an_error(self) -> None:
        code, out = Lab("windows").invoke(["cli", "--", "doctor"])
        self.assertEqual(code, 1)
        self.assertIn("install the service first", out)

    def test_logs_prints_the_tail_of_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            os.makedirs(os.path.join(data_dir, "logs"))
            for name, text in (("desktalk.log", "a\nb\nc\n"), ("boot.log", "x\ny\n")):
                with open(os.path.join(data_dir, "logs", name), "w", encoding="utf-8") as handle:
                    handle.write(text)
            state = dict(self.lab.state(), data_dir=data_dir)
            self.lab.files[self.lab.backend.state_path()] = json.dumps(state).encode("utf-8")
            code, out = self.lab.invoke(["logs", "-n", "2"])
        self.assertEqual(code, 0)
        self.assertLess(out.index("b\nc"), out.index("x\ny"))  # desktalk.log first, then boot.log
        self.assertNotIn("\na\n", out)

    def test_logs_without_files_is_an_error_code(self) -> None:
        code, out = self.lab.invoke(["logs"])
        self.assertEqual(code, 1)
        self.assertIn("cannot read", out)


# ---------------------------------------------------------------------------------------------------------------------
# Linux (systemd)
# ---------------------------------------------------------------------------------------------------------------------


class LinuxTests(LabTestCase):
    def setUp(self) -> None:
        self.lab = Lab("linux")
        self.lab.reply("getent passwd", 2)  # the service user does not exist yet
        self.lab.reply("ufw status", 0, "Status: active\n")
        self.unit = "/etc/systemd/system/desktalk.service"
        self.data = self.lab.paths["data"]

    def test_first_install_sequence_with_the_system_user(self) -> None:
        lab = self.lab
        code, out = lab.invoke(lab.install_args("--admin", "bob", "--password-stdin"), PASSWORD + "\n")
        self.assertEqual(code, 0, out)
        self.assert_order(
            lab,
            "sqlite3",
            "getent passwd desktalk",
            "useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin desktalk",
            "install -d -o desktalk -g desktalk -m 0700 " + self.data,
            "chown -R desktalk:desktalk " + self.data,
            "runuser -u desktalk -- /usr/bin/python3 -c import chatd",
            "runuser -u desktalk -- test -w " + self.data,
            "runuser -u desktalk -- env ",
            "write: " + self.unit,
            "systemctl daemon-reload",
            "systemctl enable --now desktalk",
            "ufw status",
            "ufw allow %d/tcp" % lab.port,
            "write: /etc/desktalk/install.json",
        )
        admin = lab.find_run("create-admin")
        self.assertEqual(admin["input"], (PASSWORD + "\n").encode("utf-8"))
        self.assertIn("DESKTALK_PORT=%d" % lab.port, admin["argv"])
        self.assertIn("-m chatd create-admin bob --password-stdin --data-dir " + self.data, " ".join(admin["argv"]))
        self.assertNotIn(PASSWORD, "\n".join(lab.commands()) + out)
        unit = configparser.RawConfigParser(strict=False)
        unit.read_string(lab.files[self.unit].decode("utf-8"))
        self.assertEqual(unit.get("Service", "User"), "desktalk")
        self.assertEqual(unit.get("Service", "RestartPreventExitStatus"), "78")
        self.assertEqual(lab.state()["firewall"]["tool"], "ufw")
        self.assertEqual(lab.state()["user"], "desktalk")
        self.assertIn("HandleLidSwitch=ignore", out)

    def test_tls_init_runs_as_the_service_user_after_the_runtime_check(self) -> None:
        lab = self.lab
        code, out = lab.invoke(lab.install_args("--tls", "--admin", "bob", "--password-stdin"), PASSWORD + "\n")
        self.assertEqual(code, 0, out)
        self.assert_order(
            lab,
            "install -d -o desktalk -g desktalk -m 0700 " + self.data,
            "runuser -u desktalk -- test -w " + self.data,
            "-m chatd tls-init --data-dir " + self.data,
            "-m chatd create-admin bob",
            "write: " + self.unit,
        )
        argv = lab.find_run("tls-init")["argv"]
        self.assertEqual(argv[:5], ["runuser", "-u", "desktalk", "--", "env"])
        self.assertIn("DESKTALK_TLS=1", argv)
        self.assertEqual(argv[-5:], ["-m", "chatd", "tls-init", "--data-dir", self.data])

    def test_reinstall_restarts_and_moves_the_firewall_rule(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        old_port, new_port = lab.port, free_port()
        lab.events.clear()
        lab.replies.clear()
        lab.reply("getent passwd", 0, "desktalk:x:998:998::/nonexistent:/usr/sbin/nologin\n")
        lab.reply("ufw status", 0, "Status: active\n")
        code, out = lab.invoke(["install", "--port", str(new_port)])
        self.assertEqual(code, 0, out)
        self.assertIn("  port: %d -> %d" % (old_port, new_port), out)
        self.assert_absent(lab, "useradd")
        self.assert_absent(lab, "enable --now")
        self.assert_order(
            lab,
            "write: " + self.unit,
            "systemctl daemon-reload",
            "systemctl enable desktalk",
            "systemctl restart desktalk",
            "ufw delete allow %d/tcp" % old_port,
            "ufw allow %d/tcp" % new_port,
        )

    def test_firewalld_and_no_firewall_tool(self) -> None:
        lab = self.lab
        lab.reply("ufw status", 0, "Status: inactive\n")
        lab.reply("firewall-cmd --state", 0, "running\n")
        self.assertEqual(lab.invoke(lab.install_args("--redirect-port", "80", "--tls"))[0], 0)
        self.assert_order(
            lab,
            "firewall-cmd --state",
            "firewall-cmd --permanent --add-port=%d/tcp" % lab.port,
            "firewall-cmd --permanent --add-port=80/tcp",
            "firewall-cmd --reload",
        )
        self.assertEqual(lab.state()["firewall"]["tool"], "firewalld")
        self.assertEqual(lab.state()["firewall"]["ports"], [lab.port, 80])
        other = Lab("linux")
        other.reply("getent passwd", 0)
        other.reply("ufw status", 0, "Status: inactive\n")
        other.reply("firewall-cmd --state", 1, "", "not running")
        code, out = other.invoke(other.install_args())
        self.assertEqual(code, 0)
        self.assertIn("no active ufw/firewalld found", out)
        self.assertIsNone(other.state()["firewall"])

    def test_the_service_user_must_be_able_to_read_the_app(self) -> None:
        lab = self.lab
        lab.reply("runuser -u desktalk -- /usr/bin/python3 -c import chatd", 1, "", "PermissionError: /home/bob/app")
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 2)
        self.assertIn("/opt/desktalk", out)
        self.assertIn("--user <your own user>", out)
        self.assertIn("SELinux", out)
        self.assert_absent(lab, "write: " + self.unit)

    def test_data_dir_must_be_writable_by_the_service_user(self) -> None:
        lab = self.lab
        lab.reply("test -w", 1)
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 2)
        self.assertIn("cannot write the data directory", out)

    def test_user_selection(self) -> None:
        with mock.patch.object(inst._PosixBackend, "user_exists", return_value=True):
            lab = Lab("linux", env={"SUDO_USER": "alice"})
            self.assertEqual(lab.invoke(lab.install_args())[0], 0)
            self.assertEqual(lab.state()["user"], "alice")
            self.assertIn("install -d -o alice -g alice -m 0700 " + lab.paths["data"], lab.commands())
            self.assertEqual([c for c in lab.commands() if "useradd" in c], [])
            explicit = Lab("linux", env={"SUDO_USER": "alice"})
            self.assertEqual(explicit.invoke(explicit.install_args("--user", "bob"))[0], 0)
            self.assertEqual(explicit.state()["user"], "bob")
            root_sudo = Lab("linux", env={"SUDO_USER": "root"})
            root_sudo.reply("getent passwd", 0)
            self.assertEqual(root_sudo.invoke(root_sudo.install_args())[0], 0)
            self.assertEqual(root_sudo.state()["user"], "desktalk")  # SUDO_USER=root is ignored

    def test_root_and_unknown_users_are_refused(self) -> None:
        code, out = self.lab.invoke(self.lab.install_args("--user", "root"))
        self.assertEqual(code, 2)
        self.assertIn("must not run as root", out)
        with mock.patch.object(inst._PosixBackend, "user_exists", return_value=False):
            code, out = self.lab.invoke(self.lab.install_args("--user", "ghost"))
        self.assertEqual(code, 2)
        self.assertIn("'ghost' does not exist", out)

    def test_windows_only_flags_are_refused(self) -> None:
        for flag in ("--run-as-system", "--harden"):
            code, out = self.lab.invoke(self.lab.install_args(flag))
            self.assertEqual(code, 2, flag)
            self.assertIn("Windows", out)

    def test_allow_sleep_and_missing_inhibit(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args("--allow-sleep"))[0], 0)
        self.assertNotIn("systemd-inhibit", lab.files[self.unit].decode("utf-8"))
        self.assertTrue(lab.state()["allow_sleep"])
        other = Lab("linux")
        other.binaries.pop("systemd-inhibit")
        other.reply("getent passwd", 0)
        other.reply("ufw status", 0, "Status: active\n")
        self.assertEqual(other.invoke(other.install_args())[0], 0)
        self.assertNotIn("systemd-inhibit", other.files[self.unit].decode("utf-8"))
        third = Lab("linux")
        third.reply("getent passwd", 0)
        self.assertEqual(third.invoke(third.install_args("--allow-sleep"))[0], 0)
        self.assertEqual(third.invoke(["install", "--port", str(third.port), "--no-allow-sleep"])[0], 0)
        self.assertIn("systemd-inhibit", third.files[self.unit].decode("utf-8"))

    def test_systemctl_failure_is_reported(self) -> None:
        lab = self.lab
        lab.reply("enable --now", 1, "", "Failed to enable unit: Unit file desktalk.service does not exist.")
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 1)
        self.assertIn("systemctl enable --now failed (exit 1)", out)

    def test_control_commands(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        lab.events.clear()
        for command in ("start", "stop", "restart"):
            self.assertEqual(lab.invoke([command])[0], 0, command)
            self.assert_order(lab, "systemctl %s desktalk" % command)
        lab.reply("systemctl stop", 5, "", "Failed to stop desktalk.service")
        code, out = lab.invoke(["stop"])
        self.assertEqual(code, 1)
        self.assertIn("systemctl stop failed", out)
        lab.admin = False
        self.assertEqual(lab.invoke(["restart"])[0], 2)
        self.assertIn("sudo", lab.invoke(["restart"])[1])

    def test_status_and_logs(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        lab.reply("is-active", 0, "active\n")
        lab.reply("systemctl show", 0, "ActiveState=active\nSubState=running\nMainPID=4242\nExecMainStatus=0\n")
        code, out = lab.invoke(["status"])
        self.assertEqual(code, 0)
        self.assertIn("unit desktalk: active (ActiveState=active SubState=running MainPID=4242 ExecMainStatus=0)", out)
        self.assertEqual(lab.invoke(["logs", "-n", "25"])[0], 0)
        journal = lab.find_run("journalctl")
        self.assertEqual(journal["argv"], ["journalctl", "-u", "desktalk", "-n", "25", "--no-pager"])
        self.assertTrue(journal["stream"])

    def test_uninstall_removes_the_unit_and_the_recorded_rule_only(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        lab.events.clear()
        code, out = lab.invoke(["uninstall"])
        self.assertEqual(code, 0, out)
        self.assert_order(
            lab,
            "systemctl disable --now desktalk",
            "remove: " + self.unit,
            "systemctl daemon-reload",
            "ufw delete allow %d/tcp" % lab.port,
            "remove: /etc/desktalk/install.json",
        )
        self.assertIn('rm -rf "%s"' % self.data, out)
        self.assertIn("userdel desktalk", out)
        self.assertIn("Your data was NOT deleted", out)

    def test_uninstall_of_a_personal_user_install_does_not_mention_userdel(self) -> None:
        with mock.patch.object(inst._PosixBackend, "user_exists", return_value=True):
            lab = Lab("linux")
            self.assertEqual(lab.invoke(lab.install_args("--user", "alice"))[0], 0)
            out = lab.invoke(["uninstall"])[1]
        self.assertNotIn("userdel", out)

    def test_cli_passthrough_as_the_service_user(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args("--tls", "--name", "Free Chat"))[0], 0)
        lab.reply("-m chatd", 0)
        code, _out = lab.invoke(["cli", "--", "reset-password", "alice", "--must-change"])
        self.assertEqual(code, 0)
        argv = lab.find_run("reset-password")["argv"]
        self.assertEqual(argv[:5], ["runuser", "-u", "desktalk", "--", "env"])
        self.assertIn("DESKTALK_TLS=1", argv)
        self.assertIn("DESKTALK_NAME=Free Chat", argv)
        self.assertEqual(
            argv[-10:],
            ["/usr/bin/python3", "-X", "utf8", "-m", "chatd", "reset-password", "alice", "--must-change"]
            + ["--data-dir", self.data],
        )
        self.assertEqual(lab.find_run("reset-password")["cwd"], lab.paths["app"])
        lab.admin = False
        code, out = lab.invoke(["cli", "--", "doctor"])
        self.assertEqual(code, 2)
        self.assertIn("sudo", out)

    def test_cli_runs_directly_when_already_the_service_user(self) -> None:
        lab = self.lab
        self.assertEqual(lab.invoke(lab.install_args())[0], 0)
        lab.admin = False
        with mock.patch.object(inst._PosixBackend, "is_current_user", return_value=True):
            code, _out = lab.invoke(["cli", "--", "doctor"])
        self.assertEqual(code, 0)
        argv = lab.find_run("doctor")["argv"]
        self.assertEqual(argv[0], "env")
        self.assertNotIn("runuser", argv)


# ---------------------------------------------------------------------------------------------------------------------
# macOS (launchd)
# ---------------------------------------------------------------------------------------------------------------------


class MacTests(LabTestCase):
    PLIST = "/Library/LaunchDaemons/com.desktalk.server.plist"

    def setUp(self) -> None:
        self.lab = Lab("macos")
        self.lab.reply("--getglobalstate", 0, "Firewall is enabled. (State = 1)\n")
        patcher = mock.patch.object(inst._PosixBackend, "user_exists", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, *extra: str) -> Tuple[int, str]:
        return self.lab.invoke(self.lab.install_args("--user", "alice", *extra))

    def test_first_install_sequence(self) -> None:
        lab = self.lab
        code, out = self.install()
        self.assertEqual(code, 0, out)
        data = lab.paths["data"]
        self.assert_order(
            lab,
            "install -d -o alice -g staff -m 0700 " + data,
            "install -d -o alice -g staff -m 0700 " + data + "/logs",
            "write: " + data + "/logs/launchd.log",
            "chown -R alice:staff " + data,
            "sudo -n -u alice -- /usr/local/bin/python3 -c import chatd",
            "write: " + self.PLIST,
            "chown root:wheel " + self.PLIST,
            "chmod 0644 " + self.PLIST,
            "launchctl bootout system/com.desktalk.server",
            "launchctl enable system/com.desktalk.server",
            "launchctl bootstrap system " + self.PLIST,
            "socketfilterfw --getglobalstate",
            "socketfilterfw --add /usr/local/bin/python3",
            "socketfilterfw --unblockapp /usr/local/bin/python3",
            "socketfilterfw --getappblocked /usr/local/bin/python3",
            "write: /Library/Application Support/DeskTalk/install.json",
        )
        plist = plistlib.loads(lab.files[self.PLIST])
        self.assertEqual((plist["UserName"], plist["GroupName"]), ("alice", "staff"))
        self.assertEqual(plist["ProgramArguments"][:2], ["/usr/bin/caffeinate", "-i"])
        state = lab.state()
        self.assertEqual(state["firewall"]["tool"], "socketfilterfw")
        self.assertEqual(state["firewall"]["binary"], "/usr/local/bin/python3")
        self.assertEqual(state["user"], "alice")
        self.assertIn("caffeinate", out)

    def test_application_firewall_off_is_left_alone(self) -> None:
        lab = self.lab
        lab.reply("--getglobalstate", 0, "Firewall is disabled. (State = 0)\n")
        code, out = self.install()
        self.assertEqual(code, 0)
        self.assert_absent(lab, "socketfilterfw --add")
        self.assertIn("Application Firewall is off", out)
        self.assertIsNone(lab.state()["firewall"])

    def test_framework_builds_use_the_python_app_binary(self) -> None:
        lab = self.lab
        framework = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
        app_binary = "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"
        lab.present.update({framework, app_binary})
        code, _out = lab.invoke(["install", "--port", str(lab.port), "--python", framework, "--user", "alice"])
        self.assertEqual(code, 0)
        self.assertEqual(lab.state()["firewall"]["binary"], app_binary)
        self.assertIn("socketfilterfw --add " + app_binary, " ".join(lab.commands()))

    def test_tcc_protected_locations_are_refused_unless_forced(self) -> None:
        lab = self.lab
        lab.present.update({"/Users/bob/Documents/desktalk/server.py", "/Users/bob/Documents/desktalk/chatd"})
        for argument in ("--data-dir", "--app-root"):
            code, out = self.install(argument, "/Users/bob/Documents/desktalk")
            self.assertEqual(code, 2, argument)
            self.assertIn("/usr/local/desktalk", out)
            self.assertIn("--force", out)
        for needle in ("write:", "mkdir:", "launchctl", "install -d"):
            self.assert_absent(lab, needle)
        code, out = self.install("--data-dir", "/Volumes/Backup/desktalk-data", "--force")
        self.assertEqual(code, 0, out)
        self.assertIn("--force given, continuing", out)

    def test_user_is_required(self) -> None:
        lab = Lab("macos")
        code, out = lab.invoke(lab.install_args())
        self.assertEqual(code, 2)
        self.assertIn("pass --user", out)
        with_sudo = Lab("macos", env={"SUDO_USER": "bob"})
        with_sudo.reply("--getglobalstate", 0, "Firewall is disabled.")
        self.assertEqual(with_sudo.invoke(with_sudo.install_args())[0], 0)
        self.assertEqual(with_sudo.state()["user"], "bob")

    def test_legacy_launchctl_before_10_10(self) -> None:
        lab = self.lab
        lab.ctx.macos_version = lambda: (10, 9)
        self.assertEqual(self.install()[0], 0)
        self.assertIn("launchctl load -w " + self.PLIST, lab.commands())
        self.assertIn("launchctl unload -w " + self.PLIST, lab.commands())
        self.assertNotIn("launchctl bootstrap system " + self.PLIST, lab.commands())

    def test_bootstrap_failure_is_an_error_unless_the_job_is_already_loaded(self) -> None:
        lab = self.lab
        lab.reply("launchctl bootstrap", 5, "", "Bootstrap failed: 5: Input/output error")
        lab.reply("launchctl print", 113, "", "Could not find service")
        code, out = self.install()
        self.assertEqual(code, 1)
        self.assertIn("Input/output error", out)
        lab.reply("launchctl print", 0, "state = running\n")
        self.assertEqual(self.install()[0], 0)

    def test_control_status_and_uninstall(self) -> None:
        lab = self.lab
        self.assertEqual(self.install()[0], 0)
        lab.events.clear()
        self.assertEqual(lab.invoke(["restart"])[0], 0)
        self.assert_order(lab, "launchctl kickstart -k system/com.desktalk.server")
        self.assertEqual(lab.invoke(["stop"])[0], 0)
        self.assert_order(lab, "launchctl bootout system/com.desktalk.server")
        self.assertEqual(lab.invoke(["start"])[0], 0)
        self.assert_order(
            lab, "launchctl enable system/com.desktalk.server", "launchctl bootstrap system " + self.PLIST
        )
        lab.reply("launchctl print", 0, "com.desktalk.server = {\n\tstate = running\n\tpid = 777\n}\n")
        code, out = lab.invoke(["status"])
        self.assertEqual(code, 0)
        self.assertIn("state=running pid=777", out)
        logs_code, _ = lab.invoke(["logs"])
        self.assertEqual(logs_code, 1)  # no log files in the fake file system
        lab.events.clear()
        code, out = lab.invoke(["uninstall"])
        self.assertEqual(code, 0, out)
        self.assert_order(
            lab,
            "launchctl bootout system/com.desktalk.server",
            "remove: " + self.PLIST,
            "socketfilterfw --remove /usr/local/bin/python3",
            "remove: /Library/Application Support/DeskTalk/install.json",
        )
        self.assertIn("sudo rm -rf", out)

    def test_tls_init_runs_through_sudo_as_the_service_user(self) -> None:
        lab = self.lab
        code, out = self.install("--tls")
        self.assertEqual(code, 0, out)
        argv = lab.find_run("tls-init")["argv"]
        self.assertEqual(argv[:6], ["sudo", "-n", "-u", "alice", "--", "env"])
        self.assertEqual(argv[-5:], ["-m", "chatd", "tls-init", "--data-dir", lab.paths["data"]])
        self.assert_order(lab, "-c import chatd", "tls-init", "write: " + self.PLIST)

    def test_restart_of_an_unloaded_job_loads_it(self) -> None:
        lab = self.lab
        lab.reply("launchctl kickstart", 113, "", "Could not find service")
        code, out = lab.invoke(["restart"])
        self.assertEqual(code, 0, out)
        self.assert_order(
            lab,
            "launchctl kickstart -k system/com.desktalk.server",
            "launchctl enable system/com.desktalk.server",
            "launchctl bootstrap system " + self.PLIST,
        )

    def test_cli_passthrough_uses_sudo(self) -> None:
        lab = self.lab
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(lab.invoke(["cli", "--", "backup"])[0], 0)
        argv = lab.find_run("backup")["argv"]
        self.assertEqual(argv[:6], ["sudo", "-n", "-u", "alice", "--", "env"])
        self.assertEqual(argv[-3:], ["backup", "--data-dir", lab.paths["data"]])


# ---------------------------------------------------------------------------------------------------------------------
# main(): dry-run on every target, usage errors, exit codes, running the file as a script
# ---------------------------------------------------------------------------------------------------------------------


def run_main(argv: List[str], stdin: str = "") -> Tuple[int, str, str]:
    """``inst.main(argv)`` with stdout/stderr captured, nothing executed and no saved state."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(stdin)))
        stack.enter_context(mock.patch.object(inst, "read_state", return_value=None))
        stack.enter_context(
            mock.patch.object(inst.subprocess, "run", side_effect=AssertionError("executed in dry-run"))
        )
        stack.enter_context(
            mock.patch.object(
                inst,
                "choose_interpreter",
                side_effect=lambda ctx, explicit, saved, reg=None: explicit or sys.executable,
            )
        )
        code = inst.main(argv)
    return code, out.getvalue(), err.getvalue()


def blocks(text: str) -> Dict[str, str]:
    """``{path: content}`` of every ``===== BEGIN path =====`` block in ``text``."""
    return {
        m.group(1): m.group(2) for m in re.finditer(r"===== BEGIN (.*?) =====\n(.*?)\n===== END \1 =====", text, re.S)
    }


TARGET_ARGS = {
    "windows": ["--python", "C:\\Python313\\python.exe", "--app-root", "C:\\DeskTalk"],
    "linux": ["--python", "/usr/bin/python3", "--app-root", "/opt/desktalk", "--user", "alice"],
    "macos": ["--python", "/usr/local/bin/python3", "--app-root", "/usr/local/desktalk", "--user", "alice"],
}


class DryRunTests(unittest.TestCase):
    def dry_install(self, target: str, *extra: str, stdin: str = "") -> Tuple[int, str, str]:
        return run_main(["install", "--dry-run", "--target", target, *TARGET_ARGS[target], *extra], stdin)

    def test_every_target_exits_0_and_prints_a_parsable_artefact(self) -> None:
        for target in inst.TARGETS:
            code, out, err = self.dry_install(target, "--data-dir", SPEC_LITERAL, "--name", "Free Chat")
            self.assertEqual((code, err), (0, ""), target)
            self.assertIn("DRY RUN for %s" % target, out)
            found = blocks(out)
            artefact = next(text for path, text in found.items() if re.search(r"task\.xml|\.service|\.plist", path))
            if target == "windows":
                root = ET.fromstring(codecs.BOM_UTF16_LE + artefact.encode("utf-16-le"))
                arguments = root.findtext("%sActions/%sExec/%sArguments" % (NS, NS, NS))
                self.assertIn("--data-dir", arguments)
                self.assertIn("&amp;", artefact)
            elif target == "linux":
                parser = configparser.RawConfigParser(strict=False)
                parser.read_string(artefact)
                self.assertIn("Free Chat 100%", parser.get("Service", "ExecStart"))
                self.assertIn("100%%", parser.get("Service", "ExecStart"))
            else:
                plist = plistlib.loads(artefact.encode("utf-8"))
                self.assertTrue(any("Free Chat 100%" in arg for arg in plist["ProgramArguments"]))
            state = json.loads(next(text for path, text in found.items() if path.endswith("install.json")))
            self.assertEqual(tuple(state), inst.STATE_KEYS)
            self.assertEqual(state["name"], "Free Chat")

    def test_dry_run_prints_the_commands_it_would_run(self) -> None:
        _code, out, _err = self.dry_install("windows")
        for needle in (
            "[dry-run] $ schtasks /Create /TN DeskTalk /XML",
            "[dry-run] $ icacls",
            "[dry-run] $ netsh advfirewall",
        ):
            self.assertIn(needle, out)
        _code, out, _err = self.dry_install("linux")
        for needle in (
            "systemctl enable --now desktalk",
            "install -d -o alice -g alice -m 0700",
            "runuser -u alice --",
        ):
            self.assertIn(needle, out)
        _code, out, _err = self.dry_install("macos")
        for needle in (
            "launchctl bootstrap system /Library/LaunchDaemons/com.desktalk.server.plist",
            "chown root:wheel",
        ):
            self.assertIn(needle, out)

    def test_tls_dry_run_shows_tls_init_and_no_certificate_code(self) -> None:
        for target in inst.TARGETS:
            code, out, _err = self.dry_install(target, "--tls")
            self.assertEqual(code, 0, target)
            self.assertIn("-m chatd tls-init --data-dir", out)
            if target != "windows":  # POSIX passes the installed options as `env K=V` on the command line
                self.assertIn("DESKTALK_TLS=1", out)
            self.assertNotIn("openssl", out.lower())
            _code, plain, _err = self.dry_install(target, "--no-tls")
            self.assertNotIn("tls-init", plain)

    def test_dry_run_never_executes_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            before = os.getcwd()
            os.chdir(folder)
            try:
                for target in inst.TARGETS:
                    code, _out, _err = self.dry_install(target, "--tls", "--admin", "bob", "--password-stdin")
                    self.assertEqual(code, 0, target)
                self.assertEqual(os.listdir(folder), [])
            finally:
                os.chdir(before)

    def test_first_admin_password_is_never_read_or_printed_in_dry_run(self) -> None:
        for target in inst.TARGETS:
            code, out, _err = self.dry_install(target, "--admin", "bob", "--password-stdin", stdin=PASSWORD + "\n")
            self.assertEqual(code, 0)
            self.assertIn("create-admin bob --password-stdin", out)
            self.assertIn("secret withheld", out)
            self.assertNotIn(PASSWORD, out)

    def test_plain_http_warning_unless_tls_decision_given(self) -> None:
        self.assertIn(
            "Plain HTTP: anyone on this network can read passwords and messages", self.dry_install("linux")[1]
        )
        self.assertNotIn("Plain HTTP", self.dry_install("linux", "--tls")[1])
        self.assertNotIn("Plain HTTP", self.dry_install("linux", "--no-tls")[1])

    def test_start_stop_restart_uninstall_dry_run_show_the_windows_sequences(self) -> None:
        base = ["--dry-run", "--target", "windows"]
        code, out, _err = run_main(["stop", *base])
        self.assertEqual(code, 0)
        self.assertLess(out.index("/DISABLE"), out.index("stop.request"))
        self.assertLess(out.index("stop.request"), out.index("/End"))
        code, out, _err = run_main(["start", *base])
        self.assertEqual(code, 0)
        self.assertLess(out.index("[dry-run] delete "), out.index("/ENABLE"))
        self.assertLess(out.index("/ENABLE"), out.index("/Run"))
        code, out, _err = run_main(["uninstall", "--dry-run", "--target", "linux"])
        self.assertEqual(code, 0)
        self.assertIn("rm -rf", out)
        self.assertIn("[dry-run] $ systemctl disable --now desktalk", out)
        self.assertEqual(run_main(["restart", "--dry-run", "--target", "macos"])[0], 0)

    def test_cli_dry_run(self) -> None:
        code, out, _err = run_main(["cli", "--dry-run", "--target", "linux", "--", "create-admin", "bob"])
        self.assertEqual(code, 0)
        self.assertIn("[dry-run] $ runuser -u desktalk -- env ", out)
        self.assertIn("-m chatd create-admin bob --data-dir /var/lib/desktalk", out)

    def test_print_config_matches_the_dry_run_artefact(self) -> None:
        for target in inst.TARGETS:
            code, printed, _err = run_main(["print-config", "--target", target, *TARGET_ARGS[target]])
            self.assertEqual(code, 0)
            _code, dry, _err = self.dry_install(target)
            printed_blocks, dry_blocks = blocks(printed), blocks(dry)
            path, text = next((p, t) for p, t in printed_blocks.items() if re.search(r"task\.xml|\.service|\.plist", p))
            self.assertEqual(dry_blocks[path], text)
            self.assertIn("# command: ", printed)

    def test_quoting_case_in_print_config_everywhere(self) -> None:
        for target in inst.TARGETS:
            code, out, _err = run_main(
                ["print-config", "--target", target, *TARGET_ARGS[target], "--data-dir", SPEC_LITERAL]
            )
            self.assertEqual(code, 0, target)
            self.assertIn("Free Chat 100%", out)
            self.assertIn("A & B", out if target != "windows" else out.replace("&amp;", "&"))


class ExitCodeTests(unittest.TestCase):
    def code(self, *argv: str) -> int:
        return run_main(list(argv))[0]

    def test_usage_errors_are_exit_2(self) -> None:
        cases = [
            [],
            ["bogus"],
            ["install", "--bogus"],
            ["install", "--port", "abc"],
            ["install", "--port", "70000", "--dry-run", "--target", "linux", "--user", "a"],
            ["install", "--target", "linux"],  # --target needs --dry-run
            ["status", "--target", "linux"],
            ["install", "--dry-run", "--target", "windows", "--user", "bob", "--python", "C:\\p.exe"],
            ["install", "--dry-run", "--target", "linux", "--run-as-system", "--user", "a", "--python", "/p"],
            ["install", "--dry-run", "--target", "linux", "--harden", "--user", "a", "--python", "/p"],
            ["install", "--dry-run", "--target", "linux", "--user", "root", "--python", "/p"],
            ["install", "--dry-run", "--target", "windows", "--allow-from", "everybody", "--python", "C:\\p.exe"],
            ["install", "--dry-run", "--target", "windows", "--firewall-profile", "work", "--python", "C:\\p.exe"],
            ["install", "--dry-run", "--target", "linux", "--redirect-port", "8765", "--user", "a", "--python", "/p"],
            ["install", "--dry-run", "--target", "linux", "--name", "", "--user", "a", "--python", "/p"],
            [
                "install",
                "--dry-run",
                "--target",
                "linux",
                "--allowed-host",
                "bad host",
                "--user",
                "a",
                "--python",
                "/p",
            ],
            ["install", "--password-stdin"],
            ["install", "--password-stdin", "--admin", "bob", "--elevate"],
            ["cli"],
            ["cli", "create-admin"],
            ["cli", "--"],
            ["logs", "-n", "many"],
            [
                "install",
                "--elevate",
                "--dry-run",
                "--target",
                "linux",
                "--user",
                "a",
                "--python",
                "/p",
                "--password-stdin",
            ],
        ]
        for argv in cases:
            self.assertEqual(self.code(*argv), 2, argv)

    def test_help_is_exit_0(self) -> None:
        self.assertEqual(self.code("--help"), 0)
        self.assertEqual(self.code("install", "--help"), 0)

    def test_help_text_names_every_flag_of_the_spec(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            inst.parse_args(["install", "--help"])
        text = out.getvalue()
        spec_flags = "--port --host --data-dir --python --name --user --tls --no-tls --redirect-port --allowed-host"
        spec_flags += " --allow-sleep --no-firewall --allow-from --firewall-profile --run-as-system --harden --admin"
        spec_flags += " --password-stdin --force --dry-run --target --elevate"
        for flag in spec_flags.split():
            self.assertIn(flag, text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            inst.parse_args(["uninstall", "--help"])
        self.assertIn("--keep-firewall", out.getvalue())

    def test_privileged_commands_without_privileges_are_exit_2(self) -> None:
        with mock.patch.object(inst.Context, "_detect_admin", return_value=False):
            for argv in (["install"], ["uninstall"], ["start"], ["stop"], ["restart"]):
                code, out, _err = run_main(argv)
                self.assertEqual(code, 2, argv)
                self.assertTrue("Run as administrator" in out or "sudo" in out, out)

    def test_elevate_flag_only_exists_on_windows(self) -> None:
        if inst.detect_host() == "windows":
            self.skipTest("on Windows --elevate is real")
        with mock.patch.object(inst.Context, "_detect_admin", return_value=False):
            code, _out, err = run_main(["stop", "--elevate"])
        self.assertEqual(code, 2)
        self.assertIn("--elevate exists on Windows only", err)

    def test_installer_errors_print_to_stderr_with_the_code(self) -> None:
        code, _out, err = run_main(["install", "--dry-run", "--target", "linux", "--user", "root", "--python", "/p"])
        self.assertEqual(code, 2)
        self.assertIn("ERROR: the service must not run as root", err)

    def test_status_uses_exit_3_for_unhealthy_and_0_for_healthy(self) -> None:
        with mock.patch.object(inst, "health_probe", return_value=None), mock.patch.object(
            inst.Context, "run", return_value=inst.Result(1, "", "")
        ):
            self.assertEqual(self.code("status"), 3)
        with mock.patch.object(inst, "health_probe", return_value=dict(INFO)), mock.patch.object(
            inst.Context, "run", return_value=inst.Result(1, "", "")
        ):
            self.assertEqual(self.code("status"), 0)


class ScriptTests(unittest.TestCase):
    """The file must run as ``python service/install_service.py`` from any directory."""

    def run_script(self, *argv: str) -> "subprocess.CompletedProcess[str]":
        with tempfile.TemporaryDirectory() as folder:
            return subprocess.run(
                [sys.executable, SCRIPT, *argv], cwd=folder, capture_output=True, text=True, timeout=60, check=False
            )

    def test_print_config_from_an_unrelated_directory(self) -> None:
        result = self.run_script(
            "print-config", "--target", "linux", "--python", "/usr/bin/python3", "--app-root", "/opt/desktalk",
            "--data-dir", "/srv/A & B/Free Chat 100%/", "--user", "bob",
        )  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ReadWritePaths="/srv/A & B/Free Chat 100%%"', result.stdout)
        self.assertIn("RestartPreventExitStatus=78", result.stdout)

    def test_dry_run_install_for_windows_from_any_os(self) -> None:
        result = self.run_script(
            "install", "--dry-run", "--target", "windows", "--python", "C:\\Python313\\python.exe",
            "--app-root", "C:\\DeskTalk", "--data-dir", "C:\\A & B\\Free Chat 100%\\",
        )  # fmt: skip
        if inst.detect_host() == "windows":
            self.assertIn(result.returncode, (0, 2))  # a real C:\Python313 may or may not exist on this machine
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("&amp;", result.stdout)

    def test_help_and_usage_error(self) -> None:
        self.assertEqual(self.run_script("--help").returncode, 0)
        bad = self.run_script("install", "--target", "linux")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--target is only valid together with --dry-run or print-config", bad.stderr)

    def test_stdout_survives_a_narrow_console_encoding(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="ascii")
        result = subprocess.run(
            [sys.executable, SCRIPT, "print-config", "--target", "linux", "--python", "/p", "--app-root", "/opt/d",
             "--user", "bob", "--name", "Caf\u00e9"],
            capture_output=True, text=True, env=env, timeout=60, check=False,
        )  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_imports_nothing_from_chatd_and_not_sqlite3(self) -> None:
        with open(SCRIPT, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIsNone(re.search(r"^\s*(from|import)\s+chatd\b", source, re.M))
        self.assertIsNone(re.search(r"^\s*(from|import)\s+sqlite3\b", source, re.M))

    def test_contains_no_tls_code_of_its_own(self) -> None:
        """SPEC 6.2 / 10.1: certificates are made by `chatd tls-init`, never by the installer."""
        with open(SCRIPT, encoding="utf-8") as handle:
            source = handle.read().lower()
        for forbidden in ("openssl", "key.pem", "load_cert_chain", "-newkey", "subjectaltname"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
