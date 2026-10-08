"""Users, sessions, registration, profile, admin, audit log and attachments (SPEC §3, §4, §5.1, §7.4).

Every public function here is synchronous ``fn(conn, ...)`` code for a database thread: it runs inside
``Database.run`` (writer, already inside ``BEGIN IMMEDIATE``), ``Database.run_read`` (reader, ``query_only``) or on
an own connection (``Database.connect_extra``, ``maintenance``). None of them commits, rolls back or runs a script,
and every authorisation or limit is re-read inside the call. Protocol failures raise ``db.RequestError``.
The function table with arguments, results and callers is ``docs/DB_API.md``.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import db, util

try:
    import sqlite3
except ImportError:  # the server reports this through db.sqlite_problem()
    sqlite3 = None  # type: ignore[assignment]

log = logging.getLogger("chatd.db_users")

__all__ = [
    "ATTACHMENT_ACCESS_SQL",
    "ATTACHMENT_BATCH_SQL",
    "ORPHAN_UNATTACHED_SQL",
    "UPLOAD_RECENT_BYTES_SQL",
    "UPLOAD_UNATTACHED_BYTES_SQL",
    "admin_audit",
    "admin_reset_password",
    "admin_settings",
    "admin_stats",
    "admin_users",
    "attachment_access",
    "attachment_public",
    "attachment_storage_bytes",
    "audit",
    "change_password",
    "complete_login",
    "create_session",
    "delete_session",
    "drop_attachment_if_unreferenced",
    "get_attachment",
    "get_instance_id",
    "get_login_record",
    "get_me",
    "get_password_record",
    "get_user",
    "insert_attachment",
    "list_sessions",
    "list_users",
    "lookup_session",
    "needs_setup",
    "normalize_display_name",
    "normalize_status_text",
    "normalize_username",
    "normalize_workspace_name",
    "profile_update",
    "purge_expired_sessions",
    "register_user",
    "require_active_user",
    "require_admin",
    "revoke_other_sessions",
    "revoke_session",
    "revoke_user_sessions",
    "session_statuses",
    "set_last_seen",
    "set_user_password",
    "sweep_orphans",
    "touch_session",
    "update_pw_hash",
    "upload_usage",
    "user_count",
    "workspace_settings",
]

DISPLAY_NAME_MAX = 40
STATUS_TEXT_MAX = 140
WORKSPACE_NAME_MAX = 40
SESSION_ID_RE = re.compile(r"^[0-9a-f]{16}\Z")
ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
_IN_CHUNK = 400
BACKSLASH = chr(92)


# --------------------------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------------------------


def _fail(code: str, msg: str, reason: Optional[str] = None) -> "db.RequestError":
    return db.RequestError(code, msg, reason)


def _clip(value: Any, limit: int) -> Optional[str]:
    """``value`` as text without NUL, at most ``limit`` characters; ``None`` stays ``None``."""
    if value is None:
        return None
    return str(value).replace("\x00", "")[:limit]


def _chunks(items: Sequence[Any], size: int = _IN_CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _marks(count: int) -> str:
    return ",".join("?" * count)


def _data_dir_of(conn: Any) -> Optional[str]:
    """The directory of the main database file (the data dir, SPEC §2), or ``None`` for an in-memory database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return os.path.dirname(str(row[2]))
    return None


# --------------------------------------------------------------------------------------------------------------------
# Identity normalisation (SPEC §4.3) -- pure functions, also used by api.py / hub.py before they call the db
# --------------------------------------------------------------------------------------------------------------------


def normalize_username(raw: Any) -> Optional[str]:
    """The stored form of a username (ASCII-lowercased) or ``None`` when it does not match ``^[a-z0-9._-]{3,32}$``."""
    if not isinstance(raw, str) or not raw.isascii():
        return None
    lowered = raw.lower()
    return lowered if util.USERNAME_RE.match(lowered) else None


def normalize_display_name(raw: Any) -> Optional[str]:
    """Normalised display name (§4.3(4)), or ``None`` when it is not text or not 1..40 characters long."""
    if not isinstance(raw, str):
        return None
    text = util.normalize_text(raw)
    return text if 1 <= len(text) <= DISPLAY_NAME_MAX else None


def normalize_status_text(raw: Any) -> Optional[str]:
    """Normalised status text (may be empty), or ``None`` when it is not text or longer than 140 characters."""
    if not isinstance(raw, str):
        return None
    text = util.normalize_text(raw)
    return text if len(text) <= STATUS_TEXT_MAX else None


def normalize_workspace_name(raw: Any) -> Optional[str]:
    """Normalised workspace name, or ``None`` when it is not text or not 1..40 characters long."""
    if not isinstance(raw, str):
        return None
    text = util.normalize_text(raw)
    return text if 1 <= len(text) <= WORKSPACE_NAME_MAX else None


# --------------------------------------------------------------------------------------------------------------------
# User rows and shapes (SPEC §7.2)
# --------------------------------------------------------------------------------------------------------------------

_USER_COLUMNS = (
    "id, username, display_name, status_text, role, read_receipts, show_last_seen, disabled, must_change_password, "
    "created_at, last_seen_at, last_login_at"
)


def _public(row: Sequence[Any]) -> Dict[str, Any]:
    """``User`` of §7.2 without the live ``online`` flag (the hub adds it)."""
    return {
        "id": row[0],
        "username": row[1],
        "display_name": row[2],
        "status_text": row[3],
        "role": row[4],
        "last_seen": row[10] if row[6] and row[10] is not None else None,
        "disabled": bool(row[7]),
        "activated": row[11] is not None,
        "read_receipts": bool(row[5]),
    }


def _me(row: Sequence[Any]) -> Dict[str, Any]:
    """``me`` of §4.2: ``User`` plus ``show_last_seen`` and ``must_change_password``."""
    me = _public(row)
    me["show_last_seen"] = bool(row[6])
    me["must_change_password"] = bool(row[8])
    return me


