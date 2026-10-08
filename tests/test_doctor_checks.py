"""``chatd/doctor.py``: parsers, every check and the report (SPEC 2.2, 10.1, 10.2).

The checks are driven with fake configurations, canned tool output and temp dirs; nothing here changes the machine
(the one short-lived socket is a listener bound to a free loopback port of the test itself).
"""

from __future__ import annotations

import collections
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest import mock

from chatd import doctor, util

CONFIG_DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 0,
    "workspace_name": "DeskTalk",
    "registration_open": False,
    "max_users": 2000,
    "max_upload_mb": 100,
    "blocked_extensions": ["exe", "bat"],
    "tls": False,
    "redirect_port": 0,
    "allowed_hosts": [],
    "allow_sleep": False,
    "edit_window_s": 900,
    "delete_window_s": 172800,
    "max_body_chars": 8000,
    "session_days": 30,
    "min_password_len": 8,
    "scrypt_n": 1024,
    "log_level": "INFO",
    "test_scale": 1.0,
    "test_limits": {},
}


def make_cfg(data_dir: str, **overrides: Any) -> Any:
    """A stand-in for ``config.Config`` carrying exactly the attributes the doctor reads."""
    values = dict(CONFIG_DEFAULTS)
    values.update(overrides)
    values.setdefault("sources", {})
    values.setdefault("warnings", [])
    values["data_dir"] = Path(data_dir)
    values["backup_dir"] = values.get("backup_dir") or Path(data_dir) / "backups"
    return types.SimpleNamespace(**values)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def levels(rows: List[doctor.Row]) -> List[str]:
    return [row.level for row in rows]


def find(rows: List[doctor.Row], name: str) -> doctor.Row:
    for row in rows:
        if row.name == name:
            return row
    raise AssertionError("no row %r in %s" % (name, [r.name for r in rows]))


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dt-doctor-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)


# ---------------------------------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------------------------------


class ParserTests(unittest.TestCase):
    def test_netstat_columns_not_words(self) -> None:
        text = (
            "Active Connections\r\n\r\n  Proto  Local Address          Foreign Address        State           PID\r\n"
            "  TCP    0.0.0.0:22             0.0.0.0:0              LISTENING       4916\r\n"
            "  TCP    0.0.0.0:8765           0.0.0.0:0              ABHOEREN        777\r\n"
            "  TCP    [::]:8765              [::]:0                 LISTENING       777\r\n"
            "  TCP    127.0.0.1:8765         127.0.0.1:50500        ESTABLISHED     888\r\n"
            "  TCP    0.0.0.0:18765          0.0.0.0:0              LISTENING       999\r\n"
        )
        self.assertEqual(doctor.parse_netstat_listeners(text, 8765), [777])
        self.assertEqual(doctor.parse_netstat_listeners(text, 22), [4916])
        self.assertEqual(doctor.parse_netstat_listeners(text, 9999), [])
        self.assertEqual(doctor.parse_netstat_listeners("", 22), [])

    def test_ss_output(self) -> None:
        text = (
            "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            'LISTEN 0      4096         0.0.0.0:8765      0.0.0.0:*     users:(("python3",pid=1234,fd=6))\n'
            "LISTEN 0      128             [::]:22           [::]:*\n"
            "LISTEN 0      128          0.0.0.0:87650     0.0.0.0:*\n"
        )
        self.assertEqual(doctor.parse_ss_listeners(text, 8765), [("python3", 1234)])
        self.assertEqual(doctor.parse_ss_listeners(text, 22), [("a hidden process", 0)])

    def test_lsof_output(self) -> None:
        text = (
            "COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
            "Python  41234 alice    6u  IPv4 0x1234      0t0  TCP *:8765 (LISTEN)\n"
        )
        self.assertEqual(doctor.parse_lsof_listeners(text), [("Python", 41234, "alice")])
        self.assertEqual(doctor.parse_lsof_listeners(""), [])

    def test_powercfg_index_english_and_localised(self) -> None:
        english = (
            "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)\n"
            "  Subgroup GUID: 238c9fa8-0aad-41ed-83f4-97be242c8f20  (Sleep)\n"
            "    Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)\n"
            "      Minimum Possible Setting: 0x00000000\n      Maximum Possible Setting: 0xffffffff\n"
            "      Possible Settings increment: 0x00000001\n      Possible Settings units: Seconds\n"
            "    Current AC Power Setting Index: 0x00000e10\n    Current DC Power Setting Index: 0x00000384\n"
        )
        german = english.replace("Current AC Power Setting Index", "Aktueller Netzbetriebs-Einstellungsindex")
        self.assertEqual(doctor.parse_powercfg_index(english), 0xE10)
        self.assertEqual(doctor.parse_powercfg_index(german), 0xE10)
        self.assertEqual(doctor.parse_powercfg_index(english.replace("0x00000e10", "0x00000000")), 0)
        self.assertIsNone(doctor.parse_powercfg_index("Invalid Parameters -- try /?"))

    def test_probe_host_and_age(self) -> None:
        for host in ("0.0.0.0", "::", "localhost", "127.0.0.1", "127.9.9.9", ""):
            self.assertEqual(doctor.probe_host(host), "127.0.0.1", host)
        self.assertEqual(doctor.probe_host("192.168.1.9"), "192.168.1.9")
        self.assertEqual(doctor.format_age(45), "45 s")
        self.assertEqual(doctor.format_age(600), "10 min")
        self.assertEqual(doctor.format_age(7200), "2 h")
        self.assertEqual(doctor.format_age(3 * 86400), "3 days")

    def test_san_missing(self) -> None:
        meta = {"san": ["PC-01", "localhost", "DNS:other", "127.0.0.1", "IP:192.168.1.5"]}
        self.assertEqual(doctor.san_missing(meta, ["pc-01", "192.168.1.5", "10.0.0.2"]), ["10.0.0.2"])
        self.assertEqual(doctor.san_missing(None, ["a"]), ["a"])
        self.assertEqual(doctor.san_missing({"san": "x"}, ["a"]), ["a"])

    def test_exit_code(self) -> None:
        ok, warn = doctor.Row("PASS", "a", ""), doctor.Row("WARN", "b", "")
        stranger = doctor.Row("FAIL", "port", "")
        environment = doctor.Row("FAIL", "sqlite3", "", True)
        self.assertEqual(doctor.exit_code([ok, warn]), 0)
        self.assertEqual(doctor.exit_code([ok, stranger]), 1)
        self.assertEqual(doctor.exit_code([stranger, environment]), 78)

    def test_config_rows_show_db_as_source(self) -> None:
        cfg = make_cfg(self.id(), workspace_name="Office", registration_open=False)
        cfg.sources = {"workspace_name": "flag", "port": "env"}
        rows = {key: (value, source) for key, value, source in doctor.config_rows(cfg, {})}
        self.assertEqual(rows["workspace_name"], ("Office", "flag"))
        self.assertEqual(rows["port"], ("0", "env"))
        self.assertEqual(rows["host"], ("127.0.0.1", "default"))
        self.assertEqual(rows["blocked_extensions"][0], "exe,bat")
        self.assertEqual(rows["test_limits"][0], "{}")
        overlay = {key: (value, source) for key, value, source in doctor.config_rows(
            cfg, {"workspace_name": "Team Chat", "registration_open": "1"})}  # fmt: skip
        self.assertEqual(overlay["workspace_name"], ("Team Chat", "db"))
        self.assertEqual(overlay["registration_open"], ("True", "db"))
        same = {
            key: source
            for key, _value, source in doctor.config_rows(cfg, {"workspace_name": "Office", "registration_open": "0"})
        }
        self.assertEqual((same["workspace_name"], same["registration_open"]), ("flag", "default"))
        self.assertEqual([k for k, _v, _s in doctor.config_rows(cfg, {})], list(doctor.CONFIG_KEYS))


