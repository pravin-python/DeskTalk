#!/usr/bin/env python3
"""DeskTalk service installer (SPEC section 10).

Installs, removes and controls the DeskTalk server as an operating-system service: a Windows Task Scheduler
task (``DeskTalk``), a systemd unit (``desktalk``) or a launchd daemon (``com.desktalk.server``).
Standard library only, Python >= 3.8. It imports nothing from ``chatd`` (it carries its own health probe and LAN
discovery; certificates are made by ``chatd tls-init``) and never imports ``sqlite3``.

    python service/install_service.py install   [--port N] [--host H] [--data-dir D] [--python PATH] ...
    python service/install_service.py uninstall [--keep-firewall]
    python service/install_service.py start|stop|restart|status
    python service/install_service.py logs [-n 100]
    python service/install_service.py print-config [--target windows|linux|macos]
    python service/install_service.py cli -- <chatd args>

Every mutating command honours ``--dry-run``: it prints each command and file and executes/writes nothing, on any host
OS for any target OS (``--target``). Exit codes: 0 ok, 1 error, 2 usage / privilege / refused check, 3 health check
failed; ``cli --`` returns the exit status of the ``chatd`` command it ran.

Layout: pure generators and parsers first (Task XML, systemd unit, launchd plist, SDDL checks, state
diff; all unit-tested without touching the machine), then the dry-run aware execution context, the three platform
backends behind one interface, the commands and finally ``main``.
"""

from __future__ import annotations

import abc
import argparse
import codecs
import csv
import dataclasses
import getpass
import glob
import http.client
import io
import ipaddress
import json
import locale
import ntpath
import os
import plistlib
import posixpath
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------------------------------------------
# Constants (SPEC 10.1 "Fixed names", exit codes, defaults)
# --------------------------------------------------------------------------------------------------------------------

TASK_NAME = "DeskTalk"
UNIT_NAME = "desktalk"
LAUNCHD_LABEL = "com.desktalk.server"
FIREWALL_RULE = "DeskTalk"
SERVICE_USER = "desktalk"
TARGETS = ("windows", "linux", "macos")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_NAME = "DeskTalk"
DEFAULT_ALLOW_FROM = "localsubnet"
DEFAULT_FW_PROFILE = "private,domain"
HEALTH_WAIT_S = 15.0
STOP_WAIT_S = 10.0
STOP_RECHECK_S = 5.0

WIN_LOCAL_SERVICE = "NT AUTHORITY\\LOCAL SERVICE"
WIN_SYSTEM = "NT AUTHORITY\\SYSTEM"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNHEALTHY = 3

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ET.register_namespace("", TASK_NS)

# The two interpreter checks of SPEC 10.1 (executed by `<py> -c ...`; the second one with cwd = app root).
PYTHON_CHECK_CODE = (
    "import sys,sqlite3,ssl,hashlib,asyncio;assert sys.version_info>=(3,8);"
    "assert sqlite3.sqlite_version_info>=(3,24);hashlib.pbkdf2_hmac"
)
CHATD_CHECK_CODE = "import chatd"

STATE_KEYS = (
    "python",
    "app_root",
    "data_dir",
    "host",
    "port",
    "tls",
    "redirect_port",
    "user",
    "name",
    "allowed_hosts",
    "allow_sleep",
    "firewall",
)


class InstallerError(Exception):
    """A user-facing failure. ``code`` is the installer exit code (SPEC 10.1): 1 error, 2 usage/privilege/refused."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


def usage_error(message: str) -> InstallerError:
    """Build the exit-code-2 error used for bad options and refused safety checks."""
    return InstallerError(message, EXIT_USAGE)


# --------------------------------------------------------------------------------------------------------------------
# Paths per target OS. Generators work for any target on any host, so they never use the host's os.path blindly.
# --------------------------------------------------------------------------------------------------------------------


def detect_host() -> str:
    """Return the OS this process runs on: ``windows``, ``linux``, ``macos`` or ``other`` (unsupported)."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def pathmod(target: str) -> Any:
    """Return the path module that implements the semantics of ``target`` (ntpath or posixpath)."""
    return ntpath if target == "windows" else posixpath


def norm_path(path: str, target: str, host: str) -> str:
    """Normalise ``path`` per SPEC 10.1: ``abspath(normpath(p))`` without trailing separators except drive roots.

    ``abspath`` needs the working directory of the *host*, so it is applied only when ``target`` is the host OS;
    for a foreign target (dry-run) a relative path just stays normalised.
    """
    mod = pathmod(target)
    result = mod.normpath(path)
    if target == host:
        result = mod.abspath(result)
    return result


def is_within(child: str, parent: str, target: str) -> bool:
    """True when ``child`` is ``parent`` or lies below it (case-insensitively on Windows)."""
    mod = pathmod(target)
    try:
        common = mod.commonpath([mod.normcase(child), mod.normcase(parent)])
    except ValueError:
        return False
    return common == mod.normcase(parent)


def env_get(env: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive environment lookup (Windows copies of ``os.environ`` are upper-cased)."""
    wanted = name.upper()
    for key, value in env.items():
        if key.upper() == wanted:
            return value
    return None


def windows_program_data(env: Mapping[str, str]) -> str:
    """``%ProgramData%`` with the usual fallbacks."""
    return env_get(env, "ProgramData") or env_get(env, "ALLUSERSPROFILE") or "C:\\ProgramData"


def state_file_path(target: str, env: Mapping[str, str]) -> str:
    """Location of the install state file (SPEC 10.1)."""
    if target == "windows":
        return ntpath.join(windows_program_data(env), "DeskTalk", "install.json")
    if target == "macos":
        return "/Library/Application Support/DeskTalk/install.json"
    return "/etc/desktalk/install.json"


def default_data_dir(target: str, env: Mapping[str, str]) -> str:
    """OS location of the data dir used by installers (SPEC 0 / 10.1)."""
    if target == "windows":
        return ntpath.join(windows_program_data(env), "DeskTalk", "data")
    if target == "macos":
        return "/Library/Application Support/DeskTalk/data"
    return "/var/lib/desktalk"


def reject_control_chars(label: str, value: str) -> None:
    """Refuse control characters (incl. newlines) in anything that ends up in a unit file, XML or command line."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise usage_error("%s must not contain control characters or newlines: %r" % (label, value))


def tail_lines(path: str, count: int) -> List[str]:
    """Return the last ``count`` lines of a UTF-8 text file (decoding errors replaced)."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= count:
            step = min(8192, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
    return data.decode("utf-8", "replace").splitlines()[-count:]


def decode_output(raw: bytes) -> str:
    """Decode console output: UTF-8 when valid, else the locale code page (schtasks/netsh use the OEM page)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "latin-1", "replace")


# --------------------------------------------------------------------------------------------------------------------
# Execution context: the single place that runs commands and writes files, so --dry-run is guaranteed to be honest.
# --------------------------------------------------------------------------------------------------------------------


def emit_block(path: str, text: str) -> None:
    """Print a generated file between ``===== BEGIN <path> =====`` / ``===== END <path> =====`` markers."""
    print("===== BEGIN %s =====" % path)
    print(text.rstrip("\n"))
    print("===== END %s =====" % path)


class Result(NamedTuple):
    """Outcome of one command. ``dry`` marks the placeholder returned in dry-run mode (nothing was executed)."""

    returncode: int
    stdout: str
    stderr: str
    dry: bool = False


class GeneratedFile(NamedTuple):
    """A file the installer writes: its path, bytes on disk, readable text and POSIX mode."""

    path: str
    data: bytes
    text: str
    mode: int


class Context:
    """The outside world for one invocation: target/host OS, dry-run flag, environment and I/O hooks.

    ``target`` is the OS the artefacts are generated for; ``host`` is the OS this process runs on. They differ only
    in dry-run mode (``--target``). All side effects (commands, files, directories) go through the methods below,
    which print instead of acting when ``dry_run`` is set. Tests replace the ``*_fn`` hooks and ``env``.
    """

    def __init__(
        self,
        target: str,
        *,
        dry_run: bool = False,
        host: Optional[str] = None,
        app_root: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        interactive: Optional[bool] = None,
    ) -> None:
        self.target = target
        self.host = host or detect_host()
        self.dry_run = dry_run
        self.env: Dict[str, str] = dict(os.environ if env is None else env)
        self.app_root = app_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if interactive is None:
            interactive = bool(sys.stdin and sys.stdin.isatty() and sys.stdout.isatty())
        self.interactive = interactive
        self.input_fn: Callable[[str], str] = input
        self.getpass_fn: Callable[[str], str] = getpass.getpass
        self.health_fn: Callable[..., Optional[Dict[str, Any]]] = health_probe
        self.lan_fn: Callable[[], Tuple[Optional[str], List[str]]] = lan_addresses
        self.admin_fn: Callable[[], bool] = self._detect_admin
        self.sleep_fn: Callable[[float], None] = time.sleep
        self.exists_fn: Callable[[str], bool] = os.path.exists
        self.size_fn: Callable[[str], int] = os.path.getsize
        self.realpath_fn: Callable[[str], str] = os.path.realpath

    # -- output ---------------------------------------------------------------------------------------------------

    @property
    def foreign(self) -> bool:
        """True when generating for another OS than the one running (dry-run only)."""
        return self.target != self.host

    def say(self, message: str = "") -> None:
        """Print one line of normal output."""
        print(message)

    def warn(self, message: str) -> None:
        """Print a warning line."""
        print("WARNING: " + message)

    def show(self, message: str) -> None:
        """Print a ``[dry-run]`` line (only in dry-run mode)."""
        if self.dry_run:
            print("[dry-run] " + message)

    def fmt(self, argv: Sequence[str]) -> str:
        """Render a command line for display with the quoting rules of the target OS."""
        if self.target == "windows":
            return subprocess.list2cmdline(list(argv))
        return " ".join(shlex.quote(str(a)) for a in argv)

    # -- environment ----------------------------------------------------------------------------------------------

    def norm(self, path: str) -> str:
        """``norm_path`` for this context."""
        return norm_path(path, self.target, self.host)

    def join(self, *parts: str) -> str:
        """Join path parts with the separator of the target OS."""
        return pathmod(self.target).join(*parts)

    def exists(self, path: str) -> bool:
        """``os.path.exists``; always False for a foreign target (the path belongs to another machine)."""
        return not self.foreign and self.exists_fn(path)

    def which(self, name: str) -> Optional[str]:
        """``shutil.which`` on the host; the conventional location for a foreign target."""
        if not self.foreign:
            return shutil.which(name)
        known = {
            "systemd-inhibit": "/usr/bin/systemd-inhibit",
            "nologin": "/usr/sbin/nologin",
            "runuser": "/usr/sbin/runuser",
        }
        return known.get(name)

    def is_admin(self) -> bool:
        """True when running as root / with an elevated Administrator token."""
        return self.admin_fn()

    def _detect_admin(self) -> bool:
        if self.host == "windows":
            try:
                import ctypes

                return bool(ctypes.windll.shell32.IsUserAnAdmin())
            except (AttributeError, OSError):
                return False
        geteuid = getattr(os, "geteuid", None)
        return geteuid is not None and geteuid() == 0

    def macos_version(self) -> Tuple[int, ...]:
        """Return ``platform.mac_ver()`` as a tuple; modern (11, 0) for a foreign target."""
        if self.foreign:
            return (11, 0)
        import platform

        parts = platform.mac_ver()[0].split(".")
        return tuple(int(p) for p in parts if p.isdigit()) or (11, 0)

    # -- side effects ---------------------------------------------------------------------------------------------

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        input_text: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: float = 120.0,
        check: bool = False,
        ok: Sequence[int] = (0,),
        stream: bool = False,
        what: Optional[str] = None,
    ) -> Result:
        """Run a command (or print it in dry-run mode).

        ``stream=True`` inherits stdin/stdout/stderr (interactive passthrough); otherwise output is captured and stdin
        is closed (``input_text`` is sent when given and is never echoed). Missing executables yield rc 127 and
        timeouts rc 124 instead of exceptions. With ``check`` a return code outside ``ok`` raises ``InstallerError``.
        """
        args = [str(a) for a in argv]
        if self.dry_run:
            note = ""
            if cwd:
                note += "   (cwd: %s)" % cwd
            if input_text is not None:
                note += "   (stdin: secret withheld)"
            self.show("$ " + self.fmt(args) + note)
            return Result(0, "", "", True)
        sys.stdout.flush()
        run_env = dict(env) if env is not None else None
        try:
            if stream:
                proc = subprocess.run(args, cwd=cwd, env=run_env, check=False)
                result = Result(proc.returncode, "", "")
            else:
                completed = subprocess.run(
                    args,
                    cwd=cwd,
                    env=run_env,
                    input=input_text.encode("utf-8") if input_text is not None else None,
                    stdin=None if input_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
                result = Result(completed.returncode, decode_output(completed.stdout), decode_output(completed.stderr))
        except subprocess.TimeoutExpired:
            result = Result(124, "", "timed out after %.0f s" % timeout)
        except OSError as exc:
            result = Result(127, "", "cannot run %s: %s" % (args[0], exc))
        if check and result.returncode not in ok:
            detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
            raise InstallerError(
                "%s failed (exit %d)%s" % (what or args[0], result.returncode, ": " + detail[-1] if detail else "")
            )
        return result

    def write_file(self, path: str, data: bytes, mode: Optional[int] = None, text: Optional[str] = None) -> None:
        """Write ``data`` atomically (temp file + replace); in dry-run print the path and ``text``."""
        if self.dry_run:
            self.show("write %s (%d bytes%s)" % (path, len(data), ", mode %o" % mode if mode else ""))
            if text is not None:
                emit_block(path, text)
            return
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "wb") as handle:
            handle.write(data)
        if mode is not None and hasattr(os, "chmod"):
            os.chmod(temp, mode)
        os.replace(temp, path)

    def makedirs(self, path: str, mode: int = 0o755) -> None:
        """Create a directory tree (or print it in dry-run mode)."""
        if self.dry_run:
            self.show("mkdir %s" % path)
            return
        os.makedirs(path, mode=mode, exist_ok=True)

    def remove_file(self, path: str) -> None:
        """Delete a file if present (or print it in dry-run mode)."""
        if self.dry_run:
            self.show("delete %s" % path)
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def remove_empty_dir(self, path: str) -> None:
        """Remove a directory only when it is empty (a non-empty or missing one is left alone)."""
        if self.dry_run:
            self.show("rmdir %s (only if empty)" % path)
            return
        try:
            os.rmdir(path)
        except OSError:
            pass