def get_user(conn: Any, user_id: int) -> Optional[Dict[str, Any]]:
    """The public ``User`` (no ``online``) of ``user_id`` or ``None``."""
    row = conn.execute("SELECT " + _USER_COLUMNS + " FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row) if row is not None else None


def get_me(conn: Any, user_id: int) -> Optional[Dict[str, Any]]:
    """The ``me`` object of ``user_id`` or ``None`` (a disabled user is returned too; callers decide)."""
    row = conn.execute("SELECT " + _USER_COLUMNS + " FROM users WHERE id = ?", (user_id,)).fetchone()
    return _me(row) if row is not None else None


def list_users(conn: Any) -> List[Dict[str, Any]]:
    """Every user as a public ``User`` without ``online``, ordered by id (the ``ev.ready.users`` directory)."""
    return [_public(row) for row in conn.execute("SELECT " + _USER_COLUMNS + " FROM users ORDER BY id")]


def user_count(conn: Any) -> int:
    """Number of accounts."""
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def needs_setup(conn: Any) -> bool:
    """``True`` while no account exists yet (``/api/info.needs_setup``)."""
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None


def get_instance_id(conn: Any) -> Optional[str]:
    """``meta.instance_id`` (``ev.ready.instance_id``)."""
    return db.get_meta(conn, "instance_id")


def require_active_user(conn: Any, user_id: Any) -> Dict[str, Any]:
    """``{id, username, role}`` of an existing, enabled user; otherwise ``RequestError('unauthorized')`` (§7.1.1(e))."""
    row = (
        conn.execute("SELECT id, username, role, disabled FROM users WHERE id = ?", (user_id,)).fetchone()
        if isinstance(user_id, int) and not isinstance(user_id, bool)
        else None
    )
    if row is None or row[3]:
        raise _fail("unauthorized", "not signed in")
    return {"id": row[0], "username": row[1], "role": row[2]}


def require_admin(conn: Any, user_id: Any) -> Dict[str, Any]:
    """:func:`require_active_user` plus ``users.role = 'admin'`` (else ``RequestError('forbidden')``)."""
    user = require_active_user(conn, user_id)
    if user["role"] != "admin":
        raise _fail("forbidden", "administrators only")
    return user


# --------------------------------------------------------------------------------------------------------------------
# Workspace settings (meta) and the audit log
# --------------------------------------------------------------------------------------------------------------------


def workspace_settings(
    conn: Any, default_name: str, default_registration_open: bool, include_join_code: bool = False
) -> Dict[str, Any]:
    """``{name, registration_open[, join_code]}``: ``meta`` wins over the config defaults once an admin edited them."""
    name = db.get_meta(conn, "workspace_name") or default_name
    stored = db.get_meta(conn, "registration_open")
    result: Dict[str, Any] = {
        "name": name,
        "registration_open": default_registration_open if stored is None else stored == "1",
    }
    if include_join_code:
        result["join_code"] = db.get_meta(conn, "join_code")
    return result


def audit(
    conn: Any,
    actor_id: Optional[int],
    action: str,
    target_id: Optional[int] = None,
    ip: Optional[str] = None,
    ts: Optional[float] = None,
) -> None:
    """Append one ``audit_log`` row with the vocabulary of SPEC §3 (``actor_id`` is ``None`` for CLI rows)."""
    conn.execute(
        "INSERT INTO audit_log(ts, actor_id, action, target_id, ip) VALUES (?, ?, ?, ?, ?)",
        (util.now() if ts is None else ts, actor_id, action, target_id, _clip(ip, 64)),
    )


# --------------------------------------------------------------------------------------------------------------------
# Registration (SPEC §3.2(4), §4.3, §6.1): THE single registration function
# --------------------------------------------------------------------------------------------------------------------


def _check_display_key(conn: Any, key: str, own_id: Optional[int], allow_reserved: bool) -> None:
    """Uniqueness rules for a display key (§4.3(4)); ``own_id`` is excepted from the "other user" checks."""
    marker = -1 if own_id is None else own_id
    if conn.execute("SELECT 1 FROM users WHERE display_key = ? AND id != ?", (key, marker)).fetchone() is not None:
        raise _fail("conflict", "that display name is already taken", "name_taken")
    if conn.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (key, marker)).fetchone() is not None:
        raise _fail("conflict", "that display name is already taken", "name_taken")
    if not allow_reserved and key in util.RESERVED_USERNAMES:
        raise _fail("conflict", "that display name is reserved", "name_taken")


def _verify_setup_code(conn: Any, data_dir: Optional[str], code: Any) -> None:
    from . import auth

    if not isinstance(code, str) or not code:
        raise _fail("setup_code_required", "the setup code is required")
    directory = data_dir or _data_dir_of(conn)
    if directory is None or not auth.verify_setup_code(directory, code):
        raise _fail("bad_setup_code", "the setup code is wrong")


def _verify_join_code(conn: Any, code: Any) -> None:
    stored = db.get_meta(conn, "join_code")
    if not isinstance(code, str) or not code or not stored:
        raise _fail("bad_join_code", "the join code is wrong")
    if not hmac.compare_digest(code.encode("utf-8"), stored.encode("utf-8")):
        raise _fail("bad_join_code", "the join code is wrong")