class WalLocationTests(TempDirCase):
    def test_unc_and_cloud_folders(self) -> None:
        if os.name == "nt":
            self.assertIn("UNC", doctor.wal_hazard("\\\\server\\share\\desktalk", {}) or "")
            self.assertIn("UNC", doctor.wal_hazard("//server/share/desktalk", {}) or "")
            self.assertIsNone(doctor.wal_hazard("\\\\?\\C:\\desktalk", {}))
        for path in (
            "C:\\Users\\bob\\OneDrive\\DeskTalk\\data",
            "C:\\Users\\bob\\OneDrive - Contoso\\data",
            "/home/bob/Dropbox/chat",
            "/Users/bob/Library/Mobile Documents/com~apple~CloudDocs/chat",
            "D:\\Google Drive\\chat",
        ):
            self.assertIn("cloud-sync", doctor.wal_hazard(path, {}) or "", path)
        for path in (
            "C:\\ProgramData\\DeskTalk\\data",
            "/var/lib/desktalk",
            "/home/bob/onedrivers/x",
            "D:\\boxes\\data",
        ):
            self.assertIsNone(doctor.wal_hazard(path, {}), path)

    def test_onedrive_environment_root(self) -> None:
        env = {"OneDrive": os.path.join(self.tmp, "Cloud")}
        inside = os.path.join(self.tmp, "Cloud", "Documents", "chat")
        self.assertIn("OneDrive", doctor.wal_hazard(inside, env) or "")
        self.assertIsNone(doctor.wal_hazard(os.path.join(self.tmp, "CloudNine", "chat"), env))

    def test_network_file_system_from_proc_mounts(self) -> None:
        mounts = "/dev/sda1 / ext4 rw 0 0\nserver:/export /srv/share nfs4 rw 0 0\n//nas/x /mnt/nas cifs rw 0 0\n"
        self.assertIn("nfs4", doctor.wal_hazard("/srv/share/desktalk", {}, mounts) or "")
        self.assertIn("cifs", doctor.wal_hazard("/mnt/nas/data", {}, mounts) or "")
        self.assertIsNone(doctor.wal_hazard("/srv/other", {}, mounts))
        self.assertIsNone(doctor.wal_hazard("/srv/sharedir", {}, mounts))  # prefix of a name is not a mount point


# ---------------------------------------------------------------------------------------------------------------------
# Windows ACL reading (real icacls dumps, UTF-16 LE without BOM)
# ---------------------------------------------------------------------------------------------------------------------

REAL = {
    "python313": "Python313\r\nD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)S:PAINO_ACCESS_CONTROL\r\n",
    "e_root": (
        "\r\nD:(A;;FA;;;BA)(A;OICIIO;GA;;;BA)(A;;FA;;;SY)(A;OICIIO;GA;;;SY)(A;;0x1301bf;;;AU)"
        "(A;OICIIO;SDGXGWGR;;;AU)(A;;0x1200a9;;;BU)(A;OICIIO;GXGR;;;BU)\r\n"
    ),
    "program_data": (
        "ProgramData\r\nD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIIO;GA;;;CO)(A;OICI;0x1200a9;;;BU)"
        "(A;CI;DCLCRPCR;;;BU)\r\n"
    ),
    "private": "data\r\nD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)\r\n",
}


class SddlTests(unittest.TestCase):
    def sddl(self, key: str) -> str:
        found = doctor.extract_sddl(REAL[key].encode("utf-16-le"))
        self.assertIsNotNone(found)
        return str(found)

    def test_real_dumps_without_bom(self) -> None:
        self.assertEqual(doctor.broad_access(self.sddl("python313"), doctor.WRITE_MASK), [])
        names = [name for name, _rights in doctor.broad_access(self.sddl("e_root"), doctor.WRITE_MASK)]
        self.assertEqual(names, ["Authenticated Users", "Authenticated Users"])
        self.assertEqual([n for n, _r in doctor.broad_access(self.sddl("program_data"), doctor.WRITE_MASK)], ["Users"])
        self.assertEqual(doctor.broad_access(self.sddl("private"), doctor.READ_MASK | doctor.WRITE_MASK), [])

    def test_read_access_of_broad_groups_is_found(self) -> None:
        sddl = self.sddl("python313")  # Users have read+execute: the data dir must not look like this
        self.assertEqual([n for n, _r in doctor.broad_access(sddl, doctor.READ_MASK)], ["Users"])
        self.assertEqual(doctor.broad_access(sddl, doctor.WRITE_MASK), [])

    def test_decoder_variants_and_noise(self) -> None:
        text = "name\r\nD:PAI(A;;FA;;;SY)\r\n"
        for raw in (text.encode("utf-16-le"), text.encode("utf-16"), text.encode("utf-8"), text.encode("utf-8-sig")):
            self.assertEqual(doctor.extract_sddl(raw), "D:PAI(A;;FA;;;SY)")
        self.assertIsNone(doctor.extract_sddl(b"nothing here"))
        self.assertEqual(doctor.extract_sddl(("D:\\\r\nD:(A;;FA;;;SY)\r\n").encode("utf-16-le")), "D:(A;;FA;;;SY)")

    def test_trustees_ace_types_and_malformed_input(self) -> None:
        for trustee, name in (
            ("WD", "Everyone"),
            ("S-1-1-0", "Everyone"),
            ("IU", "Interactive"),
            ("S-1-5-4", "Interactive"),
        ):
            self.assertEqual(
                [n for n, _r in doctor.broad_access("D:(A;;0x1;;;%s)" % trustee, doctor.READ_MASK)], [name]
            )
        self.assertEqual(doctor.broad_access("D:(D;;FA;;;WD)(AU;SA;FA;;;WD)(A;;FA;;;BA)", doctor.READ_MASK), [])
        self.assertEqual(doctor.broad_access('D:(XA;;FR;;;WD;(@User.Title=="PM"))', doctor.READ_MASK)[0][0], "Everyone")
        for junk in ("", "garbage", "D:", "D:((((", "D:)A;;FA;;;WD("):
            self.assertEqual(doctor.broad_access(junk, doctor.READ_MASK), [], junk)


# ---------------------------------------------------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------------------------------------------------


