"""``python -m chatd doctor``: diagnose an installation (SPEC 2.2, 10.1, 10.2).

Every check returns :class:`Row` objects (``PASS``/``WARN``/``FAIL``/``INFO``); :func:`run` prints them as a table and
returns the exit code: ``0`` when the server can run, ``78`` when a ``FAIL`` is an environment or configuration
problem (SPEC 2.2: sqlite3 missing or old, unusable data dir, WAL-incompatible location, database newer than the
code), ``1`` for any other ``FAIL`` (port taken by a foreign process, damaged database ...).

Rules of this module:

* No SQL. Facts about ``chat.db`` come from ``maintenance.schema_info``; ``sqlite3``, ``maintenance``, ``tlsutil`` and
  ``ssl`` are imported inside functions, so ``doctor`` still runs (and reports it) when they are unusable.
* It never changes the machine. The only writes are a throw-away probe file in the data directory (or its nearest
  existing parent) and a short-lived listening socket on the configured port.
* ``health_probe`` and the SDDL helpers are small pure copies of the ones in ``service/install_service.py``: that
  script imports nothing from this package (SPEC 6.2) and a test asserts both probes behave identically.
* External tools are parsed locale-independently (exit codes, enum names, hex numbers, column positions).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import locale
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from . import __version__, util
from .config import Config

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"

APP_DIR = Path(__file__).resolve().parent.parent
MIN_SQLITE = (3, 24)
MIN_PYTHON = (3, 8)
NOFILE_WARN = 4096
TLS_RENEW_DAYS = 30
EXIT_FAIL = 1
EXIT_ENVIRONMENT = 78

#: The SPEC 2.1 keys in table order (the ``Config`` attribute names).
CONFIG_KEYS = (
    "host", "port", "data_dir", "workspace_name", "registration_open", "max_users", "max_upload_mb",
    "blocked_extensions", "tls", "redirect_port", "allowed_hosts", "allow_sleep", "backup_dir", "edit_window_s",
    "delete_window_s", "max_body_chars", "session_days", "min_password_len", "scrypt_n", "log_level", "test_scale",
    "test_limits",
)  # fmt: skip

#: Folder names of cloud-sync clients: SQLite WAL needs shared memory and locks that sync clients break.
_CLOUD_FOLDERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "mobile documents", "box sync")
_CLOUD_ENV_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
_NETWORK_FS = frozenset(("nfs", "nfs4", "cifs", "smb", "smb2", "smb3", "smbfs", "9p", "afs", "ncpfs", "fuse.sshfs"))


class Row(NamedTuple):
    """One line of the report. ``environment`` marks a FAIL that maps to exit code 78."""

    level: str
    name: str
    detail: str
    environment: bool = False


class CommandResult(NamedTuple):
    """Outcome of :func:`run_command`: rc 124 = timed out, 127 = could not start."""

    returncode: int
    stdout: str
    stderr: str


# --------------------------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------------------------


def os_kind() -> str:
    """``windows``, ``macos`` or ``linux`` (every other POSIX system is treated like Linux)."""
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "latin-1", "replace")


def run_command(argv: List[str], timeout: float = 15.0) -> CommandResult:
    """Run a command without a console window; never raises (missing tool: rc 127, timeout: rc 124)."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "timed out after %.0f s" % timeout)
    except OSError as exc:
        return CommandResult(127, "", "cannot run %s: %s" % (argv[0], exc))
    return CommandResult(proc.returncode, _decode(proc.stdout), _decode(proc.stderr))


def probe_host(host: str) -> str:
    """Host to probe for a given bind host: loopback for wildcard/loopback binds, else the value itself (SPEC 10.1)."""
    value = host.strip()
    if value in ("", "0.0.0.0", "::", "localhost") or value.startswith("127."):
        return "127.0.0.1"
    return value


def _http_get(host: str, port: int, path: str, context: Any, timeout: float) -> Tuple[int, bytes]:
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

    ``host`` is mapped with :func:`probe_host`; with ``tls`` the connection uses an unverified context (loopback
    health check only: a self-signed certificate is expected). Any network, TLS, protocol or JSON problem yields None.
    ``service/install_service.py`` carries an identical copy (SPEC 6.2).
    """
    context: Any = None
    if tls:
        import ssl

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


def bind_error(host: str, port: int) -> Optional[OSError]:
    """Try to bind ``host:port`` the way the server does; ``None`` when it works, else the ``OSError``.

    Windows: no ``SO_REUSEADDR`` (asyncio sets it only on POSIX; on Windows it would allow port hijacking and report
    "free" for a port that is in use). POSIX: ``SO_REUSEADDR`` so a lingering ``TIME_WAIT`` does not count as taken.
    """
    try:
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        return exc
    try:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host or "0.0.0.0", port))
    except OSError as exc:
        return exc
    finally:
        sock.close()
    return None


def format_age(seconds: float) -> str:
    """``45 s`` / ``3 min`` / ``5 h`` / ``2 days``."""
    if seconds < 90:
        return "%d s" % seconds
    if seconds < 5400:
        return "%d min" % round(seconds / 60)
    if seconds < 36 * 3600:
        return "%d h" % round(seconds / 3600)
    return "%d days" % round(seconds / 86400)


def file_owner(path: str) -> str:
    """Owner of ``path`` for the "run it as that account" hint (best effort; only used on an access error)."""
    if os.name == "posix":
        try:
            import pwd

            return pwd.getpwuid(os.stat(path).st_uid).pw_name
        except (OSError, KeyError, ImportError):
            return "unknown"
    script = "(Get-Acl -LiteralPath $args[0]).Owner"
    result = run_command(["powershell", "-NoProfile", "-NonInteractive", "-Command", script, path], timeout=20)
    return result.stdout.strip() or "unknown"


def _nearest_existing(path: str) -> str:
    current = os.path.abspath(path)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def _under(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` or lies below it (case-insensitive on Windows, separator aware)."""
    left = os.path.normcase(os.path.abspath(path))
    right = os.path.normcase(os.path.abspath(root))
    return left == right or left.startswith(right.rstrip("\\/") + os.sep)


# --------------------------------------------------------------------------------------------------------------
# WAL-incompatible locations (SPEC 2.2)
# --------------------------------------------------------------------------------------------------------------


