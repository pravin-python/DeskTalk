"""CLI business logic and all of its SQL: create-admin, reset-password, backup, restore, schema info (SPEC §2.2, §6.2).

Imported by ``__main__.py``, ``doctor.py`` and the auto-backup ticker in ``app.py``. ``sqlite3`` and every ``db*``
module are imported inside the functions (SPEC §2.2): this module imports cleanly, and ``doctor`` keeps working, when
``sqlite3`` is missing. CLI writers do not take the instance lock: they use ``BEGIN IMMEDIATE`` with the 5 s busy
timeout of ``db.open_connection`` and then ``touch_reload`` so a running server notices within 2 s (SPEC §2.4).
Failures a human can act on raise :class:`MaintenanceError` (a ``util.FatalError``: message only, exit code 1).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional

from . import util

log = logging.getLogger("chatd.maintenance")

BACKUP_NAME_RE = r"^%s-\d{8}-\d{6}(?:-\d+)?\.db\Z"


class MaintenanceError(util.FatalError):
    """A CLI-level failure with a message meant for the console (exit code 1 unless ``code`` says otherwise)."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message, code)


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------


def _app_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _is_below(path: str, root: str) -> bool:
    child, parent = _norm(path), _norm(root)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _make_private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)


def _restrict(path: str) -> None:
    """Best-effort 0600 on POSIX; Windows inherits the data directory's ACL (SPEC §10.2)."""
    try:
        os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)
    except OSError:
        log.debug("could not restrict permissions of a backup file")


def touch_reload(data_dir: Any) -> None:
    """Create or bump ``<data>/control/reload`` so a running server re-reads the database within 2 s (SPEC §2.4)."""
    directory = util.control_dir(str(data_dir))
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "reload")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("%r\n" % time.time())
    stamp = time.time()
    os.utime(target, (stamp, stamp))


def _open_database(data_dir: str, create: bool) -> Any:
    """An own connection to ``<data>/chat.db`` with the schema migrated (created when ``create``)."""
    from . import db

    problem = db.sqlite_problem()
    if problem is not None:
        raise MaintenanceError(problem, 78)
    path = util.db_path(data_dir)
    if not os.path.exists(path):
        if not create:
            raise MaintenanceError("there is no database in %s (wrong --data-dir?)" % data_dir)
        _make_private_dir(data_dir)
    conn = db.open_connection(path, "extra")
    try:
        db.apply_migrations(conn, util.backups_dir(data_dir))
    except BaseException:
        conn.close()
        raise
    return conn


def _default_hash_cost() -> int:
    from . import auth

    return auth.hasher.scrypt_n


def _begin(conn: Any) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _rollback(conn: Any) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


# --------------------------------------------------------------------------------------------------------------------
# create-admin, reset-password
# --------------------------------------------------------------------------------------------------------------------