def register_user(
    conn: Any,
    spec: Dict[str, Any],
    max_users: int = 2000,
    data_dir: Optional[str] = None,
    workspace_name: Optional[str] = None,
    registration_open_default: bool = False,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Create an account inside the caller's transaction: the one registration function (§6.1).

    ``spec`` keys (SPEC §6.1): ``username``, ``display_name`` (raw strings, normalised and validated here),
    ``pw_hash`` (already hashed), ``role`` (``'admin'|'member'``; ignored for the very first user, who is always admin
    and forced to ``'member'`` on public registration), ``activated`` (``last_login_at = now`` when true),
    ``must_change_password``, ``setup_code``, ``check_setup_code``, ``actor_id`` (an enabled admin, i.e.
    ``admin.create_user``, which also appends its audit row), ``ip``; plus ``check_registration`` and ``join_code`` for
    ``POST /api/register``. Public registration (``check_setup_code`` or ``check_registration``) enforces, inside the
    transaction: while ``users`` is empty the setup code (``setup_code_required`` / ``bad_setup_code``, verified with
    ``auth.verify_setup_code``); otherwise, with ``check_registration``, ``registration_open`` (meta, else
    ``registration_open_default``) and ``max_users`` (``registration_closed``) and the join code (``bad_join_code``).
    Trusted callers (admin, CLI) skip those and may use reserved names; every caller is bound by ``max_users``
    (``invalid_state`` / ``max_users``), the username and display-name rules (``bad_request``, ``conflict`` with
    ``username_taken`` / ``name_taken``). The first user creates ``Everyone`` (title = workspace name) with the
    ``created`` system message and gets a fresh join code; later users join it with the ``joined`` message.

    Returns ``{user, me, first_user, chat_id, message_id, event, member, message_event, index}``: ``user``/``me`` as in
    §7.2/§4.2 (no ``online``), ``chat_id`` of ``Everyone``, ``message_id`` of the system message, ``event``
    ``'created'|'joined'``, ``member`` the ``{user_id, role, delivered_up_to, read_up_to}`` entry for
    ``ev.chat_members {added}``, ``message_event`` the ``ev.message`` of the system message
    (``db_messages.system_message_event``) and ``index`` the membership-index entry of ``Everyone``
    (``db_chats.index_entry``), both built in the same transaction so the hub fans out without another query.
    """
    from . import db_chats, db_messages

    ts = util.now() if ts is None else ts
    actor_id = spec.get("actor_id")
    check_setup = bool(spec.get("check_setup_code"))
    check_registration = bool(spec.get("check_registration"))
    public = check_setup or check_registration
    if actor_id is not None:
        require_admin(conn, actor_id)
    existing = user_count(conn)
    first = existing == 0

    if first:
        if public:
            _verify_setup_code(conn, data_dir, spec.get("setup_code"))
    else:
        if check_registration:
            opened = workspace_settings(conn, "", registration_open_default)["registration_open"]
            if not opened or existing >= max_users:
                raise _fail("registration_closed", "registration is closed")
            _verify_join_code(conn, spec.get("join_code"))
        if existing >= max_users:
            raise _fail("invalid_state", "the user limit has been reached", "max_users")

    username = normalize_username(spec.get("username"))
    if username is None:
        raise _fail("bad_request", "username must be 3-32 characters of a-z, 0-9, '.', '_' or '-'")
    display_name = normalize_display_name(spec.get("display_name"))
    if display_name is None:
        raise _fail("bad_request", "display name must be 1-40 characters")
    pw_hash = spec.get("pw_hash")
    if not isinstance(pw_hash, str) or not pw_hash:
        raise _fail("bad_request", "missing password hash")

    if public and username in util.RESERVED_USERNAMES:
        raise _fail("conflict", "that username is already taken", "username_taken")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is not None:
        raise _fail("conflict", "that username is already taken", "username_taken")
    if conn.execute("SELECT 1 FROM users WHERE display_key = ?", (username,)).fetchone() is not None:
        raise _fail("conflict", "that username is already taken", "username_taken")
    key = util.display_key(display_name)
    _check_display_key(conn, key, None, allow_reserved=(not public) or first)

    role = "admin" if first else ("member" if public else ("admin" if spec.get("role") == "admin" else "member"))
    activated = bool(spec.get("activated"))
    cursor = conn.execute(
        "INSERT INTO users(username, display_name, display_key, pw_hash, role, must_change_password, created_at,"
        " last_seen_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            username,
            display_name,
            key,
            pw_hash,
            role,
            1 if spec.get("must_change_password") else 0,
            ts,
            ts if activated else None,
            ts if activated else None,
        ),
    )
    user_id = int(cursor.lastrowid)

    workspace = db.get_meta(conn, "workspace_name") or workspace_name or "DeskTalk"
    if first:
        chat_id = db_chats.create_everyone_chat(conn, workspace, user_id, ts)
        db.set_meta(conn, "join_code", util.short_code())
    else:
        row = conn.execute("SELECT id FROM chats WHERE is_default = 1 LIMIT 1").fetchone()
        if row is None:
            raise db.DbError("the default chat is missing")
        chat_id = int(row[0])
    db_chats.add_member_row(conn, chat_id, user_id, role, ts)
    watermark = int(
        conn.execute(
            "SELECT history_from_id FROM chat_members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()[0]
    )
    if first:
        event = "created"
        message_id = db_messages.insert_system_message(
            conn, chat_id, "created", user_id, [user_id], "Welcome to " + workspace, title=workspace, ts=ts
        )
    else:
        event = "joined"
        message_id = db_messages.insert_system_message(
            conn, chat_id, "joined", None, [user_id], display_name + " joined", ts=ts, bump_activity=False
        )
    if actor_id is not None:
        audit(conn, actor_id, "admin.create_user", user_id, spec.get("ip"), ts)

    row = conn.execute("SELECT " + _USER_COLUMNS + " FROM users WHERE id = ?", (user_id,)).fetchone()
    return {
        "user": _public(row),
        "me": _me(row),
        "first_user": first,
        "chat_id": chat_id,
        "message_id": message_id,
        "event": event,
        "member": {"user_id": user_id, "role": role, "delivered_up_to": watermark, "read_up_to": watermark},
        "message_event": db_messages.system_message_event(conn, chat_id, message_id),
        "index": db_chats.index_entry(conn, chat_id),
    }


# --------------------------------------------------------------------------------------------------------------------
# Login, sessions (SPEC §4.1, §4.2)
# --------------------------------------------------------------------------------------------------------------------


def get_login_record(conn: Any, username: Any) -> Optional[Dict[str, Any]]:
    """``{id, username, display_name, pw_hash, disabled, must_change_password}`` for a login attempt or ``None``.

    ``None`` also for a string that is not a valid username (no query is made). The caller verifies the password
    (``auth.verify_password``) before it looks at ``disabled`` (§4.1).
    """
    name = normalize_username(username)
    if name is None:
        return None
    row = conn.execute(
        "SELECT id, username, display_name, pw_hash, disabled, must_change_password FROM users WHERE username = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "display_name": row[2],
        "pw_hash": row[3],
        "disabled": bool(row[4]),
        "must_change_password": bool(row[5]),
    }


def get_password_record(conn: Any, user_id: int) -> Optional[Dict[str, Any]]:
    """Same shape as :func:`get_login_record`, by user id (``POST /api/password``)."""
    row = conn.execute(
        "SELECT id, username, display_name, pw_hash, disabled, must_change_password FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "display_name": row[2],
        "pw_hash": row[3],
        "disabled": bool(row[4]),
        "must_change_password": bool(row[5]),
    }


def update_pw_hash(conn: Any, user_id: int, old_pw_hash: str, new_pw_hash: str) -> bool:
    """Transparent re-hash (§4.1): store ``new_pw_hash`` only while the stored hash is still ``old_pw_hash``."""
    cursor = conn.execute(
        "UPDATE users SET pw_hash = ? WHERE id = ? AND pw_hash = ?", (new_pw_hash, user_id, old_pw_hash)
    )
    return cursor.rowcount == 1


def create_session(
    conn: Any,
    user_id: int,
    token_hash: str,
    ip: Optional[str],
    user_agent: Optional[str],
    session_days: float,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Insert a session row for an enabled user (``unauthorized`` otherwise); returns ``{token_hash, expires_at}``."""
    ts = util.now() if ts is None else ts
    require_active_user(conn, user_id)
    expires_at = ts + session_days * 86400
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, last_used_at, expires_at, user_agent, ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token_hash, user_id, ts, ts, expires_at, _clip(user_agent, 300), _clip(ip, 64)),
    )
    return {"token_hash": token_hash, "expires_at": expires_at}


