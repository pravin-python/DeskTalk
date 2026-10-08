"""Command line of the server: ``python -m chatd <command>`` (SPEC 2.2).

Commands: ``serve`` (the default), ``create-admin``, ``reset-password``, ``backup``, ``restore``, ``tls-init``,
``doctor`` and ``--version``.  This module contains no SQL: it runs the preflight, builds the
:class:`~chatd.config.Config` with ``config.load`` (the one parser), hands over to ``app`` / ``maintenance`` /
``tlsutil`` / ``doctor`` and translates every failure into the documented exit code:

====  =====================================================================================================
0     ok
1     runtime error
2     usage / invalid configuration value
73    another instance holds ``<data>/control/server.lock``
78    environment or configuration problem (sqlite3 missing/old, unusable data dir, WAL, newer schema)
====  =====================================================================================================

Everything that fails, import-time errors included, is also appended to ``<data>/logs/boot.log`` (the system temp
directory when the data directory is unusable) so a service that has no console still leaves a trace.  Nothing
that needs ``sqlite3`` (``db``, ``maintenance``, ``hub``, ``app``) is imported before :func:`preflight` passed.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any, List, Optional

from . import __version__, config, util

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_RUNNING = 73
EXIT_ENVIRONMENT = 78

MIN_SQLITE = (3, 24)
COMMANDS = ("serve", "create-admin", "reset-password", "backup", "restore", "tls-init", "doctor")
#: Commands that work without a usable ``sqlite3`` (SPEC 2.2): they are exempt from the preflight.
_NO_SQLITE_NEEDED = frozenset(("doctor", "tls-init"))


# --------------------------------------------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------------------------------------------


def _reconfigure_streams() -> None:
    """UTF-8 on stdout/stderr whatever the console code page says (``sys.stderr`` may be ``None`` under a service)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except (AttributeError, ValueError, OSError):
                continue


def _say(text: str, err: bool = False) -> None:
    """Print to stdout (or stderr when ``err``); silent when that stream does not exist (services)."""
    target = sys.stderr if err else sys.stdout
    if target is not None:
        try:
            print(text, file=target, flush=True)
        except (OSError, ValueError):
            return


def _data_dir_hint(argv: List[str]) -> str:
    """Best guess of the data directory before the configuration could be loaded (for ``boot.log``)."""
    for index, token in enumerate(argv):
        if token == "--data-dir" and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith("--data-dir="):
            return token.split("=", 1)[1]
    return os.environ.get("DESKTALK_DATA_DIR") or str(config.APP_DIR / "data")