def create_admin(
    data_dir: Any,
    username: str,
    password: str,
    display_name: Optional[str] = None,
    workspace_name: Optional[str] = None,
    min_password_len: int = 8,
    max_users: int = 2000,
    scrypt_n: Optional[int] = None,
) -> Dict[str, Any]:
    """Create an admin account, or promote an existing user, and return the public ``User`` (no ``online``).

    A new account goes through the same ``db.register_user`` as ``hub.create_user`` (``activated=False``,
    ``check_setup_code=False``): the first account creates ``Everyone`` with the ``created`` message, later ones join it
    with ``joined``. Reserved usernames are allowed. For an existing user the CLI sets ``role='admin'``, enables the
    account, stores the given password, clears ``must_change_password``, signs the user out everywhere and mirrors
    the role in ``Everyone``. Writes one ``cli.create_admin`` audit row and touches ``control/reload``.
    ``display_name`` defaults to the username; the password policy of §4.1 applies (``MaintenanceError``).
    """
    from . import auth, db, db_users

    data = str(data_dir)
    name = db_users.normalize_username(username)
    if name is None:
        raise MaintenanceError("a username has 3-32 characters of a-z, 0-9, '.', '_' and '-'")
    shown = db_users.normalize_display_name(display_name if display_name is not None else name)
    if shown is None:
        raise MaintenanceError("a display name has 1-40 characters")
    weak = auth.weak_password(password, name, shown, min_password_len)
    if weak is not None:
        raise MaintenanceError("weak password: " + weak)
    pw_hash = auth.hash_password_sync(password, scrypt_n or _default_hash_cost())

    conn = _open_database(data, create=True)
    try:
        _begin(conn)
        try:
            row = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()
            if row is None:
                spec = {
                    "username": name,
                    "display_name": shown,
                    "pw_hash": pw_hash,
                    "role": "admin",
                    "activated": False,
                    "must_change_password": False,
                    "check_setup_code": False,
                    "actor_id": None,
                    "ip": None,
                }
                result = db_users.register_user(
                    conn, spec, max_users=max_users, data_dir=data, workspace_name=workspace_name
                )
                user = result["user"]
            else:
                user_id = int(row[0])
                conn.execute(
                    "UPDATE users SET role = 'admin', disabled = 0, pw_hash = ?, must_change_password = 0 WHERE id = ?",
                    (pw_hash, user_id),
                )
                for default_chat in conn.execute("SELECT id FROM chats WHERE is_default = 1").fetchall():
                    conn.execute(
                        "UPDATE chat_members SET role = 'admin' WHERE user_id = ? AND chat_id = ?",
                        (user_id, default_chat[0]),
                    )
                db_users.revoke_user_sessions(conn, user_id)
                user = db_users.get_user(conn, user_id)
            db_users.audit(conn, None, "cli.create_admin", user["id"], None)
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
    except db.RequestError as exc:
        raise MaintenanceError(exc.msg)
    finally:
        conn.close()
    touch_reload(data)
    return user


def reset_password(
    data_dir: Any,
    username: str,
    password: str,
    must_change: bool = False,
    min_password_len: int = 8,
    scrypt_n: Optional[int] = None,
) -> None:
    """Set a new password for ``username``, delete all of the user's sessions and set ``must_change_password``.

    ``must_change_password`` is set only when ``must_change`` is true (it is cleared otherwise: the person at the
    console chose the password). Policy of §4.1 applies. One ``cli.reset_password`` audit row; touches
    ``control/reload`` so the server kicks the user's sockets (``ev.kicked 'revoked'``).
    """
    from . import auth, db, db_users

    data = str(data_dir)
    name = db_users.normalize_username(username)
    conn = _open_database(data, create=False)
    try:
        row = (
            conn.execute("SELECT id, display_name FROM users WHERE username = ?", (name,)).fetchone() if name else None
        )
        if row is None:
            raise MaintenanceError("there is no user named %s" % util.safe_log_value(username))
        weak = auth.weak_password(password, name or "", row[1], min_password_len)
        if weak is not None:
            raise MaintenanceError("weak password: " + weak)
        pw_hash = auth.hash_password_sync(password, scrypt_n or _default_hash_cost())
        _begin(conn)
        try:
            db_users.set_user_password(conn, int(row[0]), pw_hash, must_change)
            db_users.audit(conn, None, "cli.reset_password", int(row[0]), None)
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
    except db.RequestError as exc:
        raise MaintenanceError(exc.msg)
    finally:
        conn.close()
    touch_reload(data)


# --------------------------------------------------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------------------------------------------------


def _link_or_copy(source: str, target: str) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _copy_uploads(source_dir: str, target_dir: str) -> None:
    """Copy ``uploads/`` (hard links where the file system allows) without the ``.tmp`` upload staging directory."""
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [name for name in dirs if name != ".tmp"]
        relative = os.path.relpath(root, source_dir)
        destination = target_dir if relative == "." else os.path.join(target_dir, relative)
        os.makedirs(destination, exist_ok=True)
        for name in files:
            _link_or_copy(os.path.join(root, name), os.path.join(destination, name))


def _free_name(directory: str, base: str, suffix: str) -> str:
    candidate = os.path.join(directory, base + suffix)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s-%d%s" % (base, counter, suffix))
        counter += 1
    return candidate