# --------------------------------------------------------------------------------------------------------------------
# Settings, validation and the state file (SPEC 10.1)
# --------------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Settings:
    """The fully resolved configuration of one installation (flags > state file > defaults).

    ``user`` is the service identity: a Windows principal name or a POSIX user; ``group`` (POSIX only) is derived
    and not stored in the state file. ``firewall`` is the rule that was applied (``{port, scope, profile}``) or None.
    """

    target: str
    python: str
    app_root: str
    data_dir: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    tls: bool = False
    redirect_port: int = 0
    user: str = ""
    group: str = ""
    name: str = DEFAULT_NAME
    allowed_hosts: List[str] = dataclasses.field(default_factory=list)
    allow_sleep: bool = False
    firewall: Optional[Dict[str, Any]] = None

    @property
    def server_py(self) -> str:
        """Absolute path of ``server.py`` (the entry point the service runs)."""
        return pathmod(self.target).join(self.app_root, "server.py")

    @property
    def run_as_system(self) -> bool:
        """True for the Windows ``--run-as-system`` principal."""
        return self.user == WIN_SYSTEM

    @property
    def scheme(self) -> str:
        """``https`` when TLS is on, else ``http``."""
        return "https" if self.tls else "http"

    @property
    def ports(self) -> List[int]:
        """The TCP ports the server listens on (main port plus the optional HTTP->HTTPS redirect port)."""
        return [self.port] + ([self.redirect_port] if self.redirect_port else [])

    def to_state(self) -> Dict[str, Any]:
        """The state-file dict (``STATE_KEYS`` order)."""
        return {
            "python": self.python,
            "app_root": self.app_root,
            "data_dir": self.data_dir,
            "host": self.host,
            "port": self.port,
            "tls": self.tls,
            "redirect_port": self.redirect_port,
            "user": self.user,
            "name": self.name,
            "allowed_hosts": list(self.allowed_hosts),
            "allow_sleep": self.allow_sleep,
            "firewall": self.firewall,
        }


def read_state(path: str) -> Optional[Dict[str, Any]]:
    """Load the state file; ``None`` when it is missing, unreadable or not a JSON object."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _show_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def diff_state(old: Optional[Mapping[str, Any]], new: Mapping[str, Any]) -> List[str]:
    """One ``key: old -> new`` line per state key that changes (empty when ``old`` is None or nothing changes)."""
    if old is None:
        return []
    return [
        "  %s: %s -> %s" % (key, _show_value(old.get(key)), _show_value(new.get(key)))
        for key in STATE_KEYS
        if old.get(key) != new.get(key)
    ]


def normalise_scope(text: str) -> str:
    """Validate ``--allow-from``: ``localsubnet``, ``any`` or a comma separated CIDR list."""
    value = text.strip().lower()
    if value in ("localsubnet", "any"):
        return value
    networks = [part.strip() for part in value.split(",")]
    for part in networks:
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise usage_error("--allow-from must be localsubnet, any or a comma separated CIDR list, got %r" % text)
    return ",".join(networks)


def normalise_profile(text: str) -> str:
    """Validate ``--firewall-profile``: ``any`` or a subset of ``public,private,domain``."""
    value = text.strip().lower()
    if value == "any":
        return value
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts or any(part not in ("public", "private", "domain") for part in parts):
        raise usage_error("--firewall-profile must be 'any' or a list of public, private, domain, got %r" % text)
    return ",".join(parts)


_ALLOWED_HOST_RE = re.compile(r"^([a-z0-9.-]{1,253}|\[[0-9a-f:.]+\])(:\d{1,5})?$")


def validate_settings(s: Settings) -> None:
    """Refuse unusable values early (exit 2): ports, name length, control characters, malformed hosts."""
    for label, value in (
        ("--python", s.python),
        ("the application directory", s.app_root),
        ("--data-dir", s.data_dir),
        ("--host", s.host),
        ("--name", s.name),
        ("the service user", s.user),
    ):
        reject_control_chars(label, value)
    if not s.host.strip() or " " in s.host:
        raise usage_error("--host must be a non-empty address or name without spaces")
    if not 1 <= s.port <= 65535:
        raise usage_error("--port must be 1..65535")
    if not 0 <= s.redirect_port <= 65535 or s.redirect_port == s.port:
        raise usage_error("--redirect-port must be 0 (off) or a port different from --port")
    if not 1 <= len(s.name) <= 40:
        raise usage_error("--name (the workspace name) must be 1..40 characters")
    for allowed in s.allowed_hosts:
        reject_control_chars("--allowed-host", allowed)
        if not _ALLOWED_HOST_RE.match(allowed.lower()):
            raise usage_error("--allowed-host %r is not a valid host name or IP literal" % allowed)


# --------------------------------------------------------------------------------------------------------------------
# Pure generators: server command line, Task Scheduler XML, systemd unit, launchd plist, firewall
# --------------------------------------------------------------------------------------------------------------------


def serve_arguments(s: Settings) -> List[str]:
    """Arguments after the interpreter: ``-X utf8 -I <app>/server.py serve ...`` (SPEC 10.1, isolated mode)."""
    args = [
        "-X", "utf8", "-I", s.server_py, "serve",
        "--host", s.host, "--port", str(s.port), "--data-dir", s.data_dir, "--name", s.name,
    ]  # fmt: skip
    if s.tls:
        args.append("--tls")
    if s.redirect_port:
        args += ["--redirect-port", str(s.redirect_port)]
    for allowed in s.allowed_hosts:
        args += ["--allowed-host", allowed]
    if s.allow_sleep:
        args.append("--allow-sleep")
    return args


def service_env(s: Settings) -> Dict[str, str]:
    """``DESKTALK_*`` variables that make ``cli --`` commands see the installed options (flags still win over env)."""
    env = {
        "DESKTALK_HOST": s.host,
        "DESKTALK_PORT": str(s.port),
        "DESKTALK_DATA_DIR": s.data_dir,
        "DESKTALK_NAME": s.name,
        "DESKTALK_TLS": "1" if s.tls else "0",
        "DESKTALK_REDIRECT_PORT": str(s.redirect_port),
        "DESKTALK_ALLOW_SLEEP": "1" if s.allow_sleep else "0",
    }
    if s.allowed_hosts:
        env["DESKTALK_ALLOWED_HOSTS"] = ",".join(s.allowed_hosts)
    return env


def _indent(elem: ET.Element, level: int = 0) -> None:
    """Pretty-print an element tree in place (``ET.indent`` is 3.9+). Leaf text is never touched."""
    children = list(elem)
    if not children:
        return
    pad = "\n" + "  " * level
    elem.text = pad + "  "
    for child in children:
        _indent(child, level + 1)
        child.tail = pad + "  "
    children[-1].tail = pad


def _task_el(parent: ET.Element, tag: str, text: Optional[str] = None, **attrs: str) -> ET.Element:
    child = ET.SubElement(parent, "{%s}%s" % (TASK_NS, tag), attrs)
    if text is not None:
        child.text = text
    return child


def windows_task_tree(s: Settings) -> ET.Element:
    """Build the Task Scheduler definition of SPEC 10.2 with ElementTree (never string formatting)."""
    root = ET.Element("{%s}Task" % TASK_NS, {"version": "1.2"})
    _task_el(_task_el(root, "RegistrationInfo"), "Description", "DeskTalk LAN chat server")

    triggers = _task_el(root, "Triggers")
    boot = _task_el(triggers, "BootTrigger")
    _task_el(boot, "Enabled", "true")
    _task_el(boot, "Delay", "PT20S")
    watchdog = _task_el(triggers, "TimeTrigger")  # restarts the task every 5 minutes if it is not running
    _task_el(watchdog, "StartBoundary", "2020-01-01T00:00:00")
    _task_el(watchdog, "Enabled", "true")
    repetition = _task_el(watchdog, "Repetition")
    _task_el(repetition, "Interval", "PT5M")
    _task_el(repetition, "StopAtDurationEnd", "false")

    principal = _task_el(_task_el(root, "Principals"), "Principal", id="Author")
    _task_el(principal, "UserId", "S-1-5-18" if s.run_as_system else "S-1-5-19")
    _task_el(principal, "RunLevel", "HighestAvailable" if s.run_as_system else "LeastPrivilege")

    settings = _task_el(root, "Settings")
    for tag, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Priority", "4"),
    ):
        _task_el(settings, tag, value)
    restart = _task_el(settings, "RestartOnFailure")
    _task_el(restart, "Interval", "PT1M")
    _task_el(restart, "Count", "999")

    exec_el = _task_el(_task_el(root, "Actions", Context="Author"), "Exec")
    _task_el(exec_el, "Command", s.python)
    _task_el(exec_el, "Arguments", subprocess.list2cmdline(serve_arguments(s)))
    _task_el(exec_el, "WorkingDirectory", s.app_root)
    return root


def windows_task_xml_text(s: Settings) -> str:
    """The Task XML as text including the UTF-16 declaration (what is stored in the file, minus the BOM)."""
    root = windows_task_tree(s)
    _indent(root)
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode")


def windows_task_xml(s: Settings) -> bytes:
    """Task XML as bytes: UTF-16 LE with BOM (a UTF-8 file with a UTF-16 declaration is rejected by schtasks)."""
    return codecs.BOM_UTF16_LE + windows_task_xml_text(s).encode("utf-16-le")


def systemd_quote(token: str, dollar: bool = True) -> str:
    """Quote one systemd word: double quotes, ``\\`` and ``"`` escaped, ``%`` -> ``%%``, ``$`` -> ``$$`` (ExecStart)."""
    text = token.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if dollar:
        text = text.replace("$", "$$")
    return '"%s"' % text


def systemd_unit(s: Settings, inhibit: Optional[str] = None) -> str:
    """Render ``/etc/systemd/system/desktalk.service`` (SPEC 10.3).

    ``inhibit`` is the absolute path of ``systemd-inhibit`` when it exists; it prefixes ``ExecStart`` unless
    ``allow_sleep`` (systemd requires an absolute executable path, so the bare name of the spec is not used).
    """
    words: List[str] = []
    if inhibit and not s.allow_sleep:
        words += [inhibit, "--what=sleep:idle", "--who=DeskTalk", "--why=chat server"]
    words += [s.python] + serve_arguments(s)
    exec_start = " ".join(systemd_quote(word) for word in words)
    low_port = any(port < 1024 for port in s.ports)
    caps = "CAP_NET_BIND_SERVICE" if low_port else ""
    lines = [
        "# Generated by service/install_service.py; re-run `install` to regenerate instead of editing.",
        "[Unit]",
        "Description=DeskTalk LAN chat server",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "User=" + s.user,
        "Group=" + (s.group or s.user),
        "WorkingDirectory=" + s.app_root.replace("%", "%%"),
        "ExecStart=" + exec_start,
        "Restart=always",
        "RestartSec=3",
        "RestartPreventExitStatus=78",
        "TimeoutStopSec=20",
        "LimitNOFILE=8192",
        "UMask=0077",
        "Environment=PYTHONUNBUFFERED=1 PYTHONUTF8=1",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=full",
        "ProtectHome=read-only",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "RestrictNamespaces=true",
        "LockPersonality=true",
    ]
    if low_port:
        lines.append("AmbientCapabilities=" + caps)
    lines += [
        "CapabilityBoundingSet=" + caps,
        "ReadWritePaths=" + systemd_quote(s.data_dir, dollar=False),
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def launchd_log_path(s: Settings) -> str:
    """``<data>/logs/launchd.log`` (stdout/stderr of the daemon)."""
    return posixpath.join(s.data_dir, "logs", "launchd.log")


def launchd_plist(s: Settings) -> bytes:
    """Render ``/Library/LaunchDaemons/com.desktalk.server.plist`` with plistlib (SPEC 10.4)."""
    program = [] if s.allow_sleep else ["/usr/bin/caffeinate", "-i"]
    program += [s.python] + serve_arguments(s)
    log = launchd_log_path(s)
    data: Dict[str, Any] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": program,
        "WorkingDirectory": s.app_root,
        "UserName": s.user,
        "GroupName": s.group or "staff",
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        },
        "SoftResourceLimits": {"NumberOfFiles": 8192},
        "HardResourceLimits": {"NumberOfFiles": 8192},
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False)


def netsh_add_rule(s: Settings) -> List[str]:
    """``netsh advfirewall firewall add rule`` for the fixed rule name ``DeskTalk`` (SPEC 10.2)."""
    fw = s.firewall or {}
    return [
        "netsh", "advfirewall", "firewall", "add", "rule", "name=" + FIREWALL_RULE, "dir=in", "action=allow",
        "protocol=TCP", "localport=" + ",".join(str(p) for p in s.ports),
        "profile=" + str(fw.get("profile", DEFAULT_FW_PROFILE)), "remoteip=" + str(fw.get("scope", DEFAULT_ALLOW_FROM)),
    ]  # fmt: skip


NETSH_DELETE_RULE = ["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + FIREWALL_RULE]

# SIDs that are used in the Windows ACL commands (S-1-5-18 SYSTEM, -544 Administrators, -19 LocalService, -545 Users).
_ACL_GRANT_FULL = ("*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")


def icacls_harden_command(path: str) -> List[str]:
    """Hardening for the app root / interpreter directory: only SYSTEM and Administrators may write (SPEC 10.2)."""
    return [
        "icacls", path, "/inheritance:r", "/grant:r", *_ACL_GRANT_FULL,
        "*S-1-5-19:(OI)(CI)RX", "*S-1-5-32-545:(OI)(CI)RX",
    ]  # fmt: skip


def icacls_data_command(path: str, run_as_system: bool) -> List[str]:
    """ACL of the data dir: SYSTEM + Administrators full, the service account modify, nobody else (SPEC 10.2)."""
    grants = list(_ACL_GRANT_FULL)
    if not run_as_system:
        grants.append("*S-1-5-19:(OI)(CI)M")
    return ["icacls", path, "/inheritance:r", "/grant:r", *grants]


def icacls_state_dir_command(path: str) -> List[str]:
    """ACL of ``%ProgramData%\\DeskTalk`` (state file, task XML copy): writable by SYSTEM/Administrators only."""
    return ["icacls", path, "/inheritance:r", "/grant:r", *_ACL_GRANT_FULL, "*S-1-5-32-545:(OI)(CI)RX"]


# --------------------------------------------------------------------------------------------------------------------
# SDDL ACL safety check (SPEC 10.2): parse `icacls /save` output and flag broad groups that can write
# --------------------------------------------------------------------------------------------------------------------

SDDL_RIGHTS = {
    "GA": 0x10000000, "GR": 0x80000000, "GW": 0x40000000, "GX": 0x20000000,
    "RC": 0x20000, "SD": 0x10000, "WD": 0x40000, "WO": 0x80000,
    "CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "RP": 0x10, "WP": 0x20, "DT": 0x40, "LO": 0x80, "CR": 0x100,
    "FA": 0x1F01FF, "FR": 0x120089, "FW": 0x120116, "FX": 0x1200A0,
    "KA": 0xF003F, "KR": 0x20019, "KW": 0x20006, "KX": 0x20019,
}  # fmt: skip

# Trustee aliases that matter here (rights and trustees are different positions of an ACE, so "WD" is unambiguous).
SDDL_TRUSTEES = {
    "WD": "S-1-1-0", "AU": "S-1-5-11", "BU": "S-1-5-32-545", "IU": "S-1-5-4", "BA": "S-1-5-32-544",
    "SY": "S-1-5-18", "LS": "S-1-5-19", "NS": "S-1-5-20", "CO": "S-1-3-0", "OW": "S-1-3-4", "AN": "S-1-5-7",
    "NU": "S-1-5-2", "BG": "S-1-5-32-546", "PU": "S-1-5-32-547",
}  # fmt: skip

BROAD_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
    "S-1-5-4": "Interactive",
}