def complete_login(
    conn: Any,
    user_id: int,
    token_hash: str,
    ip: Optional[str],
    user_agent: Optional[str],
    session_days: float,
    old_pw_hash: Optional[str] = None,
    new_pw_hash: Optional[str] = None,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """The one transaction of a successful ``POST /api/login``: re-read the user, store the session.

    A user that was disabled meanwhile raises ``RequestError('disabled')``. Sets ``last_login_at`` and
    ``last_seen_at``; the optional re-hash is applied only while ``old_pw_hash`` is still stored (a concurrent
    password change is never overwritten). Returns ``{first_login, user, me, expires_at}``; ``first_login`` is true
    when ``last_login_at`` was ``NULL`` (the caller then broadcasts ``ev.user_update`` with ``activated:true``).
    """
    ts = util.now() if ts is None else ts
    row = conn.execute("SELECT " + _USER_COLUMNS + " FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise _fail("unauthorized", "unknown user")
    if row[7]:
        raise _fail("disabled", "this account has been disabled")
    first_login = row[11] is None
    if new_pw_hash is not None and old_pw_hash is not None:
        update_pw_hash(conn, user_id, old_pw_hash, new_pw_hash)
    session = create_session(conn, user_id, token_hash, ip, user_agent, session_days, ts)
    conn.execute("UPDATE users SET last_login_at = ?, last_seen_at = ? WHERE id = ?", (ts, ts, user_id))
    row = conn.execute("SELECT " + _USER_COLUMNS + " FROM users WHERE id = ?", (user_id,)).fetchone()
    return {"first_login": first_login, "user": _public(row), "me": _me(row), "expires_at": session["expires_at"]}


def lookup_session(conn: Any, token_hash: str, ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The live session of ``token_hash`` or ``None`` (unknown, expired or disabled user).

    Returns ``{user_id, last_used_at, expires_at, must_change_password}``.
    """
    ts = util.now() if ts is None else ts
    row = conn.execute(
        "SELECT s.user_id, s.last_used_at, s.expires_at, u.must_change_password FROM sessions s"
        " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0",
        (token_hash, ts),
    ).fetchone()
    if row is None:
        return None
    return {
        "user_id": row[0],
        "last_used_at": row[1],
        "expires_at": row[2],
        "must_change_password": bool(row[3]),
    }


def touch_session(conn: Any, token_hash: str, session_days: float, ts: Optional[float] = None) -> Optional[float]:
    """Slide a session: ``last_used_at = ts``, ``expires_at = ts + session_days``. Returns the new ``expires_at``."""
    ts = util.now() if ts is None else ts
    expires_at = ts + session_days * 86400
    cursor = conn.execute(
        "UPDATE sessions SET last_used_at = ?, expires_at = ? WHERE token_hash = ?", (ts, expires_at, token_hash)
    )
    return expires_at if cursor.rowcount == 1 else None


def list_sessions(
    conn: Any, user_id: int, current_token_hash: Optional[str], ts: Optional[float] = None
) -> List[Dict[str, Any]]:
    """``GET /api/sessions``: the user's live sessions, most recently used first.

    Each entry is ``{id, created_at, last_used_at, ip, user_agent, current}``; ``id`` = first 16 hex chars of the hash.
    """
    ts = util.now() if ts is None else ts
    rows = conn.execute(
        "SELECT token_hash, created_at, last_used_at, ip, user_agent FROM sessions"
        " WHERE user_id = ? AND expires_at > ? ORDER BY last_used_at DESC, created_at DESC",
        (user_id, ts),
    ).fetchall()
    return [
        {
            "id": row[0][:16],
            "created_at": row[1],
            "last_used_at": row[2],
            "ip": row[3],
            "user_agent": row[4],
            "current": row[0] == current_token_hash,
        }
        for row in rows
    ]


def _delete_sessions(conn: Any, where: str, params: Tuple[Any, ...]) -> List[str]:
    hashes = [row[0] for row in conn.execute("SELECT token_hash FROM sessions WHERE " + where, params)]
    if hashes:
        conn.execute("DELETE FROM sessions WHERE " + where, params)
    return hashes


def revoke_session(conn: Any, user_id: int, session_id: Any) -> Optional[str]:
    """Delete one of the user's own sessions by its public 16-hex id; returns its ``token_hash`` or ``None``.

    ``None`` for an unknown id, another user's id or a malformed id (``POST /api/sessions/revoke`` answers 404).
    """
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None
    hashes = _delete_sessions(conn, "user_id = ? AND substr(token_hash, 1, 16) = ?", (user_id, session_id))
    return hashes[0] if hashes else None


def revoke_other_sessions(conn: Any, user_id: int, keep_token_hash: Optional[str]) -> List[str]:
    """Delete every session of the user except ``keep_token_hash``; returns the revoked hashes."""
    return _delete_sessions(conn, "user_id = ? AND token_hash != ?", (user_id, keep_token_hash or ""))


def revoke_user_sessions(conn: Any, user_id: int) -> List[str]:
    """Delete every session of the user (admin reset, disable); returns the revoked hashes."""
    return _delete_sessions(conn, "user_id = ?", (user_id,))


def delete_session(conn: Any, user_id: int, token_hash: str) -> bool:
    """``POST /api/logout``: delete this session of this user. ``True`` when a row was removed."""
    return len(_delete_sessions(conn, "user_id = ? AND token_hash = ?", (user_id, token_hash))) == 1


def purge_expired_sessions(conn: Any, ts: Optional[float] = None) -> int:
    """Delete expired sessions (hourly sweep); returns how many."""
    cursor = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (util.now() if ts is None else ts,))
    return int(cursor.rowcount)


def session_statuses(conn: Any, sessions: Sequence[Tuple[int, str]], ts: Optional[float] = None) -> Dict[str, str]:
    """Set-based re-validation for ``hub.revalidate_all`` (§2.4): ``{token_hash: 'ok'|'revoked'|'disabled'}``.

    ``sessions`` is a list of ``(user_id, token_hash)``. ``disabled`` wins over ``revoked``; ``revoked`` means the
    session row is gone or expired.
    """
    ts = util.now() if ts is None else ts
    result: Dict[str, str] = {}
    for part in _chunks(list(sessions)):
        hashes = [token for _, token in part]
        live = {
            row[0]
            for row in conn.execute(
                "SELECT token_hash FROM sessions WHERE token_hash IN (" + _marks(len(hashes)) + ") AND expires_at > ?",
                (*hashes, ts),
            )
        }
        ids = sorted({user_id for user_id, _ in part})
        disabled = {
            row[0]
            for row in conn.execute(
                "SELECT id FROM users WHERE disabled = 1 AND id IN (" + _marks(len(ids)) + ")", tuple(ids)
            )
        }
        for user_id, token in part:
            if user_id in disabled:
                result[token] = "disabled"
            elif token in live:
                result[token] = "ok"
            else:
                result[token] = "revoked"
    return result


def set_last_seen(conn: Any, user_ids: Iterable[int], ts: float) -> None:
    """``users.last_seen_at = ts`` for these users (the 60-second heartbeat and the shutdown write, §8.4)."""
    ids = [int(user_id) for user_id in user_ids]
    for part in _chunks(ids):
        conn.execute("UPDATE users SET last_seen_at = ? WHERE id IN (" + _marks(len(part)) + ")", (ts, *part))


# --------------------------------------------------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------------------------------------------------


def set_user_password(
    conn: Any, user_id: int, pw_hash: str, must_change: bool, revoke_sessions: bool = True
) -> List[str]:
    """Store a new hash, set ``must_change_password`` and (by default) delete every session; returns revoked hashes.

    Raises ``RequestError('not_found')`` for an unknown user. Used by ``admin_reset_password`` and the CLI.
    """
    cursor = conn.execute(
        "UPDATE users SET pw_hash = ?, must_change_password = ? WHERE id = ?",
        (pw_hash, 1 if must_change else 0, user_id),
    )
    if cursor.rowcount != 1:
        raise _fail("not_found", "no such user")
    return revoke_user_sessions(conn, user_id) if revoke_sessions else []


def change_password(
    conn: Any, user_id: int, old_pw_hash: str, new_pw_hash: str, keep_token_hash: Optional[str]
) -> List[str]:
    """``POST /api/password`` after the old password verified against ``old_pw_hash``.

    Re-reads the user (disabled: ``unauthorized``; stored hash no longer ``old_pw_hash``: ``conflict``),
    stores ``new_pw_hash``, clears ``must_change_password`` and deletes every OTHER session. Returns the revoked
    ``token_hash`` list (the hub then calls ``hub.revoke(..., except_token_hash=keep_token_hash)``).
    """
    require_active_user(conn, user_id)
    cursor = conn.execute(
        "UPDATE users SET pw_hash = ?, must_change_password = 0 WHERE id = ? AND pw_hash = ?",
        (new_pw_hash, user_id, old_pw_hash),
    )
    if cursor.rowcount != 1:
        raise _fail("conflict", "the password was changed meanwhile")
    return revoke_other_sessions(conn, user_id, keep_token_hash)


# --------------------------------------------------------------------------------------------------------------------
# Profile (SPEC §7.4 profile.update)
# --------------------------------------------------------------------------------------------------------------------


def profile_update(conn: Any, user_id: int, fields: Dict[str, Any], ts: Optional[float] = None) -> Dict[str, Any]:
    """``profile.update``: apply ``display_name``, ``status_text``, ``read_receipts``, ``show_last_seen``.

    ``bad_request`` when none of the four fields is present or a value is invalid; ``conflict`` with
    ``reason='name_taken'`` for a taken or reserved (non-admin) display name; ``unauthorized`` when the caller is
    disabled. Fields equal to the stored value are a no-op (§7.3). Returns ``{me, changed, changed_fields}``; the hub
    emits ``ev.user_update`` and ``ev.me`` only when ``changed``.
    """
    row = conn.execute("SELECT " + _USER_COLUMNS + " FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row[7]:
        raise _fail("unauthorized", "not signed in")
    wanted = {
        name: fields[name]
        for name in ("display_name", "status_text", "read_receipts", "show_last_seen")
        if name in fields
    }
    if not wanted:
        raise _fail("bad_request", "nothing to update")
    updates: Dict[str, Any] = {}
    if "display_name" in wanted:
        name = normalize_display_name(wanted["display_name"])
        if name is None:
            raise _fail("bad_request", "display name must be 1-40 characters")
        if name != row[2]:
            key = util.display_key(name)
            _check_display_key(conn, key, user_id, allow_reserved=row[4] == "admin")
            updates["display_name"] = name
            updates["display_key"] = key
    if "status_text" in wanted:
        status = normalize_status_text(wanted["status_text"])
        if status is None:
            raise _fail("bad_request", "status must be at most 140 characters")
        if status != row[3]:
            updates["status_text"] = status
    for flag, index in (("read_receipts", 5), ("show_last_seen", 6)):
        if flag in wanted:
            if not isinstance(wanted[flag], bool):
                raise _fail("bad_request", flag + " must be true or false")
            if int(wanted[flag]) != int(row[index]):
                updates[flag] = int(wanted[flag])
    if updates:
        assignments = ", ".join(column + " = ?" for column in updates)
        conn.execute("UPDATE users SET " + assignments + " WHERE id = ?", (*updates.values(), user_id))
    row = conn.execute("SELECT " + _USER_COLUMNS + " FROM users WHERE id = ?", (user_id,)).fetchone()
    changed = sorted(name for name in updates if name != "display_key")
    return {"me": _me(row), "changed": bool(updates), "changed_fields": changed}


# --------------------------------------------------------------------------------------------------------------------
# Admin (SPEC §7.4 admin.*; admin.update_user is owned by db_chats)
# --------------------------------------------------------------------------------------------------------------------


def admin_users(conn: Any, actor_id: int) -> List[Dict[str, Any]]:
    """``admin.users``: every ``User`` plus ``{created_at, last_login_at, message_count, must_change_password}``."""
    require_admin(conn, actor_id)
    rows = conn.execute(
        "SELECT " + _USER_COLUMNS + ", (SELECT COUNT(*) FROM messages m WHERE m.sender_id = u.id) AS message_count"
        " FROM users u ORDER BY u.id"
    ).fetchall()
    users = []
    for row in rows:
        entry = _public(row)
        entry["created_at"] = row[9]
        entry["last_login_at"] = row[11]
        entry["message_count"] = row[12]
        entry["must_change_password"] = bool(row[8])
        users.append(entry)
    return users


def admin_reset_password(
    conn: Any, actor_id: int, user_id: int, pw_hash: str, ip: Optional[str] = None, ts: Optional[float] = None
) -> Dict[str, Any]:
    """``admin.reset_password``: new hash, ``must_change_password=1``, every session of the target deleted, audit row.

    ``forbidden``/``unauthorized`` for a non-admin actor, ``not_found`` for an unknown target. Returns
    ``{user_id, username, revoked}``; ``revoked`` lists the deleted token hashes (the hub then calls
    ``hub.revoke(reason='revoked')``).
    """
    require_admin(conn, actor_id)
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise _fail("not_found", "no such user")
    revoked = set_user_password(conn, user_id, pw_hash, must_change=True)
    audit(conn, actor_id, "admin.reset_password", user_id, ip, ts)
    return {"user_id": user_id, "username": row[0], "revoked": revoked}


def admin_settings(
    conn: Any,
    actor_id: int,
    default_name: str,
    default_registration_open: bool,
    ip: Optional[str] = None,
    workspace_name: Any = None,
    registration_open: Any = None,
    rotate_join_code: Any = None,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """``admin.settings``: read (no field given) or edit the workspace settings; admin only.

    ``{}`` is a read: nothing is written, no audit row. Fields equal to the effective values are a no-op.
    ``bad_request`` for an invalid name or a non-boolean flag. Returns ``{workspace:{name, registration_open,
    join_code}, changed}`` (the hub emits ``ev.workspace`` only when ``changed``); one ``admin.settings`` audit row per
    effective change. Never renames the default group.
    """
    require_admin(conn, actor_id)
    current = workspace_settings(conn, default_name, default_registration_open)
    changed = False
    if workspace_name is not None:
        name = normalize_workspace_name(workspace_name)
        if name is None:
            raise _fail("bad_request", "workspace name must be 1-40 characters")
        if name != current["name"]:
            db.set_meta(conn, "workspace_name", name)
            changed = True
    if registration_open is not None:
        if not isinstance(registration_open, bool):
            raise _fail("bad_request", "registration_open must be true or false")
        if registration_open != current["registration_open"]:
            db.set_meta(conn, "registration_open", "1" if registration_open else "0")
            changed = True
    if rotate_join_code is not None and not isinstance(rotate_join_code, bool):
        raise _fail("bad_request", "rotate_join_code must be true or false")
    if rotate_join_code:
        db.set_meta(conn, "join_code", util.short_code())
        changed = True
    if changed:
        audit(conn, actor_id, "admin.settings", None, ip, ts)
    return {"workspace": workspace_settings(conn, default_name, default_registration_open, True), "changed": changed}


def admin_stats(conn: Any, actor_id: int) -> Dict[str, Any]:
    """The database part of ``admin.stats``: ``{users, chats, messages, attachments, last_backup_at}``.

    The hub adds ``online``, ``storage_bytes`` (the hourly cache of :func:`attachment_storage_bytes`), ``db_bytes``,
    ``disk_free_bytes``, ``uptime_s``, ``python``, ``version`` and ``urls``.
    """
    require_admin(conn, actor_id)
    stats: Dict[str, Any] = {
        "users": user_count(conn),
        "chats": int(conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]),
        "messages": int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
        "attachments": int(conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]),
        "last_backup_at": None,
    }
    stored = db.get_meta(conn, "last_backup_at")
    if stored is not None:
        try:
            stats["last_backup_at"] = float(stored)
        except ValueError:
            log.warning("meta.last_backup_at is not a number")
    return stats


def admin_audit(conn: Any, actor_id: int, limit: Any = 100) -> List[Dict[str, Any]]:
    """``admin.audit``: newest first, ``limit`` 1..100; entries ``{id, ts, actor_id, action, target_id, ip}``."""
    require_admin(conn, actor_id)
    count = min(max(limit if isinstance(limit, int) and not isinstance(limit, bool) else 100, 1), 100)
    rows = conn.execute(
        "SELECT id, ts, actor_id, action, target_id, ip FROM audit_log ORDER BY id DESC LIMIT ?", (count,)
    ).fetchall()
    return [{"id": r[0], "ts": r[1], "actor_id": r[2], "action": r[3], "target_id": r[4], "ip": r[5]} for r in rows]


# --------------------------------------------------------------------------------------------------------------------
# Attachments: insert, access rule, quotas, orphan sweep (SPEC §2.4, §4.2, §5.1)
# --------------------------------------------------------------------------------------------------------------------

#: Bytes of the user's uploads that no message references (§5.1). ``(user_id,)``. Plan: SEARCH attachments by
#: ``attachments_uploader`` and SEARCH messages by ``messages_attachment`` (no ``SCAN`` of either table). The index
#: hints keep the plan independent of ``ANALYZE`` statistics (a single heavy uploader would otherwise tip it to a scan).
UPLOAD_UNATTACHED_BYTES_SQL = (
    "SELECT COALESCE(SUM(a.size), 0) FROM attachments a INDEXED BY attachments_uploader WHERE a.uploader_id = ?"
    " AND NOT EXISTS (SELECT 1 FROM messages m INDEXED BY messages_attachment WHERE m.attachment_id = a.id)"
)

#: Bytes the user uploaded after a timestamp (rolling 24 h quota, §5.1). ``(user_id, since_ts)``.
UPLOAD_RECENT_BYTES_SQL = (
    "SELECT COALESCE(SUM(size), 0) FROM attachments INDEXED BY attachments_uploader"
    " WHERE uploader_id = ? AND created_at > ?"
)

#: ``/files/<id>`` access rule (§4.2): the attachment row when the user uploaded it or can see a non-deleted message
#: that references it (§3.1). Parameters ``(attachment_id, user_id, user_id, user_id)`` (plain ``?`` placeholders).
ATTACHMENT_ACCESS_SQL = (
    "SELECT a.id, a.uploader_id, a.name, a.mime, a.kind, a.size, a.path, a.width, a.height, a.duration, a.created_at"
    " FROM attachments a WHERE a.id = ? AND (a.uploader_id = ? OR EXISTS ("
    "SELECT 1 FROM messages m INDEXED BY messages_attachment"
    " JOIN chat_members cm ON cm.chat_id = m.chat_id AND cm.user_id = ? AND cm.listed = 1"
    " WHERE m.attachment_id = a.id AND m.deleted_at IS NULL"
    " AND m.id > MAX(cm.history_from_id, cm.cleared_before_id)"
    " AND NOT EXISTS (SELECT 1 FROM hidden_messages h WHERE h.user_id = ? AND h.message_id = m.id)))"
)

#: Orphan sweep, step 1 (§2.4): unattached attachments created before a cutoff, oldest first. ``(cutoff_ts, limit)``.
ORPHAN_UNATTACHED_SQL = (
    "SELECT a.id, a.path FROM attachments a INDEXED BY attachments_created WHERE a.created_at < ?"
    " AND NOT EXISTS (SELECT 1 FROM messages m INDEXED BY messages_attachment WHERE m.attachment_id = a.id)"
    " ORDER BY a.created_at LIMIT ?"
)

#: Orphan sweep, step 3 (§2.4): attachment rows in id order for the missing-file check. ``(after_id, limit)``.
ATTACHMENT_BATCH_SQL = "SELECT id, path FROM attachments WHERE id > ? ORDER BY id LIMIT ?"


def attachment_public(row: Dict[str, Any]) -> Dict[str, Any]:
    """The ``attachment`` object of §7.2: ``{id, name, mime, size, kind, url, width, height, duration}``."""
    return {
        "id": row["id"],
        "name": row["name"],
        "mime": row["mime"],
        "size": row["size"],
        "kind": row["kind"],
        "url": "/files/" + row["id"],
        "width": row.get("width"),
        "height": row.get("height"),
        "duration": row.get("duration"),
    }


def _attachment_dict(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "id": row[0],
        "uploader_id": row[1],
        "name": row[2],
        "mime": row[3],
        "kind": row[4],
        "size": row[5],
        "path": row[6],
        "width": row[7],
        "height": row[8],
        "duration": row[9],
        "created_at": row[10],
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_attachment(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the row of an upload before it is stored (``bad_request`` for a malformed field).

    ``width``/``height`` (1..16384) and ``duration`` (finite, 0..86400, never for ``kind='file'``) are client hints
    (§5.6): anything else becomes ``None`` instead of failing the upload.
    """
    attachment_id = raw.get("id")
    if not isinstance(attachment_id, str) or not ATTACHMENT_ID_RE.match(attachment_id):
        raise _fail("bad_request", "bad attachment id")
    for key in ("name", "mime", "path"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise _fail("bad_request", "bad attachment " + key)
    if raw.get("kind") not in ("image", "audio", "video", "file"):
        raise _fail("bad_request", "bad attachment kind")
    if not _is_int(raw.get("size")) or raw["size"] < 0:
        raise _fail("bad_request", "bad attachment size")
    parts = raw["path"].replace(BACKSLASH, "/").split("/")
    if raw["path"].startswith(("/", BACKSLASH)) or ".." in parts or ":" in raw["path"]:
        raise _fail("bad_request", "bad attachment path")
    duration = raw.get("duration")
    plausible = (
        raw["kind"] != "file"
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and 0 <= duration <= 86400  # false for NaN
    )
    cleaned = {key: raw[key] for key in ("id", "name", "mime", "kind", "size", "path")}
    for key in ("width", "height"):
        value = raw.get(key)
        cleaned[key] = value if _is_int(value) and 1 <= value <= 16384 else None
    cleaned["duration"] = float(duration) if plausible else None
    return cleaned


def upload_usage(conn: Any, user_id: int, ts: Optional[float] = None, window: float = 86400.0) -> Dict[str, int]:
    """The two upload-quota numbers of §5.1: ``{unattached_bytes, recent_bytes}`` (uploads since ``ts - window``)."""
    ts = util.now() if ts is None else ts
    unattached = conn.execute(UPLOAD_UNATTACHED_BYTES_SQL, (user_id,)).fetchone()[0]
    recent = conn.execute(UPLOAD_RECENT_BYTES_SQL, (user_id, ts - window)).fetchone()[0]
    return {"unattached_bytes": int(unattached), "recent_bytes": int(recent)}


def insert_attachment(
    conn: Any,
    uploader_id: int,
    attachment: Dict[str, Any],
    max_unattached_bytes: Optional[int] = None,
    max_recent_bytes: Optional[int] = None,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Store the row of a finished upload; returns the public ``attachment`` object (§7.2).

    ``attachment`` keys: ``id`` (32 hex), ``name``, ``mime``, ``kind``, ``size``, ``path`` (relative to ``uploads/``),
    optional ``width``/``height``/``duration``. Re-checks, inside the transaction, that the uploader is enabled
    (``unauthorized``) and that the quotas still hold (``RequestError('quota_exceeded')`` when ``unattached bytes +
    size`` exceeds ``max_unattached_bytes`` or the last 24 h exceed ``max_recent_bytes``; ``None`` skips a check).
    """
    ts = util.now() if ts is None else ts
    require_active_user(conn, uploader_id)
    attachment = _clean_attachment(attachment)
    attachment_id = attachment["id"]
    size = attachment["size"]
    usage = upload_usage(conn, uploader_id, ts)
    if max_unattached_bytes is not None and usage["unattached_bytes"] + size > max_unattached_bytes:
        raise _fail("quota_exceeded", "too many unsent uploads; send or delete some first")
    if max_recent_bytes is not None and usage["recent_bytes"] + size > max_recent_bytes:
        raise _fail("quota_exceeded", "daily upload quota exceeded")
    conn.execute(
        "INSERT INTO attachments(id, uploader_id, name, mime, kind, size, path, width, height, duration, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attachment_id,
            uploader_id,
            attachment["name"],
            attachment["mime"],
            attachment["kind"],
            size,
            attachment["path"],
            attachment["width"],
            attachment["height"],
            attachment["duration"],
            ts,
        ),
    )
    return attachment_public(attachment)


def get_attachment(conn: Any, attachment_id: str) -> Optional[Dict[str, Any]]:
    """The attachment row (including ``uploader_id``, ``path``, ``created_at``) or ``None``. No access check."""
    row = conn.execute(
        "SELECT id, uploader_id, name, mime, kind, size, path, width, height, duration, created_at"
        " FROM attachments WHERE id = ?",
        (attachment_id,),
    ).fetchone()
    return _attachment_dict(row) if row is not None else None


def attachment_access(conn: Any, user_id: int, attachment_id: Any) -> Optional[Dict[str, Any]]:
    """``/files/<id>`` access rule (§4.2): the attachment row, or ``None`` (404, never 403).

    ``None`` as well for an id that is not 32 lowercase hex characters (no query is made).
    """
    if not isinstance(attachment_id, str) or not ATTACHMENT_ID_RE.match(attachment_id):
        return None
    caller = conn.execute("SELECT disabled FROM users WHERE id = ?", (user_id,)).fetchone()
    if caller is None or caller[0]:  # a disabled or unknown account reaches nothing (rule §7.1.1(e))
        return None
    row = conn.execute(ATTACHMENT_ACCESS_SQL, (attachment_id, user_id, user_id, user_id)).fetchone()
    return _attachment_dict(row) if row is not None else None


def attachment_storage_bytes(conn: Any) -> int:
    """Total size of all attachment rows (the hourly cached ``admin.stats.storage_bytes``)."""
    return int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM attachments").fetchone()[0])


def drop_attachment_if_unreferenced(conn: Any, attachment_id: str) -> Optional[str]:
    """Delete the row when no message references it; returns its ``path`` (the caller deletes the file) or ``None``.

    Used after delete-for-everyone (SPEC §3 rules) and by :func:`sweep_orphans`.
    """
    row = conn.execute("SELECT path FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None:
        return None
    cursor = conn.execute(
        "DELETE FROM attachments WHERE id = ? AND NOT EXISTS"
        " (SELECT 1 FROM messages m WHERE m.attachment_id = attachments.id)",
        (attachment_id,),
    )
    return str(row[0]) if cursor.rowcount == 1 else None


def _stored_file(uploads_dir: str, relative: str) -> Optional[str]:
    """Absolute path of ``relative`` below ``uploads_dir``, or ``None`` when it would escape it."""
    full = os.path.normpath(os.path.join(uploads_dir, relative))
    root = os.path.normpath(uploads_dir)
    try:
        inside = os.path.commonpath([root, full]) == root
    except ValueError:
        inside = False
    return full if inside and full != root else None


def _remove_stored(uploads_dir: str, relative: str) -> None:
    target = _stored_file(uploads_dir, relative)
    if target is not None:
        util.retry_file_op(os.remove, target)


def sweep_orphans(
    conn: Any,
    uploads_dir: str,
    ts: Optional[float] = None,
    unattached_after: float = 7200.0,
    part_after: float = 3600.0,
    file_after: float = 86400.0,
    batch: int = 500,
) -> Dict[str, int]:
    """The 15-minute orphan sweep of SPEC §2.4 on an own connection (never the writer).

    Deletes (1) unattached attachments older than ``unattached_after`` (row, then file), (2) ``uploads/.tmp/*.part``
    older than ``part_after``, (3) attachment rows whose file is missing and that no message references (a referenced
    row with a missing file is kept and logged once), (4) files in ``uploads/<aa>/`` with a 32-hex name, no row and
    an mtime older than ``file_after``. Each row deletion has its own ``IntegrityError`` guard so one bad row cannot
    abort the sweep. Returns ``{unattached, parts, missing_rows, orphan_files, missing_referenced}``.
    """
    ts = util.now() if ts is None else ts
    counts = {"unattached": 0, "parts": 0, "missing_rows": 0, "orphan_files": 0, "missing_referenced": 0}

    stale = conn.execute(ORPHAN_UNATTACHED_SQL, (ts - unattached_after, batch)).fetchall()
    for attachment_id, _relative in stale:
        try:
            path = drop_attachment_if_unreferenced(conn, attachment_id)
        except sqlite3.IntegrityError:
            log.warning("orphan sweep: could not delete attachment row %s", util.safe_log_value(attachment_id))
            continue
        if path is not None:
            _remove_stored(uploads_dir, path)
            counts["unattached"] += 1

    tmp_dir = os.path.join(uploads_dir, ".tmp")
    if os.path.isdir(tmp_dir):
        for entry in os.scandir(tmp_dir):
            if entry.name.endswith(".part") and entry.is_file(follow_symlinks=False):
                try:
                    age_ok = entry.stat().st_mtime < ts - part_after
                except OSError:
                    continue
                if age_ok and util.retry_file_op(os.remove, entry.path):
                    counts["parts"] += 1

    after = ""
    while True:
        rows = conn.execute(ATTACHMENT_BATCH_SQL, (after, batch)).fetchall()
        if not rows:
            break
        after = rows[-1][0]
        for attachment_id, relative in rows:
            target = _stored_file(uploads_dir, relative)
            if target is not None and os.path.exists(target):
                continue
            try:
                dropped = drop_attachment_if_unreferenced(conn, attachment_id)
            except sqlite3.IntegrityError:
                dropped = None
            if dropped is not None:
                counts["missing_rows"] += 1
            else:
                counts["missing_referenced"] += 1
    if counts["missing_referenced"]:
        log.warning("orphan sweep: %d referenced attachment(s) have no file", counts["missing_referenced"])

    if os.path.isdir(uploads_dir):
        for shard in os.scandir(uploads_dir):
            if len(shard.name) != 2 or not shard.is_dir(follow_symlinks=False):
                continue
            if not re.match(r"^[0-9a-f]{2}\Z", shard.name):
                continue
            for entry in os.scandir(shard.path):
                if not ATTACHMENT_ID_RE.match(entry.name) or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    old = entry.stat().st_mtime < ts - file_after
                except OSError:
                    continue
                if not old or conn.execute("SELECT 1 FROM attachments WHERE id = ?", (entry.name,)).fetchone():
                    continue
                if util.retry_file_op(os.remove, entry.path):
                    counts["orphan_files"] += 1
    return counts