class EnvironmentCheckTests(TempDirCase):
    def test_python(self) -> None:
        rows = doctor.check_python()
        self.assertEqual(rows[0].level, doctor.PASS)
        self.assertIn(sys.executable, rows[0].detail)
        with mock.patch.object(sys, "version_info", (3, 7, 9, "final", 0)):
            old = doctor.check_python()
        self.assertEqual((old[0].level, old[0].environment), (doctor.FAIL, True))

    def test_sqlite_ok_missing_and_too_old(self) -> None:
        facts: Dict[str, Any] = {}
        rows = doctor.check_sqlite(facts)
        self.assertEqual((rows[0].level, facts["sqlite_ok"]), (doctor.PASS, True))
        self.assertIn("SQLite", rows[0].detail)

        facts = {}
        with mock.patch.dict(sys.modules, {"sqlite3": None}):  # `import sqlite3` raises ImportError
            rows = doctor.check_sqlite(facts)
        self.assertEqual((rows[0].level, rows[0].environment, facts["sqlite_ok"]), (doctor.FAIL, True, False))
        self.assertIn("cannot be imported", rows[0].detail)

        old = types.SimpleNamespace(sqlite_version_info=(3, 22, 0), sqlite_version="3.22.0")
        facts = {}
        with mock.patch.dict(sys.modules, {"sqlite3": old}):
            rows = doctor.check_sqlite(facts)
        self.assertEqual((rows[0].level, rows[0].environment, facts["sqlite_ok"]), (doctor.FAIL, True, False))
        self.assertIn("3.22.0 is too old", rows[0].detail)
        edge = types.SimpleNamespace(sqlite_version_info=(3, 24, 0), sqlite_version="3.24.0")
        with mock.patch.dict(sys.modules, {"sqlite3": edge}):
            self.assertEqual(doctor.check_sqlite({})[0].level, doctor.PASS)

    def test_scrypt(self) -> None:
        cfg = make_cfg(self.data, scrypt_n=1024)
        row = doctor.check_scrypt(cfg)[0]
        self.assertEqual(row.level, doctor.PASS)
        self.assertIn("n=1024", row.detail)
        no_scrypt = types.SimpleNamespace()
        with mock.patch.object(doctor, "hashlib", no_scrypt):
            row = doctor.check_scrypt(cfg)[0]
        self.assertEqual(row.level, doctor.WARN)
        self.assertIn("PBKDF2", row.detail)
        bad = make_cfg(self.data, scrypt_n=3)  # not a power of two: OpenSSL refuses
        row = doctor.check_scrypt(bad)[0]
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))
        with mock.patch.object(doctor.time, "perf_counter", side_effect=[0.0, 2.0]):
            self.assertEqual(doctor.check_scrypt(cfg)[0].level, doctor.WARN)

    def test_ssl(self) -> None:
        self.assertEqual(doctor.check_ssl()[0].level, doctor.PASS)
        with mock.patch.dict(sys.modules, {"ssl": None}):
            row = doctor.check_ssl()[0]
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))

    def test_openssl(self) -> None:
        from chatd import tlsutil

        plain, tls = make_cfg(self.data), make_cfg(self.data, tls=True)
        fake_version = doctor.CommandResult(0, "OpenSSL 3.0.13 30 Jan 2024\n", "")
        with mock.patch.object(tlsutil, "find_openssl", return_value="/usr/bin/openssl"), mock.patch.object(
            doctor, "run_command", return_value=fake_version
        ) as run:
            row = doctor.check_openssl(plain)[0]
        self.assertEqual(row.level, doctor.PASS)
        self.assertIn("OpenSSL 3.0.13", row.detail)
        self.assertEqual(run.call_args[0][0], ["/usr/bin/openssl", "version"])
        with mock.patch.object(tlsutil, "find_openssl", return_value=None):
            self.assertEqual(doctor.check_openssl(plain)[0].level, doctor.INFO)
            self.assertEqual(doctor.check_openssl(tls)[0].level, doctor.FAIL)  # --tls, no certificate, no openssl
            cert_dir = Path(self.data) / "tls"
            cert_dir.mkdir()
            (cert_dir / "cert.pem").write_text("x", encoding="ascii")
            (cert_dir / "key.pem").write_text("x", encoding="ascii")
            self.assertEqual(doctor.check_openssl(tls)[0].level, doctor.WARN)  # an old certificate keeps working


# ---------------------------------------------------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------------------------------------------------


class DataDirTests(TempDirCase):
    def test_writable_existing_dir(self) -> None:
        rows = doctor.check_data_dir(make_cfg(self.data))
        self.assertEqual(find(rows, "WAL location").level, doctor.PASS)
        self.assertEqual(find(rows, "Data dir").level, doctor.PASS)
        self.assertEqual(find(rows, "Disk space").level, doctor.PASS)
        self.assertEqual(os.listdir(self.data), [])  # the probe file is gone

    def test_missing_dir_is_fine_when_its_parent_is_writable(self) -> None:
        target = os.path.join(self.tmp, "not", "yet", "there")
        rows = doctor.check_data_dir(make_cfg(target))
        row = find(rows, "Data dir")
        self.assertEqual(row.level, doctor.PASS)
        self.assertIn("created at the first start", row.detail)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "not")))  # doctor creates nothing

    def test_unwritable_dir_names_the_owner(self) -> None:
        denied = mock.patch.object(doctor.tempfile, "mkstemp", side_effect=PermissionError(13, "denied"))
        owner = mock.patch.object(doctor, "file_owner", return_value="NT SERVICE\\desktalk")
        with denied, owner:
            rows = doctor.check_data_dir(make_cfg(self.data))
        row = find(rows, "Data dir")
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))
        self.assertIn("NT SERVICE\\desktalk", row.detail)
        self.assertIn("elevated shell", row.detail)
        self.assertIn("cli -- doctor", row.detail)

    def test_database_files_are_opened_for_writing(self) -> None:
        for name in ("chat.db", "chat.db-wal", "chat.db-shm"):
            with open(os.path.join(self.data, name), "wb") as handle:
                handle.write(b"x" * 2048)
        rows = [r for r in doctor.check_data_dir(make_cfg(self.data)) if r.name == "Database file"]
        self.assertEqual([r.level for r in rows], [doctor.PASS] * 3)
        self.assertIn("chat.db-wal (2 KiB)", rows[1].detail)

    def test_locked_or_foreign_database_file_is_an_environment_failure(self) -> None:
        os.makedirs(os.path.join(self.data, "chat.db"))  # opening a directory for writing fails on every OS
        with mock.patch.object(doctor, "file_owner", return_value="postgres"):
            rows = doctor.check_data_dir(make_cfg(self.data))
        row = find(rows, "Database file")
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))
        self.assertIn("postgres", row.detail)
        self.assertIn("stop the service", row.detail)

    def test_wal_hostile_location_fails(self) -> None:
        cloud = os.path.join(self.tmp, "OneDrive - Contoso", "chat")
        os.makedirs(cloud)
        row = find(doctor.check_data_dir(make_cfg(cloud)), "WAL location")
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))
        self.assertIn("cloud-sync", row.detail)
        with mock.patch.object(doctor, "drive_is_remote", return_value=True):
            row = find(doctor.check_data_dir(make_cfg(self.data)), "WAL location")
        self.assertIn("mapped network drive", row.detail)

    def test_low_disk_space_warns(self) -> None:
        usage = collections.namedtuple("usage", "total used free")(1000 * 2**30, 950 * 2**30, 50 * 2**30)
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=usage):
            row = find(doctor.check_data_dir(make_cfg(self.data)), "Disk space")
        self.assertEqual(row.level, doctor.WARN)
        self.assertIn("50.0 GiB free of 1000.0 GiB (5%)", row.detail)

    def test_data_dir_inside_the_app_tree_is_only_informational(self) -> None:
        inside = str(doctor.APP_DIR / "data")
        with mock.patch.object(doctor, "_nearest_existing", return_value=self.tmp):  # never write into the repository
            rows = doctor.check_data_dir(make_cfg(inside))
        self.assertIn("inside the application folder", find(rows, "Data dir").detail)
        self.assertEqual(find(rows, "Data dir").level, doctor.INFO)

    def test_file_owner_posix_and_unknown(self) -> None:
        if os.name == "posix":
            self.assertNotEqual(doctor.file_owner(self.tmp), "unknown")
        else:
            with mock.patch.object(
                doctor, "run_command", return_value=doctor.CommandResult(0, "BUILTIN\\Administrators\r\n", "")
            ):
                self.assertEqual(doctor.file_owner(self.tmp), "BUILTIN\\Administrators")
            with mock.patch.object(doctor, "run_command", return_value=doctor.CommandResult(127, "", "x")):
                self.assertEqual(doctor.file_owner(self.tmp), "unknown")