# (mask bit, symbolic name) for every right the check treats as "can modify": SPEC 10.2.
WRITE_BITS = (
    (0x2, "FILE_WRITE_DATA/ADD_FILE"),
    (0x4, "FILE_APPEND_DATA/ADD_SUBDIRECTORY"),
    (0x10000, "DELETE"),
    (0x40000, "WRITE_DAC"),
    (0x80000, "WRITE_OWNER"),
    (0x40000000, "GENERIC_WRITE"),
    (0x10000000, "GENERIC_ALL"),
)
WRITE_MASK = sum(bit for bit, _name in WRITE_BITS)
_ALLOW_KINDS = ("A", "OA", "XA")


class Ace(NamedTuple):
    """One parsed DACL entry: ACE type, flags, access mask and trustee SID (aliases resolved)."""

    kind: str
    flags: str
    mask: int
    sid: str
    text: str


class AclFinding(NamedTuple):
    """A broad group (``name``/``sid``) with an allow ACE whose ``mask`` includes a write right."""

    sid: str
    name: str
    mask: int
    ace: str


def split_sddl(sddl: str) -> Dict[str, str]:
    """Split an SDDL string into its top-level ``O``/``G``/``D``/``S`` components (markers inside ACEs ignored)."""
    marks: List[int] = []
    depth = 0
    for index, char in enumerate(sddl):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char in "OGDS" and sddl[index + 1 : index + 2] == ":":
            marks.append(index)
    parts: Dict[str, str] = {}
    for position, mark in enumerate(marks):
        end = marks[position + 1] if position + 1 < len(marks) else len(sddl)
        parts[sddl[mark]] = sddl[mark + 2 : end]
    return parts


