"""Pure generators of service/install_service.py: Task XML, systemd unit, launchd plist, openssl.cnf, commands.

Nothing here touches the machine: every artefact is generated from fake settings and parsed back the way the
operating system would parse it (SPEC 12 "the installer").
"""

from __future__ import annotations

import codecs
import configparser
import dataclasses
import importlib.util
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from typing import Any, List

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
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

DEFAULTS = {
    "windows": {
        "python": "C:\\Python313\\python.exe",
        "app_root": "C:\\DeskTalk",
        "data_dir": "C:\\ProgramData\\DeskTalk\\data",
        "user": inst.WIN_LOCAL_SERVICE,
    },
    "linux": {
        "python": "/usr/bin/python3",
        "app_root": "/opt/desktalk",
        "data_dir": "/var/lib/desktalk",
        "user": "desktalk",
        "group": "desktalk",
    },
    "macos": {
        "python": "/usr/local/bin/python3",
        "app_root": "/usr/local/desktalk",
        "data_dir": "/Library/Application Support/DeskTalk/data",
        "user": "alice",
        "group": "staff",
    },
}
# SPEC 10.1: a data dir named `A & B\Free Chat 100%\` must survive every target (normalised: no trailing separator).
NASTY = {
    "windows": "C:\\A & B\\Free Chat 100%",
    "linux": "/srv/A & B/Free Chat 100%",
    "macos": "/Users/Shared/A & B/Free Chat 100%",
}
SPEC_LITERAL = "A & B\\Free Chat 100%\\"


def make_settings(target: str, **overrides: Any) -> Any:
    """Settings for ``target`` with sensible fake values."""
    values = dict(DEFAULTS[target])
    values.update(overrides)
    return inst.Settings(target=target, **values)


def split_windows_cmdline(text: str) -> List[str]:
    """Split a command line with the CRT rules (inverse of ``subprocess.list2cmdline``)."""
    args: List[str] = []
    current: List[str] = []
    quoted = False
    started = False
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            j = i
            while j < len(text) and text[j] == "\\":
                j += 1
            count = j - i
            if j < len(text) and text[j] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    i = j + 1
                else:
                    i = j
            else:
                current.append("\\" * count)
                i = j
            started = True
        elif char == '"':
            quoted = not quoted
            started = True
            i += 1
        elif char in " \t" and not quoted:
            if started:
                args.append("".join(current))
                current, started = [], False
            i += 1
        else:
            current.append(char)
            started = True
            i += 1
    if started:
        args.append("".join(current))
    return args


class ServeArgumentsTests(unittest.TestCase):
    def test_order_and_optional_flags(self) -> None:
        s = make_settings(
            "linux",
            tls=True,
            redirect_port=80,
            allowed_hosts=["chat.corp", "10.0.0.5"],
            allow_sleep=True,
            name="Free Chat",
        )
        self.assertEqual(
            inst.serve_arguments(s),
            ["-X", "utf8", "-I", "/opt/desktalk/server.py", "serve", "--host", "0.0.0.0", "--port", "8765",
             "--data-dir", "/var/lib/desktalk", "--name", "Free Chat", "--tls", "--redirect-port", "80",
             "--allowed-host", "chat.corp", "--allowed-host", "10.0.0.5", "--allow-sleep"],
        )  # fmt: skip

    def test_minimal(self) -> None:
        args = inst.serve_arguments(make_settings("linux"))
        self.assertNotIn("--tls", args)
        self.assertNotIn("--allow-sleep", args)
        self.assertEqual(args[:5], ["-X", "utf8", "-I", "/opt/desktalk/server.py", "serve"])

    def test_service_env_carries_installed_options(self) -> None:
        env = inst.service_env(make_settings("linux", tls=True, port=9000, allowed_hosts=["a.corp", "b.corp"]))
        self.assertEqual(env["DESKTALK_PORT"], "9000")
        self.assertEqual(env["DESKTALK_TLS"], "1")
        self.assertEqual(env["DESKTALK_ALLOWED_HOSTS"], "a.corp,b.corp")
        self.assertEqual(env["DESKTALK_DATA_DIR"], "/var/lib/desktalk")