class WindowsAclCheckTests(TempDirCase):
    def run_check(self, dumps: Dict[str, Optional[str]]) -> List[doctor.Row]:
        """Run check_windows_acl as on Windows; ``dumps`` maps a path fragment to the SDDL ``_icacls`` should return."""

        def fake_icacls(path: str) -> Any:
            for fragment, sddl in dumps.items():
                if fragment in path:
                    return (sddl, "" if sddl else "Access is denied.")
            return (doctor.extract_sddl(REAL["private"].encode("utf-16-le")), "")

        with mock.patch.object(doctor, "os_kind", return_value="windows"), mock.patch.object(
            doctor, "_icacls", fake_icacls
        ):
            return doctor.check_windows_acl(make_cfg(self.data))

    def sddl(self, key: str) -> str:
        return str(doctor.extract_sddl(REAL[key].encode("utf-16-le")))

    def test_other_systems_have_no_acl_rows(self) -> None:
        with mock.patch.object(doctor, "os_kind", return_value="linux"):
            self.assertEqual(doctor.check_windows_acl(make_cfg(self.data)), [])

    def test_private_data_dir_passes(self) -> None:
        rows = self.run_check({})
        self.assertEqual(find(rows, "Data dir ACL").level, doctor.PASS)

    def test_data_dir_readable_by_users_fails_with_the_fix(self) -> None:
        rows = self.run_check({self.data: self.sddl("python313")})  # Users have read+execute here
        row = find(rows, "Data dir ACL")
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))
        self.assertIn("Users", row.detail)
        self.assertIn('icacls "%s" /inheritance:r /grant:r' % self.data, row.detail)
        self.assertIn("*S-1-5-19:(OI)(CI)M", row.detail)

    def test_app_and_python_dirs_only_warn_when_writable(self) -> None:
        rows = self.run_check(
            {str(doctor.APP_DIR): self.sddl("e_root"), os.path.dirname(sys.executable): self.sddl("python313")}
        )
        self.assertEqual(find(rows, "App folder ACL").level, doctor.WARN)
        self.assertIn("--harden", find(rows, "App folder ACL").detail)
        self.assertEqual(find(rows, "Python folder ACL").level, doctor.PASS)
        self.assertEqual(find(rows, "Data dir ACL").level, doctor.PASS)

    def test_unreadable_acl_is_not_a_failure(self) -> None:
        rows = self.run_check({self.data: None, str(doctor.APP_DIR): None})
        self.assertEqual(find(rows, "Data dir ACL").level, doctor.WARN)
        self.assertEqual(find(rows, "App folder ACL").level, doctor.INFO)

    def test_icacls_helper_reads_a_real_dump_without_bom(self) -> None:
        dumps: List[str] = []

        def fake_run(argv: List[str], timeout: float = 15.0) -> Any:
            dumps.append(argv[3])
            with open(argv[3], "wb") as handle:
                handle.write(REAL["python313"].encode("utf-16-le"))
            return doctor.CommandResult(0, "", "")

        with mock.patch.object(doctor, "run_command", fake_run):
            sddl, problem = doctor._icacls("C:\\Python313")
        self.assertFalse(os.path.exists(os.path.dirname(dumps[0])))  # the temp dir is removed again
        self.assertEqual(problem, "")
        self.assertTrue(str(sddl).startswith("D:PAI(A;OICI;FA;;;SY)"))
        with mock.patch.object(doctor, "run_command", return_value=doctor.CommandResult(5, "", "Access is denied.\n")):
            self.assertEqual(doctor._icacls("C:\\x"), (None, "Access is denied."))


class OpenFilesTests(unittest.TestCase):
    def fake_resource(self, soft: int, hard: int) -> Any:
        return types.SimpleNamespace(RLIMIT_NOFILE=7, RLIM_INFINITY=-1, getrlimit=lambda which: (soft, hard))

    def test_limits(self) -> None:
        cases = [
            ((8192, 8192), doctor.PASS),
            ((1024, 524288), doctor.PASS),
            ((1024, 2048), doctor.WARN),
            ((-1, -1), doctor.PASS),
        ]
        for (soft, hard), level in cases:
            with mock.patch.dict(sys.modules, {"resource": self.fake_resource(soft, hard)}):
                row = doctor.check_nofile()[0]
            self.assertEqual(row.level, level, (soft, hard))
        with mock.patch.dict(sys.modules, {"resource": self.fake_resource(1024, 524288)}):
            self.assertIn("raises the soft limit", doctor.check_nofile()[0].detail)
        with mock.patch.dict(sys.modules, {"resource": self.fake_resource(-1, -1)}):
            self.assertIn("unlimited", doctor.check_nofile()[0].detail)

    def test_windows_has_no_such_limit(self) -> None:
        with mock.patch.dict(sys.modules, {"resource": None}):
            self.assertEqual(doctor.check_nofile()[0].level, doctor.INFO)


# ---------------------------------------------------------------------------------------------------------------------
# Port, running server, LAN
# ---------------------------------------------------------------------------------------------------------------------

INFO_JSON = {"name": "DeskTalk", "registration_open": False, "needs_setup": True, "tls": False}


@contextlib.contextmanager
def fake_desktalk_server(routes: Optional[Dict[str, Any]] = None) -> Iterator[int]:
    """A real local HTTP server that answers like DeskTalk (``/healthz`` and ``/api/info``)."""
    import http.server

    table = routes or {"/healthz": (200, b"ok"), "/api/info": (200, json.dumps(INFO_JSON).encode())}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            status, body = table.get(self.path, (404, b"no"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def write_lock(data_dir: str, info: Dict[str, Any]) -> None:
    os.makedirs(os.path.join(data_dir, "control"), exist_ok=True)
    with open(os.path.join(data_dir, "control", "server.lock"), "wb") as handle:
        handle.write(json.dumps(info).encode("utf-8").ljust(512, b" "))


class PortCheckTests(TempDirCase):
    def test_free_port(self) -> None:
        facts: Dict[str, Any] = {}
        port = free_port()
        rows = doctor.check_ports(make_cfg(self.data, port=port), facts)
        self.assertEqual(find(rows, "Port").level, doctor.PASS)
        self.assertIn("127.0.0.1:%d is free" % port, find(rows, "Port").detail)
        self.assertIsNone(facts["running"])

    def test_port_zero_is_not_checked(self) -> None:
        rows = doctor.check_ports(make_cfg(self.data, port=0), {})
        self.assertEqual(find(rows, "Port").level, doctor.INFO)

    def test_foreign_listener_fails_and_is_named(self) -> None:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        facts: Dict[str, Any] = {}
        with mock.patch.object(doctor, "port_owner", return_value="sshd.exe (pid 4916)"):
            rows = doctor.check_ports(make_cfg(self.data, port=port), facts)
        row = find(rows, "Port")
        self.assertEqual((row.level, row.environment), (doctor.FAIL, False))
        self.assertIn("sshd.exe (pid 4916)", row.detail)
        self.assertIn("choose another --port", row.detail)
        self.assertIsNone(facts["running"])

    def test_running_deskta_server_is_a_pass_with_pid_and_version_from_the_lock(self) -> None:
        with fake_desktalk_server() as port:
            write_lock(self.data, {"pid": 4321, "port": port, "tls": False, "version": "2.0.0"})
            facts: Dict[str, Any] = {}
            rows = doctor.check_ports(make_cfg(self.data, port=port), facts)
        row = find(rows, "Port")
        self.assertEqual(row.level, doctor.PASS)
        self.assertIn("version 2.0.0", row.detail)
        self.assertIn("pid 4321", row.detail)
        self.assertEqual(facts["running"]["port"], port)
        self.assertEqual(facts["running"]["info"]["needs_setup"], True)

    def test_running_server_without_a_readable_lock(self) -> None:
        with fake_desktalk_server() as port:
            rows = doctor.check_ports(make_cfg(self.data, port=port), {})
        row = find(rows, "Port")
        self.assertEqual(row.level, doctor.PASS)
        self.assertIn("pid unknown", row.detail)
        self.assertIn("version unknown", row.detail)

    def test_server_on_another_port_than_configured(self) -> None:
        with fake_desktalk_server() as running_port:
            write_lock(self.data, {"pid": 7, "port": running_port, "tls": False, "version": "2.0.0"})
            configured = free_port()
            facts: Dict[str, Any] = {}
            rows = doctor.check_ports(make_cfg(self.data, port=configured), facts)
        self.assertEqual(find(rows, "Port").level, doctor.PASS)
        self.assertIn("another port", find(rows, "Server").detail)
        self.assertEqual(facts["running"]["port"], running_port)

    def test_stale_lock_and_stop_request(self) -> None:
        write_lock(self.data, {"pid": 99999, "port": free_port(), "tls": False, "version": "1.9"})
        control = os.path.join(self.data, "control")
        with open(os.path.join(control, "stop.request"), "w", encoding="utf-8") as handle:
            handle.write("stop\n")
        rows = doctor.check_ports(make_cfg(self.data, port=free_port()), {})
        self.assertIn("stale", find(rows, "server.lock").detail)
        self.assertEqual(find(rows, "stop.request").level, doctor.INFO)

    def test_privileged_port_on_posix(self) -> None:
        denied = mock.patch.object(doctor, "bind_error", return_value=PermissionError(13, "denied"))
        with denied, mock.patch.object(doctor, "os_kind", return_value="linux"):
            row = find(doctor.check_ports(make_cfg(self.data, port=80), {}), "Port")
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))
        self.assertIn("needs root", row.detail)

    def test_redirect_port(self) -> None:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        taken = listener.getsockname()[1]
        with mock.patch.object(doctor, "port_owner", return_value="nginx (pid 5)"):
            rows = doctor.check_ports(make_cfg(self.data, port=free_port(), redirect_port=taken), {})
        self.assertEqual(find(rows, "Redirect port").level, doctor.FAIL)
        rows = doctor.check_ports(make_cfg(self.data, port=free_port(), redirect_port=free_port()), {})
        self.assertEqual(find(rows, "Redirect port").level, doctor.PASS)

    def test_bind_mimics_the_server_reuse_rules(self) -> None:
        calls: List[Any] = []

        class FakeSocket:
            def __init__(self, *args: Any) -> None:
                calls.append(("socket", args))

            def setsockopt(self, *args: Any) -> None:
                calls.append(("setsockopt", args))

            def bind(self, address: Any) -> None:
                calls.append(("bind", address))

            def close(self) -> None:
                calls.append(("close",))

        with mock.patch.object(doctor.socket, "socket", FakeSocket):
            self.assertIsNone(doctor.bind_error("0.0.0.0", 8765))
        kinds = [c[0] for c in calls]
        if os.name == "nt":
            self.assertNotIn("setsockopt", kinds)  # a REUSEADDR probe would "succeed" on a port that is in use
        else:
            self.assertIn("setsockopt", kinds)
        self.assertEqual(kinds[-2:], ["bind", "close"])
        self.assertIn(("bind", ("0.0.0.0", 8765)), calls)

    def test_port_owner_per_os(self) -> None:
        netstat = "  TCP    0.0.0.0:8765   0.0.0.0:0   LISTENING   777\r\n"
        tasklist = doctor.CommandResult(0, '"python.exe","777","Services","0","20,000 K"\r\n', "")

        def windows_run(argv: List[str], timeout: float = 15.0) -> Any:
            return doctor.CommandResult(0, netstat, "") if argv[0] == "netstat" else tasklist

        with mock.patch.object(doctor, "os_kind", return_value="windows"), mock.patch.object(
            doctor, "run_command", windows_run
        ):
            self.assertEqual(doctor.port_owner(8765), "python.exe (pid 777)")
            self.assertIn("unknown", doctor.port_owner(1))
        ss = doctor.CommandResult(0, 'LISTEN 0 4096 0.0.0.0:8765 0.0.0.0:* users:(("nginx",pid=55,fd=6))\n', "")
        with mock.patch.object(doctor, "os_kind", return_value="linux"), mock.patch.object(
            doctor, "run_command", return_value=ss
        ):
            self.assertEqual(doctor.port_owner(8765), "nginx (pid 55)")
        lsof = doctor.CommandResult(0, "COMMAND PID USER FD\nnginx 55 root 6u\n", "")
        with mock.patch.object(doctor, "os_kind", return_value="macos"), mock.patch.object(
            doctor, "run_command", return_value=lsof
        ):
            self.assertEqual(doctor.port_owner(8765), "nginx (pid 55, user root)")
            with mock.patch.object(doctor, "run_command", return_value=doctor.CommandResult(127, "", "")):
                self.assertIn("try with sudo", doctor.port_owner(8765))