def _boot_log(text: str, data_dir: str) -> None:
    """Append ``text`` to ``<data>/logs/boot.log``; fall back to the temp directory when that is not writable."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = "[%s] pid %d\n%s\n\n" % (stamp, os.getpid(), text.rstrip())
    targets = (
        os.path.join(data_dir, "logs", "boot.log"),
        os.path.join(tempfile.gettempdir(), "desktalk-boot.log"),
    )
    for path in targets:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(entry)
            return
        except OSError:
            continue


def preflight() -> Optional[str]:
    """``None`` when ``sqlite3`` imports and is at least 3.24, else the message of SPEC 0.4 (no traceback).

    The message says what is wrong, which version was found, which interpreter runs and what to do.  It is checked
    here, with a plain ``import sqlite3``, because nothing that imports the database layer may run before it.
    """
    hint = "Run `python -m chatd doctor` for details, or use Python 3.11-3.13 (the python.org installer)."
    try:
        import sqlite3
    except ImportError as exc:
        return "This Python has no usable 'sqlite3' module (%s).\nPython: %s (%s)\n%s" % (
            type(exc).__name__, sys.executable, sys.version.split()[0], hint,
        )
    if tuple(sqlite3.sqlite_version_info) < MIN_SQLITE:
        return "SQLite %s is too old: DeskTalk needs 3.24 or newer.\nPython: %s (%s)\n%s" % (
            sqlite3.sqlite_version, sys.executable, sys.version.split()[0], hint,
        )
    return None


def _normalise(argv: List[str]) -> List[str]:
    """``python -m chatd --port 9000`` means ``serve --port 9000``; no arguments mean ``serve``."""
    if not argv:
        return ["serve"]
    first = argv[0]
    if first in COMMANDS or first in ("-h", "--help", "--version"):
        return argv
    return ["serve"] + argv if first.startswith("-") else argv


def build_parser() -> argparse.ArgumentParser:
    """The command parser.  Configuration flags are accepted after every command (``config.load`` interprets them)."""
    parser = argparse.ArgumentParser(prog="chatd", description="DeskTalk 2 - LAN chat server (standard library only)")
    parser.add_argument("--version", action="store_true", help="print the version and the supported schema version")
    common = argparse.ArgumentParser(add_help=False)
    config.add_config_arguments(common)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("serve", parents=[common], help="run the server (default)")
    create = sub.add_parser("create-admin", parents=[common], help="create an admin account or promote a user")
    create.add_argument("username")
    create.add_argument("--password-stdin", action="store_true", help="read the password from the first stdin line")
    reset = sub.add_parser("reset-password", parents=[common], help="set a new password and sign the user out")
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true", help="read the password from the first stdin line")
    reset.add_argument("--must-change", action="store_true", help="force a password change at the next sign-in")
    backup = sub.add_parser("backup", parents=[common], help="write a consistent snapshot of the database")
    backup.add_argument("--out", metavar="DIR", help="target directory (default: the backup directory)")
    backup.add_argument("--with-uploads", action="store_true", help="include uploads/ (creates a folder)")
    restore = sub.add_parser("restore", parents=[common], help="replace the database by a snapshot (server stopped)")
    restore.add_argument("path", help="a chat-*.db file or a --with-uploads folder")
    tls = sub.add_parser("tls-init", parents=[common], help="create or refresh the self-signed TLS certificate")
    tls.add_argument("--force", action="store_true", help="regenerate even when the current certificate is valid")
    sub.add_parser("doctor", parents=[common], help="diagnose the installation")
    return parser


# --------------------------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------------------------


def _read_password(args: argparse.Namespace) -> Optional[str]:
    if args.password_stdin:
        line = sys.stdin.readline() if sys.stdin is not None else ""
        return line.rstrip("\r\n") if line else None
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        _say("The passwords do not match.", err=True)
        return None
    return first


def _cmd_serve(cfg: config.Config, args: argparse.Namespace) -> int:
    from . import app

    app.Server(cfg).run()
    return EXIT_OK


def _cmd_create_admin(cfg: config.Config, args: argparse.Namespace) -> int:
    from . import maintenance

    password = _read_password(args)
    if not password:
        _say("No password was given.", err=True)
        return EXIT_USAGE
    user = maintenance.create_admin(
        cfg.data_dir, args.username, password, workspace_name=cfg.workspace_name,
        min_password_len=cfg.min_password_len, max_users=cfg.max_users, scrypt_n=cfg.scrypt_n,
    )
    _say("Admin account ready: %s (id %s)." % (user.get("username", args.username), user.get("id", "?")))
    return EXIT_OK


def _cmd_reset_password(cfg: config.Config, args: argparse.Namespace) -> int:
    from . import maintenance

    password = _read_password(args)
    if not password:
        _say("No password was given.", err=True)
        return EXIT_USAGE
    maintenance.reset_password(
        cfg.data_dir, args.username, password, must_change=args.must_change,
        min_password_len=cfg.min_password_len, scrypt_n=cfg.scrypt_n,
    )
    _say("The password of %s was changed and the user was signed out everywhere." % args.username)
    return EXIT_OK


def _cmd_backup(cfg: config.Config, args: argparse.Namespace) -> int:
    from . import maintenance

    target = maintenance.backup(cfg.db_path, args.out or cfg.backup_dir, with_uploads=args.with_uploads, prefix="chat")
    _say(target)
    return EXIT_OK


def _cmd_restore(cfg: config.Config, args: argparse.Namespace) -> int:
    from . import maintenance

    maintenance.restore(args.path, cfg.data_dir)
    _say("Restored %s. Start the server again." % args.path)
    return EXIT_OK


def _cmd_tls_init(cfg: config.Config, args: argparse.Namespace) -> int:
    """``tls-init``: the same certificate logic as ``serve --tls`` (SPEC 5.7); needs no database and no lock."""
    from . import tlsutil

    if not tlsutil.ensure_cert(cfg.data_dir, force=args.force):
        _say("No TLS certificate could be produced (is `openssl` installed? Try `python -m chatd doctor`).", err=True)
        return EXIT_ERROR
    meta = tlsutil.read_meta(cfg.tls_dir) or {}
    san = meta.get("san")
    _say("TLS certificate ready: %s" % (cfg.tls_dir / "cert.pem"))
    _say("Names and addresses: %s" % (", ".join(str(n) for n in san) if isinstance(san, list) else "unknown"))
    _say("SHA-256 fingerprint: %s" % (tlsutil.cert_fingerprint(cfg.data_dir) or "unknown"))
    return EXIT_OK


def _cmd_doctor(cfg: config.Config, args: argparse.Namespace) -> int:
    try:
        from . import doctor
    except ImportError:
        _say("The doctor module is not installed in this copy of DeskTalk.", err=True)
        return EXIT_ERROR
    return int(doctor.run(cfg))


_HANDLERS = {
    "serve": _cmd_serve,
    "create-admin": _cmd_create_admin,
    "reset-password": _cmd_reset_password,
    "backup": _cmd_backup,
    "restore": _cmd_restore,
    "tls-init": _cmd_tls_init,
    "doctor": _cmd_doctor,
}


def _data_dir_owner(path: Any) -> str:
    """Owner of the data directory for the "run it as the service account" hint (best effort, error path only)."""
    try:
        if os.name == "posix":
            import pwd

            return pwd.getpwuid(os.stat(str(path)).st_uid).pw_name
        script = "(Get-Acl -LiteralPath $args[0]).Owner"
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("utf-8", "replace").strip()
        return out or "unknown"
    except (OSError, KeyError, subprocess.SubprocessError, ImportError):
        return "unknown"


def _explain_access_failure(cfg: config.Config, argv: List[str], exc: BaseException) -> int:
    """SPEC 2.2: the database/data dir cannot be opened by this account: say who owns it and what to run instead."""
    owner = _data_dir_owner(cfg.data_dir)
    rest: List[str] = []
    skip = False
    for token in argv:  # the service wrapper adds its own --data-dir
        if skip:
            skip = False
        elif token == "--data-dir":
            skip = True
        elif not token.startswith("--data-dir="):
            rest.append(token)
    _say("Cannot open the data directory %s (%s)." % (cfg.data_dir, type(exc).__name__), err=True)
    _say("It belongs to: %s. Run this command as that account, or from an elevated shell, or stop the service." % owner,
         err=True)
    script = os.path.join(str(config.APP_DIR), "service", "install_service.py")
    _say("For an installed service use:  %s" % shlex.join([sys.executable, script, "cli", "--"] + rest), err=True)
    return EXIT_ENVIRONMENT


def _print_version() -> None:
    try:
        from . import db

        schema = "schema v%d" % db.SCHEMA_VERSION
    except ImportError:
        schema = "schema unknown (sqlite3 unavailable)"
    _say("DeskTalk %s, %s" % (__version__, schema))


def _run(argv: List[str]) -> int:
    parser = build_parser()
    normalised = _normalise(argv)
    args = parser.parse_args(normalised)
    if args.version:
        _print_version()
        return EXIT_OK
    if args.command is None:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    if args.command not in _NO_SQLITE_NEEDED:
        problem = preflight()
        if problem is not None:
            _say(problem, err=True)
            _boot_log(problem, _data_dir_hint(argv))
            return EXIT_ENVIRONMENT
    try:
        cfg = config.load(normalised)
    except SystemExit as exc:
        if exc.code not in (0, None):
            _boot_log("configuration error (exit %s)" % exc.code, _data_dir_hint(argv))
        raise
    if args.command != "serve":
        util.setup_logging(cfg.log_level, cfg.data_dir, attach_file=False)
    expected: tuple = (PermissionError,)
    if args.command not in _NO_SQLITE_NEEDED:
        import sqlite3

        expected = (PermissionError, sqlite3.OperationalError)
    try:
        return _HANDLERS[args.command](cfg, args)
    except expected as exc:
        return _explain_access_failure(cfg, argv, exc)
    finally:
        if args.command != "serve":
            util.stop_logging()


def main(argv: Optional[List[str]] = None) -> int:
    """Run the command line; returns the process exit code (never raises, except for ``SystemExit`` of argparse)."""
    args = list(sys.argv[1:] if argv is None else argv)
    _reconfigure_streams()
    try:
        return _run(args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_ERROR)
    except KeyboardInterrupt:
        _say("Interrupted.", err=True)
        return EXIT_ERROR
    except util.FatalError as exc:
        _say(str(exc), err=True)
        _boot_log("%s (exit %d)" % (exc, exc.code), _data_dir_hint(args))
        return exc.code
    except BaseException:  # noqa: BLE001 - the boot wrapper of SPEC 2.2: nothing may escape without a trace
        trace = traceback.format_exc()
        _say(trace, err=True)
        _boot_log(trace, _data_dir_hint(args))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