def backup(db_path: Any, out_dir: Any, with_uploads: bool = False, prefix: str = "chat") -> str:
    """Consistent snapshot of ``db_path`` into ``out_dir`` and the path of what was written (SPEC §2.2).

    The snapshot uses the sqlite backup API on an OWN connection (``pages=256, sleep=0.02``), is built as ``.tmp`` and
    moved into place with ``util.retry_file_op``, and has its ``sessions`` table emptied. Plain: ``<out>/<prefix>-
    YYYYmmdd-HHMMSS.db``. ``with_uploads``: a folder ``<prefix>-YYYYmmdd-HHMMSS/`` holding ``chat.db`` and a copy
    (hard links where possible) of ``<data>/uploads/``. ``prefix`` is ``chat`` for the CLI and ``auto`` for the
    ticker of SPEC §2.4. Refuses an ``out_dir`` below the app's ``web/`` directory (it would be served).
    """
    from . import db

    if not re.match(r"^[a-z]{1,16}\Z", prefix):
        raise MaintenanceError("bad backup prefix")
    source_path = str(db_path)
    destination_dir = str(out_dir)
    if _is_below(destination_dir, os.path.join(_app_dir(), "web")):
        raise MaintenanceError("refusing to write backups below web/: that directory is served over HTTP")
    if not os.path.isfile(source_path):
        raise MaintenanceError("there is no database at %s" % source_path)
    _make_private_dir(destination_dir)
    base = "%s-%s" % (prefix, time.strftime("%Y%m%d-%H%M%S"))
    source = db.open_connection(source_path, "extra")
    try:
        if not with_uploads:
            target = _free_name(destination_dir, base, ".db")
            db.snapshot_database(source, target)
        else:
            target = _free_name(destination_dir, base, "")
            staging = target + ".tmp"
            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging)
            try:
                db.snapshot_database(source, os.path.join(staging, "chat.db"))
                uploads = util.uploads_dir(os.path.dirname(os.path.abspath(source_path)))
                if os.path.isdir(uploads):
                    _copy_uploads(uploads, os.path.join(staging, "uploads"))
                util.retry_file_op(os.replace, staging, target)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    finally:
        source.close()
    _restrict(target)
    return target


def prune_backups(out_dir: Any, prefix: str = "auto", keep: int = 7) -> List[str]:
    """Delete all but the newest ``keep`` ``<prefix>-*.db`` files of ``out_dir``; returns the removed paths.

    Only the pattern ``<prefix>-YYYYmmdd-HHMMSS[-n].db`` is touched: manual ``chat-*`` snapshots, ``pre-migrate-*``
    and ``pre-restore-*`` are never pruned (SPEC §2.4).
    """
    directory = str(out_dir)
    pattern = re.compile(BACKUP_NAME_RE % re.escape(prefix))
    try:
        names = sorted(name for name in os.listdir(directory) if pattern.match(name))
    except OSError:
        return []
    removed: List[str] = []
    for name in names[: max(len(names) - keep, 0)]:
        path = os.path.join(directory, name)
        if util.retry_file_op(os.remove, path):
            removed.append(path)
    return removed


# --------------------------------------------------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------------------------------------------------


def _check_snapshot(path: str) -> None:
    """Refuse a snapshot that fails ``integrity_check``, is not a DeskTalk database or is newer than this program."""
    import sqlite3

    from . import db

    try:
        conn = sqlite3.connect(path, isolation_level=None)
    except sqlite3.Error as exc:
        raise MaintenanceError("cannot open the snapshot: %s" % exc)
    try:
        problems = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        if problems != ["ok"]:
            raise MaintenanceError("the snapshot failed PRAGMA integrity_check: " + "; ".join(problems[:3]))
        version = db.read_schema_version(conn)
        if version < 1:
            raise MaintenanceError("the snapshot is not a DeskTalk database")
        if version > len(db.MIGRATIONS):
            raise db.SchemaTooNew(version, len(db.MIGRATIONS))
    except sqlite3.DatabaseError as exc:
        raise MaintenanceError("the snapshot is damaged: %s" % type(exc).__name__)
    finally:
        conn.close()