class LanCheckTests(TempDirCase):
    def test_primary_and_other_adapters(self) -> None:
        cfg = make_cfg(self.data, host="0.0.0.0", port=8765)
        with mock.patch.object(util, "lan_addresses", return_value=("192.168.1.5", ["172.31.0.1", "10.0.0.8"])):
            rows = doctor.check_lan(cfg)
        self.assertEqual(find(rows, "Share this link").detail, "http://192.168.1.5:8765/")
        self.assertIn("http://172.31.0.1:8765/", find(rows, "Other adapters").detail)
        self.assertIn("DHCP reservation", find(rows, "Other adapters").detail)

    def test_https_default_ports_and_specific_host(self) -> None:
        with mock.patch.object(util, "lan_addresses", return_value=("192.168.1.5", [])):
            rows = doctor.check_lan(make_cfg(self.data, host="10.1.1.1", port=443, tls=True))
        self.assertEqual(find(rows, "Share this link").detail, "https://192.168.1.5/")
        self.assertIn("10.1.1.1 only", find(rows, "Listening on").detail)

    def test_no_address_yet(self) -> None:
        with mock.patch.object(util, "lan_addresses", return_value=(None, [])):
            self.assertEqual(doctor.check_lan(make_cfg(self.data, port=8765))[0].level, doctor.WARN)

    def test_only_secondary_addresses(self) -> None:
        with mock.patch.object(util, "lan_addresses", return_value=(None, ["10.0.0.8", "10.0.0.9"])):
            rows = doctor.check_lan(make_cfg(self.data, host="0.0.0.0", port=8765))
        self.assertEqual(find(rows, "Share this link").detail, "http://10.0.0.8:8765/")
        self.assertIn("10.0.0.9", find(rows, "Other adapters").detail)


# ---------------------------------------------------------------------------------------------------------------------
# Firewall and power (canned tool output)
# ---------------------------------------------------------------------------------------------------------------------


def runner(replies: Dict[str, doctor.CommandResult]) -> Any:
    """``run_command`` replacement: the reply whose key is the longest prefix of the joined command line."""

    def fake(argv: List[str], timeout: float = 15.0) -> Any:
        line = " ".join(argv)
        best = max((k for k in replies if line.startswith(k)), key=len, default=None)
        return replies[best] if best else doctor.CommandResult(127, "", "unexpected: " + line)

    return fake


POWERSHELL_PUBLIC = "fw:Domain=True\r\nfw:Private=True\r\nfw:Public=False\r\nnet:Ethernet=Public\r\n"


