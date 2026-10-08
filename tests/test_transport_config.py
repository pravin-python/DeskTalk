"""Configuration: precedence, booleans, unknown and tests-only keys, validation (SPEC 2.1)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

from chatd.config import DEFAULT_BLOCKED_EXTENSIONS, Config, load


class ConfigBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dt-config-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_file(self, data: Any, name: str = "config.json") -> None:
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))

    def load(self, argv: Optional[list] = None, env: Optional[Dict[str, str]] = None, **file_values: Any) -> Config:
        if file_values:
            self.write_file(file_values)
        return load(["--data-dir", self.tmp] + list(argv or []), env or {})


class DefaultsAndPrecedence(ConfigBase):
    def test_defaults(self) -> None:
        cfg = self.load()
        self.assertEqual((cfg.host, cfg.port, cfg.workspace_name), ("0.0.0.0", 8765, "DeskTalk"))
        self.assertFalse(cfg.registration_open)
        self.assertEqual((cfg.max_users, cfg.max_upload_mb), (2000, 100))
        self.assertEqual(cfg.blocked_extensions, DEFAULT_BLOCKED_EXTENSIONS)
        self.assertEqual(
            [
                "exe",
                "scr",
                "com",
                "pif",
                "bat",
                "cmd",
                "msi",
                "msp",
                "vbs",
                "vbe",
                "wsf",
                "wsh",
                "hta",
                "lnk",
                "reg",
                "cpl",
                "dll",
                "jar",
            ],
            cfg.blocked_extensions,
        )
        self.assertEqual((cfg.tls, cfg.redirect_port, cfg.allowed_hosts, cfg.allow_sleep), (False, 0, [], False))
        self.assertEqual((cfg.edit_window_s, cfg.delete_window_s, cfg.max_body_chars), (900, 172800, 8000))
        self.assertEqual((cfg.session_days, cfg.min_password_len, cfg.scrypt_n, cfg.log_level), (30, 8, 65536, "INFO"))
        self.assertEqual((cfg.test_scale, cfg.test_limits), (1.0, {}))
        self.assertEqual(str(cfg.backup_dir), os.path.join(self.tmp, "backups"))
        self.assertEqual(cfg.sources.get("data_dir"), "flag")
        self.assertTrue(all(row[2] in ("default", "flag") for row in cfg.describe()))

    def test_default_data_dir_is_app_data(self) -> None:
        cfg = load([], {})
        self.assertEqual(cfg.data_dir.name, "data")
        self.assertEqual(cfg.source("data_dir"), "default")
        self.assertEqual(
            load([], {"DESKTALK_DATA_DIR": self.tmp}).data_dir, Path(os.path.abspath(self.tmp))
        )

    def test_flags_beat_env_beat_file_beat_defaults(self) -> None:
        self.write_file({"port": 1111, "workspace_name": "From File", "max_upload_mb": 7, "host": "10.0.0.1"})
        env = {"DESKTALK_PORT": "2222", "DESKTALK_NAME": "From Env", "DESKTALK_MAX_UPLOAD_MB": "9"}
        cfg = load(["--data-dir", self.tmp, "--port", "3333"], env)
        self.assertEqual(cfg.port, 3333)
        self.assertEqual(cfg.workspace_name, "From Env")
        self.assertEqual(cfg.max_upload_mb, 9)
        self.assertEqual(cfg.host, "10.0.0.1")
        self.assertEqual(cfg.sources["port"], "flag")
        self.assertEqual(cfg.sources["workspace_name"], "env")
        self.assertEqual(cfg.sources["host"], "file")
        self.assertEqual(cfg.source("session_days"), "default")
        sources = {row[0]: row[2] for row in cfg.describe()}
        self.assertEqual(
            (sources["port"], sources["workspace_name"], sources["host"], sources["tls"]),
            ("flag", "env", "file", "default"),
        )

    def test_data_dir_is_resolved_first_and_locates_config_json(self) -> None:
        other = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(other, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"port": 4444}, fh)
        self.assertEqual(load(["--data-dir", other], {}).port, 4444)
        self.assertEqual(load([], {"DESKTALK_DATA_DIR": other}).port, 4444)
        cfg = load(["--data-dir", other], {"DESKTALK_DATA_DIR": self.tmp})
        self.assertEqual((cfg.port, cfg.source("data_dir")), (4444, "flag"))

    def test_file_cannot_set_data_dir(self) -> None:
        cfg = self.load(data_dir="/elsewhere")
        self.assertEqual(os.path.abspath(str(cfg.data_dir)), os.path.abspath(self.tmp))
        self.assertTrue(any("data_dir" in w for w in cfg.warnings))


class Booleans(ConfigBase):
    def test_accepted_spellings(self) -> None:
        for text in ("1", "true", "TRUE", "Yes", "on", " On "):
            self.assertTrue(self.load(env={"DESKTALK_TLS": text}).tls, text)
        for text in ("0", "false", "No", "OFF"):
            self.assertFalse(self.load(env={"DESKTALK_TLS": text}).tls, text)

    def test_anything_else_is_a_usage_error(self) -> None:
        for text in ("2", "maybe", "", "y", "enabled"):
            with self.assertRaises(SystemExit) as caught:
                self.load(env={"DESKTALK_REGISTRATION": text})
            self.assertEqual(caught.exception.code, 2, text)
        with self.assertRaises(SystemExit):
            self.load(tls="sometimes")

    def test_file_booleans(self) -> None:
        self.assertTrue(self.load(tls=True).tls)
        self.assertTrue(self.load(registration_open="yes").registration_open)
        self.assertFalse(self.load(allow_sleep=0).allow_sleep)

    def test_flag_pairs_override_in_both_directions(self) -> None:
        self.assertTrue(self.load(["--tls"], {"DESKTALK_TLS": "0"}).tls)
        self.assertFalse(self.load(["--no-tls"], {"DESKTALK_TLS": "1"}).tls)
        self.assertFalse(self.load(["--no-tls"], tls=True).tls)
        self.assertTrue(self.load(["--registration"], registration_open=False).registration_open)
        self.assertFalse(self.load(["--no-registration"], {"DESKTALK_REGISTRATION": "true"}).registration_open)
        self.assertEqual(self.load(["--tls"]).source("tls"), "flag")
        self.assertTrue(self.load(["--allow-sleep"]).allow_sleep)
        self.assertEqual(self.load().source("tls"), "default")

    def test_last_flag_wins(self) -> None:
        self.assertFalse(self.load(["--tls", "--no-tls"]).tls)
        self.assertTrue(self.load(["--no-tls", "--tls"]).tls)


class Lists(ConfigBase):
    def test_allowed_hosts(self) -> None:
        self.assertEqual(
            self.load(["--allowed-host", "Chat.Example.com", "--allowed-host", "pc1"]).allowed_hosts,
            ["chat.example.com", "pc1"],
        )
        self.assertEqual(
            self.load(env={"DESKTALK_ALLOWED_HOSTS": "a.lan, b.lan,,c.lan"}).allowed_hosts, ["a.lan", "b.lan", "c.lan"]
        )
        self.assertEqual(self.load(allowed_hosts=["x.corp"]).allowed_hosts, ["x.corp"])
        self.assertEqual(
            self.load(["--allowed-host", "flag.host"], {"DESKTALK_ALLOWED_HOSTS": "env.host"}).allowed_hosts,
            ["flag.host"],
        )
        with self.assertRaises(SystemExit):
            self.load(["--allowed-host", "bad host!"])

    def test_blocked_extensions(self) -> None:
        self.assertEqual(
            self.load(env={"DESKTALK_BLOCKED_EXT": "EXE .bat, scr"}).blocked_extensions, ["exe", "bat", "scr"]
        )
        self.assertEqual(self.load(env={"DESKTALK_BLOCKED_EXT": ""}).blocked_extensions, [])
        self.assertEqual(self.load(blocked_extensions=[]).blocked_extensions, [])
        self.assertEqual(self.load(blocked_extensions=["Js", "exe", "js"]).blocked_extensions, ["js", "exe"])
        with self.assertRaises(SystemExit):
            self.load(env={"DESKTALK_BLOCKED_EXT": "a/b"})


class Validation(ConfigBase):
    def test_numbers(self) -> None:
        for bad in ("abc", "-1", "65536", "1_0", "+5", " ", "1.5", "0x10"):
            with self.assertRaises(SystemExit, msg=bad):
                self.load(["--port", bad])
        self.assertEqual(self.load(["--port", "0"]).port, 0)
        self.assertEqual(self.load(["--port", "65535"]).port, 65535)
        for key in ("max_users", "max_upload_mb", "session_days", "min_password_len"):
            with self.assertRaises(SystemExit, msg=key):
                self.load(**{key: 0})
            with self.assertRaises(SystemExit, msg=key):
                self.load(**{key: True})
            with self.assertRaises(SystemExit, msg=key):
                self.load(**{key: "ten"})
        os.remove(os.path.join(self.tmp, "config.json"))
        self.assertEqual(self.load(env={"DESKTALK_MAX_UPLOAD_MB": "250"}).max_upload_bytes, 250 * 1024 * 1024)
        self.assertEqual(self.load(edit_window_s=900.0).edit_window_s, 900)
        with self.assertRaises(SystemExit):
            self.load(edit_window_s=1.5)

    def test_names_and_levels(self) -> None:
        self.assertEqual(self.load(["--name", "  Acme   Chat "]).workspace_name, "Acme Chat")
        with self.assertRaises(SystemExit):
            self.load(["--name", "x" * 41])
        with self.assertRaises(SystemExit):
            self.load(["--name", "   "])
        self.assertEqual(self.load(["--log-level", "debug"]).log_level, "DEBUG")
        self.assertEqual(self.load(env={"DESKTALK_LOG": "warning"}).log_level, "WARNING")
        with self.assertRaises(SystemExit):
            self.load(["--log-level", "loud"])

    def test_host(self) -> None:
        self.assertEqual(self.load(["--host", "127.0.0.1"]).host, "127.0.0.1")
        with self.assertRaises(SystemExit):
            self.load(["--host", "a b"])

    def test_error_text_goes_to_stderr_and_names_the_key(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            self.load(["--port", "nope"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("port", err.getvalue())

    def test_a_flag_without_its_value_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            load(["serve", "--data-dir", self.tmp, "--port"], {})
        self.assertEqual(caught.exception.code, 2)

    def test_the_command_word_and_foreign_tokens_are_ignored(self) -> None:
        cfg = load(["create-admin", "boss", "--password-stdin", "--out", "x", "--data-dir", self.tmp, "--port", "7"], {})
        self.assertEqual((cfg.port, str(cfg.data_dir)), (7, os.path.abspath(self.tmp)))
        for command in ("serve", "doctor", "backup", "restore", "tls-init"):
            self.assertEqual(load([command, "--data-dir", self.tmp], {}).port, 8765)

    def test_the_given_environment_replaces_os_environ(self) -> None:
        with mock.patch.dict(os.environ, {"DESKTALK_PORT": "4242", "DESKTALK_NAME": "From OS"}):
            cfg = load(["--data-dir", self.tmp], {})
            self.assertEqual((cfg.port, cfg.workspace_name), (8765, "DeskTalk"))
            cfg = load(["--data-dir", self.tmp])
            self.assertEqual((cfg.port, cfg.workspace_name), (4242, "From OS"))

    def test_argv_none_means_sys_argv(self) -> None:
        with mock.patch.object(sys, "argv", ["chatd", "serve", "--data-dir", self.tmp, "--port", "5151"]):
            self.assertEqual(load(None, {}).port, 5151)


class ConfigFile(ConfigBase):
    def test_unknown_keys_warn_without_crashing(self) -> None:
        cfg = self.load(port=5555, mystery="x", another=1)
        self.assertEqual(cfg.port, 5555)
        self.assertEqual(sum("unknown key" in w for w in cfg.warnings), 2)

    def test_broken_files_are_environment_errors(self) -> None:
        self.write_file("{not json")
        with self.assertRaises(SystemExit) as caught:
            load(["--data-dir", self.tmp], {})
        self.assertEqual(caught.exception.code, 78)
        self.write_file("[1, 2]")
        with self.assertRaises(SystemExit):
            load(["--data-dir", self.tmp], {})

    def test_invalid_value_in_file_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.load(port="http")
        self.assertEqual(caught.exception.code, 2)

    def test_bom_is_tolerated(self) -> None:
        self.write_file("﻿" + json.dumps({"port": 6001}))
        self.assertEqual(load(["--data-dir", self.tmp], {}).port, 6001)

    def test_backup_dir_sources_and_relative_paths(self) -> None:
        cfg = self.load(["--backup-dir", self.tmp + os.sep + "flag-backups"])
        self.assertEqual((cfg.backup_dir.name, cfg.source("backup_dir")), ("flag-backups", "flag"))
        cfg = self.load(env={"DESKTALK_BACKUP_DIR": os.path.join(self.tmp, "env-b")})
        self.assertEqual((cfg.backup_dir.name, cfg.source("backup_dir")), ("env-b", "env"))
        cfg = self.load(backup_dir="rel/dir")
        self.assertEqual(cfg.backup_dir, cfg.data_dir / "rel" / "dir")
        self.assertEqual(cfg.source("backup_dir"), "file")


class TestsOnlyKeys(ConfigBase):
    def test_ignored_with_a_warning_without_the_environment_switch(self) -> None:
        cfg = self.load(test_scale=0.1, test_limits={"msg.send": [3, 1.0]}, scrypt_n=1024)
        self.assertEqual((cfg.test_scale, cfg.test_limits, cfg.scrypt_n), (1.0, {}, 65536))
        text = " ".join(cfg.warnings)
        self.assertIn("test_scale", text)
        self.assertIn("test_limits", text)
        self.assertIn("scrypt_n", text)
        cfg = self.load(env={"DESKTALK_SCRYPT_N": "1024"})
        self.assertEqual(cfg.scrypt_n, 65536)
        self.assertTrue(cfg.warnings)

    def test_honoured_with_desktalk_test(self) -> None:
        cfg = self.load(
            env={"DESKTALK_TEST": "1", "DESKTALK_SCRYPT_N": "1024"},
            test_scale=0.1,
            test_limits={"msg.send": [3, 1.0], "ws_per_ip": 400, "login_a": [100, 60], "unread_cap": 5},
        )
        self.assertEqual((cfg.test_scale, cfg.scrypt_n), (0.1, 1024))
        self.assertEqual(
            cfg.test_limits, {"msg.send": [3, 1.0], "ws_per_ip": 400, "login_a": [100, 60.0], "unread_cap": 5}
        )
        self.assertEqual(cfg.limit("ws_per_ip", 30), 400)
        self.assertEqual(cfg.limit("ws_total", 800), 800)
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(cfg.source("test_scale"), "file")

    def test_switch_must_be_exactly_one(self) -> None:
        self.assertEqual(self.load(env={"DESKTALK_TEST": "true"}, test_scale=0.5).test_scale, 1.0)
        self.assertEqual(self.load(env={"DESKTALK_TEST": "0"}, test_scale=0.5).test_scale, 1.0)

    def test_higher_scrypt_cost_is_always_allowed(self) -> None:
        self.assertEqual(self.load(env={"DESKTALK_SCRYPT_N": "131072"}).scrypt_n, 131072)

    def test_invalid_test_values_are_rejected_when_honoured(self) -> None:
        env = {"DESKTALK_TEST": "1"}
        for bad in (0, -1, "fast", True, float("inf")):
            self.write_file({"test_scale": bad} if bad != float("inf") else '{"test_scale": 1e999}')
            with self.assertRaises(SystemExit, msg=repr(bad)):
                load(["--data-dir", self.tmp], env)
        for limits in ({"msg.send": 3}, {"msg.send": [3]}, {"nope": 1}, {"ws_per_ip": "x"}, {"msg.send": [3, 0]}, [1]):
            self.write_file({"test_limits": limits})
            with self.assertRaises(SystemExit, msg=repr(limits)):
                load(["--data-dir", self.tmp], env)
        self.write_file({"scrypt_n": 1000})
        with self.assertRaises(SystemExit):
            load(["--data-dir", self.tmp], env)


class ConfigContract(ConfigBase):
    """SPEC 6.2: ``Config.sources`` (every key), ``app_dir``, ``replace``; the ``*`` wildcard of ``test_limits``."""

    def test_sources_cover_every_key(self) -> None:
        cfg = self.load(["--port", "1"], {"DESKTALK_NAME": "N"}, host="10.0.0.1")
        rows = {name: source for name, _value, source in cfg.describe()}
        self.assertEqual(set(cfg.sources) >= set(rows), True)
        self.assertEqual((cfg.sources["port"], cfg.sources["workspace_name"], cfg.sources["host"]), ("flag", "env", "file"))
        self.assertTrue(all(v in ("flag", "env", "file", "default") for v in cfg.sources.values()))
        self.assertEqual(cfg.sources["tls"], "default")
        self.assertEqual(Config().sources["port"], "default")

    def test_app_dir_is_the_repository_root(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.app_dir, Path(__file__).resolve().parent.parent)
        self.assertTrue((cfg.app_dir / "chatd").is_dir())

    def test_replace_returns_an_independent_copy(self) -> None:
        cfg = self.load(["--port", "5"])
        other = cfg.replace(port=0, tls=True, allowed_hosts=["a.lan"])
        self.assertEqual((other.port, other.tls, other.allowed_hosts), (0, True, ["a.lan"]))
        self.assertEqual((cfg.port, cfg.tls, cfg.allowed_hosts), (5, False, []))
        other.blocked_extensions.append("zzz")
        other.sources["port"] = "changed"
        self.assertNotIn("zzz", cfg.blocked_extensions)
        self.assertEqual(cfg.sources["port"], "flag")
        moved = cfg.replace(data_dir=Path(self.tmp) / "elsewhere")
        self.assertEqual(moved.backup_dir, Path(self.tmp) / "elsewhere" / "backups")
        explicit = self.load(["--backup-dir", os.path.join(self.tmp, "b")]).replace(data_dir=Path(self.tmp) / "x")
        self.assertEqual(explicit.backup_dir.name, "b")

    def test_wildcard_limit_is_accepted(self) -> None:
        env = {"DESKTALK_TEST": "1"}
        cfg = self.load(env=env, test_limits={"*": [100000, 1], "msg.send": [3, 1.0], "ws_handshakes_per_ip_min": 100000})
        self.assertEqual(cfg.test_limits["*"], [100000, 1.0])
        self.assertEqual(cfg.test_limits.get("msg.react", cfg.test_limits["*"]), [100000, 1.0])


class ConfigObject(unittest.TestCase):
    def test_direct_construction_and_derived_paths(self) -> None:
        cfg = Config(data_dir="/srv/dt", max_upload_mb=3, tls=True)
        self.assertEqual(str(cfg.db_path).replace("\\", "/"), "/srv/dt/chat.db")
        self.assertEqual(str(cfg.tmp_dir).replace("\\", "/"), "/srv/dt/uploads/.tmp")
        self.assertEqual(str(cfg.control_dir).replace("\\", "/"), "/srv/dt/control")
        self.assertEqual(str(cfg.backup_dir).replace("\\", "/"), "/srv/dt/backups")
        self.assertEqual((cfg.max_upload_bytes, cfg.scheme), (3 * 1024 * 1024, "https"))
        self.assertTrue(cfg.web_dir.name == "web")
        self.assertEqual(cfg.limit("anything", 5), 5)
        self.assertIsNot(Config().blocked_extensions, Config().blocked_extensions)


if __name__ == "__main__":
    unittest.main()