class WindowsTaskXmlTests(unittest.TestCase):
    def test_bytes_are_utf16_with_bom_and_parse(self) -> None:
        data = inst.windows_task_xml(make_settings("windows"))
        self.assertTrue(data.startswith(codecs.BOM_UTF16_LE))
        text = data[2:].decode("utf-16-le")
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>\n'))
        root = ET.fromstring(data)
        self.assertEqual(root.tag, NS + "Task")
        self.assertEqual(root.get("version"), "1.2")

    def test_task_matches_spec_10_2(self) -> None:
        root = ET.fromstring(inst.windows_task_xml(make_settings("windows", name="Free Chat")))
        self.assertEqual(root.findtext(f"{NS}Triggers/{NS}BootTrigger/{NS}Delay"), "PT20S")
        self.assertEqual(root.findtext(f"{NS}Triggers/{NS}BootTrigger/{NS}Enabled"), "true")
        watchdog = f"{NS}Triggers/{NS}TimeTrigger"
        self.assertEqual(root.findtext(f"{watchdog}/{NS}StartBoundary"), "2020-01-01T00:00:00")
        self.assertEqual(root.findtext(f"{watchdog}/{NS}Repetition/{NS}Interval"), "PT5M")
        self.assertEqual(root.findtext(f"{watchdog}/{NS}Repetition/{NS}StopAtDurationEnd"), "false")
        principal = root.find(f"{NS}Principals/{NS}Principal")
        self.assertEqual(principal.get("id"), "Author")
        self.assertEqual(principal.findtext(f"{NS}UserId"), "S-1-5-19")
        self.assertEqual(principal.findtext(f"{NS}RunLevel"), "LeastPrivilege")
        settings = root.find(f"{NS}Settings")
        expected = {
            "MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
            "StopIfGoingOnBatteries": "false", "AllowHardTerminate": "true", "StartWhenAvailable": "true",
            "RunOnlyIfNetworkAvailable": "false", "AllowStartOnDemand": "true", "Enabled": "true",
            "Hidden": "false", "WakeToRun": "false", "ExecutionTimeLimit": "PT0S", "Priority": "4",
        }  # fmt: skip
        for tag, value in expected.items():
            self.assertEqual(settings.findtext(NS + tag), value, tag)
        self.assertEqual(settings.findtext(f"{NS}RestartOnFailure/{NS}Interval"), "PT1M")
        self.assertEqual(settings.findtext(f"{NS}RestartOnFailure/{NS}Count"), "999")
        actions = root.find(f"{NS}Actions")
        self.assertEqual(actions.get("Context"), "Author")
        self.assertEqual(actions.findtext(f"{NS}Exec/{NS}Command"), "C:\\Python313\\python.exe")
        self.assertEqual(actions.findtext(f"{NS}Exec/{NS}WorkingDirectory"), "C:\\DeskTalk")
        arguments = actions.findtext(f"{NS}Exec/{NS}Arguments")
        self.assertTrue(arguments.startswith("-X utf8 -I C:\\DeskTalk\\server.py serve --host 0.0.0.0 --port 8765"))
        self.assertIn('--name "Free Chat"', arguments)

    def test_run_as_system_uses_system_sid_and_highest_available(self) -> None:
        s = make_settings("windows", user=inst.WIN_SYSTEM)
        root = ET.fromstring(inst.windows_task_xml(s))
        principal = root.find(f"{NS}Principals/{NS}Principal")
        self.assertEqual(principal.findtext(f"{NS}UserId"), "S-1-5-18")
        self.assertEqual(principal.findtext(f"{NS}RunLevel"), "HighestAvailable")

    def test_quoting_case_round_trips(self) -> None:
        s = make_settings("windows", data_dir=NASTY["windows"], name="Free & Chat 100%")
        data = inst.windows_task_xml(s)
        raw = data[2:].decode("utf-16-le")
        self.assertIn("&amp;", raw)  # ElementTree escapes; an f-string would have produced invalid XML
        self.assertNotIn("A & B", raw)
        arguments = ET.fromstring(data).findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments")
        self.assertEqual(arguments, subprocess.list2cmdline(inst.serve_arguments(s)))
        self.assertEqual(split_windows_cmdline(arguments), inst.serve_arguments(s))

    def test_trailing_backslash_is_doubled_not_swallowing_next_flag(self) -> None:
        s = make_settings("windows", data_dir="D:\\Free Chat\\")  # un-normalised on purpose
        arguments = ET.fromstring(inst.windows_task_xml(s)).findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments")
        self.assertEqual(split_windows_cmdline(arguments), inst.serve_arguments(s))
        self.assertIn('--data-dir "D:\\Free Chat\\\\" --name', arguments)

    def test_spec_literal_relative_name_is_kept(self) -> None:
        data_dir = inst.norm_path(SPEC_LITERAL, "windows", "linux")
        self.assertEqual(data_dir, "A & B\\Free Chat 100%")
        arguments = ET.fromstring(inst.windows_task_xml(make_settings("windows", data_dir=data_dir))).findtext(
            f"{NS}Actions/{NS}Exec/{NS}Arguments"
        )
        self.assertIn('"A & B\\Free Chat 100%"', arguments)


