"""Server configuration (SPEC section 2.1).

Resolution order, strongest first: CLI flags, ``DESKTALK_*`` environment, ``<data-dir>/config.json``, defaults.
``data_dir`` itself is resolved first (flag, env, default) because it locates ``config.json``.

The module exposes:

* :class:`Config` - a plain dataclass.  Every key of the SPEC table is an attribute with its spec name.  Extra
  attributes: ``sources`` (every key -> ``flag|env|file|default``), ``warnings`` (messages the caller logs once
  logging is up), ``app_dir``, derived paths (``db_path``, ``uploads_dir``, ``control_dir`` ...) and
  :meth:`Config.replace` (a copy with overrides, tests only).
* :func:`load` - THE parser (SPEC 6.2): ``load(argv=None, env=None)``.  The command word and every argument that is
  not a configuration flag are ignored here (``__main__`` parses its own command arguments); a usage error raises
  ``SystemExit(2)``, an unusable ``config.json`` ``SystemExit(78)``.
* :func:`add_config_arguments` - adds the flags of the table to an ``argparse`` parser (``--help`` output).
* :class:`ConfigError` - the internal error behind those exits; ``exit_code`` is 2 for bad values and 78 for an
  unusable ``config.json`` file.

The two *tests only* keys (``test_scale``, ``test_limits``) and a lowered ``scrypt_n`` are honoured only when the
environment variable ``DESKTALK_TEST=1`` is set; otherwise they are ignored with a warning.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

APP_DIR = Path(__file__).resolve().parent.parent

DEFAULT_BLOCKED_EXTENSIONS = [
    "exe", "scr", "com", "pif", "bat", "cmd", "msi", "msp", "vbs", "vbe", "wsf", "wsh", "hta", "lnk", "reg", "cpl",
    "dll", "jar",
]
DEFAULT_SCRYPT_N = 65536

_TRUE = frozenset(("1", "true", "yes", "on"))
_FALSE = frozenset(("0", "false", "no", "off"))
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_HOST_NAME_RE = re.compile(r"^[a-z0-9.-]{1,253}$")

#: ``test_limits`` keys that hold ``[count, window_s]`` (SPEC 7.5 bucket names, the SPEC 4.1 login counters and
#: the wildcard ``*`` that overrides every 7.5 bucket not named explicitly).
_LIMIT_PAIR_KEYS = frozenset(
    (
        "msg.send", "msg.forward", "typing", "msg.search", "msg.react", "msg.pin", "chat.create_group",
        "chat.add_members", "chat.open_direct", "profile.update", "admin.create_user", "admin.reset_password",
        "receipt", "read", "admin_other", "global_user", "global_conn", "login_a", "login_b", "login_c", "*",
    )
)
#: ``test_limits`` keys that hold a single positive integer.
_LIMIT_INT_KEYS = frozenset(
    (
        "ws_per_user", "ws_per_ip", "ws_total", "sockets_per_ip", "sockets_total", "ws_handshakes_per_ip_min",
        "ws_handshakes_per_user_min", "reg_per_ip_hour", "reg_global_hour", "unread_cap",
    )
)


class ConfigError(Exception):
    """A configuration problem.  ``exit_code`` is the process exit code SPEC 2.2 assigns to it."""

    exit_code = 2


class ConfigFileError(ConfigError):
    """``config.json`` exists but cannot be used (unreadable, not JSON, not an object): environment error."""

    exit_code = 78


# --------------------------------------------------------------------------------------------------------------
# Value parsers.  Each takes a raw value (str from flags/env, any JSON type from config.json) and returns the
# normalised value or raises ValueError with a short reason.
# --------------------------------------------------------------------------------------------------------------


def _to_int(raw: Any, lo: int, hi: int) -> int:
    """Parse an integer in ``[lo, hi]``; bools, ``1_0``, ``+5`` and fractional floats are rejected."""
    if isinstance(raw, bool):
        raise ValueError("expected an integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and math.isfinite(raw) and raw == int(raw):
        value = int(raw)
    elif isinstance(raw, str) and re.fullmatch(r"[0-9]{1,18}", raw.strip()):
        value = int(raw.strip())
    else:
        raise ValueError("expected an integer")
    if not lo <= value <= hi:
        raise ValueError("must be between %d and %d" % (lo, hi))
    return value


def _to_bool(raw: Any) -> bool:
    """Parse ``1|true|yes|on`` / ``0|false|no|off`` (case-insensitive); JSON booleans and 0/1 are accepted too."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise ValueError("expected one of 1/true/yes/on or 0/false/no/off")