class FirewallTests(TempDirCase):
    def windows(
        self, rule_rc: int, rule_text: str, powershell: str, state: Optional[Dict[str, Any]], port: int = 8765
    ) -> Any:
        replies = {
            "netsh": doctor.CommandResult(rule_rc, rule_text, ""),
            "powershell": doctor.CommandResult(0, powershell, ""),
        }
        with mock.patch.object(doctor, "os_kind", return_value="windows"), mock.patch.object(
            doctor, "run_command", runner(replies)
        ):
            return doctor.check_firewall(make_cfg(self.data, port=port), state)

    def test_windows_rule_by_exit_code_and_port_number(self) -> None:
        rows = self.windows(0, "Rule Name: DeskTalk\r\nLocalPort: 8765\r\n", "net:Ethernet=Private\r\n", None)
        self.assertEqual(find(rows, "Firewall rule").level, doctor.PASS)
        german = "Regelname: DeskTalk\r\nLokaler Port: 8765,80\r\n"
        self.assertEqual(find(self.windows(0, german, "", None), "Firewall rule").level, doctor.PASS)
        other_port = self.windows(0, "LocalPort: 9000\r\n", "", None)
        self.assertEqual(find(other_port, "Firewall rule").level, doctor.WARN)
        absent = self.windows(1, "No rules match the specified criteria.\r\n", "", None)
        self.assertIn("install_service.py install", find(absent, "Firewall rule").detail)
        self.assertEqual(find(absent, "Firewall rule").level, doctor.WARN)

    def test_windows_public_network_warns_when_the_rule_does_not_cover_it(self) -> None:
        covered_private = {"firewall": {"port": 8765, "scope": "localsubnet", "profile": "private,domain"}}
        rows = self.windows(0, "LocalPort: 8765", POWERSHELL_PUBLIC, covered_private)
        row = find(rows, "Network category")
        self.assertEqual(row.level, doctor.WARN)
        self.assertIn("Set-NetConnectionProfile", row.detail)
        self.assertIn("private,domain", row.detail)
        any_profile = {"firewall": {"port": 8765, "scope": "any", "profile": "any"}}
        self.assertEqual(
            find(self.windows(0, "LocalPort: 8765", POWERSHELL_PUBLIC, any_profile), "Network category").level,
            doctor.INFO,
        )
        no_rule = self.windows(1, "", POWERSHELL_PUBLIC, None)
        self.assertEqual(find(no_rule, "Network category").level, doctor.INFO)
        self.assertEqual(find(rows, "Windows Firewall").detail, "off for: Public")

    def test_windows_without_powershell_still_reports_the_rule(self) -> None:
        replies = {"netsh": doctor.CommandResult(0, "LocalPort: 8765", "")}
        with mock.patch.object(doctor, "os_kind", return_value="windows"), mock.patch.object(
            doctor, "run_command", runner(replies)
        ):
            rows = doctor.check_firewall(make_cfg(self.data, port=8765), None)
        self.assertEqual([r.name for r in rows], ["Firewall rule"])

    def test_linux_ufw_and_firewalld(self) -> None:
        active = doctor.CommandResult(0, "Status: active\n\nTo   Action  From\n8765/tcp  ALLOW  Anywhere\n", "")
        replies = {"/usr/sbin/ufw status": active}
        with mock.patch.object(doctor, "os_kind", return_value="linux"), mock.patch.object(
            doctor.shutil, "which", side_effect=lambda n: "/usr/sbin/" + n
        ):
            with mock.patch.object(doctor, "run_command", runner(replies)):
                self.assertEqual(doctor.check_firewall(make_cfg(self.data, port=8765), None)[0].level, doctor.PASS)
            replies["/usr/sbin/ufw status"] = doctor.CommandResult(0, "Status: active\n22/tcp ALLOW Anywhere\n", "")
            with mock.patch.object(doctor, "run_command", runner(replies)):
                row = doctor.check_firewall(make_cfg(self.data, port=8765), None)[0]
            self.assertEqual(row.level, doctor.WARN)
            self.assertIn("sudo ufw allow 8765/tcp", row.detail)
            replies["/usr/sbin/ufw status"] = doctor.CommandResult(0, "Status: inactive\n", "")
            replies["/usr/sbin/firewall-cmd --state"] = doctor.CommandResult(0, "running\n", "")
            replies["/usr/sbin/firewall-cmd --list-ports"] = doctor.CommandResult(0, "22/tcp 8765/tcp\n", "")
            with mock.patch.object(doctor, "run_command", runner(replies)):
                self.assertEqual(doctor.check_firewall(make_cfg(self.data, port=8765), None)[0].level, doctor.PASS)
            replies["/usr/sbin/firewall-cmd --list-ports"] = doctor.CommandResult(0, "22/tcp\n", "")
            with mock.patch.object(doctor, "run_command", runner(replies)):
                self.assertEqual(doctor.check_firewall(make_cfg(self.data, port=8765), None)[0].level, doctor.WARN)

    def test_linux_without_a_known_firewall(self) -> None:
        with mock.patch.object(doctor, "os_kind", return_value="linux"), mock.patch.object(
            doctor.shutil, "which", return_value=None
        ):
            self.assertEqual(doctor.check_firewall(make_cfg(self.data, port=8765), None)[0].level, doctor.INFO)

    def test_ufw_state_needs_root(self) -> None:
        replies = {"/usr/sbin/ufw status": doctor.CommandResult(1, "", "ERROR: You need to be root")}
        which = {"ufw": "/usr/sbin/ufw"}
        with mock.patch.object(doctor, "os_kind", return_value="linux"), mock.patch.object(
            doctor.shutil, "which", side_effect=which.get
        ), mock.patch.object(doctor, "run_command", runner(replies)):
            row = doctor.check_firewall(make_cfg(self.data, port=8765), None)[0]
        self.assertIn("needs root", row.detail)

    def test_macos_application_firewall(self) -> None:
        on = doctor.CommandResult(0, "Firewall is enabled. (State = 1)\n", "")
        off = doctor.CommandResult(0, "Firewall is disabled. (State = 0)\n", "")
        with mock.patch.object(doctor, "os_kind", return_value="macos"):
            with mock.patch.object(doctor, "run_command", return_value=on):
                self.assertIn("must be allowed", doctor.check_firewall(make_cfg(self.data, port=8765), None)[0].detail)
            with mock.patch.object(doctor, "run_command", return_value=off):
                self.assertIn(
                    "off or not readable", doctor.check_firewall(make_cfg(self.data, port=8765), None)[0].detail
                )

    def test_port_zero_has_nothing_to_check(self) -> None:
        self.assertEqual(doctor.check_firewall(make_cfg(self.data, port=0), None), [])


def powercfg(ac: int) -> doctor.CommandResult:
    text = (
        "      Minimum Possible Setting: 0x00000000\n      Maximum Possible Setting: 0xffffffff\n"
        "      Possible Settings increment: 0x00000001\n      Possible Settings units: Seconds\n"
        "    Current AC Power Setting Index: 0x%08x\n    Current DC Power Setting Index: 0x00000384\n" % ac
    )
    return doctor.CommandResult(0, text, "")


class PowerTests(TempDirCase):
    def test_windows_never_sleeps(self) -> None:
        with mock.patch.object(doctor, "os_kind", return_value="windows"), mock.patch.object(
            doctor, "run_command", return_value=powercfg(0)
        ):
            rows = doctor.check_power(make_cfg(self.data))
        self.assertEqual(find(rows, "Standby timeout").level, doctor.PASS)
        self.assertEqual(find(rows, "Hibernate timeout").level, doctor.PASS)

    def test_windows_sleeps_after_a_while(self) -> None:
        with mock.patch.object(doctor, "os_kind", return_value="windows"), mock.patch.object(
            doctor, "run_command", return_value=powercfg(1800)
        ):
            rows = doctor.check_power(make_cfg(self.data))
        row = find(rows, "Standby timeout")
        self.assertEqual(row.level, doctor.WARN)
        self.assertIn("30 min", row.detail)
        self.assertIn("powercfg /change standby-timeout-ac 0", row.detail)
        self.assertIn("powercfg /change hibernate-timeout-ac 0", find(rows, "Hibernate timeout").detail)

    def test_unreadable_power_plan_and_allow_sleep(self) -> None:
        with mock.patch.object(doctor, "os_kind", return_value="windows"), mock.patch.object(
            doctor, "run_command", return_value=doctor.CommandResult(1, "", "x")
        ):
            rows = doctor.check_power(make_cfg(self.data, allow_sleep=True))
        self.assertEqual(find(rows, "Standby timeout").level, doctor.INFO)
        self.assertEqual(find(rows, "Keep awake").level, doctor.WARN)

    def test_other_systems_rely_on_the_service_wrapper(self) -> None:
        with mock.patch.object(doctor, "os_kind", return_value="linux"):
            rows = doctor.check_power(make_cfg(self.data))
        self.assertIn("systemd-inhibit", find(rows, "Keep awake").detail)


# ---------------------------------------------------------------------------------------------------------------------
# Database, backups, TLS, service, setup code
# ---------------------------------------------------------------------------------------------------------------------


def schema(**overrides: Any) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "path": "chat.db", "exists": True, "code_schema_version": 3, "schema_version": 3, "journal_mode": "wal",
        "integrity": "ok", "meta": {"workspace_name": "Office", "registration_open": "1"}, "error": None,
    }  # fmt: skip
    info.update(overrides)
    return info