class SystemdUnitTests(unittest.TestCase):
    def parse(self, text: str) -> configparser.RawConfigParser:
        parser = configparser.RawConfigParser(strict=False)
        parser.optionxform = str  # keep the case of the keys
        parser.read_string(text)
        return parser

    def exec_words(self, parser: configparser.RawConfigParser) -> List[str]:
        return [w.replace("%%", "%").replace("$$", "$") for w in shlex.split(parser.get("Service", "ExecStart"))]

    def test_sections_and_hardening(self) -> None:
        parser = self.parse(inst.systemd_unit(make_settings("linux"), "/usr/bin/systemd-inhibit"))
        self.assertEqual(parser.sections(), ["Unit", "Service", "Install"])
        service = dict(parser.items("Service"))
        expected = {
            "Type": "simple", "User": "desktalk", "Group": "desktalk", "WorkingDirectory": "/opt/desktalk",
            "Restart": "always", "RestartSec": "3", "RestartPreventExitStatus": "78", "TimeoutStopSec": "20",
            "LimitNOFILE": "8192", "UMask": "0077", "Environment": "PYTHONUNBUFFERED=1 PYTHONUTF8=1",
            "NoNewPrivileges": "true", "PrivateTmp": "true", "ProtectSystem": "full", "ProtectHome": "read-only",
            "ProtectKernelTunables": "true", "ProtectKernelModules": "true", "ProtectControlGroups": "true",
            "RestrictAddressFamilies": "AF_INET AF_INET6 AF_UNIX", "RestrictNamespaces": "true",
            "LockPersonality": "true", "CapabilityBoundingSet": "", "ReadWritePaths": '"/var/lib/desktalk"',
        }  # fmt: skip
        for key, value in expected.items():
            self.assertEqual(service.get(key), value, key)
        self.assertNotIn("AmbientCapabilities", service)
        self.assertEqual(parser.get("Unit", "After"), "network-online.target")
        self.assertEqual(parser.get("Unit", "Wants"), "network-online.target")
        self.assertEqual(parser.get("Install", "WantedBy"), "multi-user.target")

    def test_exec_start_is_fully_quoted_and_starts_with_inhibit(self) -> None:
        s = make_settings("linux", name="Free Chat")
        parser = self.parse(inst.systemd_unit(s, "/usr/bin/systemd-inhibit"))
        words = self.exec_words(parser)
        self.assertEqual(
            words[:4], ["/usr/bin/systemd-inhibit", "--what=sleep:idle", "--who=DeskTalk", "--why=chat server"]
        )
        self.assertEqual(words[4:], [s.python, *inst.serve_arguments(s)])
        self.assertTrue(parser.get("Service", "ExecStart").startswith('"/usr/bin/systemd-inhibit" "--what=sleep:idle"'))

    def test_no_inhibit_when_allow_sleep_or_missing(self) -> None:
        for s, inhibit in (
            (make_settings("linux", allow_sleep=True), "/usr/bin/systemd-inhibit"),
            (make_settings("linux"), None),
        ):
            words = self.exec_words(self.parse(inst.systemd_unit(s, inhibit)))
            self.assertEqual(words[0], s.python)

    def test_quoting_case(self) -> None:
        s = make_settings("linux", data_dir=NASTY["linux"])
        parser = self.parse(inst.systemd_unit(s, None))
        self.assertIn("100%%", parser.get("Service", "ExecStart"))
        self.assertEqual(self.exec_words(parser), [s.python, *inst.serve_arguments(s)])
        self.assertEqual(parser.get("Service", "ReadWritePaths"), '"/srv/A & B/Free Chat 100%%"')

    def test_special_characters_are_escaped(self) -> None:
        tricky = '/srv/we"ird $HOME 50% \\end'
        s = make_settings("linux", data_dir=tricky, app_root="/opt/my app%")
        parser = self.parse(inst.systemd_unit(s, None))
        exec_start = parser.get("Service", "ExecStart")
        self.assertIn('\\"', exec_start)
        self.assertIn("$$HOME", exec_start)
        self.assertIn("50%%", exec_start)
        self.assertEqual(self.exec_words(parser), [s.python, *inst.serve_arguments(s)])
        self.assertEqual(parser.get("Service", "WorkingDirectory"), "/opt/my app%%")

    def test_low_ports_get_bind_capability(self) -> None:
        for kwargs in ({"port": 80}, {"port": 8443, "tls": True, "redirect_port": 80}):
            service = dict(self.parse(inst.systemd_unit(make_settings("linux", **kwargs), None)).items("Service"))
            self.assertEqual(service["AmbientCapabilities"], "CAP_NET_BIND_SERVICE")
            self.assertEqual(service["CapabilityBoundingSet"], "CAP_NET_BIND_SERVICE")

    def test_newlines_in_paths_are_rejected(self) -> None:
        s = make_settings("linux", data_dir="/srv/bad\nExecStartPre=/bin/evil")
        with self.assertRaises(inst.InstallerError) as caught:
            inst.validate_settings(s)
        self.assertEqual(caught.exception.code, inst.EXIT_USAGE)