_SPLIT_WS_COMMA = re.compile(r"[\s,]+")
_SPLIT_COMMA = re.compile(r"\s*,\s*")


def _to_str_list(raw: Any, splitter: "re.Pattern[str]") -> List[str]:
    """Parse a list given as a JSON array or as a string split by ``splitter``."""
    if isinstance(raw, str):
        items = splitter.split(raw)
    elif isinstance(raw, (list, tuple)) and all(isinstance(i, str) for i in raw):
        items = list(raw)
    else:
        raise ValueError("expected a list of strings")
    return [i.strip() for i in items if i.strip()]


def _to_extensions(raw: Any) -> List[str]:
    out: List[str] = []
    for item in _to_str_list(raw, _SPLIT_WS_COMMA):
        ext = item.lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9_+-]{1,16}", ext):
            raise ValueError("invalid extension %r" % item[:20])
        if ext not in out:
            out.append(ext)
    return out


def _to_hosts(raw: Any) -> List[str]:
    out: List[str] = []
    for item in _to_str_list(raw, _SPLIT_COMMA):
        name = item.lower().rstrip(".")
        if not _HOST_NAME_RE.match(name):
            raise ValueError("invalid host name %r" % item[:40])
        if name not in out:
            out.append(name)
    return out


def _to_name(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("expected a string")
    text = " ".join(raw.split())
    if not 1 <= len(text) <= 40:
        raise ValueError("must be 1..40 characters")
    return text


def _to_host(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 255 or re.search(r"\s", raw.strip()):
        raise ValueError("expected a host name or address")
    return raw.strip()


def _to_log_level(raw: Any) -> str:
    if isinstance(raw, str) and raw.strip().upper() in _LOG_LEVELS:
        return raw.strip().upper()
    raise ValueError("expected one of " + ", ".join(_LOG_LEVELS))


def _to_scale(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("expected a number")
    value = float(raw)
    if not math.isfinite(value) or not 0 < value <= 1000:
        raise ValueError("must be a number > 0")
    return value


def _to_scrypt_n(raw: Any) -> int:
    value = _to_int(raw, 2, 1 << 20)
    if value & (value - 1):
        raise ValueError("must be a power of two")
    return value


def _to_limits(raw: Any) -> Dict[str, Any]:
    """Validate ``test_limits``: bucket pairs ``[count, window_s]`` and plain positive integers."""
    if not isinstance(raw, dict):
        raise ValueError("expected an object")
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        if key in _LIMIT_PAIR_KEYS:
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)
                or not (value[0] >= 0 and value[1] > 0 and math.isfinite(value[1]))
            ):
                raise ValueError("%s must be [count, window_s]" % key)
            out[key] = [int(value[0]), float(value[1])]
        elif key in _LIMIT_INT_KEYS:
            out[key] = _to_int(value, 0, 1 << 31)
        else:
            raise ValueError("unknown limit %r" % str(key)[:40])
    return out


def _to_path(raw: Any, base: Path) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise ValueError("expected a path")
    return Path(os.path.abspath(os.path.join(str(base), os.path.expanduser(raw.strip()))))


# --------------------------------------------------------------------------------------------------------------
# The key table
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Key:
    name: str
    parse: Callable[[Any], Any]
    env: Optional[str] = None
    tests_only: bool = False


_KEYS: Tuple[_Key, ...] = (
    _Key("host", _to_host, "DESKTALK_HOST"),
    _Key("port", lambda v: _to_int(v, 0, 65535), "DESKTALK_PORT"),
    _Key("workspace_name", _to_name, "DESKTALK_NAME"),
    _Key("registration_open", _to_bool, "DESKTALK_REGISTRATION"),
    _Key("max_users", lambda v: _to_int(v, 1, 1_000_000)),
    _Key("max_upload_mb", lambda v: _to_int(v, 1, 102_400), "DESKTALK_MAX_UPLOAD_MB"),
    _Key("blocked_extensions", _to_extensions, "DESKTALK_BLOCKED_EXT"),
    _Key("tls", _to_bool, "DESKTALK_TLS"),
    _Key("redirect_port", lambda v: _to_int(v, 0, 65535), "DESKTALK_REDIRECT_PORT"),
    _Key("allowed_hosts", _to_hosts, "DESKTALK_ALLOWED_HOSTS"),
    _Key("allow_sleep", _to_bool, "DESKTALK_ALLOW_SLEEP"),
    _Key("edit_window_s", lambda v: _to_int(v, 0, 10 * 365 * 86400)),
    _Key("delete_window_s", lambda v: _to_int(v, 0, 10 * 365 * 86400)),
    _Key("max_body_chars", lambda v: _to_int(v, 1, 1_000_000)),
    _Key("session_days", lambda v: _to_int(v, 1, 3650)),
    _Key("min_password_len", lambda v: _to_int(v, 1, 128)),
    _Key("scrypt_n", _to_scrypt_n, "DESKTALK_SCRYPT_N", tests_only=True),
    _Key("log_level", _to_log_level, "DESKTALK_LOG"),
    _Key("test_scale", _to_scale, None, tests_only=True),
    _Key("test_limits", _to_limits, None, tests_only=True),
)
_KEY_BY_NAME = {k.name: k for k in _KEYS}

#: Order in which ``Config.describe`` lists the keys (the SPEC table order).
_ORDER = (
    "host", "port", "data_dir", "workspace_name", "registration_open", "max_users", "max_upload_mb",
    "blocked_extensions", "tls", "redirect_port", "allowed_hosts", "allow_sleep", "backup_dir", "edit_window_s",
    "delete_window_s", "max_body_chars", "session_days", "min_password_len", "scrypt_n", "log_level", "test_scale",
    "test_limits",
)


@dataclass
class Config:
    """Effective server configuration.  All attributes are plain values; ``data_dir``/``backup_dir`` are Paths."""

    host: str = "0.0.0.0"
    port: int = 8765
    data_dir: Path = field(default_factory=lambda: APP_DIR / "data")
    workspace_name: str = "DeskTalk"
    registration_open: bool = False
    max_users: int = 2000
    max_upload_mb: int = 100
    blocked_extensions: List[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_EXTENSIONS))
    tls: bool = False
    redirect_port: int = 0
    allowed_hosts: List[str] = field(default_factory=list)
    allow_sleep: bool = False
    backup_dir: Optional[Path] = None
    edit_window_s: int = 900
    delete_window_s: int = 172800
    max_body_chars: int = 8000
    session_days: int = 30
    min_password_len: int = 8
    scrypt_n: int = DEFAULT_SCRYPT_N
    log_level: str = "INFO"
    test_scale: float = 1.0
    test_limits: Dict[str, Any] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict, repr=False)
    warnings: List[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.backup_dir = Path(self.backup_dir) if self.backup_dir is not None else self.data_dir / "backups"
        for name in _ORDER:
            self.sources.setdefault(name, "default")

    @property
    def app_dir(self) -> Path:
        """The repository root (the parent of ``chatd/``)."""
        return APP_DIR

    def replace(self, **overrides: Any) -> "Config":
        """A copy with ``overrides`` applied (tests only); lists and dicts are copied, ``sources`` keep their origin.

        A default ``backup_dir`` follows an overridden ``data_dir``.
        """
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        if "data_dir" in overrides and "backup_dir" not in overrides and self.sources.get("backup_dir") == "default":
            values["backup_dir"] = None
        values.update(overrides)
        for name in ("blocked_extensions", "allowed_hosts", "warnings"):
            values[name] = list(values[name])
        values["test_limits"] = dict(values["test_limits"])
        values["sources"] = dict(values["sources"])
        return Config(**values)

    # ---- derived locations (SPEC 2 / 5 / 10) -------------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chat.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def tmp_dir(self) -> Path:
        """Upload temp directory; same volume as the destination so ``os.replace`` works (SPEC 5.1)."""
        return self.data_dir / "uploads" / ".tmp"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "tls"

    @property
    def control_dir(self) -> Path:
        return self.data_dir / "control"

    @property
    def web_dir(self) -> Path:
        return APP_DIR / "web"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def scheme(self) -> str:
        return "https" if self.tls else "http"

    def limit(self, name: str, default: Any) -> Any:
        """Tests-only override from ``test_limits`` (SPEC 2.1) or ``default``."""
        return self.test_limits.get(name, default)

    def source(self, key: str) -> str:
        """Where the effective value of ``key`` came from: ``flag|env|file|default``."""
        return self.sources.get(key, "default")

    def describe(self) -> List[Tuple[str, str, str]]:
        """``(key, printable value, source)`` for every key in SPEC order (used by ``doctor``)."""
        rows = []
        for name in _ORDER:
            value = getattr(self, name)
            if isinstance(value, (list, tuple)):
                text = ",".join(str(v) for v in value)
            elif isinstance(value, dict):
                text = json.dumps(value, sort_keys=True)
            else:
                text = str(value)
            rows.append((name, text, self.source(name)))
        return rows


# --------------------------------------------------------------------------------------------------------------
# argparse integration
# --------------------------------------------------------------------------------------------------------------


def add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the configuration flags of SPEC 2.1.  Every default is ``None`` so "not given" is detectable.

    Boolean pairs are two ``store_const`` actions on one destination (the newer stdlib helper needs Python 3.9).
    """
    add = parser.add_argument
    add("--host", dest="host", default=None, metavar="HOST", help="bind address (default 0.0.0.0)")
    add("--port", dest="port", default=None, metavar="PORT", help="TCP port (default 8765, 0 = any free port)")
    add("--data-dir", dest="data_dir", default=None, metavar="DIR", help="data directory (default <app>/data)")
    add("--name", dest="workspace_name", default=None, metavar="NAME", help="workspace name (default DeskTalk)")
    add("--registration", dest="registration_open", action="store_const", const=True, default=None,
        help="open self-registration (join code required)")
    add("--no-registration", dest="registration_open", action="store_const", const=False, default=None,
        help="close self-registration")
    add("--max-upload-mb", dest="max_upload_mb", default=None, metavar="MB", help="upload size cap (default 100)")
    add("--tls", dest="tls", action="store_const", const=True, default=None, help="serve HTTPS (self-signed)")
    add("--no-tls", dest="tls", action="store_const", const=False, default=None, help="serve plain HTTP")
    add("--redirect-port", dest="redirect_port", default=None, metavar="PORT",
        help="plain-HTTP port that redirects to https (0 = off)")
    add("--allowed-host", dest="allowed_hosts", action="append", default=None, metavar="HOST",
        help="extra Host name accepted by the DNS-rebinding guard (repeatable)")
    add("--allow-sleep", dest="allow_sleep", action="store_const", const=True, default=None,
        help="do not keep the PC awake while serving")
    add("--backup-dir", dest="backup_dir", default=None, metavar="DIR", help="backup directory")
    add("--log-level", dest="log_level", default=None, metavar="LEVEL", help="DEBUG, INFO, WARNING, ERROR")


# --------------------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------------------


def _flag_value(args: Optional[argparse.Namespace], name: str) -> Any:
    return getattr(args, name, None) if args is not None else None


def _read_config_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigFileError("cannot read %s: %s" % (path, type(exc).__name__))
    if not isinstance(data, dict):
        raise ConfigFileError("%s must contain a JSON object" % path)
    return data


def _resolve_data_dir(args: Optional[argparse.Namespace], env: Mapping[str, str]) -> Tuple[Path, str]:
    cwd = Path.cwd()
    flag = _flag_value(args, "data_dir")
    if flag is not None:
        return _wrap("data_dir", _to_path, flag, cwd), "flag"
    if env.get("DESKTALK_DATA_DIR"):
        return _wrap("data_dir", _to_path, env["DESKTALK_DATA_DIR"], cwd), "env"
    return APP_DIR / "data", "default"


def _wrap(name: str, parse: Callable[..., Any], raw: Any, *extra: Any) -> Any:
    """Run a parser and turn its ``ValueError`` into a :class:`ConfigError` naming the key."""
    try:
        return parse(raw, *extra)
    except ValueError as exc:
        raise ConfigError("invalid value for %s: %s" % (name, exc))


def _build(args: Optional[argparse.Namespace], env: Mapping[str, str]) -> Config:
    """Build the effective :class:`Config` (flags > env > ``config.json`` > defaults); raises :class:`ConfigError`.

    Problems that are not fatal (unknown ``config.json`` keys, ignored tests-only keys) end up in ``Config.warnings``.
    """
    testing = env.get("DESKTALK_TEST") == "1"
    warnings: List[str] = []
    sources: Dict[str, str] = {}

    data_dir, sources["data_dir"] = _resolve_data_dir(args, env)
    file_values = _read_config_file(data_dir / "config.json")

    known = set(_KEY_BY_NAME) | {"data_dir", "backup_dir"}
    for key in sorted(file_values):
        if key not in known:
            warnings.append("config.json: unknown key %s ignored" % ascii(key)[:60])
    if "data_dir" in file_values:
        warnings.append("config.json: data_dir is ignored (it is chosen by --data-dir / DESKTALK_DATA_DIR)")

    values: Dict[str, Any] = {}

    def pick(name: str, flag_raw: Any, env_name: Optional[str]) -> Tuple[Any, str]:
        if flag_raw is not None:
            return flag_raw, "flag"
        if env_name is not None and env_name in env:
            return env[env_name], "env"
        if name in file_values:
            return file_values[name], "file"
        return None, "default"

    for key in _KEYS:
        raw, source = pick(key.name, _flag_value(args, key.name), key.env)
        if source == "default":
            continue
        if key.name in ("test_scale", "test_limits") and not testing:
            warnings.append("%s is a tests-only key and is ignored without DESKTALK_TEST=1" % key.name)
            continue
        values[key.name] = _wrap(key.name, key.parse, raw)
        sources[key.name] = source

    if "scrypt_n" in values and values["scrypt_n"] < DEFAULT_SCRYPT_N and not testing:
        warnings.append("scrypt_n below %d is for tests only and is ignored without DESKTALK_TEST=1" % DEFAULT_SCRYPT_N)
        del values["scrypt_n"]
        del sources["scrypt_n"]

    backup_raw, backup_source = pick("backup_dir", _flag_value(args, "backup_dir"), "DESKTALK_BACKUP_DIR")
    if backup_source != "default":
        base = data_dir if backup_source == "file" else Path.cwd()
        values["backup_dir"] = _wrap("backup_dir", _to_path, backup_raw, base)
        sources["backup_dir"] = backup_source

    return Config(data_dir=data_dir, sources=sources, warnings=warnings, **values)


def load(argv: Optional[List[str]] = None, env: Optional[Mapping[str, str]] = None) -> Config:
    """The one configuration parser (SPEC 2.1 precedence, SPEC 6.2).

    ``argv=None`` means ``sys.argv[1:]``; ``env=None`` means ``os.environ`` and a given mapping is used INSTEAD of it
    (tests are hermetic).  The command word (``serve``, ``doctor``, ...) and every token that is not a configuration
    flag are ignored.  A usage error (bad flag or value) raises ``SystemExit(2)``; an unusable ``config.json`` raises
    ``SystemExit(78)``; the message goes to stderr in both cases.
    """
    parser = argparse.ArgumentParser(prog="chatd", add_help=False, allow_abbrev=False)
    add_config_arguments(parser)
    args, _ignored = parser.parse_known_args(sys.argv[1:] if argv is None else list(argv))
    try:
        return _build(args, os.environ if env is None else env)
    except ConfigError as exc:
        print("Configuration error: %s" % exc, file=sys.stderr)
        raise SystemExit(exc.exit_code)