class DatabaseCheckTests(TempDirCase):
    def check(self, info: Dict[str, Any], sqlite_ok: bool = True) -> List[doctor.Row]:
        from chatd import maintenance

        facts: Dict[str, Any] = {"sqlite_ok": sqlite_ok}
        with mock.patch.object(maintenance, "schema_info", return_value=info):
            rows = doctor.check_database(make_cfg(self.data), facts)
        self.facts = facts
        return rows

    def test_healthy_database(self) -> None:
        rows = self.check(schema())
        self.assertEqual(levels(rows), ["INFO", "PASS", "PASS", "PASS"])
        self.assertIn("DeskTalk %s, database schema v3" % doctor.__version__, rows[0].detail)
        self.assertEqual(self.facts["meta"], {"workspace_name": "Office", "registration_open": "1"})

    def test_schema_newer_than_the_code_is_an_environment_failure(self) -> None:
        row = find(self.check(schema(schema_version=4)), "Schema")
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))
        self.assertIn("newer than the code", row.detail)

    def test_older_schema_is_migrated_and_missing_version_warns(self) -> None:
        self.assertEqual(find(self.check(schema(schema_version=2)), "Schema").level, doctor.INFO)
        self.assertEqual(find(self.check(schema(schema_version=None)), "Schema").level, doctor.WARN)

    def test_journal_mode_and_integrity(self) -> None:
        rows = self.check(schema(journal_mode="delete", integrity="row 1 missing from index"))
        self.assertEqual(find(rows, "Journal mode").level, doctor.WARN)
        row = find(rows, "Integrity")
        self.assertEqual((row.level, row.environment), (doctor.FAIL, False))
        self.assertIn("restore", row.detail)

    def test_no_database_yet_and_unreadable_database(self) -> None:
        rows = self.check(schema(exists=False, schema_version=None, journal_mode=None, integrity=None, meta={}))
        self.assertEqual(find(rows, "Database").level, doctor.INFO)
        rows = self.check(schema(error="cannot open: unable to open database file"))
        self.assertEqual((find(rows, "Database").level, find(rows, "Database").environment), (doctor.FAIL, True))

    def test_unusable_sqlite_skips_the_checks_but_still_reports(self) -> None:
        rows = doctor.check_database(make_cfg(self.data), {"sqlite_ok": False})
        self.assertEqual((rows[0].level, rows[0].environment), (doctor.FAIL, True))

    def test_import_error_inside_maintenance_is_reported(self) -> None:
        from chatd import maintenance

        with mock.patch.object(maintenance, "schema_info", side_effect=ImportError("No module named sqlite3")):
            rows = doctor.check_database(make_cfg(self.data), {"sqlite_ok": True})
        self.assertEqual(rows[0].level, doctor.FAIL)
        self.assertIn("sqlite3", rows[0].detail)

    def test_real_database_round_trip(self) -> None:
        from chatd import maintenance

        maintenance.create_admin(
            self.data, "boss", "correct-horse-battery-staple", workspace_name="Office", scrypt_n=1024
        )
        facts: Dict[str, Any] = {"sqlite_ok": True}
        rows = doctor.check_database(make_cfg(self.data), facts)
        self.assertNotIn(doctor.FAIL, levels(rows), rows)
        self.assertEqual(find(rows, "Schema").level, doctor.PASS)
        self.assertEqual(find(rows, "Integrity").level, doctor.PASS)
        self.assertIsInstance(facts["meta"], dict)  # empty until an admin edits the workspace settings


class BackupTests(TempDirCase):
    def test_backup_listing(self) -> None:
        cfg = make_cfg(self.data)
        self.assertEqual(doctor.check_backups(cfg), [])  # no backup dir at all
        os.makedirs(str(cfg.backup_dir))
        self.assertIn("none yet", doctor.check_backups(cfg)[0].detail)
        for name, age in (("auto-20260101-000000.db", 7200), ("chat-20260102-000000.db", 60), ("pre-restore-x.db", 10)):
            path = os.path.join(str(cfg.backup_dir), name)
            with open(path, "wb") as handle:
                handle.write(b"x")
            os.utime(path, (doctor.time.time() - age,) * 2)
        row = doctor.check_backups(cfg)[0]
        self.assertIn("newest chat-20260102-000000.db", row.detail)
        self.assertIn("2 files", row.detail)