def wal_hazard(path: str, env: Mapping[str, str], mounts: Optional[str] = None) -> Optional[str]:
    """Why SQLite WAL cannot live at ``path`` (UNC share, network file system, cloud-sync folder), else None.

    ``mounts`` is the text of ``/proc/mounts`` (Linux); ``env`` supplies the ``OneDrive*`` roots on Windows.
    """
    flat = path.replace("/", "\\")
    if flat.startswith("\\\\") and not flat.startswith(("\\\\?\\", "\\\\.\\")) and os.name == "nt":
        return "a UNC network share (SQLite WAL does not work over the network)"
    for part in (p for p in path.replace("\\", "/").lower().split("/") if p):
        for marker in _CLOUD_FOLDERS:
            if part == marker or part.startswith((marker + " ", marker + "-")):
                return "inside a cloud-sync folder (%s): sync clients lock and rewrite database files" % marker
    for var in _CLOUD_ENV_VARS:
        root = env.get(var)
        if root and _under(path, root):
            return "inside the OneDrive folder %s: sync clients lock and rewrite database files" % root
    best: Tuple[int, str] = (-1, "")
    target = path if path.startswith("/") else os.path.abspath(path)  # a POSIX path stays one on any host
    for line in (mounts or "").splitlines():
        fields = line.split()
        if len(fields) >= 3 and (target == fields[1] or target.startswith(fields[1].rstrip("/") + "/")):
            best = max(best, (len(fields[1]), fields[2]))
    if best[1] in _NETWORK_FS:
        return "on a network file system (%s): SQLite WAL needs local shared memory" % best[1]
    return None


def drive_is_remote(path: str) -> bool:
    """Windows: True when the drive letter of ``path`` is a mapped network drive (``GetDriveTypeW`` == DRIVE_REMOTE)."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if os.name != "nt" or len(drive) != 2:
        return False
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")) == 4
    except (AttributeError, OSError):
        return False


# --------------------------------------------------------------------------------------------------------------
# Windows ACLs: a pure SDDL reader (SPEC 10.2). `icacls /save` writes UTF-16 LE WITHOUT a BOM.
# --------------------------------------------------------------------------------------------------------------

_SDDL_RIGHTS = {
    "GA": 0x10000000, "GR": 0x80000000, "GW": 0x40000000, "GX": 0x20000000, "RC": 0x20000, "SD": 0x10000,
    "WD": 0x40000, "WO": 0x80000, "CC": 0x1, "DC": 0x2, "LC": 0x4, "SW": 0x8, "RP": 0x10, "WP": 0x20, "DT": 0x40,
    "LO": 0x80, "CR": 0x100, "FA": 0x1F01FF, "FR": 0x120089, "FW": 0x120116, "FX": 0x1200A0,
}  # fmt: skip
_ALIASES = {"WD": "S-1-1-0", "AU": "S-1-5-11", "BU": "S-1-5-32-545", "IU": "S-1-5-4"}
BROAD_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
    "S-1-5-4": "Interactive",
}
READ_MASK = 0x1 | 0x10000000 | 0x80000000  # FILE_LIST_DIRECTORY/READ_DATA, GENERIC_ALL, GENERIC_READ
WRITE_MASK = 0x2 | 0x4 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000  # the SPEC 10.2 list


def decode_icacls_dump(raw: bytes) -> str:
    """Decode an ``icacls /save`` file: UTF-16 LE without a BOM in practice (a BOM or UTF-8 are tolerated)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    if b"\x00" in raw[:32]:
        return raw.decode("utf-16-le", "replace")
    return _decode(raw)


def extract_sddl(raw: bytes) -> Optional[str]:
    """The SDDL line of an ``icacls /save`` file (the object name precedes it; a drive root has an empty name)."""
    for line in decode_icacls_dump(raw).splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if re.match(r"^[OGDS]:(?![\\/])", stripped):
            return stripped
    return None


def _rights(text: str) -> int:
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
        mask |= _SDDL_RIGHTS.get(value[index : index + 2].upper(), 0)
    return mask


def dacl_aces(sddl: str) -> List[Tuple[str, int, str]]:
    """``(ACE type, access mask, trustee SID)`` of every ACE in the DACL of ``sddl`` (malformed entries skipped)."""
    marks: List[int] = []
    depth = 0
    for index, char in enumerate(sddl):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char in "OGDS" and sddl[index + 1 : index + 2] == ":":
            marks.append(index)
    dacl = ""
    for position, mark in enumerate(marks):
        if sddl[mark] == "D":
            end = marks[position + 1] if position + 1 < len(marks) else len(sddl)
            dacl = sddl[mark + 2 : end]
    aces: List[Tuple[str, int, str]] = []
    depth = 0
    start = 0
    for index, char in enumerate(dacl):
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == ")":
            depth -= 1
            fields = dacl[start:index].split(";", 6) if depth == 0 else []
            if len(fields) >= 6:
                trustee = fields[5].strip().upper()
                aces.append((fields[0].strip().upper(), _rights(fields[2]), _ALIASES.get(trustee, trustee)))
    return aces


def broad_access(sddl: str, mask: int) -> List[Tuple[str, int]]:
    """``(group, rights)`` of every allow ACE giving Everyone/Users/Authenticated Users/Interactive any of ``mask``."""
    return [
        (BROAD_SIDS[sid], rights)
        for kind, rights, sid in dacl_aces(sddl)
        if kind in ("A", "OA", "XA") and sid in BROAD_SIDS and rights & mask
    ]


# --------------------------------------------------------------------------------------------------------------
# Parsers for the tools that name the owner of a port, and for powercfg
# --------------------------------------------------------------------------------------------------------------


def parse_netstat_listeners(text: str, port: int) -> List[int]:
    """PIDs listening on ``port`` from ``netstat -ano -p tcp`` (column positions only; the state word is localised)."""
    pids: List[int] = []
    for line in text.splitlines():
        cells = line.split()
        listening = len(cells) >= 5 and cells[0].upper() == "TCP" and cells[2].endswith(":0")
        if listening and cells[1].endswith(":%d" % port) and cells[-1].isdigit() and int(cells[-1]) not in pids:
            pids.append(int(cells[-1]))
    return pids


def parse_ss_listeners(text: str, port: int) -> List[Tuple[str, int]]:
    """``(process name, pid)`` of listeners on ``port`` from ``ss -ltnp`` (pid 0 when the owner is hidden)."""
    found: List[Tuple[str, int]] = []
    for line in text.splitlines():
        cells = line.split()
        if len(cells) >= 5 and cells[3].endswith(":%d" % port):
            match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            found.append((match.group(1), int(match.group(2))) if match else ("a hidden process", 0))
    return found