class LaunchdPlistTests(unittest.TestCase):
    def test_plist_matches_spec_10_4(self) -> None:
        s = make_settings("macos", name="Free Chat", tls=True)
        plist = plistlib.loads(inst.launchd_plist(s))
        self.assertEqual(plist["Label"], "com.desktalk.server")
        self.assertIs(plist["RunAtLoad"], True)
        self.assertIs(plist["KeepAlive"], True)
        self.assertGreaterEqual(plist["ThrottleInterval"], 10)
        self.assertEqual(plist["WorkingDirectory"], "/usr/local/desktalk")
        self.assertEqual((plist["UserName"], plist["GroupName"]), ("alice", "staff"))
        log = "/Library/Application Support/DeskTalk/data/logs/launchd.log"
        self.assertEqual((plist["StandardOutPath"], plist["StandardErrorPath"]), (log, log))
        self.assertEqual(plist["EnvironmentVariables"]["PYTHONUTF8"], "1")
        self.assertEqual(plist["EnvironmentVariables"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(
            plist["EnvironmentVariables"]["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        )
        self.assertEqual(plist["SoftResourceLimits"], {"NumberOfFiles": 8192})
        self.assertEqual(plist["HardResourceLimits"], {"NumberOfFiles": 8192})
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/caffeinate", "-i", s.python, *inst.serve_arguments(s)])

    def test_allow_sleep_drops_caffeinate(self) -> None:
        plist = plistlib.loads(inst.launchd_plist(make_settings("macos", allow_sleep=True)))
        self.assertEqual(plist["ProgramArguments"][0], "/usr/local/bin/python3")
        self.assertIn("--allow-sleep", plist["ProgramArguments"])

    def test_quoting_case_needs_no_escaping(self) -> None:
        s = make_settings("macos", data_dir=NASTY["macos"])
        plist = plistlib.loads(inst.launchd_plist(s))
        self.assertIn(NASTY["macos"], plist["ProgramArguments"])
        self.assertEqual(plist["StandardOutPath"], NASTY["macos"] + "/logs/launchd.log")


class QuotingCaseOnAllTargets(unittest.TestCase):
    """The SPEC literal `A & B\\Free Chat 100%\\` must produce a valid artefact on every target."""

    def test_literal_name_everywhere(self) -> None:
        for target in inst.TARGETS:
            data_dir = inst.norm_path(SPEC_LITERAL, target, "other")
            s = make_settings(target, data_dir=data_dir)
            inst.validate_settings(s)
            if target == "windows":
                ET.fromstring(inst.windows_task_xml(s))
            elif target == "linux":
                parser = configparser.RawConfigParser(strict=False)
                parser.read_string(inst.systemd_unit(s, None))
                self.assertIn("100%", parser.get("Service", "ExecStart"))
            else:
                plist = plistlib.loads(inst.launchd_plist(s))
                self.assertIn(data_dir, plist["ProgramArguments"])


class OpensslAndCommandTests(unittest.TestCase):
    def test_openssl_config(self) -> None:
        text = inst.openssl_config("PC-01", ["192.168.1.5", "10.0.0.7"])
        self.assertIn("basicConstraints=critical,CA:FALSE", text)
        self.assertIn("extendedKeyUsage=serverAuth", text)
        self.assertIn("keyUsage=critical,digitalSignature,keyEncipherment", text)
        for line in (
            "CN=PC-01",
            "DNS.1=PC-01",
            "DNS.2=localhost",
            "IP.1=127.0.0.1",
            "IP.2=192.168.1.5",
            "IP.3=10.0.0.7",
        ):
            self.assertIn(line + "\n", text)

    def test_netsh_rule(self) -> None:
        s = make_settings(
            "windows", redirect_port=80, firewall={"port": 8765, "scope": "10.0.0.0/24", "profile": "private,domain"}
        )
        command = inst.netsh_add_rule(s)
        self.assertEqual(command[:6], ["netsh", "advfirewall", "firewall", "add", "rule", "name=DeskTalk"])
        for part in (
            "dir=in",
            "action=allow",
            "protocol=TCP",
            "localport=8765,80",
            "profile=private,domain",
            "remoteip=10.0.0.0/24",
        ):
            self.assertIn(part, command)
        self.assertEqual(inst.NETSH_DELETE_RULE[-1], "name=DeskTalk")

    def test_icacls_commands(self) -> None:
        self.assertEqual(
            inst.icacls_harden_command("C:\\app"),
            ["icacls", "C:\\app", "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
             "*S-1-5-19:(OI)(CI)RX", "*S-1-5-32-545:(OI)(CI)RX"],
        )  # fmt: skip
        self.assertEqual(
            inst.icacls_data_command("C:\\d", False),
            ["icacls", "C:\\d", "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
             "*S-1-5-19:(OI)(CI)M"],
        )  # fmt: skip
        self.assertNotIn("*S-1-5-19:(OI)(CI)M", inst.icacls_data_command("C:\\d", True))


class SettingsAndStateTests(unittest.TestCase):
    def test_to_state_has_exactly_the_spec_keys(self) -> None:
        state = make_settings("linux", firewall={"port": 8765, "scope": "any", "profile": "any"}).to_state()
        self.assertEqual(tuple(state), inst.STATE_KEYS)
        self.assertEqual(
            set(state),
            {"python", "app_root", "data_dir", "host", "port", "tls", "redirect_port", "user", "name", "allowed_hosts",
             "allow_sleep", "firewall"},
        )  # fmt: skip

    def test_diff_state(self) -> None:
        old = make_settings("linux").to_state()
        self.assertEqual(inst.diff_state(old, old), [])
        self.assertEqual(inst.diff_state(None, old), [])
        new = dataclasses.replace(make_settings("linux"), port=9000, tls=True, allowed_hosts=["a.corp"]).to_state()
        lines = inst.diff_state(old, new)
        self.assertEqual(lines, ["  port: 8765 -> 9000", "  tls: false -> true", '  allowed_hosts: [] -> ["a.corp"]'])

    def test_read_state_tolerates_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "install.json")
            self.assertIsNone(inst.read_state(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertIsNone(inst.read_state(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("[1, 2]")
            self.assertIsNone(inst.read_state(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"port": 9000}')
            self.assertEqual(inst.read_state(path), {"port": 9000})

    def test_state_file_locations(self) -> None:
        env = {"ProgramData": "D:\\PD"}
        self.assertEqual(inst.state_file_path("windows", env), "D:\\PD\\DeskTalk\\install.json")
        self.assertEqual(inst.state_file_path("linux", env), "/etc/desktalk/install.json")
        self.assertEqual(inst.state_file_path("macos", env), "/Library/Application Support/DeskTalk/install.json")
        self.assertEqual(inst.default_data_dir("windows", {}), "C:\\ProgramData\\DeskTalk\\data")
        self.assertEqual(inst.default_data_dir("linux", {}), "/var/lib/desktalk")
        self.assertEqual(inst.default_data_dir("macos", {}), "/Library/Application Support/DeskTalk/data")

    def test_validate_settings_refusals(self) -> None:
        bad = [
            {"port": 0},
            {"port": 70000},
            {"redirect_port": 8765},
            {"redirect_port": -1},
            {"name": ""},
            {"name": "x" * 41},
            {"host": "a b"},
            {"allowed_hosts": ["bad host!"]},
            {"name": "a\tb"},
        ]
        for override in bad:
            with self.assertRaises(inst.InstallerError, msg=str(override)) as caught:
                inst.validate_settings(make_settings("linux", **override))
            self.assertEqual(caught.exception.code, inst.EXIT_USAGE)
        inst.validate_settings(make_settings("linux", allowed_hosts=["Chat.Corp", "10.0.0.5:8765", "[::1]"]))

    def test_scope_and_profile_normalisation(self) -> None:
        self.assertEqual(inst.normalise_scope("LocalSubnet"), "localsubnet")
        self.assertEqual(inst.normalise_scope("any"), "any")
        self.assertEqual(inst.normalise_scope("10.0.0.0/24, 192.168.0.0/16"), "10.0.0.0/24,192.168.0.0/16")
        self.assertEqual(inst.normalise_profile("Private, Domain"), "private,domain")
        self.assertEqual(inst.normalise_profile("any"), "any")
        for call, value in ((inst.normalise_scope, "everyone"), (inst.normalise_scope, "10.0.0.0/99"),
                            (inst.normalise_profile, "work"), (inst.normalise_profile, "")):  # fmt: skip
            with self.assertRaises(inst.InstallerError):
                call(value)


class PathAndParsingHelperTests(unittest.TestCase):
    def test_norm_path_per_target(self) -> None:
        self.assertEqual(inst.norm_path("C:/x/y/", "windows", "linux"), "C:\\x\\y")
        self.assertEqual(inst.norm_path("C:\\", "windows", "linux"), "C:\\")  # drive roots keep their separator
        self.assertEqual(inst.norm_path("/srv/a/../b//", "linux", "windows"), "/srv/b")
        self.assertEqual(inst.norm_path("/", "macos", "windows"), "/")

    def test_norm_path_uses_abspath_only_for_the_host_os(self) -> None:
        host = inst.detect_host()
        if host == "other":
            self.skipTest("unsupported host")
        result = inst.norm_path("relative dir", host, host)
        self.assertTrue(os.path.isabs(result))

    def test_is_within(self) -> None:
        self.assertTrue(inst.is_within("C:\\App\\data", "c:\\app", "windows"))
        self.assertFalse(inst.is_within("C:\\Apple", "C:\\App", "windows"))
        self.assertFalse(inst.is_within("D:\\x", "C:\\x", "windows"))
        self.assertTrue(inst.is_within("/opt/a/data", "/opt/a", "linux"))
        self.assertFalse(inst.is_within("/opt/ab", "/opt/a", "linux"))

    def test_tcc_problem(self) -> None:
        for path in ("/Users/bob/Desktop/x", "/Users/bob/Documents", "/Users/bob/Downloads/a/b",
                     "/Users/bob/Library/Mobile Documents/c", "/Volumes/Backup/desktalk"):  # fmt: skip
            self.assertIsNotNone(inst.tcc_problem(path), path)
        for path in (
            "/usr/local/desktalk",
            "/Users/Shared/DeskTalk",
            "/Users/bob/code/desktalk",
            "/Library/Application Support/x",
        ):
            self.assertIsNone(inst.tcc_problem(path), path)

    def test_parse_schtasks_csv_by_column_index(self) -> None:
        row = ['"PC"', '"\\DeskTalk"', '"N/A"', '"Running"', '"Interactive/Background"', '"1/1/2026 9:00:00 AM"',
               '"267009"', '"SYSTEM"', '"python.exe"', '"C:\\x"', '"N/A"', '"Enabled"', '"Disabled"']  # fmt: skip
        parsed = inst.parse_schtasks_csv(",".join(row) + "\r\n")
        self.assertEqual(parsed["status"], "Running")
        self.assertEqual(parsed["state"], "Enabled")
        self.assertEqual(parsed["last_result"], "267009")
        self.assertIsNone(inst.parse_schtasks_csv("ERROR: nope\r\n"))

    def test_tail_lines(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "log.txt")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("".join("line %d\n" % i for i in range(1, 5001)))
            self.assertEqual(inst.tail_lines(path, 3), ["line 4998", "line 4999", "line 5000"])
            self.assertEqual(len(inst.tail_lines(path, 10000)), 5000)

    def test_reject_control_chars(self) -> None:
        inst.reject_control_chars("x", "plain text with spaces")
        for value in ("a\nb", "a\rb", "a\x00b", "tab\there"):
            with self.assertRaises(inst.InstallerError):
                inst.reject_control_chars("x", value)


if __name__ == "__main__":
    unittest.main()