def split_aces(component: str) -> List[str]:
    """Return the bodies of the top-level ``( ... )`` groups of a DACL component (conditional ACEs nest)."""
    aces: List[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(component):
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                aces.append(component[start:index])
    return aces


def parse_rights(text: str) -> int:
    """Convert the rights field of an ACE (hex, decimal or concatenated two-letter codes) to an access mask."""
    value = text.strip()
    if value.lower().startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return 0
    if value.isdigit():
        return int(value)
    mask = 0
    for index in range(0, len(value) - 1, 2):
        mask |= SDDL_RIGHTS.get(value[index : index + 2].upper(), 0)
    return mask


def parse_dacl(sddl: str) -> List[Ace]:
    """Parse the DACL of an SDDL string into ``Ace`` tuples (malformed entries are skipped)."""
    aces: List[Ace] = []
    for body in split_aces(split_sddl(sddl).get("D", "")):
        fields = body.split(";", 6)
        if len(fields) < 6:
            continue
        trustee = fields[5].strip().upper()
        aces.append(
            Ace(
                fields[0].strip().upper(), fields[1], parse_rights(fields[2]), SDDL_TRUSTEES.get(trustee, trustee), body
            )
        )
    return aces


def describe_mask(mask: int) -> str:
    """Name the write rights contained in ``mask`` (``0x1301bf: FILE_WRITE_DATA/ADD_FILE, DELETE``)."""
    names = [name for bit, name in WRITE_BITS if mask & bit]
    return "0x%x: %s" % (mask, ", ".join(names))


def unsafe_aces(sddl: str) -> List[AclFinding]:
    """Allow ACEs that let Everyone / Authenticated Users / Users / Interactive write (SPEC 10.2 safety check)."""
    return [
        AclFinding(ace.sid, BROAD_SIDS[ace.sid], ace.mask, ace.text)
        for ace in parse_dacl(sddl)
        if ace.kind in _ALLOW_KINDS and ace.sid in BROAD_SIDS and ace.mask & WRITE_MASK
    ]


def decode_icacls_dump(raw: bytes) -> str:
    """Decode an ``icacls /save`` file: UTF-16 LE *without* a BOM in practice (a BOM or UTF-8 are tolerated)."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", "replace")
    if b"\x00" in raw[:32]:  # ASCII text encoded as UTF-16 has a NUL in every other byte
        return raw.decode("utf-16-le", "replace")
    return decode_output(raw)


def extract_sddl(raw: bytes) -> Optional[str]:
    """Pick the SDDL line out of an ``icacls /save`` file (the object name precedes it; a drive root has none)."""
    for line in decode_icacls_dump(raw).splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if re.match(r"^[OGDS]:(?![\\/])", stripped):
            return stripped
    return None


# --------------------------------------------------------------------------------------------------------------------
# Network helpers: LAN discovery (SPEC 5.8), health probe (SPEC 10.1), port waits. Own copies, nothing from chatd.
# --------------------------------------------------------------------------------------------------------------------


def _usable_lan_ip(address: str) -> bool:
    """True for a routable-looking IPv4 address: not loopback, link-local (169.254/16), unspecified or multicast."""
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def lan_addresses() -> Tuple[Optional[str], List[str]]:
    """Return ``(primary, others)`` per SPEC 5.8.

    The primary address is the one the OS would use for outgoing traffic (UDP ``connect`` to 10.255.255.255, nothing
    is sent). Others come from ``gethostbyname_ex(gethostname())`` then ``getaddrinfo(..., AF_INET)``; loopback,
    169.254.x.x and duplicates are dropped. Every discovery call tolerates ``gaierror``/``OSError``.
    """
    primary: Optional[str] = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            primary = sock.getsockname()[0]
    except OSError:
        primary = None
    if primary is not None and not _usable_lan_ip(primary):
        primary = None
    candidates: List[str] = []
    try:  # socket.gaierror is an OSError; a failed host-name lookup ends the search, as in chatd.util.lan_addresses
        hostname = socket.gethostname()
        candidates += list(socket.gethostbyname_ex(hostname)[2])
        candidates += [str(info[4][0]) for info in socket.getaddrinfo(hostname, None, socket.AF_INET)]
    except OSError:
        pass
    others: List[str] = []
    for address in candidates:
        if address != primary and address not in others and _usable_lan_ip(address):
            others.append(address)
    return primary, others


def probe_host(host: str) -> str:
    """Host to probe for a given ``--host``: loopback for wildcard/loopback binds, else the value itself (SPEC 10.1)."""
    value = host.strip()
    if value in ("", "0.0.0.0", "::", "localhost") or value.startswith("127."):
        return "127.0.0.1"
    return value


def _http_get(host: str, port: int, path: str, context: Optional[ssl.SSLContext], timeout: float) -> Tuple[int, bytes]:
    """One GET; returns ``(status, body)`` with the body capped at 64 KiB."""
    conn: http.client.HTTPConnection
    if context is not None:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close", "User-Agent": "desktalk-installer"})
        response = conn.getresponse()
        return response.status, response.read(65536)
    finally:
        conn.close()


def health_probe(host: str, port: int, tls: bool, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """Probe ``GET /healthz`` (must be 200 ``ok``) then ``GET /api/info`` and return the parsed JSON object, else None.

    ``host`` is mapped with ``probe_host``; with ``tls`` the connection uses an unverified context (loopback health
    check only, a self-signed certificate is expected). Any network, TLS, protocol or JSON problem yields None.
    """
    context: Optional[ssl.SSLContext] = None
    if tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    target = probe_host(host)
    try:
        status, body = _http_get(target, port, "/healthz", context, timeout)
        if status != 200 or body.strip() != b"ok":
            return None
        status, body = _http_get(target, port, "/api/info", context, timeout)
        if status != 200:
            return None
        info = json.loads(body.decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError):
        return None
    return info if isinstance(info, dict) else None


def port_is_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when a TCP connection to ``host:port`` (mapped with ``probe_host``) succeeds."""
    try:
        with socket.create_connection((probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port_closed(ctx: Context, host: str, port: int, timeout: float) -> bool:
    """Poll until the port refuses connections (True) or ``timeout`` seconds pass (False)."""
    deadline = time.monotonic() + timeout
    while True:
        if not port_is_open(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        ctx.sleep_fn(0.25)


# --------------------------------------------------------------------------------------------------------------------
# Interpreter discovery and validation (SPEC 10.1, 10.4)
# --------------------------------------------------------------------------------------------------------------------

PEP514_ROOTS = (
    ("HKLM", "SOFTWARE\\Python\\PythonCore"),
    ("HKLM", "SOFTWARE\\WOW6432Node\\Python\\PythonCore"),
    ("HKCU", "SOFTWARE\\Python\\PythonCore"),
)


class WinRegistry:
    """Read-only view of the Windows registry (``winreg``). Tests substitute any object with these two methods."""

    def __init__(self) -> None:
        import winreg  # only importable on Windows

        self._winreg = winreg
        self._hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}

    def subkeys(self, hive: str, path: str) -> List[str]:
        """Names of the sub-keys of ``hive\\path`` (empty when the key does not exist)."""
        names: List[str] = []
        try:
            with self._winreg.OpenKey(self._hives[hive], path) as key:
                index = 0
                while True:
                    names.append(self._winreg.EnumKey(key, index))
                    index += 1
        except OSError:
            pass
        return names

    def value(self, hive: str, path: str, name: str) -> Optional[str]:
        """String value ``name`` ("" = the default value) of ``hive\\path``, or None."""
        try:
            with self._winreg.OpenKey(self._hives[hive], path) as key:
                data, _kind = self._winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return data if isinstance(data, str) and data else None


def _version_key(version: str) -> Tuple[int, int]:
    match = re.match(r"(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def pep514_executables(registry: Any) -> List[Tuple[str, str, str]]:
    """Return ``(hive, version, python.exe)`` from the PEP 514 registry keys, machine-wide first, newest first.

    ``ExecutablePath`` is preferred; otherwise the default value of ``InstallPath`` is the install directory and
    ``python.exe`` is appended (SPEC 10.1).
    """
    found: List[Tuple[str, str, str, int]] = []
    for root_index, (hive, root) in enumerate(PEP514_ROOTS):
        for version in registry.subkeys(hive, root):
            key = "%s\\%s\\InstallPath" % (root, version)
            exe = registry.value(hive, key, "ExecutablePath")
            if not exe:
                base = registry.value(hive, key, "")
                if not base:
                    continue
                exe = ntpath.join(base, "python.exe")
            found.append((hive, version, exe, root_index))

    def order(item: Tuple[str, str, str, int]) -> Tuple[int, int, int, int, int]:
        major, minor = _version_key(item[1])
        return (0 if item[0] == "HKLM" else 1, -major, -minor, 1 if item[1].endswith("-32") else 0, item[3])

    return [(hive, version, exe) for hive, version, exe, _idx in sorted(found, key=order)]


def parse_py_launcher(text: str) -> List[str]:
    """Extract interpreter paths from ``py -0p`` output (`` -V:3.12 *   C:\\...\\python.exe``)."""
    paths: List[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*-\S+\s+(?:\*\s+)?(\S.*?)\s*$", line)
        if match and match.group(1).lower().endswith(".exe"):
            paths.append(match.group(1))
    return paths


def _unique(items: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def windows_candidates(ctx: Context, registry: Any) -> List[str]:
    """Discovery order of SPEC 10.1 after ``--python``: registry, ``py -0p``, ``python`` on PATH."""
    found = [exe for _hive, _version, exe in pep514_executables(registry)]
    if ctx.which("py"):
        launcher = ctx.run(["py", "-0p"], timeout=15)
        found += parse_py_launcher(launcher.stdout)
    on_path = ctx.which("python")
    if on_path:
        found.append(on_path)
    return found


def _version_in_path(path: str) -> Tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)", path)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


_PYTHON_NAME_RE = re.compile(r"^python3(\.\d+)?$")
_CLT_SHIM = "/usr/bin/python3"


def posix_candidates(ctx: Context) -> List[str]:
    """Interpreter candidates for Linux (newest first) and macOS (SPEC 10.1 / 10.4), existing files only.

    On macOS ``/usr/bin/python3`` is the Command Line Tools shim: it pops an install dialog (or exits 1) unless
    ``xcode-select -p`` succeeds, so it is never offered for execution in that case.
    """
    found: List[str] = []
    if ctx.target == "macos":
        for pattern in (
            "/Library/Frameworks/Python.framework/Versions/3.*/bin/python3",
            "/opt/homebrew/bin/python3.*",
            "/usr/local/bin/python3.*",
        ):
            matches = [
                p for p in glob.glob(pattern) if _PYTHON_NAME_RE.match(os.path.basename(p))
            ]  # not python3.12-config
            found += sorted(matches, key=_version_in_path, reverse=True)
        found += [ctx.which("python3") or "", _CLT_SHIM]
        if _CLT_SHIM in found and ctx.run(["xcode-select", "-p"], timeout=15).returncode != 0:
            found = [p for p in found if p != _CLT_SHIM]
    else:
        found += [ctx.which("python3.%d" % minor) or "" for minor in range(13, 7, -1)]
        found += [ctx.which("python3") or "", _CLT_SHIM, "/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
    return [path for path in _unique(found) if ctx.exists_fn(path)]


def interpreter_problem(ctx: Context, path: str) -> Optional[str]:
    """Why ``path`` can never serve as the service interpreter (None when it passes the static checks)."""
    if ctx.target == "windows":
        lowered = path.lower().replace("/", "\\")
        if "\\windowsapps\\" in lowered:
            return "is a Microsoft Store alias stub, not a real interpreter"
        if ntpath.basename(lowered) == "pythonw.exe":
            return "is pythonw.exe (no console); the service must run python.exe"
        if "\\users\\" in lowered:
            return (
                "is a per-user installation (under \\Users\\) that LocalService cannot use; "
                "install Python for all users (e.g. under C:\\Program Files)"
            )
    if not ctx.foreign:
        try:
            size = ctx.size_fn(path)
        except OSError:
            return "does not exist"
        if size == 0:
            return "is a 0-byte file"
    return None


def _venv_home(cfg_path: str) -> Optional[str]:
    try:
        with open(cfg_path, encoding="utf-8") as handle:
            for line in handle:
                key, _sep, value = line.partition("=")
                if key.strip().lower() == "home" and value.strip():
                    return value.strip()
    except OSError:
        return None
    return None


def resolve_venv_home(path: str) -> str:
    """If ``pyvenv.cfg`` sits next to or above ``path``, return the base interpreter named by ``home =``.

    venv launchers are redirector stubs that start the real interpreter as a child process and break PID tracking.
    """
    folder = os.path.dirname(path)
    for cfg_dir in (folder, os.path.dirname(folder)):
        cfg = os.path.join(cfg_dir, "pyvenv.cfg")
        if not os.path.isfile(cfg):
            continue
        home = _venv_home(cfg)
        if home:
            for name in _unique([os.path.basename(path), "python.exe" if os.name == "nt" else "python3", "python"]):
                candidate = os.path.join(home, name)
                if os.path.isfile(candidate):
                    return candidate
        break
    return path


def normalise_interpreter(ctx: Context, path: str) -> str:
    """Absolute, venv-resolved, symlink-free interpreter path (``os.path.realpath`` as SPEC 10.1 requires)."""
    absolute = ctx.norm(path)
    if ctx.foreign:
        return absolute
    return ctx.realpath_fn(resolve_venv_home(absolute))


def validate_interpreter(ctx: Context, python: str) -> Optional[str]:
    """Run the two checks of SPEC 10.1; return None when both pass, else a one-line reason."""
    first = ctx.run([python, "-c", PYTHON_CHECK_CODE], timeout=30)
    if first.returncode != 0:
        detail = (first.stderr.strip().splitlines() or ["no output"])[-1]
        return "needs Python >= 3.8 with sqlite3 >= 3.24, ssl, hashlib, asyncio (%s)" % detail
    second = ctx.run([python, "-c", CHATD_CHECK_CODE], cwd=ctx.app_root, timeout=30)
    if second.returncode != 0:
        detail = (second.stderr.strip().splitlines() or ["no output"])[-1]
        return "cannot import chatd from %s (%s)" % (ctx.app_root, detail)
    return None


def choose_interpreter(ctx: Context, explicit: Optional[str], saved: Optional[str], registry: Any = None) -> str:
    """Pick the service interpreter (see ``_choose_interpreter``); in dry-run a refusal is only a warning.

    A preview must still show the generated artefacts on a machine whose interpreter a real install would refuse
    (e.g. a per-user Python on a developer PC), so the dry-run falls back to the requested/saved/running interpreter.
    """
    try:
        return _choose_interpreter(ctx, explicit, saved, registry)
    except InstallerError as exc:
        if not ctx.dry_run:
            raise
        ctx.warn("dry-run only: a real install would stop here (exit %d): %s" % (exc.code, exc))
        return ctx.norm(explicit or saved or sys.executable or "python3")


def _choose_interpreter(ctx: Context, explicit: Optional[str], saved: Optional[str], registry: Any) -> str:
    """Pick and validate the service interpreter.

    Order: ``--python`` (an invalid explicit choice aborts, it is never replaced silently), the interpreter saved in
    the state file, the interpreter running the installer, then discovery (Windows: PEP 514 registry, ``py -0p``,
    PATH; POSIX: versioned names). Machine-wide beats per-user. In dry-run nothing is executed or searched: the first
    statically acceptable candidate is used and the validation commands are only printed.
    """
    if explicit:
        path = normalise_interpreter(ctx, explicit)
        problem = interpreter_problem(ctx, path)
        reason = problem or validate_interpreter(ctx, path)
        if reason:
            raise usage_error("--python %s: %s" % (path, reason))
        return path
    preferred: List[str] = []
    if saved and (ctx.foreign or ctx.exists_fn(saved)):
        preferred.append(saved)
    if ctx.target == ctx.host and sys.executable:
        preferred.append(sys.executable)
    elif not preferred:
        preferred.append(sys.executable or "python3")
    notes: List[str] = []
    discovered: List[str] = []
    discovery_done = False
    queue = list(preferred)
    seen: List[str] = []
    while queue:
        raw = queue.pop(0)
        path = normalise_interpreter(ctx, raw)
        if path in seen:
            continue
        seen.append(path)
        problem = interpreter_problem(ctx, path)
        reason = problem or validate_interpreter(ctx, path)
        if reason is None:
            return path
        notes.append("  %s: %s" % (path, reason))
        if not queue and not discovery_done and not ctx.dry_run and not ctx.foreign:
            discovery_done = True
            if ctx.target == "windows":
                try:
                    discovered = windows_candidates(ctx, registry or WinRegistry())
                except (ImportError, OSError):
                    discovered = []
            else:
                discovered = posix_candidates(ctx)
            queue.extend(discovered)
    raise usage_error(
        "no usable Python interpreter found. Checked:\n%s\n"
        "Install Python 3.8+ with sqlite3 >= 3.24%s and re-run, or pass --python PATH."
        % ("\n".join(notes), " for all users (machine-wide)" if ctx.target == "windows" else "")
    )


# --------------------------------------------------------------------------------------------------------------------
# Elevation and privilege messages (SPEC 10.1 "Privilege")
# --------------------------------------------------------------------------------------------------------------------

ERROR_CANCELLED = 1223  # the user declined the UAC prompt
SEE_MASK_NOCLOSEPROCESS = 0x40
SEE_MASK_NOASYNC = 0x100
SW_SHOWNORMAL = 1


def ps_quote(text: str) -> str:
    """Quote for PowerShell: double quotes when safe (as in the spec's text), else a literal single-quoted string."""
    if not any(ch in text for ch in '$`"'):
        return '"%s"' % text
    return "'%s'" % text.replace("'", "''")


def privilege_instructions(ctx: Context, argv: Sequence[str]) -> List[str]:
    """The exact steps to re-run ``argv`` with the privileges the command needs."""
    script = os.path.abspath(__file__)
    if ctx.host == "windows":
        python = sys.executable or "python"
        rest = " ".join(ps_quote(a) if re.search(r"[^\w.\-:\\/=]", a) else a for a in argv)
        return [
            "This command needs an elevated shell (Administrator).",
            "  Start menu -> type PowerShell -> right-click -> Run as administrator, then paste:",
            "    cd %s; & %s %s %s" % (ps_quote(ctx.app_root), ps_quote(python), ps_quote(script), rest),
            "  Or add --elevate to get a UAC prompt from this window.",
        ]
    rest = " ".join(shlex.quote(a) for a in argv)
    return [
        "This command needs root. Run:",
        '  sudo "%s" "%s" %s' % (sys.executable or "python3", script, rest),
    ]


class TeeStream:
    """Duplicate everything written to ``stream`` into ``log`` (used by the elevated child, SPEC 10.1)."""

    def __init__(self, stream: Any, log: Any) -> None:
        self._stream = stream
        self._log = log

    def write(self, text: str) -> int:
        """Write to both targets; logging failures never break the console output."""
        written = self._stream.write(text)
        try:
            self._log.write(text)
            self._log.flush()
        except (OSError, ValueError):
            pass
        return len(text) if written is None else written

    def flush(self) -> None:
        """Flush the console stream."""
        self._stream.flush()

    def isatty(self) -> bool:
        """Report the console's tty-ness so interactive prompts keep working."""
        return bool(self._stream.isatty())

    @property
    def encoding(self) -> str:
        """Encoding of the console stream."""
        return str(getattr(self._stream, "encoding", "utf-8"))


def elevation_log_path(pid: int) -> str:
    """``%TEMP%\\desktalk-install-<pid>.log``: where the elevated child tees its output."""
    return os.path.join(tempfile.gettempdir(), "desktalk-install-%d.log" % pid)


def shell_execute_info_class() -> Any:
    """The ctypes layout of ``SHELLEXECUTEINFOW`` (112 bytes on 64-bit Windows). Windows only."""
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]  # fmt: skip

    return ShellExecuteInfo


def elevate_and_wait(ctx: Context, argv: Sequence[str]) -> int:
    """Re-run this script elevated via ``ShellExecuteExW(runas)`` and return the child's exit code (Windows only).

    The parent waits for the child, treats ``GetLastError() == 1223`` as a declined UAC prompt (exit 2) and prints
    the tail of the child's log. ``argv`` is the original argument list (``--elevate`` is removed here).
    """
    import ctypes
    from ctypes import wintypes

    ShellExecuteInfo = shell_execute_info_class()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    python = sys.executable or "python.exe"
    if os.path.basename(python).lower() == "pythonw.exe":
        python = os.path.join(os.path.dirname(python), "python.exe")
    arguments = [a for a in argv if a != "--elevate"] + ["--elevated-child"]
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(ShellExecuteInfo)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = python
    info.lpParameters = subprocess.list2cmdline([os.path.abspath(__file__), *arguments])
    info.lpDirectory = os.getcwd()
    info.nShow = SW_SHOWNORMAL
    ctx.say("Requesting administrator rights (UAC prompt)...")
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        if ctypes.get_last_error() == ERROR_CANCELLED:
            raise usage_error("the UAC prompt was declined; nothing was changed")
        raise InstallerError("could not start the elevated process (Windows error %d)" % ctypes.get_last_error())
    handle = info.hProcess
    pid = int(kernel32.GetProcessId(handle))
    kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
    code = wintypes.DWORD(1)
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    log = elevation_log_path(pid)
    if os.path.isfile(log):
        ctx.say("--- output of the elevated process (%s) ---" % log)
        for line in tail_lines(log, 40):
            ctx.say(line)
    return int(code.value)


# --------------------------------------------------------------------------------------------------------------------
# Platform backends (SPEC 10.2 Windows, 10.3 Linux, 10.4 macOS) behind one interface
# --------------------------------------------------------------------------------------------------------------------

TCC_RE = re.compile(r"^/Users/[^/]+/(Desktop|Documents|Downloads|Library/Mobile Documents|Library/CloudStorage)(/|$)")
SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def tcc_problem(path: str) -> Optional[str]:
    """Why a launchd daemon running as a normal user cannot read ``path`` (macOS TCC), else None (SPEC 10.4)."""
    if path.startswith("/Volumes/") or path == "/Volumes":
        return "is on a removable or network volume"
    if TCC_RE.match(path):
        return "is under Desktop/Documents/Downloads/iCloud Drive, which macOS denies to daemons"
    return None


def parse_schtasks_csv(text: str) -> Optional[Dict[str, str]]:
    """Parse ``schtasks /Query /FO CSV /NH /V`` by column index (the output is localised; the order is not)."""
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 12:
            return {
                "task": row[1], "next_run": row[2], "status": row[3], "last_run": row[5],
                "last_result": row[6], "state": row[11],
            }  # fmt: skip
    return None


class Backend(abc.ABC):
    """One operating system: how its service is defined, started, stopped, inspected and removed.

    Methods that change the machine go through ``self.ctx`` (dry-run aware); ``render`` and ``identity`` are pure.
    ``install_service`` registers the definition *and* (re)starts the service; the caller waits for health afterwards.
    """

    target = ""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # -- locations --------------------------------------------------------------------------------------------------

    def state_path(self) -> str:
        """Path of ``install.json`` for this OS."""
        return state_file_path(self.target, self.ctx.env)

    def default_data_dir(self) -> str:
        """Installer default data dir for this OS (outside the app tree)."""
        return default_data_dir(self.target, self.ctx.env)

    def log_files(self, s: Settings) -> List[str]:
        """Log files printed by ``logs`` and on a failed health check, most important first."""
        return [self.ctx.join(s.data_dir, "logs", "desktalk.log"), self.ctx.join(s.data_dir, "logs", "boot.log")]

    def show_logs(self, s: Settings, count: int) -> int:
        """Print the tail of every log file (``logs`` command). Returns an installer exit code."""
        readable = 0
        for path in self.log_files(s):
            self.ctx.say("===== %s (last %d lines) =====" % (path, count))
            try:
                lines = tail_lines(path, count)
            except PermissionError:
                self.ctx.say(
                    "(access denied: the data directory is private; run this from an elevated shell / as root)"
                )
                continue
            except OSError as exc:
                self.ctx.say("(cannot read: %s)" % exc)
                continue
            readable += 1
            for line in lines:
                self.ctx.say(line)
        return EXIT_OK if readable else EXIT_ERROR

    def recent_logs(self, s: Settings, count: int) -> List[str]:
        """Last ``count`` lines of each log file, for the health-failure report."""
        out: List[str] = []
        for path in self.log_files(s):
            try:
                lines = tail_lines(path, count)
            except OSError:
                continue
            out.append("--- %s ---" % path)
            out += lines
        return out

    # -- pure parts ---------------------------------------------------------------------------------------------------

    @abc.abstractmethod
    def identity(
        self, user_flag: Optional[str], state_user: Optional[str], run_as_system: Optional[bool]
    ) -> Tuple[str, str]:
        """Resolve ``(user, group)`` of the service identity from flags/state/environment."""

    @abc.abstractmethod
    def render(self, s: Settings) -> List[GeneratedFile]:
        """The service definition file(s) for ``s`` (Task XML / unit / plist)."""

    @abc.abstractmethod
    def advisories(self, s: Settings) -> List[str]:
        """Advice printed after install (SPEC 10.1); the installer changes none of these settings."""

    @abc.abstractmethod
    def leftovers(self, s: Settings) -> List[str]:
        """Copy-paste removal commands for what ``uninstall`` deliberately keeps."""

    # -- machine changes ------------------------------------------------------------------------------------------

    @abc.abstractmethod
    def preflight(self, s: Settings, harden: bool, force: bool) -> None:
        """Safety checks that may refuse the installation (exit 2) before anything is changed."""

    @abc.abstractmethod
    def prepare(self, s: Settings) -> None:
        """Create the data dir (and users/log dirs) with restrictive permissions, before the first start."""

    def finalize_permissions(self, s: Settings) -> None:  # noqa: B027 - optional hook, Windows ACLs inherit
        """Fix ownership of what root created (the macOS log file); a no-op where ACL inheritance does it."""

    @abc.abstractmethod
    def verify_runtime(self, s: Settings) -> None:
        """Check that the service identity can import ``chatd`` and write the data dir."""

    @abc.abstractmethod
    def chatd_command(self, s: Settings, args: Sequence[str]) -> Tuple[List[str], Optional[Dict[str, str]]]:
        """``(argv, env)`` that runs ``python -m chatd <args>`` as the service identity with the installed options."""

    @abc.abstractmethod
    def needs_privilege_for_cli(self, s: Settings) -> bool:
        """True when ``cli --`` must run elevated / as root."""

    @abc.abstractmethod
    def install_service(self, s: Settings, files: Sequence[GeneratedFile], previous: Optional[Settings]) -> None:
        """Write the definition, register it and (re)start the service.

        ``previous`` are the settings of the installation being replaced (None on a first install): the running
        server must be stopped with *its* port and data dir, which may differ from the new ones.
        """

    @abc.abstractmethod
    def start(self, s: Settings) -> None:
        """Start the service."""

    @abc.abstractmethod
    def stop(self, s: Settings) -> None:
        """Stop the service (gracefully where the OS allows)."""

    @abc.abstractmethod
    def restart(self, s: Settings) -> None:
        """Stop and start the service."""

    @abc.abstractmethod
    def status_lines(self, s: Settings) -> List[str]:
        """Best-effort service-manager status (``/healthz`` stays the ground truth)."""

    @abc.abstractmethod
    def uninstall_service(self, s: Settings) -> None:
        """Stop the service and remove its definition (data is never touched)."""

    @abc.abstractmethod
    def apply_firewall(self, s: Settings, previous: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        """Open the port; return the record stored in the state file (None when no firewall was touched)."""

    @abc.abstractmethod
    def remove_firewall(self, record: Mapping[str, Any]) -> None:
        """Remove exactly the rule described by a state-file record."""

    # -- shared helpers -------------------------------------------------------------------------------------------

    def control_file(self, s: Settings, name: str) -> str:
        """Path inside ``<data>/control``."""
        return self.ctx.join(s.data_dir, "control", name)

    def common_advice(self, s: Settings) -> List[str]:
        """The advisories that apply to every OS (static IP, localhost, certificate warning)."""
        return [
            "Give this PC a static IP or a DHCP reservation, otherwise http://<ip>:%d changes after a router reboot."
            % s.port,
            "On the server PC itself open %s://localhost:%d/ (a secure context: notifications, clipboard)."
            % (s.scheme, s.port),
            "Staff browsers show 'Not secure' without --tls because there is no internet certificate: expected."
            if not s.tls
            else "Staff browsers show a one-time certificate warning for the self-signed certificate: expected.",
        ]


class WindowsBackend(Backend):
    """Task Scheduler task ``DeskTalk`` running as LocalService (or SYSTEM), started at boot (SPEC 10.2)."""

    target = "windows"

    def state_dir(self) -> str:
        """``%ProgramData%\\DeskTalk``: the state file and the task XML copy live here."""
        return ntpath.dirname(self.state_path())

    def identity(
        self, user_flag: Optional[str], state_user: Optional[str], run_as_system: Optional[bool]
    ) -> Tuple[str, str]:
        if user_flag:
            raise usage_error(
                "--user exists on Linux/macOS only; the Windows task runs as LocalService (or --run-as-system)"
            )
        system = run_as_system if run_as_system is not None else state_user == WIN_SYSTEM
        return (WIN_SYSTEM if system else WIN_LOCAL_SERVICE), ""

    def render(self, s: Settings) -> List[GeneratedFile]:
        path = ntpath.join(self.state_dir(), "DeskTalk.task.xml")
        return [GeneratedFile(path, windows_task_xml(s), windows_task_xml_text(s), 0o644)]

    def advisories(self, s: Settings) -> List[str]:
        return [
            "Keep the PC awake: run these yourself (the installer never changes power settings):\n"
            "       powercfg /change standby-timeout-ac 0\n"
            "       powercfg /change hibernate-timeout-ac 0\n"
            "     The server holds a keep-awake request while running; lid-close and manual sleep still stop the chat.",
            *self.common_advice(s),
        ]

    def leftovers(self, s: Settings) -> List[str]:
        return [
            'Data (messages, uploads, backups, TLS key, logs): rmdir /s /q "%s"' % s.data_dir,
            "Python (%s): remove it via Settings > Apps if nothing else needs it." % s.python,
        ]

    # -- ACL safety check (SPEC 10.2) ---------------------------------------------------------------------------------

    def acl_findings(self, paths: Sequence[str]) -> Dict[str, List[AclFinding]]:
        """``icacls <path> /save`` each path and return the unsafe allow ACEs per path (empty dict entries omitted)."""
        ctx = self.ctx
        found: Dict[str, List[AclFinding]] = {}
        folder = tempfile.mkdtemp(prefix="desktalk-acl-") if not ctx.dry_run else "<tmp>"
        try:
            for path in paths:
                dump = os.path.join(folder, "acl.txt")
                result = ctx.run(["icacls", path, "/save", dump])
                if result.dry:
                    continue
                if result.returncode != 0:
                    detail = (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[-1]
                    raise InstallerError("icacls could not read the ACL of %s: %s" % (path, detail))
                with open(dump, "rb") as handle:
                    sddl = extract_sddl(handle.read())
                if sddl is None:
                    raise InstallerError("could not find an ACL in the icacls output for %s" % path)
                findings = unsafe_aces(sddl)
                if findings:
                    found[path] = findings
        finally:
            if not ctx.dry_run:
                shutil.rmtree(folder, ignore_errors=True)
        return found

    def acl_gate(self, paths: Sequence[str], harden: bool) -> None:
        """Refuse (exit 2) when Everyone/Users/Authenticated Users/Interactive can write a path; --harden fixes it."""
        ctx = self.ctx
        paths = _unique(paths)
        found = self.acl_findings(paths)
        if ctx.dry_run:
            if harden:
                for path in paths:
                    ctx.show("if the check fails for %s, --harden runs:" % path)
                    ctx.run(icacls_harden_command(path))
            return
        if not found:
            return
        for path, findings in found.items():
            for finding in findings:
                ctx.say("UNSAFE: %s: %s can write (%s)" % (path, finding.name, describe_mask(finding.mask)))
        hardening = "\n".join("    " + ctx.fmt(icacls_harden_command(p)) for p in found)
        if not harden:
            raise usage_error(
                "refusing to install: local users could modify code that runs as a service.\n"
                "Harden the directories (SYSTEM/Administrators write, others read-only) with:\n%s\n"
                "or re-run this command with --harden to apply it." % hardening
            )
        for path in found:
            ctx.say("Hardening %s ..." % path)
            ctx.run(icacls_harden_command(path), check=True, what="icacls (harden)")
        remaining = self.acl_findings(list(found))
        if remaining:
            raise usage_error("the ACL of %s is still writable by broad groups after hardening" % ", ".join(remaining))

    def preflight(self, s: Settings, harden: bool, force: bool) -> None:
        ctx = self.ctx
        lowered = s.app_root.lower().replace("/", "\\")
        if "\\users\\" in lowered:
            ctx.warn(
                "the application directory is under C:\\Users; LocalService normally cannot read profile folders. "
                "Move the app to e.g. C:\\DeskTalk if the service does not come up."
            )
        self.acl_gate([s.app_root, ntpath.dirname(s.python)], harden)

    def prepare(self, s: Settings) -> None:
        ctx = self.ctx
        state_dir = self.state_dir()
        ctx.makedirs(state_dir)
        ctx.run(icacls_state_dir_command(state_dir), check=True, what="icacls (state directory)")
        ctx.makedirs(s.data_dir)
        ctx.run(icacls_data_command(s.data_dir, s.run_as_system), check=True, what="icacls (data directory)")
        self.acl_gate([s.data_dir], harden=False)

    def verify_runtime(self, s: Settings) -> None:
        """Nothing to verify: the interpreter was validated and the data dir ACL grants the service account access."""

    def chatd_command(self, s: Settings, args: Sequence[str]) -> Tuple[List[str], Optional[Dict[str, str]]]:
        env = dict(self.ctx.env)
        env.update(service_env(s))
        return [s.python, "-X", "utf8", "-m", "chatd", *args], env

    def needs_privilege_for_cli(self, s: Settings) -> bool:
        return True  # the data dir ACL denies Users

    # -- service control ----------------------------------------------------------------------------------------------

    def install_service(self, s: Settings, files: Sequence[GeneratedFile], previous: Optional[Settings]) -> None:
        ctx = self.ctx
        if ctx.dry_run:
            ctx.show("if the task already exists, the stop sequence runs first:")
            exists = True
        else:
            exists = ctx.run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
        if exists:
            self.stop(previous or s)
        definition = files[0]
        ctx.write_file(definition.path, definition.data, definition.mode, definition.text)
        ctx.run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", definition.path, "/F"],
            check=True,
            what="schtasks /Create",
        )
        ctx.say("Task '%s' registered as %s; it starts at boot, before anyone logs in." % (TASK_NAME, s.user))
        self.start(s)

    def start(self, s: Settings) -> None:
        ctx = self.ctx
        ctx.remove_file(self.control_file(s, "stop.request"))  # a leftover would stop the new process at its first poll
        ctx.run(["schtasks", "/Change", "/TN", TASK_NAME, "/ENABLE"], check=True, what="schtasks /Change /ENABLE")
        ctx.run(["schtasks", "/Run", "/TN", TASK_NAME], check=True, what="schtasks /Run")

    def stop(self, s: Settings) -> None:
        """Disable (so the watchdog cannot revive it), write ``stop.request``, wait for the port, then ``/End``."""
        ctx = self.ctx
        disabled = ctx.run(["schtasks", "/Change", "/TN", TASK_NAME, "/DISABLE"])
        if disabled.returncode != 0:
            detail = (disabled.stderr.strip() or disabled.stdout.strip() or "no output").splitlines()[-1]
            ctx.warn("could not disable the task (not installed, or access denied): %s" % detail)
        if ctx.dry_run or ctx.exists(s.data_dir):
            request = self.control_file(s, "stop.request")
            ctx.makedirs(ntpath.dirname(request))
            ctx.write_file(request, b"stop\n")
        ctx.show("wait up to %.0f s until port %d refuses connections" % (STOP_WAIT_S, s.port))
        if not ctx.dry_run and not wait_port_closed(ctx, s.host, s.port, STOP_WAIT_S):
            ctx.warn("the server is still listening after %.0f s; ending the task (hard stop)" % STOP_WAIT_S)
        ctx.run(["schtasks", "/End", "/TN", TASK_NAME])
        if not ctx.dry_run and not wait_port_closed(ctx, s.host, s.port, STOP_RECHECK_S):
            raise InstallerError("port %d is still open after ending the task; check what is listening" % s.port)

    def restart(self, s: Settings) -> None:
        self.stop(s)
        self.start(s)

    def status_lines(self, s: Settings) -> List[str]:
        result = self.ctx.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH", "/V"])
        row = parse_schtasks_csv(result.stdout) if result.returncode == 0 else None
        if row is None:
            detail = (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[-1:]
            return ["task: unavailable (not installed or access denied): %s" % (detail[0] if detail else "no output")]
        return [
            "task: %s status=%s state=%s last result=%s last run=%s next run=%s"
            % (row["task"], row["status"], row["state"], row["last_result"], row["last_run"], row["next_run"])
        ]

    def uninstall_service(self, s: Settings) -> None:
        ctx = self.ctx
        self.stop(s)
        ctx.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)
        ctx.remove_file(ntpath.join(self.state_dir(), "DeskTalk.task.xml"))

    # -- firewall -------------------------------------------------------------------------------------------------

    def apply_firewall(self, s: Settings, previous: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        ctx = self.ctx
        # `delete rule` returns 1 both when the rule is absent and when not elevated; install runs elevated here.
        deleted = ctx.run(NETSH_DELETE_RULE)
        if deleted.returncode not in (0, 1):
            raise InstallerError("netsh could not delete the old firewall rule (exit %d)" % deleted.returncode)
        ctx.run(netsh_add_rule(s), check=True, what="netsh advfirewall add rule")
        return dict(s.firewall or {})

    def remove_firewall(self, record: Mapping[str, Any]) -> None:
        self.ctx.run(NETSH_DELETE_RULE, ok=(0, 1))


class _PosixBackend(Backend):
    """Shared logic of the systemd and launchd backends: unprivileged service user, chown, runuser/sudo."""

    def user_exists(self, user: str) -> bool:
        """True when ``user`` exists on this host (always True for a foreign dry-run)."""
        if self.ctx.foreign:
            return True
        try:
            import pwd

            pwd.getpwnam(user)
        except (ImportError, KeyError):
            return False
        return True

    def group_of(self, user: str, default: str) -> str:
        """Primary group name of ``user`` (``default`` when the user does not exist yet or on a foreign host)."""
        if self.ctx.foreign:
            return default
        try:
            import grp
            import pwd

            return str(grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
        except (ImportError, KeyError):
            return default

    def is_current_user(self, user: str) -> bool:
        """True when this process already runs as ``user`` (no runuser/sudo needed)."""
        if self.ctx.foreign:
            return False
        try:
            import pwd

            return os.geteuid() == pwd.getpwnam(user).pw_uid
        except (AttributeError, ImportError, KeyError):
            return False

    def as_user(self, s: Settings, argv: Sequence[str], env: Optional[Mapping[str, str]] = None) -> List[str]:
        """Wrap ``argv`` so it runs as the service user, passing ``env`` explicitly (sudo/runuser reset it)."""
        prefix: List[str] = []
        if not self.is_current_user(s.user):
            if self.target == "linux" and self.ctx.which("runuser"):
                prefix = ["runuser", "-u", s.user, "--"]
            else:
                prefix = ["sudo", "-n", "-u", s.user, "--"]
        if env:
            prefix += ["env"] + ["%s=%s" % (key, value) for key, value in sorted(env.items())]
        return prefix + list(argv)

    def identity_for(
        self, user_flag: Optional[str], state_user: Optional[str], fallback: Optional[str]
    ) -> Tuple[str, str]:
        """``--user`` > state > ``SUDO_USER`` (unless root) > ``fallback``; never root."""
        sudo_user = self.ctx.env.get("SUDO_USER")
        user = user_flag or state_user or (sudo_user if sudo_user and sudo_user != "root" else None) or fallback
        if not user:
            if not self.ctx.dry_run:
                raise usage_error(
                    "cannot tell which user the service should run as: run via sudo from your normal account "
                    "or pass --user USER (the service never runs as root)"
                )
            self.ctx.warn("dry-run: no --user and no SUDO_USER; showing the placeholder user '%s'" % SERVICE_USER)
            user = SERVICE_USER
        if user == "root":
            raise usage_error("the service must not run as root; pass --user with an unprivileged account")
        reject_control_chars("--user", user)
        return user, self.group_of(user, user)

    def preflight(self, s: Settings, harden: bool, force: bool) -> None:
        if harden:
            raise usage_error("--harden applies to Windows ACLs only")
        if not self.user_exists(s.user) and s.user != SERVICE_USER:
            raise usage_error("user %r does not exist; create it or choose another with --user" % s.user)

    def make_data_dir(self, s: Settings) -> None:
        """``install -d -o <user> -g <group> -m 0700 <data>``."""
        self.ctx.run(
            ["install", "-d", "-o", s.user, "-g", s.group, "-m", "0700", s.data_dir], check=True, what="install -d"
        )

    def finalize_permissions(self, s: Settings) -> None:
        self.ctx.run(["chown", "-R", "%s:%s" % (s.user, s.group), s.data_dir], check=True, what="chown")

    def verify_runtime(self, s: Settings) -> None:
        ctx = self.ctx
        if ctx.dry_run:
            ctx.run(self.as_user(s, [s.python, "-c", CHATD_CHECK_CODE]), cwd=s.app_root)
            ctx.run(self.as_user(s, ["test", "-w", s.data_dir]))
            return
        problem = ctx.run(self.as_user(s, [s.python, "-c", CHATD_CHECK_CODE]), cwd=s.app_root)
        if problem.returncode != 0:
            raise usage_error(self._access_hint(s, "cannot import chatd from %s" % s.app_root, problem))
        writable = ctx.run(self.as_user(s, ["test", "-w", s.data_dir]))
        if writable.returncode != 0:
            raise usage_error(self._access_hint(s, "cannot write the data directory %s" % s.data_dir, writable))

    def _access_hint(self, s: Settings, what: str, result: Result) -> str:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        return (
            "user %s %s (%s).\nMove the application to a world-readable place such as /opt/desktalk, or install with "
            "--user <your own user>. On RHEL/Fedora with SELinux enforcing the app must live outside /home."
            % (s.user, what, detail)
        )

    def chatd_command(self, s: Settings, args: Sequence[str]) -> Tuple[List[str], Optional[Dict[str, str]]]:
        return self.as_user(s, [s.python, "-X", "utf8", "-m", "chatd", *args], service_env(s)), None

    def needs_privilege_for_cli(self, s: Settings) -> bool:
        return not self.is_current_user(s.user)

    def firewall_ports(self, record: Mapping[str, Any]) -> List[int]:
        """Ports recorded for a ufw/firewalld rule."""
        ports = record.get("ports")
        if isinstance(ports, list):
            return [p for p in ports if isinstance(p, int)]
        return [record["port"]] if isinstance(record.get("port"), int) else []


class LinuxBackend(_PosixBackend):
    """systemd unit ``desktalk`` (SPEC 10.3)."""

    target = "linux"
    unit_path = "/etc/systemd/system/desktalk.service"

    def identity(
        self, user_flag: Optional[str], state_user: Optional[str], run_as_system: Optional[bool]
    ) -> Tuple[str, str]:
        if run_as_system:
            raise usage_error("--run-as-system exists on Windows only")
        return self.identity_for(user_flag, state_user, SERVICE_USER)

    def render(self, s: Settings) -> List[GeneratedFile]:
        text = systemd_unit(s, self.ctx.which("systemd-inhibit"))
        return [GeneratedFile(self.unit_path, text.encode("utf-8"), text, 0o644)]

    def advisories(self, s: Settings) -> List[str]:
        keep = (
            "Sleep: the unit is not wrapped in systemd-inhibit (--allow-sleep or systemd-inhibit missing); "
            "configure the machine not to suspend, e.g. HandleLidSwitch=ignore in logind.conf."
            if s.allow_sleep or not self.ctx.which("systemd-inhibit")
            else "Sleep: the unit runs under systemd-inhibit (sleep:idle), but lid-close or a manual suspend still "
            "stops the chat; set HandleLidSwitch=ignore in /etc/systemd/logind.conf on laptops."
        )
        return [keep, *self.common_advice(s)]

    def leftovers(self, s: Settings) -> List[str]:
        lines = ['Data (messages, uploads, backups, TLS key, logs): rm -rf "%s"' % s.data_dir]
        if s.user == SERVICE_USER:
            lines.append("The service user (optional): userdel %s" % SERVICE_USER)
        lines.append("Python (%s) is a system package and is left alone." % s.python)
        return lines

    def prepare(self, s: Settings) -> None:
        ctx = self.ctx
        if s.user == SERVICE_USER:
            nologin = ctx.which("nologin") or "/usr/sbin/nologin"
            create = [
                "useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
                "--shell", nologin, SERVICE_USER,
            ]  # fmt: skip
            if ctx.dry_run:
                ctx.show("$ getent passwd %s || %s" % (SERVICE_USER, ctx.fmt(create)))
            elif ctx.run(["getent", "passwd", SERVICE_USER]).returncode != 0:
                ctx.run(create, check=True, what="useradd")
        self.make_data_dir(s)

    def install_service(self, s: Settings, files: Sequence[GeneratedFile], previous: Optional[Settings]) -> None:
        ctx = self.ctx
        unit = files[0]
        existed = ctx.exists(unit.path)
        ctx.write_file(unit.path, unit.data, unit.mode, unit.text)
        ctx.run(["systemctl", "daemon-reload"], check=True, what="systemctl daemon-reload")
        if existed:
            ctx.run(["systemctl", "enable", UNIT_NAME], check=True, what="systemctl enable")
            ctx.run(["systemctl", "restart", UNIT_NAME], check=True, what="systemctl restart")
        else:
            ctx.run(["systemctl", "enable", "--now", UNIT_NAME], check=True, what="systemctl enable --now")

    def start(self, s: Settings) -> None:
        self.ctx.run(["systemctl", "start", UNIT_NAME], check=True, what="systemctl start")

    def stop(self, s: Settings) -> None:
        self.ctx.run(["systemctl", "stop", UNIT_NAME], check=True, what="systemctl stop")

    def restart(self, s: Settings) -> None:
        self.ctx.run(["systemctl", "restart", UNIT_NAME], check=True, what="systemctl restart")

    def status_lines(self, s: Settings) -> List[str]:
        active = self.ctx.run(["systemctl", "is-active", UNIT_NAME])
        shown = self.ctx.run(
            [
                "systemctl",
                "show",
                UNIT_NAME,
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
                "-p",
                "ExecMainStatus",
            ]
        )
        detail = " ".join(shown.stdout.split())
        return ["unit %s: %s (%s)" % (UNIT_NAME, active.stdout.strip() or active.stderr.strip() or "unknown", detail)]

    def show_logs(self, s: Settings, count: int) -> int:
        return self.ctx.run(["journalctl", "-u", UNIT_NAME, "-n", str(count), "--no-pager"], stream=True).returncode

    def recent_logs(self, s: Settings, count: int) -> List[str]:
        journal = self.ctx.run(["journalctl", "-u", UNIT_NAME, "-n", str(count), "--no-pager"])
        return ["--- journalctl -u %s ---" % UNIT_NAME, *journal.stdout.splitlines(), *super().recent_logs(s, count)]

    def uninstall_service(self, s: Settings) -> None:
        ctx = self.ctx
        ctx.run(["systemctl", "disable", "--now", UNIT_NAME])
        ctx.remove_file(self.unit_path)
        ctx.run(["systemctl", "daemon-reload"])
        ctx.run(["systemctl", "reset-failed", UNIT_NAME])

    def apply_firewall(self, s: Settings, previous: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        ctx = self.ctx
        record: Dict[str, Any] = {"port": s.port, "scope": "any", "profile": "any", "ports": s.ports}
        if ctx.dry_run:
            ctx.show("if `ufw status` starts with 'Status: active':")
            for port in s.ports:
                ctx.run(["ufw", "allow", "%d/tcp" % port])
            ctx.show("elif `firewall-cmd --state` prints 'running':")
            for port in s.ports:
                ctx.run(["firewall-cmd", "--permanent", "--add-port=%d/tcp" % port])
            ctx.run(["firewall-cmd", "--reload"])
            record["tool"] = "ufw"
            return record
        if previous and self.firewall_ports(previous) != s.ports:
            self.remove_firewall(previous)
        if ctx.which("ufw") and ctx.run(["ufw", "status"]).stdout.startswith("Status: active"):
            for port in s.ports:
                ctx.run(["ufw", "allow", "%d/tcp" % port], check=True, what="ufw allow")
            record["tool"] = "ufw"
            return record
        if ctx.which("firewall-cmd") and ctx.run(["firewall-cmd", "--state"]).stdout.strip() == "running":
            for port in s.ports:
                ctx.run(["firewall-cmd", "--permanent", "--add-port=%d/tcp" % port], check=True, what="firewall-cmd")
            ctx.run(["firewall-cmd", "--reload"], check=True, what="firewall-cmd --reload")
            record["tool"] = "firewalld"
            return record
        ctx.say(
            "Note: no active ufw/firewalld found. If a firewall is in use, allow TCP port(s) %s yourself." % s.ports
        )
        return None

    def remove_firewall(self, record: Mapping[str, Any]) -> None:
        ctx = self.ctx
        ports = self.firewall_ports(record)
        if record.get("tool") == "ufw":
            for port in ports:
                ctx.run(["ufw", "delete", "allow", "%d/tcp" % port])
        elif record.get("tool") == "firewalld":
            for port in ports:
                ctx.run(["firewall-cmd", "--permanent", "--remove-port=%d/tcp" % port])
            ctx.run(["firewall-cmd", "--reload"])


class MacBackend(_PosixBackend):
    """launchd daemon ``com.desktalk.server`` (SPEC 10.4)."""

    target = "macos"
    plist_path = "/Library/LaunchDaemons/com.desktalk.server.plist"
    system_target = "system/" + LAUNCHD_LABEL

    def identity(
        self, user_flag: Optional[str], state_user: Optional[str], run_as_system: Optional[bool]
    ) -> Tuple[str, str]:
        if run_as_system:
            raise usage_error("--run-as-system exists on Windows only")
        user, group = self.identity_for(user_flag, state_user, None)
        return user, self.group_of(user, "staff")

    def render(self, s: Settings) -> List[GeneratedFile]:
        data = launchd_plist(s)
        return [GeneratedFile(self.plist_path, data, data.decode("utf-8"), 0o644)]

    def log_files(self, s: Settings) -> List[str]:
        return [launchd_log_path(s), self.ctx.join(s.data_dir, "logs", "desktalk.log")]

    def advisories(self, s: Settings) -> List[str]:
        sleep = (
            "Sleep: --allow-sleep was given, the daemon is not wrapped in caffeinate."
            if s.allow_sleep
            else "Sleep: the daemon runs under caffeinate -i (no idle sleep); lid-close or manual sleep still stops it."
        )
        return [sleep, *self.common_advice(s)]

    def leftovers(self, s: Settings) -> List[str]:
        return [
            'Data (messages, uploads, backups, TLS key, logs): sudo rm -rf "%s"' % s.data_dir,
            "Python (%s) is left alone." % s.python,
        ]

    def preflight(self, s: Settings, harden: bool, force: bool) -> None:
        super().preflight(s, harden, force)
        for label, path in (("the application directory", s.app_root), ("the data directory", s.data_dir)):
            problem = tcc_problem(path)
            if problem and not force:
                raise usage_error(
                    "%s %s %s.\nA launchd daemon running as a normal user is denied these locations (macOS TCC). "
                    "Move it to /usr/local/desktalk or /Users/Shared/DeskTalk, or pass --force to install anyway."
                    % (label, path, problem)
                )
            if problem:
                self.ctx.warn("%s %s %s; --force given, continuing" % (label, path, problem))

    def prepare(self, s: Settings) -> None:
        ctx = self.ctx
        self.make_data_dir(s)
        logs = ctx.join(s.data_dir, "logs")
        ctx.run(["install", "-d", "-o", s.user, "-g", s.group, "-m", "0700", logs], check=True, what="install -d")
        log = launchd_log_path(s)
        if ctx.dry_run or not ctx.exists_fn(log):
            ctx.write_file(log, b"", 0o600)

    def _bootstrap(self, s: Settings) -> None:
        ctx = self.ctx
        if ctx.macos_version() < (10, 10):
            ctx.run(["launchctl", "load", "-w", self.plist_path], check=True, what="launchctl load")
            return
        ctx.run(["launchctl", "enable", self.system_target])
        booted = ctx.run(["launchctl", "bootstrap", "system", self.plist_path])
        if booted.returncode != 0 and ctx.run(["launchctl", "print", self.system_target]).returncode != 0:
            detail = (booted.stderr.strip() or booted.stdout.strip() or "no output").splitlines()[-1]
            raise InstallerError("launchctl bootstrap failed (exit %d): %s" % (booted.returncode, detail))

    def _bootout(self) -> None:
        ctx = self.ctx
        if ctx.macos_version() < (10, 10):
            ctx.run(["launchctl", "unload", "-w", self.plist_path])
        else:
            ctx.run(["launchctl", "bootout", self.system_target])

    def install_service(self, s: Settings, files: Sequence[GeneratedFile], previous: Optional[Settings]) -> None:
        ctx = self.ctx
        plist = files[0]
        ctx.write_file(plist.path, plist.data, plist.mode, plist.text)
        # launchctl bootstrap fails with "Input/output error" unless the plist is root:wheel 0644.
        ctx.run(["chown", "root:wheel", plist.path], check=True, what="chown")
        ctx.run(["chmod", "0644", plist.path], check=True, what="chmod")
        self._bootout()
        self._bootstrap(s)

    def start(self, s: Settings) -> None:
        self._bootstrap(s)

    def stop(self, s: Settings) -> None:
        self._bootout()

    def restart(self, s: Settings) -> None:
        if self.ctx.run(["launchctl", "kickstart", "-k", self.system_target]).returncode != 0:
            self._bootstrap(s)  # the job was not loaded: restarting it means loading it

    def status_lines(self, s: Settings) -> List[str]:
        result = self.ctx.run(["launchctl", "print", self.system_target])
        if result.returncode != 0:
            return ["launchd: %s is not loaded" % self.system_target]
        state = re.search(r"^\s*state = (\S+)", result.stdout, re.MULTILINE)
        pid = re.search(r"^\s*pid = (\d+)", result.stdout, re.MULTILINE)
        return [
            "launchd: %s state=%s pid=%s"
            % (self.system_target, state.group(1) if state else "unknown", pid.group(1) if pid else "none")
        ]

    def uninstall_service(self, s: Settings) -> None:
        self._bootout()
        self.ctx.remove_file(self.plist_path)

    def firewall_binary(self, python: str) -> str:
        """The real executable the Application Firewall sees (the Mach-O inside Python.app for framework builds)."""
        real = python if self.ctx.foreign else self.ctx.realpath_fn(python)
        match = re.match(r"^(.*/Python\.framework/Versions/[^/]+)/", real)
        if match:
            app_binary = match.group(1) + "/Resources/Python.app/Contents/MacOS/Python"
            if self.ctx.foreign or self.ctx.exists_fn(app_binary):
                return app_binary
        return real

    def apply_firewall(self, s: Settings, previous: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        ctx = self.ctx
        binary = self.firewall_binary(s.python)
        record = {"port": s.port, "scope": "any", "profile": "any", "ports": s.ports,
                  "tool": "socketfilterfw", "binary": binary}  # fmt: skip
        ctx.say("Application Firewall: a daemon cannot show the 'Allow incoming connections' prompt, so the "
                "interpreter is added and unblocked explicitly.")  # fmt: skip
        state = ctx.run([SOCKETFILTERFW, "--getglobalstate"])
        if not ctx.dry_run and "enabled" not in state.stdout.lower():
            ctx.say("The Application Firewall is off; nothing to do.")
            return None
        ctx.run([SOCKETFILTERFW, "--add", binary], check=True, what="socketfilterfw --add")
        ctx.run([SOCKETFILTERFW, "--unblockapp", binary], check=True, what="socketfilterfw --unblockapp")
        check = ctx.run([SOCKETFILTERFW, "--getappblocked", binary])
        if check.stdout.strip():
            ctx.say(check.stdout.strip())
        return record

    def remove_firewall(self, record: Mapping[str, Any]) -> None:
        binary = record.get("binary")
        if record.get("tool") == "socketfilterfw" and isinstance(binary, str):
            self.ctx.run([SOCKETFILTERFW, "--remove", binary])


def make_backend(ctx: Context) -> Backend:
    """The backend for ``ctx.target``."""
    return {"windows": WindowsBackend, "linux": LinuxBackend, "macos": MacBackend}[ctx.target](ctx)


# --------------------------------------------------------------------------------------------------------------------
# Settings resolution (flags > state file > defaults)
# --------------------------------------------------------------------------------------------------------------------


def load_state(ctx: Context, backend: Backend) -> Optional[Dict[str, Any]]:
    """The saved install state of this machine (None for a foreign dry-run, a fresh machine or a damaged file)."""
    return None if ctx.foreign else read_state(backend.state_path())


def _from_state(state: Optional[Mapping[str, Any]], key: str, kind: Any) -> Any:
    """Typed lookup in the state file; booleans never pass as integers."""
    value = state.get(key) if state else None
    if isinstance(value, bool) and kind is not bool:
        return None
    return value if isinstance(value, kind) else None


def resolve_settings(
    ctx: Context, backend: Backend, args: argparse.Namespace, state: Optional[Mapping[str, Any]], installing: bool
) -> Settings:
    """Merge flags, the state file and defaults into ``Settings`` (the interpreter is chosen separately).

    ``args`` may be a bare ``Namespace``: every option is read with ``getattr(..., None)``. When not installing
    (start/stop/status/...) the app root and interpreter come from the state file.
    """

    def flag(name: str) -> Any:
        return getattr(args, name, None)

    def pick(name: str, key: str, kind: Any, default: Any) -> Any:
        if flag(name) is not None:
            return flag(name)
        saved = _from_state(state, key, kind)
        return default if saved is None else saved

    mod = pathmod(ctx.target)
    saved_root = _from_state(state, "app_root", str)
    app_root = ctx.norm(flag("app_root") or (ctx.app_root if installing else saved_root or ctx.app_root))
    if flag("data_dir"):
        data_dir = ctx.norm(flag("data_dir"))
    elif _from_state(state, "data_dir", str):
        data_dir = _from_state(state, "data_dir", str)
    else:
        local = mod.join(app_root, "data")
        data_dir = local if ctx.exists(mod.join(local, "chat.db")) else backend.default_data_dir()
    port = pick("port", "port", int, DEFAULT_PORT)
    allowed = flag("allowed_host")
    if allowed is None:
        allowed = [h for h in (_from_state(state, "allowed_hosts", list) or []) if isinstance(h, str)]
    try:
        user, group = backend.identity(flag("user"), _from_state(state, "user", str), flag("run_as_system"))
    except InstallerError:
        if installing:
            raise
        user, group = "", ""

    previous = _from_state(state, "firewall", dict)
    if ctx.target == "windows":
        scope = normalise_scope(flag("allow_from")) if flag("allow_from") else (previous or {}).get("scope")
        profile = (
            normalise_profile(flag("firewall_profile")) if flag("firewall_profile") else (previous or {}).get("profile")
        )
        scope, profile = scope or DEFAULT_ALLOW_FROM, profile or DEFAULT_FW_PROFILE
    else:
        scope = profile = "any"
    desired: Dict[str, Any] = {"port": port, "scope": scope, "profile": profile}

    return Settings(
        target=ctx.target,
        python=ctx.norm(flag("python")) if flag("python") else (_from_state(state, "python", str) or sys.executable),
        app_root=app_root,
        data_dir=data_dir,
        host=pick("host", "host", str, DEFAULT_HOST),
        port=port,
        tls=pick("tls", "tls", bool, False),
        redirect_port=pick("redirect_port", "redirect_port", int, 0),
        user=user,
        group=group,
        name=pick("name", "name", str, DEFAULT_NAME),
        allowed_hosts=_unique(allowed),
        allow_sleep=pick("allow_sleep", "allow_sleep", bool, False),
        firewall=previous if flag("no_firewall") else desired,
    )


# --------------------------------------------------------------------------------------------------------------------
# Shared command helpers
# --------------------------------------------------------------------------------------------------------------------


def require_privilege(ctx: Context, args: argparse.Namespace, raw: Sequence[str], needed: bool = True) -> Optional[int]:
    """Return None when the command may proceed, else the exit code to finish with.

    Dry-run never needs privileges. With ``--elevate`` (Windows) the command is re-run through a UAC prompt and the
    child's exit code is returned; otherwise the exact steps to get a privileged shell are printed (exit 2).
    """
    if not needed or ctx.dry_run or ctx.is_admin():
        return None
    if getattr(args, "elevate", False):
        if ctx.host != "windows":
            raise usage_error("--elevate exists on Windows only; use sudo on Linux/macOS")
        return elevate_and_wait(ctx, list(raw))
    for line in privilege_instructions(ctx, raw):
        ctx.say(line)
    return EXIT_USAGE


def settings_summary(s: Settings) -> List[str]:
    """Human readable settings block."""
    listen = "%s:%d (%s)" % (s.host, s.port, s.scheme)
    if s.redirect_port:
        listen += ", plain-HTTP redirect on port %d" % s.redirect_port
    lines = [
        "  python        : %s" % s.python,
        "  application   : %s" % s.app_root,
        "  data directory: %s" % s.data_dir,
        "  listen        : %s" % listen,
        "  workspace name: %s" % s.name,
        "  runs as       : %s" % (s.user + (":" + s.group if s.group else "")),
        "  keep awake    : %s" % ("no (--allow-sleep)" if s.allow_sleep else "yes"),
    ]
    if s.allowed_hosts:
        lines.append("  allowed hosts : %s" % ", ".join(s.allowed_hosts))
    return lines


def url_lines(ctx: Context, s: Settings) -> List[str]:
    """The URLs staff can use (SPEC 5.8 rule: primary address first, other adapters afterwards)."""
    wildcard = probe_host(s.host) == "127.0.0.1"
    if not wildcard:
        return ["Share this link: %s://%s:%d/" % (s.scheme, s.host, s.port)]
    primary, others = ctx.lan_fn()
    lines: List[str] = []
    if primary:
        lines.append("Share this link: %s://%s:%d/" % (s.scheme, primary, s.port))
    elif not others:
        lines.append("No network address yet (connect to the LAN, then re-run `status`).")
    if others:
        lines.append("Other adapters (may not be reachable by phones):")
        lines += ["  %s://%s:%d/" % (s.scheme, ip, s.port) for ip in others]
    return lines


def read_setup_code(ctx: Context, s: Settings) -> Optional[str]:
    """The one-time setup code while the server still has no admin (``<data>/setup_code.txt``)."""
    try:
        with open(ctx.join(s.data_dir, "setup_code.txt"), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def wait_for_health(ctx: Context, s: Settings, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Poll the health probe for ``timeout`` (default ``HEALTH_WAIT_S``) seconds; the ``/api/info`` dict or None."""
    deadline = time.monotonic() + (HEALTH_WAIT_S if timeout is None else timeout)
    while True:
        info = ctx.health_fn(s.host, s.port, s.tls, 2.0)
        if info is not None:
            return info
        if time.monotonic() >= deadline:
            return None
        ctx.sleep_fn(0.5)


def report_unhealthy(ctx: Context, backend: Backend, s: Settings) -> int:
    """Print the log tails and return exit code 3 (SPEC 10.1)."""
    ctx.say("ERROR: the server did not answer /healthz within %.0f s." % HEALTH_WAIT_S)
    lines = backend.recent_logs(s, 30)
    if lines:
        ctx.say("Last log lines:")
        for line in lines:
            ctx.say(line)
    else:
        ctx.say("No log output is readable yet (look in %s)." % ctx.join(s.data_dir, "logs"))
    return EXIT_UNHEALTHY


def report_ready(ctx: Context, s: Settings, info: Mapping[str, Any], admin: Optional[str]) -> None:
    """Print the post-start summary: URLs, setup code while ``needs_setup``, the next step."""
    ctx.say("DeskTalk is running (workspace %r)." % info.get("name", s.name))
    for line in url_lines(ctx, s):
        ctx.say(line)
    local = "%s://127.0.0.1:%d/" % (s.scheme, s.port)
    if info.get("needs_setup"):
        code = read_setup_code(ctx, s)
        if code:
            ctx.say("Setup code: %s" % code)
        ctx.say("next step: open %s and create the admin account." % local)
        ctx.say("  or from this shell: python service/install_service.py cli -- create-admin <username>")
    elif admin:
        ctx.say("Admin account '%s' exists; sign in at %s" % (admin, local))


def create_certificate(ctx: Context, backend: Backend, s: Settings) -> None:
    """``--tls``: run ``chatd tls-init`` as the service identity (SPEC 10.1); the installer has no TLS code of its own.

    ``tls-init`` needs no database and is idempotent (a valid certificate that covers every LAN address is kept), so
    re-running ``install`` never invalidates what devices already trust.
    """
    argv, env = backend.chatd_command(s, ["tls-init", "--data-dir", s.data_dir])
    result = ctx.run(argv, cwd=s.app_root, env=env, timeout=180)
    if result.dry:
        return
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[-1]
        raise InstallerError(
            "TLS was requested but no certificate could be created (tls-init exit %d): %s" % (result.returncode, detail)
        )
    for line in result.stdout.strip().splitlines():
        ctx.say(line)


def read_password_stdin() -> str:
    """One line from stdin without its newline (``--password-stdin``)."""
    line = sys.stdin.readline().rstrip("\r\n")
    if not line:
        raise usage_error("--password-stdin: no password received on stdin")
    return line


def prompt_new_password(ctx: Context) -> str:
    """Ask twice (hidden input) until both entries match."""
    for _attempt in range(3):
        first = ctx.getpass_fn("Admin password: ")
        if first and first == ctx.getpass_fn("Repeat password: "):
            return first
        ctx.say("The entries are empty or differ, try again.")
    raise usage_error("no matching password entered")


def first_admin_step(ctx: Context, backend: Backend, s: Settings, args: argparse.Namespace) -> Optional[str]:
    """SPEC 10.1 "First admin": create the admin before the first start so nobody can claim the setup screen.

    ``--admin USER`` (+ ``--password-stdin`` or a terminal prompt) is used as given. Otherwise an interactive
    terminal is asked "Create the admin account now? [Y/n]" unless a database already exists. The password only ever
    travels on the child's stdin. Returns the username created, or None.
    """
    username: Optional[str] = args.admin
    if username is None:
        if ctx.dry_run:
            ctx.show("would ask 'Create the admin account now? [Y/n]' (interactive terminal, no existing chat.db)")
            return None
        if not ctx.interactive or ctx.exists(ctx.join(s.data_dir, "chat.db")):
            return None
        if ctx.input_fn("Create the admin account now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return None
        username = ctx.input_fn("Admin username [admin]: ").strip() or "admin"
    if username.startswith("-"):
        raise usage_error("the admin user name must not start with '-'")
    attempt = 0
    while True:
        attempt += 1
        if ctx.dry_run:
            password = "<password>"
        elif args.password_stdin:
            password = read_password_stdin()
        elif ctx.interactive:
            password = prompt_new_password(ctx)
        else:
            raise usage_error("--admin needs --password-stdin when there is no terminal")
        argv, env = backend.chatd_command(s, ["create-admin", username, "--password-stdin", "--data-dir", s.data_dir])
        result = ctx.run(argv, cwd=s.app_root, env=env, input_text=password + "\n", what="create-admin")
        if result.returncode == 0:
            if not ctx.dry_run:
                ctx.say("Admin account '%s' is ready." % username)
            return username
        message = (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[-1]
        if args.password_stdin or not ctx.interactive or attempt >= 3:
            raise InstallerError("create-admin failed (exit %d): %s" % (result.returncode, message))
        ctx.say("create-admin failed: %s" % message)  # interactive: ask for another password


# --------------------------------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------------------------------


def cmd_print_config(ctx: Context, backend: Backend, args: argparse.Namespace, raw: Sequence[str]) -> int:
    """Print the generated unit / Task XML / plist for the given options (needs no privileges)."""
    state = load_state(ctx, backend)
    s = resolve_settings(ctx, backend, args, state, installing=True)
    validate_settings(s)
    for generated in backend.render(s):
        emit_block(generated.path, generated.text)
    ctx.say("# command: %s %s" % (s.python, ctx.fmt(serve_arguments(s))))
    return EXIT_OK


def cmd_install(ctx: Context, backend: Backend, args: argparse.Namespace, raw: Sequence[str]) -> int:
    """Install or update the service, start it and wait for ``/healthz`` (SPEC 10.1)."""
    state = load_state(ctx, backend)
    s = resolve_settings(ctx, backend, args, state, installing=True)
    refused = require_privilege(ctx, args, raw)
    if refused is not None:
        return refused
    if ctx.dry_run:
        ctx.say("DeskTalk installer - DRY RUN for %s: nothing is executed or written." % ctx.target)
    s = dataclasses.replace(s, python=choose_interpreter(ctx, args.python, _from_state(state, "python", str), None))
    validate_settings(s)
    if not ctx.foreign:
        for needed in (s.server_py, ctx.join(s.app_root, "chatd")):
            if ctx.exists_fn(needed):
                continue
            if not ctx.dry_run:
                raise usage_error("%s is missing: run the installer from a complete DeskTalk checkout" % needed)
            ctx.warn("%s does not exist on this machine (dry-run continues)" % needed)

    ctx.say("Settings:")
    for line in settings_summary(s):
        ctx.say(line)
    if state is None:
        ctx.say("No saved installation found: this is a first install.")
    else:
        changes = diff_state(state, s.to_state())
        ctx.say("Changes versus the saved installation:" if changes else "No change versus the saved installation.")
        for line in changes:
            ctx.say(line)
    if not s.tls and args.tls is not False:
        ctx.warn(
            "Plain HTTP: anyone on this network can read passwords and messages (SPEC section 0) - re-run with --tls"
        )
    if is_within(s.data_dir, s.app_root, ctx.target):
        ctx.warn("the data directory lies inside the application directory; keep data outside the app tree")

    backend.preflight(s, bool(args.harden), bool(args.force))
    backend.prepare(s)
    backend.finalize_permissions(s)
    backend.verify_runtime(s)
    if s.tls:
        create_certificate(ctx, backend, s)
    admin = first_admin_step(ctx, backend, s, args)

    previous = resolve_settings(ctx, backend, argparse.Namespace(), state, installing=False) if state else None
    backend.install_service(s, backend.render(s), previous)
    record = s.firewall
    if not args.no_firewall:
        record = backend.apply_firewall(s, _from_state(state, "firewall", dict))
    new_state = dataclasses.replace(s, firewall=record).to_state()
    state_text = json.dumps(new_state, indent=2) + "\n"
    ctx.write_file(backend.state_path(), state_text.encode("utf-8"), 0o644, state_text)

    if ctx.dry_run:
        ctx.show(
            "start the service, wait up to %.0f s for /healthz, then print URLs and the setup code" % HEALTH_WAIT_S
        )
        ctx.show("expected result: exit code 0")
    else:
        info = wait_for_health(ctx, s)
        if info is None:
            return report_unhealthy(ctx, backend, s)
        report_ready(ctx, s, info, admin)
    ctx.say("")
    ctx.say("Things to do next (the installer changes none of these settings):")
    for number, advice in enumerate(backend.advisories(s), start=1):
        ctx.say("  (%d) %s" % (number, advice))
    ctx.say("")
    ctx.say("Upgrade later: (1) python service/install_service.py stop; (2) replace only chatd/ web/ service/ docs/")
    ctx.say("  server.py, never data/; (3) python service/install_service.py install (reuses these options);")
    ctx.say("  (4) python -m chatd doctor")
    return EXIT_OK


def cmd_uninstall(ctx: Context, backend: Backend, args: argparse.Namespace, raw: Sequence[str]) -> int:
    """Stop and remove the service, its firewall rule (unless ``--keep-firewall``) and the state file. Data stays."""
    state = load_state(ctx, backend)
    s = resolve_settings(ctx, backend, args, state, installing=False)
    refused = require_privilege(ctx, args, raw)
    if refused is not None:
        return refused
    if state is None and not ctx.foreign:
        ctx.warn("no install state file found at %s; removing the service definition only" % backend.state_path())
    backend.uninstall_service(s)
    recorded = _from_state(state, "firewall", dict)
    if recorded and not args.keep_firewall:
        backend.remove_firewall(recorded)
    elif recorded:
        ctx.say("Firewall rule kept (--keep-firewall).")
    path = backend.state_path()
    ctx.remove_file(path)
    ctx.remove_empty_dir(pathmod(ctx.target).dirname(path))  # only when nothing else lives there (e.g. data/)
    ctx.say("DeskTalk service removed. Your data was NOT deleted. Leftovers you may remove yourself:")
    for line in backend.leftovers(s):
        ctx.say("  " + line)
    return EXIT_OK


def cmd_control(ctx: Context, backend: Backend, args: argparse.Namespace, raw: Sequence[str]) -> int:
    """``start`` / ``stop`` / ``restart``; start and restart wait for health."""
    action = args.command
    state = load_state(ctx, backend)
    s = resolve_settings(ctx, backend, args, state, installing=False)
    refused = require_privilege(ctx, args, raw)
    if refused is not None:
        return refused
    if state is None and not ctx.foreign:
        ctx.warn("no install state file found; using default settings (port %d, data dir %s)" % (s.port, s.data_dir))
    getattr(backend, action)(s)
    if action == "stop":
        ctx.say("DeskTalk stopped.")
        return EXIT_OK
    if ctx.dry_run:
        return EXIT_OK
    info = wait_for_health(ctx, s)
    if info is None:
        return report_unhealthy(ctx, backend, s)
    report_ready(ctx, s, info, None)
    return EXIT_OK


def cmd_status(ctx: Context, backend: Backend, args: argparse.Namespace, raw: Sequence[str]) -> int:
    """Service-manager status plus the health probe (the ground truth). Never needs elevation."""
    state = load_state(ctx, backend)
    s = resolve_settings(ctx, backend, args, state, installing=False)
    if state is None:
        ctx.say("No install state file at %s (showing defaults)." % backend.state_path())
    elif not ctx.exists(s.python):
        ctx.say("interpreter missing: %s (re-run `install` with --python)" % s.python)
    for line in backend.status_lines(s):
        ctx.say(line)
    info = ctx.health_fn(s.host, s.port, s.tls, 3.0)
    ctx.say("data directory: %s" % s.data_dir)
    if info is None:
        ctx.say(
            "health: NOT healthy (no valid answer from %s://%s:%d/healthz)" % (s.scheme, probe_host(s.host), s.port)
        )
        return EXIT_UNHEALTHY
    ctx.say(
        "health: OK - workspace %r, needs_setup=%s, tls=%s"
        % (info.get("name"), info.get("needs_setup"), info.get("tls"))
    )
    for line in url_lines(ctx, s):
        ctx.say(line)
    return EXIT_OK


def cmd_logs(ctx: Context, backend: Backend, args: argparse.Namespace, raw: Sequence[str]) -> int:
    """Print the tail of the service logs."""
    state = load_state(ctx, backend)
    s = resolve_settings(ctx, backend, args, state, installing=False)
    return backend.show_logs(s, max(1, args.lines))


def cmd_cli(
    ctx: Context, backend: Backend, args: argparse.Namespace, raw: Sequence[str], chatd_args: Sequence[str]
) -> int:
    """``cli -- <chatd args>``: run ``python -m chatd <args> --data-dir D`` as the service identity."""
    state = load_state(ctx, backend)
    s = resolve_settings(ctx, backend, args, state, installing=False)
    if state is None and not ctx.foreign:
        raise InstallerError("no install state file found (%s): install the service first" % backend.state_path())
    refused = require_privilege(ctx, args, raw, backend.needs_privilege_for_cli(s))
    if refused is not None:
        return refused
    if not ctx.foreign and not ctx.exists(s.python):
        raise InstallerError("interpreter missing: %s (re-run `install`)" % s.python)
    command = list(chatd_args)
    if not any(a == "--data-dir" or a.startswith("--data-dir=") for a in command):
        command += ["--data-dir", s.data_dir]
    argv, env = backend.chatd_command(s, command)
    return ctx.run(argv, cwd=s.app_root, env=env, stream=True).returncode


# --------------------------------------------------------------------------------------------------------------------
# Argument parsing and main
# --------------------------------------------------------------------------------------------------------------------


def _settings_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    add = parent.add_argument
    add("--port", type=int, help="TCP port (default 8765)")
    add("--host", help="bind address (default 0.0.0.0)")
    add("--data-dir", help="data directory (default: OS location outside the app tree)")
    add("--python", help="interpreter for the service (default: validated, discovered automatically)")
    add("--name", help="workspace name (default DeskTalk)")
    add("--user", help="Linux/macOS service user (default: SUDO_USER, else system user 'desktalk' on Linux)")
    add("--tls", dest="tls", action="store_const", const=True, default=None, help="serve HTTPS (certificate made now)")
    add("--no-tls", dest="tls", action="store_const", const=False, default=None, help="serve plain HTTP")
    add("--redirect-port", type=int, help="plain-HTTP port that redirects to HTTPS (needs --tls)")
    add("--allowed-host", action="append", help="extra Host name the server accepts (repeatable)")
    add(
        "--allow-sleep",
        dest="allow_sleep",
        action="store_const",
        const=True,
        default=None,
        help="do not keep the PC awake",
    )
    add(
        "--no-allow-sleep",
        dest="allow_sleep",
        action="store_const",
        const=False,
        default=None,
        help="keep the PC awake",
    )
    add(
        "--run-as-system",
        dest="run_as_system",
        action="store_const",
        const=True,
        default=None,
        help="Windows: run as SYSTEM",
    )
    add(
        "--no-run-as-system",
        dest="run_as_system",
        action="store_const",
        const=False,
        default=None,
        help="Windows: LocalService",
    )
    add("--no-firewall", action="store_true", help="do not touch the firewall")
    add("--allow-from", help="Windows firewall scope: localsubnet (default), any or CIDR[,CIDR]")
    add("--firewall-profile", help="Windows firewall profiles: private,domain (default) or any")
    add(
        "--app-root", help=argparse.SUPPRESS
    )  # testing aid: pretend the app lives elsewhere (dry-run on a foreign target)
    return parent


def _target_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--target", choices=TARGETS, help="generate for another OS (only with --dry-run / print-config)"
    )
    return parent


def _exec_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--dry-run", action="store_true", help="print every command and file, change nothing")
    parent.add_argument("--elevate", action="store_true", help="Windows: ask for administrator rights (UAC)")
    parent.add_argument("--elevated-child", action="store_true", help=argparse.SUPPRESS)
    return parent


def build_parser() -> argparse.ArgumentParser:
    """The command line of SPEC 10 (sub-commands: install uninstall start stop restart status logs print-config cli)."""
    parser = argparse.ArgumentParser(
        prog="install_service.py",
        description="Install and control the DeskTalk server as a Windows task / systemd unit / launchd daemon.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    settings, target, execute = _settings_parent(), _target_parent(), _exec_parent()

    install = sub.add_parser("install", parents=[settings, target, execute], help="install or update the service")
    install.add_argument("--harden", action="store_true", help="Windows: fix unsafe ACLs instead of refusing")
    install.add_argument("--admin", metavar="USER", help="create this admin account before the first start")
    install.add_argument("--password-stdin", action="store_true", help="read the --admin password from stdin")
    install.add_argument("--force", action="store_true", help="macOS: install despite TCC-protected locations")
    uninstall = sub.add_parser("uninstall", parents=[target, execute], help="remove the service (data is kept)")
    uninstall.add_argument("--keep-firewall", action="store_true", help="leave the firewall rule in place")
    for name, text in (
        ("start", "start the service"),
        ("stop", "stop the service"),
        ("restart", "restart the service"),
    ):
        sub.add_parser(name, parents=[target, execute], help=text)
    sub.add_parser("status", help="service state and health check")
    logs = sub.add_parser("logs", help="show the service logs")
    logs.add_argument("-n", "--lines", type=int, default=100, help="lines to show (default 100)")
    sub.add_parser("print-config", parents=[settings, target], help="show the generated unit / Task XML / plist")
    sub.add_parser(
        "cli", parents=[target, execute], help="cli -- <chatd args>: run python -m chatd as the service user"
    )
    return parser


def parse_args(argv: Sequence[str]) -> Tuple[argparse.Namespace, List[str]]:
    """Parse ``argv``; for ``cli`` everything after the first ``--`` is chatd's. Usage errors raise SystemExit(2)."""
    parser = build_parser()
    head = list(argv)
    chatd_args: List[str] = []
    if head and head[0] == "cli" and "--" in head:
        cut = head.index("--")
        head, chatd_args = head[:cut], head[cut + 1 :]
    args = parser.parse_args(head)
    if args.command == "cli" and not chatd_args:
        parser.error("cli needs '--' followed by the chatd command, e.g. cli -- create-admin alice")
    if getattr(args, "target", None) and not (getattr(args, "dry_run", False) or args.command == "print-config"):
        parser.error("--target is only valid together with --dry-run or print-config")
    if getattr(args, "password_stdin", False) and not args.admin:
        parser.error("--password-stdin needs --admin USER")
    if getattr(args, "password_stdin", False) and getattr(args, "elevate", False):
        parser.error("--password-stdin cannot be combined with --elevate (stdin is not passed to the elevated window)")
    return args, chatd_args


HANDLERS = {
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "start": cmd_control,
    "stop": cmd_control,
    "restart": cmd_control,
    "status": cmd_status,
    "logs": cmd_logs,
    "print-config": cmd_print_config,
}


def run_command(args: argparse.Namespace, chatd_args: Sequence[str], raw: Sequence[str]) -> int:
    """Build the context and backend for ``args`` and run the command."""
    host = detect_host()
    target = getattr(args, "target", None) or host
    if target == "other":
        raise usage_error(
            "unsupported operating system %r; preview with --dry-run --target windows|linux|macos" % sys.platform
        )
    ctx = Context(
        target,
        dry_run=bool(getattr(args, "dry_run", False)),
        host=host,
        app_root=getattr(args, "app_root", None),
    )
    return dispatch(ctx, make_backend(ctx), args, chatd_args, raw)


def dispatch(
    ctx: Context, backend: Backend, args: argparse.Namespace, chatd_args: Sequence[str], raw: Sequence[str]
) -> int:
    """Run the command named by ``args.command`` and return its exit code (``InstallerError`` propagates)."""
    if args.command == "cli":
        return cmd_cli(ctx, backend, args, raw, chatd_args)
    return HANDLERS[args.command](ctx, backend, args, raw)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns the exit code (0 ok, 1 error, 2 usage/privilege/refused, 3 unhealthy)."""
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args, chatd_args = parse_args(raw)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    child = bool(getattr(args, "elevated_child", False))
    saved_streams = (sys.stdout, sys.stderr)
    log_handle = None
    if child:  # elevated window: mirror the output into %TEMP% so the parent can show it
        log_handle = open(elevation_log_path(os.getpid()), "a", encoding="utf-8")  # noqa: SIM115 - closed in finally
        sys.stdout, sys.stderr = TeeStream(sys.stdout, log_handle), TeeStream(sys.stderr, log_handle)
    try:
        code = run_command(args, chatd_args, raw)
    except InstallerError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        code = exc.code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        code = EXIT_ERROR
    finally:
        if child:
            try:
                input("Press Enter to close")
            except EOFError:
                pass
            sys.stdout, sys.stderr = saved_streams
            if log_handle is not None:
                log_handle.close()
    return code


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    sys.exit(main())