def parse_lsof_listeners(text: str) -> List[Tuple[str, int, str]]:
    """``(command, pid, user)`` from ``lsof -nP -iTCP:<port> -sTCP:LISTEN``."""
    found: List[Tuple[str, int, str]] = []
    for line in text.splitlines():
        cells = line.split()
        if len(cells) >= 3 and cells[1].isdigit():
            found.append((cells[0], int(cells[1]), cells[2]))
    return found


def port_owner(port: int) -> str:
    """Name the process that listens on ``port`` (SPEC 10.1: netstat / ss / lsof), or say why it is unknown."""
    kind = os_kind()
    if kind == "windows":
        pids = parse_netstat_listeners(run_command(["netstat", "-ano", "-p", "tcp"]).stdout, port)
        names = []
        for pid in pids:
            task = run_command(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
            first = task.stdout.strip().split(",")[0].strip('"') if task.returncode == 0 else ""
            names.append("%s (pid %d)" % (first or "an unknown program", pid))
        return ", ".join(names) or "an unknown process (try an elevated shell)"
    if kind == "linux":
        owners = parse_ss_listeners(run_command(["ss", "-ltnp"]).stdout, port)
        return ", ".join("%s (pid %d)" % o if o[1] else o[0] for o in owners) or "an unknown process (try as root)"
    macs = parse_lsof_listeners(run_command(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"]).stdout)
    return ", ".join("%s (pid %d, user %s)" % m for m in macs) or "an unknown process (try with sudo)"


def parse_powercfg_index(text: str) -> Optional[int]:
    """AC value (seconds) of a ``powercfg /query`` block: the hex numbers are min, max, increment, AC, DC in order."""
    values = re.findall(r"0x[0-9a-fA-F]{8}", text)
    return int(values[-2], 16) if len(values) >= 2 else None


# --------------------------------------------------------------------------------------------------------------
# Checks. Each takes what it needs and returns Rows; facts shared between checks travel in a plain dict.
# --------------------------------------------------------------------------------------------------------------


def check_python() -> List[Row]:
    where = "%d.%d.%d (%s)" % (*sys.version_info[:3], sys.executable or "unknown path")
    if sys.version_info[:2] < MIN_PYTHON:
        return [Row(FAIL, "Python", where + ": DeskTalk needs Python 3.8 or newer", True)]
    rows = [Row(PASS, "Python", where)]
    if os_kind() == "windows" and "\\users\\" in (sys.executable or "").lower().replace("/", "\\"):
        detail = (
            "a per-user Python is fine for `serve`; the Windows service needs a machine-wide one "
            "(install Python for all users)"
        )
        rows.append(Row(INFO, "Python install", detail))
    return rows


def check_sqlite(facts: Dict[str, Any]) -> List[Row]:
    """sqlite3 module and version (FAIL below 3.24); ``facts['sqlite_ok']`` says whether database checks may run."""
    facts["sqlite_ok"] = False
    try:
        import sqlite3
    except ImportError as exc:
        detail = (
            "cannot be imported (%s): this Python is a broken or minimal build. "
            "Install a full Python 3.8+ (python.org installer) and run doctor again" % exc
        )
        return [Row(FAIL, "sqlite3", detail, True)]
    if tuple(sqlite3.sqlite_version_info) < MIN_SQLITE:
        detail = (
            "SQLite %s is too old: DeskTalk needs 3.24 or newer (UPSERT). Use a newer Python build"
            % sqlite3.sqlite_version
        )
        return [Row(FAIL, "sqlite3", detail, True)]
    facts["sqlite_ok"] = True
    return [Row(PASS, "sqlite3", "SQLite %s" % sqlite3.sqlite_version)]


def check_scrypt(cfg: Config) -> List[Row]:
    if not hasattr(hashlib, "scrypt"):
        detail = (
            "hashlib.scrypt is missing (OpenSSL < 1.1): passwords use PBKDF2-HMAC-SHA256 with 600000 "
            "iterations instead (works, a little slower)"
        )
        return [Row(WARN, "scrypt", detail)]
    n, r, p = cfg.scrypt_n, 8, 1
    maxmem = max(128 * 1024 * 1024, 128 * r * (n + p + 2) + (1 << 20))
    started = time.perf_counter()
    try:
        hashlib.scrypt(b"doctor", salt=bytes(16), n=n, r=r, p=p, dklen=32, maxmem=maxmem)
    except (ValueError, MemoryError) as exc:
        return [Row(FAIL, "scrypt", "n=%d does not work here (%s)" % (n, exc), True)]
    millis = (time.perf_counter() - started) * 1000
    detail = "available, n=%d costs %d ms per hash" % (n, millis)
    if millis > 1500:
        return [Row(WARN, "scrypt", detail + " (slow: logins will queue; this PC is under load or very small)")]
    return [Row(PASS, "scrypt", detail)]


def check_ssl() -> List[Row]:
    try:
        import ssl
    except ImportError as exc:
        return [Row(FAIL, "ssl", "cannot be imported (%s): the server needs it even for plain HTTP" % exc, True)]
    return [Row(PASS, "ssl", ssl.OPENSSL_VERSION)]


def _cert_files(cfg: Config) -> Tuple[Path, Path, Path]:
    tls_dir = Path(cfg.data_dir) / "tls"
    return tls_dir / "cert.pem", tls_dir / "key.pem", tls_dir / "meta.json"


def check_openssl(cfg: Config) -> List[Row]:
    """The ``openssl`` binary (SPEC 5.7) that creates the self-signed certificate."""
    try:
        from . import tlsutil

        exe = tlsutil.find_openssl()
    except ImportError as exc:  # no ssl module: reported by check_ssl
        return [Row(INFO, "openssl", "not checked (%s)" % type(exc).__name__)]
    if exe is not None:
        version = run_command([exe, "version"]).stdout.strip() or "version unknown"
        return [Row(PASS, "openssl", "%s - %s" % (exe, version))]
    cert, key, _meta = _cert_files(cfg)
    detail = "the openssl command was not found (PATH, Git for Windows, /usr/bin, Homebrew)"
    if not cfg.tls:
        return [Row(INFO, "openssl", detail + ": only needed for --tls")]
    if cert.exists() and key.exists():
        return [Row(WARN, "openssl", detail + ": the existing certificate cannot be renewed")]
    return [Row(FAIL, "openssl", detail + ": --tls is on but there is no certificate and none can be created")]


def check_data_dir(cfg: Config) -> List[Row]:
    """Location, writability of the directory and of ``chat.db``/``-wal``/``-shm``, free disk space (SPEC 2.2)."""
    data = str(cfg.data_dir)
    mounts = None
    if os_kind() == "linux":
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
                mounts = handle.read()
        except OSError:
            mounts = None
    hazard = wal_hazard(data, os.environ, mounts)
    if hazard is None and drive_is_remote(data):
        hazard = "on a mapped network drive (SQLite WAL does not work over the network)"
    if hazard:
        rows = [Row(FAIL, "WAL location", "%s is %s. Choose a local folder with --data-dir" % (data, hazard), True)]
    else:
        rows = [Row(PASS, "WAL location", "local folder, no network share or cloud-sync folder")]
    if _under(data, str(APP_DIR)):
        detail = (
            "%s lies inside the application folder (fine for development; installed services keep "
            "their data outside the app tree)" % data
        )
        rows.append(Row(INFO, "Data dir", detail))
    exists = os.path.isdir(data)
    probe_root = data if exists else _nearest_existing(data)
    try:
        handle_fd, probe = tempfile.mkstemp(prefix="doctor-", dir=probe_root)
        os.close(handle_fd)
        os.remove(probe)
    except OSError as exc:
        detail = (
            "%s is not writable by this account (%s). It belongs to %s: run doctor from that account or an "
            "elevated shell, or stop the service first (installed service: python "
            "service/install_service.py cli -- doctor)" % (probe_root, type(exc).__name__, file_owner(probe_root))
        )
        return [*rows, Row(FAIL, "Data dir", detail, True)]
    note = "" if exists else " (does not exist yet: created at the first start)"
    rows.append(Row(PASS, "Data dir", "%s is writable%s" % (data, note)))
    for name in ("chat.db", "chat.db-wal", "chat.db-shm"):
        path = os.path.join(data, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r+b"):
                pass
        except OSError as exc:
            detail = (
                "%s cannot be opened for writing by this account (%s). It belongs to %s: run from an "
                "elevated shell or as the service account, or stop the service"
                % (name, type(exc).__name__, file_owner(path))
            )
            rows.append(Row(FAIL, "Database file", detail, True))
        else:
            rows.append(Row(PASS, "Database file", "%s (%d KiB) is writable" % (name, os.path.getsize(path) // 1024)))
    try:
        usage = shutil.disk_usage(probe_root)
    except OSError:
        return rows
    fraction = usage.free / usage.total if usage.total else 1.0
    detail = "%.1f GiB free of %.1f GiB (%d%%)" % (usage.free / 2**30, usage.total / 2**30, round(fraction * 100))
    if fraction < 0.10:
        detail += ": below 10%, uploads are refused when less than 2 GiB or 5% would remain"
    rows.append(Row(WARN if fraction < 0.10 else PASS, "Disk space", detail))
    return rows


def _icacls(path: str) -> Tuple[Optional[str], str]:
    """``(SDDL, error text)`` of ``path`` through ``icacls /save`` into a temp file."""
    folder = tempfile.mkdtemp(prefix="doctor-acl-")
    try:
        dump = os.path.join(folder, "acl.txt")
        result = run_command(["icacls", path, "/save", dump])
        if result.returncode != 0:
            return None, (result.stderr.strip() or result.stdout.strip() or "icacls failed").splitlines()[-1]
        with open(dump, "rb") as handle:
            sddl = extract_sddl(handle.read())
        return sddl, "" if sddl else "no ACL found in the icacls output"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def check_windows_acl(cfg: Config) -> List[Row]:
    """Windows: FAIL when broad groups can read the data dir; WARN when they can modify the app or Python dir."""
    if os_kind() != "windows":
        return []
    data = str(cfg.data_dir)
    rows: List[Row] = []
    if os.path.isdir(data):
        sddl, problem = _icacls(data)
        leaks = broad_access(sddl, READ_MASK | WRITE_MASK) if sddl else []
        if sddl is None:
            rows.append(Row(WARN, "Data dir ACL", "could not read the ACL of %s (%s)" % (data, problem)))
        elif leaks:
            names = ", ".join(sorted({"%s (0x%x)" % item for item in leaks}))
            fix = (
                'icacls "%s" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" '
                '"*S-1-5-19:(OI)(CI)M"' % data
            )
            detail = "%s grants access to %s: other local accounts can read the chat database. Fix: %s" % (
                data,
                names,
                fix,
            )
            rows.append(Row(FAIL, "Data dir ACL", detail, True))
        else:
            detail = "Users, Everyone and Authenticated Users have no access to the data folder"
            rows.append(Row(PASS, "Data dir ACL", detail))
    for label, path in (("App folder ACL", str(APP_DIR)), ("Python folder ACL", os.path.dirname(sys.executable or ""))):
        if not path or not os.path.isdir(path):
            continue
        sddl, problem = _icacls(path)
        if sddl is None:
            rows.append(Row(INFO, label, "not checked (%s)" % problem))
            continue
        names = ", ".join(sorted({name for name, _rights in broad_access(sddl, WRITE_MASK)}))
        if names:
            detail = (
                "%s can modify %s. Harmless for a hand-started server, but a Windows service would run code "
                "those accounts can replace: `install_service.py install --harden` fixes it" % (names, path)
            )
            rows.append(Row(WARN, label, detail))
        else:
            rows.append(Row(PASS, label, "%s is not writable by Users/Everyone" % path))
    return rows


def check_nofile() -> List[Row]:
    """``RLIMIT_NOFILE`` (WARN when the server cannot reach 4096 descriptors)."""
    try:
        import resource
    except ImportError:
        return [Row(INFO, "Open files", "no RLIMIT_NOFILE on this OS")]
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    infinity = resource.RLIM_INFINITY

    def shown(value: int) -> str:
        return "unlimited" if value == infinity else str(value)

    detail = "soft %s, hard %s" % (shown(soft), shown(hard))
    if hard != infinity and hard < NOFILE_WARN:
        detail += (
            ": below 4096, the 800-connection cap cannot be honoured; raise the hard limit (ulimit -Hn / LimitNOFILE)"
        )
        return [Row(WARN, "Open files", detail)]
    if soft != infinity and soft < NOFILE_WARN:
        detail += " (the server raises the soft limit to min(hard, 8192) at startup)"
    return [Row(PASS, "Open files", detail)]


def _probe_server(cfg: Config, lock: Optional[Dict[str, Any]], with_configured: bool) -> Optional[Dict[str, Any]]:
    """Find a running DeskTalk: the port published in ``server.lock``, then (optionally) the configured port."""
    candidates: List[Tuple[int, bool]] = []
    lock_port = (lock or {}).get("port")
    if isinstance(lock_port, int) and not isinstance(lock_port, bool) and lock_port > 0:
        candidates.append((lock_port, bool((lock or {}).get("tls"))))
    if with_configured and cfg.port:
        candidates += [(cfg.port, cfg.tls), (cfg.port, not cfg.tls)]
    tried: List[Tuple[int, bool]] = []
    for port, tls in candidates:
        if (port, tls) in tried:
            continue
        tried.append((port, tls))
        info = health_probe(cfg.host, port, tls, 2.0)
        if info is not None:
            return {"info": info, "port": port, "tls": tls, "lock": lock or {}}
    return None


def _running_text(found: Dict[str, Any]) -> str:
    lock = found["lock"]
    return "DeskTalk is running (version %s, pid %s, %s port %d)" % (
        lock.get("version") or "unknown",
        lock.get("pid", "unknown"),
        "https" if found["tls"] else "http",
        found["port"],
    )


def check_ports(cfg: Config, facts: Dict[str, Any]) -> List[Row]:
    """Port check of SPEC 10.1: free, or taken by DeskTalk (version/pid from ``server.lock``), or by a stranger.

    Sets ``facts['running']`` to the running server (``info``, ``port``, ``tls``, ``lock``) or ``None``.
    """
    rows: List[Row] = []
    lock = util.read_lock_info(str(cfg.data_dir))
    running = _probe_server(cfg, lock, with_configured=False)
    if cfg.port == 0:
        rows.append(Row(INFO, "Port", "port 0 in the configuration: a free port is chosen at the start"))
    else:
        error = bind_error(cfg.host, cfg.port)
        if error is None:
            rows.append(Row(PASS, "Port", "%s:%d is free" % (cfg.host, cfg.port)))
            if running is not None:
                rows.append(Row(INFO, "Server", _running_text(running) + ": on another port than configured here"))
        else:
            running = running or _probe_server(cfg, lock, with_configured=True)
            if running is not None and running["port"] == cfg.port:
                rows.append(Row(PASS, "Port", _running_text(running)))
            elif os_kind() != "windows" and isinstance(error, PermissionError) and cfg.port < 1024:
                detail = "port %d needs root (or CAP_NET_BIND_SERVICE) here: %s" % (cfg.port, error)
                rows.append(Row(FAIL, "Port", detail, True))
            else:
                detail = "%s:%d cannot be bound (%s); it is used by %s. Stop that program or choose another --port" % (
                    cfg.host,
                    cfg.port,
                    error,
                    port_owner(cfg.port),
                )
                rows.append(Row(FAIL, "Port", detail))
    if cfg.redirect_port:
        error = bind_error(cfg.host, cfg.redirect_port)
        if error is None:
            rows.append(Row(PASS, "Redirect port", "%s:%d is free" % (cfg.host, cfg.redirect_port)))
        elif running is not None:
            rows.append(Row(INFO, "Redirect port", "%d is in use (by the running server)" % cfg.redirect_port))
        else:
            detail = "%d cannot be bound (%s); used by %s" % (cfg.redirect_port, error, port_owner(cfg.redirect_port))
            rows.append(Row(FAIL, "Redirect port", detail))
    if running is None and lock:
        detail = "stale (pid %s, port %s, version %s): nothing answers there" % (
            lock.get("pid", "?"),
            lock.get("port", "?"),
            lock.get("version", "?"),
        )
        rows.append(Row(INFO, "server.lock", detail))
    if running is None and os.path.exists(os.path.join(str(cfg.data_dir), "control", "stop.request")):
        rows.append(Row(INFO, "stop.request", "a stale stop request exists; it is deleted at the next start"))
    facts["running"] = running
    return rows


def _scheme(cfg: Config) -> str:
    return "https" if cfg.tls else "http"


def check_lan(cfg: Config) -> List[Row]:
    """The URLs staff can use (SPEC 5.8); a note when several adapters exist."""
    primary, others = util.lan_addresses()
    scheme = _scheme(cfg)
    suffix = "" if (scheme, cfg.port) in (("http", 80), ("https", 443)) or cfg.port == 0 else ":%d" % cfg.port
    if primary is None and not others:
        detail = "no network address yet: connect this PC to the network (the server still works on localhost)"
        return [Row(WARN, "LAN address", detail)]
    rows = []
    if cfg.host not in ("0.0.0.0", "::", ""):
        detail = "%s only (--host): other PCs reach %s://%s%s/" % (cfg.host, scheme, cfg.host, suffix)
        rows.append(Row(INFO, "Listening on", detail))
    rows.append(Row(INFO, "Share this link", "%s://%s%s/" % (scheme, primary or others[0], suffix)))
    remaining = others if primary else others[1:]
    if remaining:
        urls = ", ".join("%s://%s%s/" % (scheme, ip, suffix) for ip in remaining)
        detail = (
            "%s - several addresses exist; VPN/virtual adapters are usually not reachable by staff, so give "
            "the DHCP reservation to the primary one" % urls
        )
        rows.append(Row(INFO, "Other adapters", detail))
    return rows


def _state_file_path() -> str:
    kind = os_kind()
    if kind == "windows":
        return os.path.join(os.environ.get("PROGRAMDATA") or "C:\\ProgramData", "DeskTalk", "install.json")
    if kind == "macos":
        return "/Library/Application Support/DeskTalk/install.json"
    return "/etc/desktalk/install.json"


def read_install_state() -> Optional[Dict[str, Any]]:
    """The installer's ``install.json`` (SPEC 10.1), or None."""
    try:
        with open(_state_file_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_firewall(cfg: Config, state: Optional[Dict[str, Any]]) -> List[Row]:
    """Firewall facts; never a FAIL (the doctor cannot see the network between this PC and the clients)."""
    if cfg.port == 0:
        return []
    kind = os_kind()
    if kind == "windows":
        return _firewall_windows(cfg, state)
    if kind == "linux":
        return _firewall_linux(cfg)
    result = run_command(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if result.returncode == 0 and "enabled" in result.stdout.lower():
        detail = (
            "the macOS Application Firewall is on: the Python interpreter must be allowed (the installer does "
            "it; otherwise accept the 'incoming connections' prompt)"
        )
        return [Row(INFO, "Firewall", detail)]
    return [Row(INFO, "Firewall", "the macOS Application Firewall is off or not readable")]


def _firewall_windows(cfg: Config, state: Optional[Dict[str, Any]]) -> List[Row]:
    rows: List[Row] = []
    rule = run_command(["netsh", "advfirewall", "firewall", "show", "rule", "name=DeskTalk"])
    if rule.returncode != 0:  # exit code 1 = no such rule; the text is localised and never parsed
        detail = (
            "no rule named 'DeskTalk': other PCs may be blocked by Windows Firewall. "
            "`python service/install_service.py install` creates it (or allow python.exe in the Windows prompt)"
        )
        rows.append(Row(WARN, "Firewall rule", detail))
    elif re.search(r"(?<![0-9])%d(?![0-9])" % cfg.port, rule.stdout):
        rows.append(Row(PASS, "Firewall rule", "rule 'DeskTalk' exists and mentions port %d" % cfg.port))
    else:
        detail = "rule 'DeskTalk' exists but does not mention port %d: re-run the installer" % cfg.port
        rows.append(Row(WARN, "Firewall rule", detail))
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-NetFirewallProfile | ForEach-Object { 'fw:{0}={1}' -f $_.Name, $_.Enabled };"
        "Get-NetConnectionProfile | ForEach-Object { 'net:{0}={1}' -f $_.InterfaceAlias, $_.NetworkCategory }"
    )
    facts = run_command(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=30).stdout
    profiles = {m.group(1): m.group(2) for m in re.finditer(r"^fw:(\w+)=(\w+)", facts, re.MULTILINE)}
    categories = [m.group(1) for m in re.finditer(r"^net:.*=(\w+)\s*$", facts, re.MULTILINE)]
    off = sorted(name for name, value in profiles.items() if value.lower() == "false")
    if profiles:
        rows.append(Row(INFO, "Windows Firewall", ("off for: " + ", ".join(off)) if off else "on for every profile"))
    if "Public" in categories:
        covered = str(((state or {}).get("firewall") or {}).get("profile", "private,domain")).lower()
        fix = "Set-NetConnectionProfile -NetworkCategory Private, or install with --firewall-profile any"
        if rule.returncode != 0:
            detail = (
                "this PC is on a Public network: Windows treats it as untrusted and the installer's default "
                "rule covers only private/domain networks. " + fix
            )
            rows.append(Row(INFO, "Network category", detail))
        elif covered == "any" or "public" in covered:
            rows.append(Row(INFO, "Network category", "this PC is on a Public network; the DeskTalk rule covers it"))
        else:
            detail = (
                "this PC is on a Public network but the DeskTalk rule covers only %s: phones will not connect. %s"
                % (
                    covered,
                    fix,
                )
            )
            rows.append(Row(WARN, "Network category", detail))
    elif categories:
        rows.append(Row(PASS, "Network category", ", ".join(sorted(set(categories)))))
    return rows


def _firewall_linux(cfg: Config) -> List[Row]:
    pattern = r"(?<![0-9])%d(/tcp)?\b" % cfg.port
    ufw = shutil.which("ufw")
    if ufw:
        status = run_command([ufw, "status"])
        if status.stdout.startswith("Status: active"):
            if re.search(pattern, status.stdout):
                return [Row(PASS, "Firewall", "ufw is active and allows port %d" % cfg.port)]
            detail = "ufw is active but has no rule for port %d (sudo ufw allow %d/tcp)" % (cfg.port, cfg.port)
            return [Row(WARN, "Firewall", detail)]
        if status.returncode != 0:
            return [Row(INFO, "Firewall", "ufw is installed but its state needs root (run doctor with sudo to see it)")]
    firewalld = shutil.which("firewall-cmd")
    if firewalld and run_command([firewalld, "--state"]).stdout.strip() == "running":
        if "%d/tcp" % cfg.port in run_command([firewalld, "--list-ports"]).stdout.split():
            return [Row(PASS, "Firewall", "firewalld is running and allows port %d" % cfg.port)]
        detail = "firewalld is running but port %d is not open (firewall-cmd --permanent --add-port=%d/tcp)" % (
            cfg.port,
            cfg.port,
        )
        return [Row(WARN, "Firewall", detail)]
    return [Row(INFO, "Firewall", "no active ufw/firewalld found (an nftables/iptables policy is not inspected)")]


def check_power(cfg: Config) -> List[Row]:
    """Windows: AC standby/hibernate timeouts (SPEC 10.1); everywhere: what keeps the server awake."""
    rows: List[Row] = []
    if cfg.allow_sleep:
        rows.append(Row(WARN, "Keep awake", "allow_sleep is on: the PC may go to sleep and stop the chat"))
    if os_kind() != "windows":
        detail = (
            "the systemd/launchd wrapper blocks idle sleep (systemd-inhibit / caffeinate); lid-close and "
            "manual suspend still stop the chat"
        )
        return [*rows, Row(INFO, "Keep awake", detail)]
    for label, alias, command in (
        ("Standby timeout", "STANDBYIDLE", "powercfg /change standby-timeout-ac 0"),
        ("Hibernate timeout", "HIBERNATEIDLE", "powercfg /change hibernate-timeout-ac 0"),
    ):
        result = run_command(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", alias])
        seconds = parse_powercfg_index(result.stdout) if result.returncode == 0 else None
        if seconds is None:
            rows.append(Row(INFO, label, "could not read the power plan"))
        elif seconds == 0:
            rows.append(Row(PASS, label, "never (on AC power)"))
        else:
            detail = (
                "the PC sleeps after %s on AC power and the chat stops. To change it run (the installer never "
                "does): %s" % (format_age(seconds), command)
            )
            rows.append(Row(WARN, label, detail))
    detail = "serve holds a keep-awake request; lid-close and manual sleep still stop the chat"
    rows.append(Row(INFO, "Keep awake", detail))
    return rows


def check_database(cfg: Config, facts: Dict[str, Any]) -> List[Row]:
    """Application and schema version, journal mode and integrity from ``maintenance.schema_info`` (no SQL here).

    Sets ``facts['meta']`` to the stored ``workspace_name``/``registration_open`` (the ``db`` source of SPEC 2.1).
    """
    facts["meta"] = {}
    if not facts.get("sqlite_ok"):
        return [Row(FAIL, "Database", "cannot be checked: sqlite3 is unusable (see above)", True)]
    try:
        from . import maintenance

        info = maintenance.schema_info(str(cfg.data_dir))
    except ImportError as exc:
        return [Row(FAIL, "Database", "cannot be checked: %s" % exc, True)]
    code_version = info.get("code_schema_version")
    rows = [Row(INFO, "Version", "DeskTalk %s, database schema v%s" % (__version__, code_version))]
    facts["meta"] = {str(k): str(v) for k, v in (info.get("meta") or {}).items()}
    if not info.get("exists"):
        return [*rows, Row(INFO, "Database", "no database yet: %s is created at the first start" % info.get("path"))]
    if info.get("error"):
        return [*rows, Row(FAIL, "Database", "%s cannot be read: %s" % (info.get("path"), info["error"]), True)]
    stored = info.get("schema_version")
    if stored is None:
        rows.append(Row(WARN, "Schema", "the database has no schema version (empty or foreign file?)"))
    elif isinstance(code_version, int) and stored > code_version:
        detail = (
            "the database is schema v%d but this program only knows v%d: it is newer than the code (restore "
            "an older backup or upgrade DeskTalk)" % (stored, code_version)
        )
        rows.append(Row(FAIL, "Schema", detail, True))
    elif isinstance(code_version, int) and stored < code_version:
        detail = "schema v%d is migrated to v%d at the next start (a backup is written first)" % (stored, code_version)
        rows.append(Row(INFO, "Schema", detail))
    else:
        rows.append(Row(PASS, "Schema", "v%s matches this program" % stored))
    mode = str(info.get("journal_mode") or "").lower()
    if mode == "wal":
        rows.append(Row(PASS, "Journal mode", "wal"))
    else:
        detail = (
            "%s: the server switches the database to WAL at the next start and refuses to run when the file "
            "system cannot do WAL" % (mode or "unknown")
        )
        rows.append(Row(WARN, "Journal mode", detail))
    if info.get("integrity") == "ok":
        rows.append(Row(PASS, "Integrity", "integrity check: ok"))
    else:
        detail = "the database is damaged (%s): restore the newest backup (python -m chatd restore <backup>)" % (
            info.get("integrity")
        )
        rows.append(Row(FAIL, "Integrity", detail))
    return rows


def check_backups(cfg: Config) -> List[Row]:
    """Newest backup in the backup directory (informational: only a running server writes the automatic ones)."""
    directory = str(cfg.backup_dir)
    try:
        names = [n for n in os.listdir(directory) if n.endswith(".db") and not n.startswith("pre-")]
    except OSError:
        return []
    if not names:
        return [Row(INFO, "Backups", "none yet in %s (a running server writes one about every 20 hours)" % directory)]
    newest = max(names, key=lambda n: os.path.getmtime(os.path.join(directory, n)))
    age = format_age(time.time() - os.path.getmtime(os.path.join(directory, newest)))
    plural = "" if len(names) == 1 else "s"
    return [Row(INFO, "Backups", "newest %s, %s old (%d file%s in %s)" % (newest, age, len(names), plural, directory))]


def _read_meta(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _bare(entry: str) -> str:
    """A SAN entry without an optional ``DNS:``/``IP:`` prefix, lower-cased (both spellings occur)."""
    lowered = entry.strip().lower()
    return lowered.split(":", 1)[1] if lowered.startswith(("dns:", "ip:")) else lowered


def san_missing(meta: Optional[Mapping[str, Any]], wanted: List[str]) -> List[str]:
    """Entries of ``wanted`` that the certificate's ``san`` list does not cover (all of them without metadata)."""
    san = meta.get("san") if meta else None
    covered = {_bare(str(s)) for s in san} if isinstance(san, list) else set()
    return [w for w in wanted if _bare(w) not in covered]


def check_tls(cfg: Config) -> List[Row]:
    """The certificate in ``<data>/tls``: fingerprint, names, expiry, key permissions (SPEC 5.7)."""
    cert, key, meta_path = _cert_files(cfg)
    if not cfg.tls and not cert.exists():
        detail = (
            "off: HTTP is not encrypted - anyone on this network can read passwords and messages (see the "
            "README). Start with --tls and trust the certificate on phones"
        )
        return [Row(WARN, "TLS", detail)]
    if not cert.exists():
        detail = "on, but no certificate yet: it is created at the first start (needs the openssl command)"
        return [Row(INFO, "TLS", detail)]
    try:
        import ssl

        from . import tlsutil
    except ImportError as exc:
        return [Row(FAIL, "TLS", "the ssl module is unusable (%s)" % exc, True)]
    fingerprint = tlsutil.cert_fingerprint(cfg.data_dir) or "unreadable"
    state = "on" if cfg.tls else "off (a certificate exists)"
    rows = [Row(INFO, "TLS", "%s; SHA-256 fingerprint %s" % (state, fingerprint))]
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert), str(key))
    except (OSError, ValueError) as exc:  # ssl.SSLError is an OSError
        detail = "cert.pem/key.pem do not load (%s): the server serves plain HTTP until they are replaced" % (
            type(exc).__name__
        )
        return [*rows, Row(WARN, "TLS certificate", detail)]
    meta = _read_meta(meta_path)
    primary, others = util.lan_addresses()
    wanted = [socket.gethostname() or "localhost", "127.0.0.1", *[ip for ip in [primary, *others] if ip]]
    missing = san_missing(meta, wanted)
    not_after = meta.get("not_after") if meta else None
    if isinstance(not_after, (int, float)):
        days = int((not_after - time.time()) // 86400)
        rows.append(Row(WARN if days < TLS_RENEW_DAYS else PASS, "TLS expiry", "%d days left" % days))
    if missing:
        detail = (
            "not covered by the certificate: %s. `serve --tls` replaces it at the next start (devices must "
            "trust the new one again)" % ", ".join(missing)
        )
        rows.append(Row(WARN, "TLS names", detail))
    else:
        rows.append(Row(PASS, "TLS names", "covers this host name and every LAN address"))
    if os.name == "posix":
        try:
            mode = os.stat(str(key)).st_mode & 0o777
        except OSError:
            mode = 0o600
        loose = bool(mode & 0o077)
        rows.append(
            Row(WARN if loose else PASS, "TLS key file", "mode %o%s" % (mode, " (should be 600)" if loose else ""))
        )
    return rows


def check_service(cfg: Config) -> List[Row]:
    """What the service installer recorded, compared with the data dir this doctor looks at."""
    state = read_install_state()
    if state is None:
        return []
    detail = "python %s, data dir %s, port %s" % (state.get("python"), state.get("data_dir"), state.get("port"))
    rows = [Row(INFO, "Installed service", detail)]
    recorded = state.get("data_dir")
    here = os.path.normcase(os.path.abspath(str(cfg.data_dir)))
    if isinstance(recorded, str) and os.path.normcase(os.path.abspath(recorded)) != here:
        detail = (
            "the installed service uses %s but this doctor looks at %s: run `python "
            "service/install_service.py cli -- doctor` to inspect the service's own data" % (recorded, cfg.data_dir)
        )
        rows.append(Row(WARN, "Data dir", detail))
    python = state.get("python")
    if isinstance(python, str) and not os.path.exists(python):
        detail = "interpreter missing: %s (re-run install_service.py install)" % python
        rows.append(Row(FAIL, "Service interpreter", detail))
    return rows


def check_setup_code(cfg: Config, facts: Dict[str, Any]) -> List[Row]:
    """The one-time setup code while no admin exists (SPEC 4.3), from the file the server writes."""
    running = facts.get("running")
    if running is not None and running["info"].get("needs_setup") is False:
        return [Row(INFO, "Setup", "an admin account exists: no setup code is needed")]
    try:
        with open(os.path.join(str(cfg.data_dir), "setup_code.txt"), encoding="utf-8") as handle:
            code = handle.read().strip()
    except OSError:
        return [Row(INFO, "Setup", "no setup code file (an admin exists, or the server has not run yet)")]
    if not code:
        return []
    port = running["port"] if running is not None else cfg.port
    detail = "%s (for the first admin account, valid only while no account exists); open %s://127.0.0.1:%d/" % (
        code,
        _scheme(cfg),
        port,
    )
    return [Row(INFO, "Setup code", detail)]


def _show(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(map(str, value))
    return json.dumps(value, sort_keys=True) if isinstance(value, dict) else str(value)


def config_rows(cfg: Config, meta: Mapping[str, str]) -> List[Tuple[str, str, str]]:
    """``(key, value, source)`` for every SPEC 2.1 key; ``db`` is the source of values the database overrides."""
    sources = getattr(cfg, "sources", {})
    rows = []
    for key in CONFIG_KEYS:
        value = getattr(cfg, key)
        text, source = _show(value), sources.get(key, "default")
        if key == "workspace_name" and meta.get(key) not in (None, value):
            text, source = meta[key], "db"
        elif key == "registration_open" and key in meta and (meta[key] == "1") != value:
            text, source = str(meta[key] == "1"), "db"
        rows.append((key, text, source))
    return rows


# --------------------------------------------------------------------------------------------------------------
# Orchestration and report
# --------------------------------------------------------------------------------------------------------------


def _safe(name: str, check: Any, *args: Any) -> List[Row]:
    """Run one check; a crash becomes a FAIL row so a single bug never hides the other results."""
    try:
        return list(check(*args))
    except Exception as exc:  # noqa: BLE001 - the doctor must always finish its report
        return [Row(FAIL, name, "this check crashed (%s: %s); please report it" % (type(exc).__name__, str(exc)[:120]))]


def exit_code(rows: List[Row]) -> int:
    """0 without a FAIL, 78 when a FAIL is an environment problem, else 1."""
    fails = [row for row in rows if row.level == FAIL]
    if not fails:
        return 0
    return EXIT_ENVIRONMENT if any(row.environment for row in fails) else EXIT_FAIL


def _print_rows(title: str, rows: List[Row]) -> None:
    if not rows:
        return
    print("\n%s" % title)
    for row in rows:
        first, *rest = row.detail.splitlines() or [""]
        print("  [%s] %-18s %s" % (row.level, row.name, first))
        for extra in rest:
            print("        %-18s %s" % ("", extra))


def run(cfg: Config) -> int:
    """Run every check, print the report, return the exit code (0 only when the server can run)."""
    print("DeskTalk doctor - version %s" % __version__)
    print("data dir: %s" % cfg.data_dir)
    facts: Dict[str, Any] = {}
    everything: List[Row] = []

    def section(title: str, rows: List[Row]) -> None:
        everything.extend(rows)
        _print_rows(title, rows)

    section(
        "Environment",
        _safe("Python", check_python)
        + _safe("sqlite3", check_sqlite, facts)
        + _safe("scrypt", check_scrypt, cfg)
        + _safe("ssl", check_ssl)
        + _safe("openssl", check_openssl, cfg),
    )
    section(
        "Data",
        _safe("data dir", check_data_dir, cfg)
        + _safe("ACL", check_windows_acl, cfg)
        + _safe("open files", check_nofile),
    )
    section("Database", _safe("database", check_database, cfg, facts) + _safe("backups", check_backups, cfg))
    state = read_install_state()
    section(
        "Network",
        _safe("port", check_ports, cfg, facts)
        + _safe("LAN", check_lan, cfg)
        + _safe("firewall", check_firewall, cfg, state),
    )
    section("Power", _safe("power", check_power, cfg))
    section("TLS", _safe("tls", check_tls, cfg))
    section("Service", _safe("service", check_service, cfg) + _safe("setup", check_setup_code, cfg, facts))
    section("Warnings from the configuration", [Row(WARN, "config", text) for text in getattr(cfg, "warnings", [])])

    print("\nEffective configuration (key, value, source):")
    for key, value, source in config_rows(cfg, facts.get("meta", {})):
        print("  %-20s %s  (%s)" % (key, value, source))

    counts = {level: sum(1 for row in everything if row.level == level) for level in (FAIL, WARN, PASS)}
    code = exit_code(everything)
    verdict = "the server can run" if code == 0 else "the server CANNOT run until the FAIL lines are fixed"
    print("\n%d FAIL, %d WARN, %d PASS: %s (exit code %d)" % (counts[FAIL], counts[WARN], counts[PASS], verdict, code))
    return code