def restore(path: Any, data_dir: Any) -> None:
    """Replace ``<data>/chat.db`` with a snapshot (a ``chat-*.db`` file or a ``--with-uploads`` folder), SPEC §2.2.

    Refuses while the instance lock is held (the server is running; exit code 73). The snapshot is copied next to the
    live database and checked (``PRAGMA integrity_check``, schema version) BEFORE anything moves; then ``chat.db``,
    ``chat.db-wal`` and ``chat.db-shm`` (and ``uploads/`` when the snapshot carries one) are moved to
    ``<data>/backups/pre-restore-<timestamp>/``, the snapshot becomes ``chat.db``, an older schema is migrated and a
    ``cli.restore`` audit row is written. ``MaintenanceError`` on any failure.
    """
    from . import db, db_users

    data = os.path.abspath(str(data_dir))
    source = os.path.abspath(str(path))
    if os.path.isdir(source):
        snapshot = os.path.join(source, "chat.db")
        snapshot_uploads: Optional[str] = os.path.join(source, "uploads")
        if not os.path.isdir(snapshot_uploads):
            snapshot_uploads = None
    else:
        snapshot, snapshot_uploads = source, None
    if not os.path.isfile(snapshot):
        raise MaintenanceError("%s is not a snapshot (no chat.db found)" % source)
    problem = db.sqlite_problem()
    if problem is not None:
        raise MaintenanceError(problem, 78)
    _make_private_dir(data)
    try:
        lock = util.instance_lock(data)
    except util.AlreadyRunning as exc:
        raise MaintenanceError("%s; stop the service before restoring" % exc, 73)
    live = util.db_path(data)
    staged = live + ".restore"
    try:
        shutil.copyfile(snapshot, staged)
        _check_snapshot(staged)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        parked = os.path.join(util.backups_dir(data), "pre-restore-" + stamp)
        os.makedirs(parked, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(live + suffix):
                util.retry_file_op(os.replace, live + suffix, os.path.join(parked, "chat.db" + suffix))
        current_uploads = util.uploads_dir(data)
        if snapshot_uploads is not None and os.path.isdir(current_uploads):
            util.retry_file_op(os.replace, current_uploads, os.path.join(parked, "uploads"))
        util.retry_file_op(os.replace, staged, live)
        if snapshot_uploads is not None:
            _copy_uploads(snapshot_uploads, current_uploads)
        conn = _open_database(data, create=False)
        try:
            _begin(conn)
            try:
                db_users.audit(conn, None, "cli.restore", None, None)
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        finally:
            conn.close()
        log.info("restored %s; the previous files are in %s", os.path.basename(source), parked)
    finally:
        util.retry_file_op(os.remove, staged)
        lock.release()


# --------------------------------------------------------------------------------------------------------------------
# schema_info (doctor)
# --------------------------------------------------------------------------------------------------------------------


def schema_info(data_dir: Any) -> Dict[str, Any]:
    """Read-only facts about ``<data>/chat.db`` for ``doctor`` (never writes, never raises ``sqlite3`` errors).

    Keys: ``path``, ``exists``, ``code_schema_version`` (``db.SCHEMA_VERSION``), ``schema_version`` (``int`` or
    ``None``), ``journal_mode``, ``integrity`` (``'ok'`` or the first problems), ``meta`` (``workspace_name`` and
    ``registration_open`` when stored), ``error`` (``None`` or a short text).
    """
    import sqlite3
    from urllib.request import pathname2url

    from . import db

    path = util.db_path(str(data_dir))
    info: Dict[str, Any] = {
        "path": path,
        "exists": os.path.isfile(path),
        "code_schema_version": db.SCHEMA_VERSION,
        "schema_version": None,
        "journal_mode": None,
        "integrity": None,
        "meta": {},
        "error": None,
    }
    if not info["exists"]:
        return info
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % pathname2url(os.path.abspath(path)), uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        info["error"] = "cannot open: %s" % exc
        return info
    try:
        info["journal_mode"] = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        problems = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        info["integrity"] = "ok" if problems == ["ok"] else "; ".join(problems[:3])
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        info["schema_version"] = int(version[0]) if version is not None else None
        for key, value in conn.execute(
            "SELECT key, value FROM meta WHERE key IN ('workspace_name', 'registration_open')"
        ):
            info["meta"][key] = value
    except (sqlite3.Error, ValueError) as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        conn.close()
    return info