@unittest.skipIf(
    doctor.shutil.which("openssl") is None and not os.path.isfile(r"C:\Program Files\Git\usr\bin\openssl.exe"),
    "no openssl",
)
class TlsCheckTests(TempDirCase):
    def make_cert(self, days: int = 825, san: Optional[List[str]] = None) -> Dict[str, Path]:
        import subprocess

        from chatd import tlsutil

        tls = Path(self.data) / "tls"
        tls.mkdir()
        cnf = tls / "openssl.cnf"
        hostname = socket.gethostname() or "localhost"
        cnf.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=%s\n[v3]\nsubjectAltName=@alt\n[alt]\nDNS.1=%s\nIP.1=127.0.0.1\n"
            % (hostname[:60], hostname[:60]),
            encoding="ascii",
        )
        env = {k: v for k, v in os.environ.items() if k != "OPENSSL_CONF"}
        command = [tlsutil.find_openssl(), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "825"]
        command += ["-config", str(cnf), "-keyout", str(tls / "key.pem"), "-out", str(tls / "cert.pem")]
        subprocess.run(command, check=True, capture_output=True, env=env)
        cnf.unlink()
        wanted = san if san is not None else [hostname, "localhost", "127.0.0.1"]
        meta = {"san": wanted, "created": 0.0, "not_after": doctor.time.time() + days * 86400}
        (tls / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return {"cert": tls / "cert.pem", "key": tls / "key.pem"}

    def check(self, cfg: Any, lan: Any = (None, [])) -> List[doctor.Row]:
        from chatd import tlsutil

        with mock.patch.object(util, "lan_addresses", return_value=lan), mock.patch.object(
            tlsutil, "cert_fingerprint", create=True, return_value="AA:BB:CC"
        ):
            return doctor.check_tls(cfg)

    def test_current_certificate(self) -> None:
        self.make_cert()
        rows = self.check(make_cfg(self.data, tls=True))
        self.assertIn("SHA-256 fingerprint AA:BB:CC", find(rows, "TLS").detail)
        self.assertEqual(find(rows, "TLS expiry").level, doctor.PASS)
        self.assertEqual(find(rows, "TLS names").level, doctor.PASS)
        if os.name == "posix":
            os.chmod(str(Path(self.data) / "tls" / "key.pem"), 0o600)
            self.assertEqual(find(self.check(make_cfg(self.data, tls=True)), "TLS key file").level, doctor.PASS)
            os.chmod(str(Path(self.data) / "tls" / "key.pem"), 0o644)
            self.assertEqual(find(self.check(make_cfg(self.data, tls=True)), "TLS key file").level, doctor.WARN)

    def test_expiring_and_new_lan_address(self) -> None:
        self.make_cert(days=10)
        rows = self.check(make_cfg(self.data, tls=True), lan=("192.168.1.77", []))
        self.assertEqual(find(rows, "TLS expiry").level, doctor.WARN)
        names = find(rows, "TLS names")
        self.assertEqual(names.level, doctor.WARN)
        self.assertIn("192.168.1.77", names.detail)

    def test_certificate_present_while_tls_is_off(self) -> None:
        self.make_cert()
        row = find(self.check(make_cfg(self.data, tls=False)), "TLS")
        self.assertIn("off (a certificate exists)", row.detail)

    def test_broken_files_degrade_to_plain_http(self) -> None:
        tls = Path(self.data) / "tls"
        tls.mkdir()
        (tls / "cert.pem").write_text("not a certificate", encoding="ascii")
        (tls / "key.pem").write_text("not a key", encoding="ascii")
        row = find(self.check(make_cfg(self.data, tls=True)), "TLS certificate")
        self.assertEqual(row.level, doctor.WARN)
        self.assertIn("plain HTTP", row.detail)


class TlsBasicsTests(TempDirCase):
    def test_off_without_certificate_warns_about_plain_http(self) -> None:
        row = doctor.check_tls(make_cfg(self.data, tls=False))[0]
        self.assertEqual(row.level, doctor.WARN)
        self.assertIn("anyone on this network can read passwords and messages", row.detail)

    def test_on_without_certificate_yet(self) -> None:
        row = doctor.check_tls(make_cfg(self.data, tls=True))[0]
        self.assertEqual(row.level, doctor.INFO)
        self.assertIn("created at the first start", row.detail)

    def test_ssl_missing(self) -> None:
        tls = Path(self.data) / "tls"
        tls.mkdir()
        (tls / "cert.pem").write_text("x", encoding="ascii")
        with mock.patch.dict(sys.modules, {"ssl": None, "chatd.tlsutil": None}):
            row = doctor.check_tls(make_cfg(self.data, tls=True))[0]
        self.assertEqual((row.level, row.environment), (doctor.FAIL, True))


class ServiceAndSetupTests(TempDirCase):
    def test_no_installed_service(self) -> None:
        with mock.patch.object(doctor, "read_install_state", return_value=None):
            self.assertEqual(doctor.check_service(make_cfg(self.data)), [])

    def test_same_data_dir_and_existing_interpreter(self) -> None:
        state = {"python": sys.executable, "data_dir": self.data, "port": 8765}
        with mock.patch.object(doctor, "read_install_state", return_value=state):
            rows = doctor.check_service(make_cfg(self.data))
        self.assertEqual(levels(rows), ["INFO"])

    def test_different_data_dir_and_missing_interpreter(self) -> None:
        state = {
            "python": os.path.join(self.tmp, "gone", "python.exe"),
            "data_dir": os.path.join(self.tmp, "svc"),
            "port": 8765,
        }
        with mock.patch.object(doctor, "read_install_state", return_value=state):
            rows = doctor.check_service(make_cfg(self.data))
        self.assertIn("cli -- doctor", find(rows, "Data dir").detail)
        self.assertEqual(find(rows, "Service interpreter").level, doctor.FAIL)
        self.assertIn("interpreter missing", find(rows, "Service interpreter").detail)

    def test_state_file_locations_and_parsing(self) -> None:
        for kind, expected in (
            ("linux", "/etc/desktalk/install.json"),
            ("macos", "/Library/Application Support/DeskTalk/install.json"),
        ):
            with mock.patch.object(doctor, "os_kind", return_value=kind):
                self.assertEqual(doctor._state_file_path(), expected)
        with mock.patch.object(doctor, "os_kind", return_value="windows"), mock.patch.dict(
            os.environ, {"PROGRAMDATA": "D:\\PD"}
        ):
            self.assertEqual(doctor._state_file_path(), os.path.join("D:\\PD", "DeskTalk", "install.json"))
        path = os.path.join(self.tmp, "install.json")
        with mock.patch.object(doctor, "_state_file_path", return_value=path):
            self.assertIsNone(doctor.read_install_state())
            Path(path).write_text("{broken", encoding="utf-8")
            self.assertIsNone(doctor.read_install_state())
            Path(path).write_text("[1]", encoding="utf-8")
            self.assertIsNone(doctor.read_install_state())
            Path(path).write_text('{"port": 9}', encoding="utf-8")
            self.assertEqual(doctor.read_install_state(), {"port": 9})

    def test_setup_code_shown_only_while_needed(self) -> None:
        cfg = make_cfg(self.data, port=8765)
        self.assertIn("no setup code file", doctor.check_setup_code(cfg, {})[0].detail)
        Path(self.data, "setup_code.txt").write_text("Ab3dEf9k\n", encoding="utf-8")
        row = doctor.check_setup_code(cfg, {"running": None})[0]
        self.assertEqual(row.name, "Setup code")
        self.assertIn("Ab3dEf9k", row.detail)
        self.assertIn("http://127.0.0.1:8765/", row.detail)
        running = {"info": {"needs_setup": True}, "port": 9001}
        self.assertIn("http://127.0.0.1:9001/", doctor.check_setup_code(cfg, {"running": running})[0].detail)
        done = {"info": {"needs_setup": False}, "port": 9001}
        row = doctor.check_setup_code(cfg, {"running": done})[0]
        self.assertEqual(row.name, "Setup")
        self.assertNotIn("Ab3dEf9k", row.detail)
        Path(self.data, "setup_code.txt").write_text("\n", encoding="utf-8")
        self.assertEqual(doctor.check_setup_code(cfg, {}), [])


# ---------------------------------------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------------------------------------


class ReportTests(TempDirCase):
    def run_doctor(self, cfg: Any) -> Any:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.run(cfg)
        return code, out.getvalue()

    def quiet_network(self) -> Any:
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(doctor, "run_command", return_value=doctor.CommandResult(1, "", "")))
        stack.enter_context(mock.patch.object(util, "lan_addresses", return_value=("192.168.1.5", [])))
        stack.enter_context(mock.patch.object(doctor, "read_install_state", return_value=None))
        return stack

    def test_healthy_installation_exits_0_and_prints_every_section(self) -> None:
        cfg = make_cfg(self.data, port=free_port(), host="127.0.0.1")
        cfg.sources = {"port": "flag"}
        with self.quiet_network():
            code, text = self.run_doctor(cfg)
        self.assertEqual(code, 0, text)
        for heading in ("Environment", "Data", "Database", "Network", "Power", "TLS", "Effective configuration"):
            self.assertIn(heading, text)
        self.assertIn("[PASS] sqlite3", text)
        self.assertIn("[PASS] Port", text)
        self.assertRegex(text, r"port\s+%d\s+\(flag\)" % cfg.port)
        self.assertIn("the server can run (exit code 0)", text)
        self.assertEqual(os.listdir(self.data), [])  # nothing was created in the data dir

    def test_missing_sqlite_exits_78_without_a_traceback(self) -> None:
        cfg = make_cfg(self.data, port=free_port())
        with self.quiet_network(), mock.patch.dict(sys.modules, {"sqlite3": None}):
            code, text = self.run_doctor(cfg)
        self.assertEqual(code, 78)
        self.assertIn("[FAIL] sqlite3", text)
        self.assertIn("[FAIL] Database", text)
        self.assertIn("Effective configuration", text)  # the report still finishes
        self.assertNotIn("Traceback", text)
        self.assertIn("the server CANNOT run", text)

    def test_a_foreign_port_owner_is_exit_1(self) -> None:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        cfg = make_cfg(self.data, port=listener.getsockname()[1])
        with self.quiet_network(), mock.patch.object(doctor, "port_owner", return_value="nginx (pid 5)"):
            code, text = self.run_doctor(cfg)
        self.assertEqual(code, 1, text)
        self.assertIn("nginx (pid 5)", text)

    def test_a_crashing_check_becomes_a_fail_row(self) -> None:
        cfg = make_cfg(self.data, port=free_port())
        with self.quiet_network(), mock.patch.object(doctor, "check_lan", side_effect=RuntimeError("boom")):
            code, text = self.run_doctor(cfg)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] LAN", text)
        self.assertIn("RuntimeError: boom", text)
        self.assertIn("Effective configuration", text)

    def test_config_warnings_and_db_source_are_shown(self) -> None:
        from chatd import maintenance

        cfg = make_cfg(self.data, port=free_port(), workspace_name="From flag")
        cfg.warnings = ["config.json: unknown key 'colour' ignored", "test_scale is a tests-only key and is ignored"]
        with self.quiet_network(), mock.patch.object(
            maintenance, "schema_info", return_value=schema(meta={"workspace_name": "From DB"})
        ):
            code, text = self.run_doctor(cfg)
        self.assertEqual(code, 0, text)
        self.assertIn("[WARN] config", text)
        self.assertIn("unknown key 'colour' ignored", text)
        self.assertRegex(text, r"workspace_name\s+From DB\s+\(db\)")

    def test_multiline_details_are_indented(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor._print_rows("Title", [doctor.Row(doctor.WARN, "Thing", "first line\nsecond line")])
        lines = out.getvalue().splitlines()
        self.assertIn("[WARN] Thing", lines[2])
        self.assertTrue(lines[3].startswith("        "))
        self.assertTrue(lines[3].endswith("second line"))


class RunCommandTests(unittest.TestCase):
    def test_run_command_never_raises(self) -> None:
        ok = doctor.run_command([sys.executable, "-c", "print('hi')"])
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "hi"))
        self.assertEqual(doctor.run_command(["definitely-not-a-command-desktalk"]).returncode, 127)
        slow = doctor.run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertEqual(slow.returncode, 124)
        self.assertEqual(doctor.os_kind() in ("windows", "linux", "macos"), True)


if __name__ == "__main__":
    unittest.main()
