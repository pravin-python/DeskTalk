"""Chats, members, direct/group rules, prefs, clear, the ``ev.ready`` snapshot and Chat serialisation (SPEC 3, 7).

Every public ``fn(conn, ...)`` here is synchronous and runs inside ``Database.run`` (writer, already inside
``BEGIN IMMEDIATE``) or ``Database.run_read`` (reader); none of them commits, rolls back or runs a script.
Protocol-level failures raise :class:`ChatError` following the evaluation order of SPEC 7.1.1.  Write functions return
an *outcome* dict (see :func:`outcome`): the ``res`` payload, the ordered ready-made ``events`` and the post-write
record of SPEC 7.6(6) (``recipients``, ``chats``, ``read_sync``, ``receipt`` ...).  ``docs/DB_API_chat.md`` is the
manual.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import db, db_messages, db_receipts, util

__all__ = [
    "ChatError",
    "add_member_row",
    "admin_update_user",
    "admin_user_chat_ids",
    "build_chat",
    "build_chats",
    "build_ready",
    "chat_add_members",
    "chat_clear",
    "chat_create_group",
    "chat_get",
    "chat_leave",
    "chat_members_event",
    "chat_open_direct",
    "chat_prefs",
    "chat_remove_member",
    "chat_set_admin",
    "chat_update",
    "chat_update_event",
    "configure_limits",
    "create_everyone_chat",
    "direct_chat_id",
    "index_entry",
    "load_membership_index",
]

MAX_INT = 2**53 - 1
MAX_GROUP_MEMBERS = 200
MAX_PINNED_CHATS = 3
MAX_ADD_BATCH = 50
_TITLE_MAX = 60
_DESCRIPTION_MAX = 500
_DISPLAY_NAME_MAX = 40
_MUTE_MAX = 4102444800
_IN_CHUNK = 400


class ChatError(db.RequestError):
    """A protocol-level failure (SPEC 7.1.1): ``code`` plus the optional ``err`` fields of the ``res`` envelope.

    A subclass of ``db.RequestError`` (same constructor, ``to_err()`` and fields), so ``except db.RequestError``
    catches the failures of every db function, db-core's and db-chat's alike.
    """


class _Limits:
    """Runtime limits shared by the three modules (``configure_limits``); the defaults are the SPEC 2.1 values."""

    def __init__(self) -> None:
        self.max_body_chars = 8000
        self.edit_window_s = 900
        self.delete_window_s = 172800
        self.unread_cap = 1000


LIMITS = _Limits()


def configure_limits(
    max_body_chars: Optional[int] = None,
    edit_window_s: Optional[float] = None,
    delete_window_s: Optional[float] = None,
    unread_cap: Optional[int] = None,
) -> None:
    """Set the process-wide limits (the hub calls this once from ``cfg`` / ``cfg.test_limits['unread_cap']``).

    ``None`` leaves a value unchanged.  The defaults are 8000 characters, 900 s, 172800 s and 1000.
    """
    if max_body_chars is not None:
        LIMITS.max_body_chars = int(max_body_chars)
    if edit_window_s is not None:
        LIMITS.edit_window_s = edit_window_s
    if delete_window_s is not None:
        LIMITS.delete_window_s = delete_window_s
    if unread_cap is not None:
        LIMITS.unread_cap = int(unread_cap)


# --------------------------------------------------------------------------------------------------------------------
# Low-level helpers shared by db_chats / db_messages / db_receipts
# --------------------------------------------------------------------------------------------------------------------


def q_all(conn: Any, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """Run a query and return the rows as dicts keyed by column name (works for tuple and ``sqlite3.Row`` rows)."""
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def q_one(conn: Any, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    """Like :func:`q_all` for at most one row (``None`` when there is none)."""
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in cur.description], row))


def chunked(items: Sequence[Any], size: int = _IN_CHUNK) -> List[List[Any]]:
    """Split ``items`` into lists of at most ``size`` (SQLite allows 999 bound variables in old builds)."""
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def placeholders(count: int) -> str:
    """``'?,?,?'`` for an ``IN (...)`` list of ``count`` bound values."""
    return ",".join("?" * count)


def need_int(value: Any, name: str, low: Optional[int] = None, high: Optional[int] = None) -> int:
    """Validate an ``int`` (``bool`` rejected) inside ``[low, high]``; else ``bad_request``."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or (low is not None and value < low)
        or (high is not None and value > high)
    ):
        raise ChatError("bad_request", "%s must be an integer in range" % name)
    return value


def need_id(value: Any, name: str) -> int:
    """A database id: ``int`` in ``[1, 2^53-1]``."""
    return need_int(value, name, 1, MAX_INT)


def need_uint(value: Any, name: str) -> int:
    """A watermark / cursor: ``int`` in ``[0, 2^53-1]``."""
    return need_int(value, name, 0, MAX_INT)


def need_bool(value: Any, name: str) -> bool:
    """Validate a real boolean."""
    if not isinstance(value, bool):
        raise ChatError("bad_request", "%s must be a boolean" % name)
    return value


def reject_invalid_text(*values: Any) -> None:
    """SPEC 7.1: a lone surrogate in any request string (lists included) => ``bad_request``, ``reason='invalid_text'``.

    SQLite binding and ``.encode()`` reject such strings, so every db function that takes text calls this first.
    """
    if util.has_lone_surrogate(values):
        raise ChatError("bad_request", "text contains invalid characters", reason="invalid_text")


def clean_text(value: Any, name: str, max_len: int, min_len: int = 0, newline: bool = False) -> str:
    """SPEC 4.3(4) normalisation of titles, descriptions and display names plus the length limits (code points)."""
    if not isinstance(value, str):
        raise ChatError("bad_request", "%s must be a string" % name)
    reject_invalid_text(value)
    text = util.normalize_text(value, allow_newline=newline)
    if not min_len <= len(text) <= max_len:
        raise ChatError("bad_request", "%s must be %d..%d characters" % (name, min_len, max_len))
    return text


def group(user_ids: Optional[Iterable[int]], d: Dict[str, Any]) -> Dict[str, Any]:
    """One encode-once audience group of an event: ``d`` goes to every connection of every user in ``user_ids``
    (``None`` = every connection)."""
    return {"user_ids": None if user_ids is None else list(user_ids), "d": d}


def event(
    t: str, groups: List[Dict[str, Any]], durable: bool = True, key: Optional[Tuple[Any, ...]] = None
) -> Dict[str, Any]:
    """An outbound event: type ``t``, audience ``groups``, ``durable`` flag, coalescing ``key`` and the SPEC 6 frame
    class ``cls`` (``durable``; ``keyed`` = ``ev.receipt``: coalesced by ``key``, never dropped; ``ephemeral``)."""
    cls = "durable" if durable else ("keyed" if key is not None else "ephemeral")
    return {"t": t, "cls": cls, "durable": durable, "key": key, "groups": groups}


def add_event(events: List[Dict[str, Any]], ev: Optional[Dict[str, Any]]) -> None:
    """Append ``ev`` unless it is ``None`` or reaches nobody (every group has an empty user list)."""
    if ev is None:
        return
    if ev["t"].startswith("ev.") and not any(g["user_ids"] is None or g["user_ids"] for g in ev["groups"]):
        return
    events.append(ev)


def outcome(
    res: Dict[str, Any],
    events: Optional[List[Dict[str, Any]]] = None,
    index: Optional[List[Dict[str, Any]]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """The result record of every write function.

    ``res`` is the ``d`` of the ``res`` frame; ``events`` the ordered fan-out plan (enqueue them in order, before the
    ``res``); ``index`` the membership-index entries to replace in the hub.  The remaining keys are the post-write
    record of SPEC 7.6(6) (empty / ``None`` when a request has nothing to report); ``extra`` overrides any of them.
    """
    out: Dict[str, Any] = {
        "res": res,
        "events": events if events is not None else [],
        "index": index if index is not None else [],
        "noop": False,
        "recipients": [],
        "hidden": set(),
        "starred": set(),
        "quote_hidden": set(),
        "quoted": None,
        "sender_status": None,
        "deduped": False,
        "read_sync": {},
        "receipt": None,
        "receipts": [],
        "chats": {},
    }
    out.update(extra)
    return out


def is_self_direct(direct_key: Optional[str]) -> bool:
    """True for the self-chat key ``"5:5"``."""
    if not direct_key:
        return False
    low, _, high = direct_key.partition(":")
    return low == high


def direct_peer(direct_key: str, me: int) -> int:
    """The other user of a direct chat key ``"low:high"`` (``me`` itself for the self-chat)."""
    low, _, high = direct_key.partition(":")
    return int(high) if int(low) == me else int(low)


# --------------------------------------------------------------------------------------------------------------------
# Caller, membership, users
# --------------------------------------------------------------------------------------------------------------------

_USER_COLUMNS = (
    "id, username, display_name, role, disabled, read_receipts, show_last_seen, status_text, last_login_at,"
    " last_seen_at, must_change_password"
)


def caller(conn: Any, user_id: int) -> Dict[str, Any]:
    """The caller's ``users`` row; a missing or disabled account => ``unauthorized`` (SPEC 7.1.1 (e))."""
    row = q_one(conn, "SELECT %s FROM users WHERE id = ?" % _USER_COLUMNS, (user_id,))
    if row is None or row["disabled"]:
        raise ChatError("unauthorized", "account is disabled")
    return row


def name_of(conn: Any, user_id: int) -> str:
    """``users.display_name`` of a user (empty for an unknown id)."""
    row = conn.execute("SELECT display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row is not None else ""


_MEMBER_SQL = (
    "SELECT c.id AS chat_id, c.kind AS kind, c.title AS title, c.description AS description,"
    " c.direct_key AS direct_key, c.is_default AS is_default, c.only_admins_post AS only_admins_post,"
    " c.created_by AS created_by,"
    " c.created_at AS created_at, c.last_message_id AS last_message_id, c.last_activity_at AS last_activity_at,"
    " cm.role AS my_role, cm.joined_at AS joined_at, cm.history_from_id AS history_from_id,"
    " cm.cleared_before_id AS cleared_before_id, cm.last_read_id AS last_read_id,"
    " cm.read_receipt_id AS read_receipt_id, cm.delivered_id AS delivered_id, cm.muted_until AS muted_until,"
    " cm.pinned_at AS pinned_at, cm.archived AS archived, cm.listed AS listed"
    " FROM chats c JOIN chat_members cm ON cm.chat_id = c.id AND cm.user_id = ? WHERE c.id = ?"
)


def require_member(conn: Any, user_id: int, chat_id: int) -> Dict[str, Any]:
    """The chat joined with the caller's LISTED member row, or ``not_member`` (SPEC 7.1.1: unknown chat, left chat and
    an unlisted dormant-chat peer are indistinguishable)."""
    row = q_one(conn, _MEMBER_SQL + " AND cm.listed = 1", (user_id, chat_id))
    if row is None:
        raise ChatError("not_member", "not a member of this chat", chat_id=chat_id)
    return row


def member_entry(row: Dict[str, Any]) -> Dict[str, Any]:
    """The ``members[]`` shape of SPEC 7.2 from a member row (``user_id``, ``role``, watermarks)."""
    return {
        "user_id": row["user_id"],
        "role": row["role"],
        "delivered_up_to": row["delivered_id"],
        "read_up_to": row["read_receipt_id"],
    }


def _member_rows(conn: Any, chat_ids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Members of the chats (one query per 400 chats), ordered ``joined_at, user_id``, with the user flags that the
    status aggregation needs.  Direct chats include an unlisted peer (visible only in the creator's dormant view)."""
    out: Dict[int, List[Dict[str, Any]]] = {cid: [] for cid in chat_ids}
    for chunk in chunked(list(chat_ids)):
        rows = q_all(
            conn,
            "SELECT cm.chat_id AS chat_id, cm.user_id AS user_id, cm.role AS role, cm.joined_at AS joined_at,"
            " cm.delivered_id AS delivered_id, cm.read_receipt_id AS read_receipt_id, cm.listed AS listed,"
            " u.disabled AS disabled, u.last_login_at IS NOT NULL AS activated, u.read_receipts AS read_receipts"
            " FROM chat_members cm JOIN chats c ON c.id = cm.chat_id JOIN users u ON u.id = cm.user_id"
            " WHERE cm.chat_id IN (%s) AND (cm.listed = 1 OR c.kind = 'direct')"
            " ORDER BY cm.chat_id, cm.joined_at, cm.user_id" % placeholders(len(chunk)),
            chunk,
        )
        for r in rows:
            out[r["chat_id"]].append(r)
    return out


def recipients_of(conn: Any, chat_id: int) -> List[Dict[str, Any]]:
    """The ``recipients`` list of SPEC 7.6(6): one entry per current listed member (ascending ``user_id``) with the
    watermarks, ``history_from_id`` / ``cleared_before_id`` and the three ``users`` flags (``role`` is an extra)."""
    rows = q_all(
        conn,
        "SELECT cm.user_id AS user_id, cm.role AS role, cm.history_from_id AS history_from_id,"
        " cm.cleared_before_id AS cleared_before_id, cm.last_read_id AS last_read_id,"
        " cm.delivered_id AS delivered_id, cm.read_receipt_id AS read_receipt_id, u.disabled AS disabled,"
        " u.last_login_at IS NOT NULL AS activated, u.read_receipts AS read_receipts"
        " FROM chat_members cm JOIN users u ON u.id = cm.user_id WHERE cm.chat_id = ? AND cm.listed = 1"
        " ORDER BY cm.user_id",
        (chat_id,),
    )
    for r in rows:
        r["disabled"] = bool(r["disabled"])
        r["activated"] = bool(r["activated"])
        r["read_receipts"] = bool(r["read_receipts"])
    return rows


# --------------------------------------------------------------------------------------------------------------------
# Cross-contract with db-core (registration): Everyone chat + member rows
# --------------------------------------------------------------------------------------------------------------------


def create_everyone_chat(conn: Any, title: str, created_by: Optional[int], ts: float) -> int:
    """Insert the default ``Everyone`` group row (SPEC 3 rules) and return its id.

    Only the chat row is created: the caller adds the first admin with :func:`add_member_row` and then inserts the
    ``created`` system message (``db_messages.insert_system_message``) so the admin sees it.
    """
    cur = conn.execute(
        "INSERT INTO chats(kind, title, description, direct_key, is_default, only_admins_post, created_by, created_at,"
        " last_message_id, last_activity_at) VALUES ('group', ?, '', NULL, 1, 0, ?, ?, NULL, ?)",
        (title, created_by, ts, ts),
    )
    return int(cur.lastrowid)


def add_member_row(conn: Any, chat_id: int, user_id: int, role: str, ts: float, listed: int = 1) -> None:
    """Insert a ``chat_members`` row obeying "a newly added member cannot see history" (SPEC 3).

    ``history_from_id`` and the three watermarks start at ``chats.last_message_id`` (0 for an empty chat); this must
    be called BEFORE the ``added``/``joined``/``created`` system message is inserted so the new member sees it.
    Re-adding a removed user is a plain insert (the old row was deleted), i.e. prefs reset and a new ``joined_at``.
    """
    row = conn.execute("SELECT last_message_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
    base = int(row[0]) if row is not None and row[0] is not None else 0
    conn.execute(
        "INSERT INTO chat_members(chat_id, user_id, role, joined_at, history_from_id, cleared_before_id, last_read_id,"
        " read_receipt_id, delivered_id, muted_until, pinned_at, archived, listed)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 0, NULL, 0, ?)",
        (chat_id, user_id, role, ts, base, base, base, base, listed),
    )


# --------------------------------------------------------------------------------------------------------------------
# Membership index for the hub's typing relay (SPEC 7.6(6))
# --------------------------------------------------------------------------------------------------------------------


def _index_from(chat: Dict[str, Any], members: List[Dict[str, Any]]) -> Dict[str, Any]:
    direct = chat["kind"] == "direct"
    return {
        "chat_id": chat["id"],
        "kind": chat["kind"],
        "only_admins_post": bool(chat["only_admins_post"]),
        "members": {m["user_id"]: m["role"] for m in members if m["listed"]},
        "peer_disabled": direct and any(bool(m["disabled"]) for m in members) and len(members) > 1,
        "self_chat": direct and is_self_direct(chat["direct_key"]),
    }


def load_membership_index(conn: Any) -> Dict[int, Dict[str, Any]]:
    """The hub's typing index of EVERY chat: ``{chat_id: entry}`` built with three set-based queries.

    ``entry = {chat_id, kind, only_admins_post, members: {user_id: chat role} (listed members only), peer_disabled,
    self_chat}``; ``peer_disabled`` is true for a two-member direct chat with a disabled member.
    """
    chats = q_all(conn, "SELECT id, kind, direct_key, only_admins_post FROM chats ORDER BY id")
    members: Dict[int, List[Dict[str, Any]]] = {c["id"]: [] for c in chats}
    for r in q_all(
        conn,
        "SELECT cm.chat_id AS chat_id, cm.user_id AS user_id, cm.role AS role, cm.listed AS listed,"
        " u.disabled AS disabled FROM chat_members cm JOIN users u ON u.id = cm.user_id ORDER BY cm.chat_id",
    ):
        members[r["chat_id"]].append(r)
    return {c["id"]: _index_from(c, members[c["id"]]) for c in chats}


def index_entry(conn: Any, chat_id: int) -> Dict[str, Any]:
    """The membership-index entry of one chat (same shape as :func:`load_membership_index`)."""
    chat = q_one(conn, "SELECT id, kind, direct_key, only_admins_post FROM chats WHERE id = ?", (chat_id,))
    if chat is None:
        return {
            "chat_id": chat_id,
            "kind": "group",
            "only_admins_post": False,
            "members": {},
            "peer_disabled": False,
            "self_chat": False,
        }
    members = q_all(
        conn,
        "SELECT cm.user_id AS user_id, cm.role AS role, cm.listed AS listed, u.disabled AS disabled"
        " FROM chat_members cm JOIN users u ON u.id = cm.user_id WHERE cm.chat_id = ?",
        (chat_id,),
    )
    return _index_from(chat, members)


def direct_chat_id(conn: Any, user_id: int, peer_id: int) -> Optional[int]:
    """The id of the direct chat between two users (no authorisation): lets the hub pick the chat lock before
    ``chat.open_direct`` on an existing chat (SPEC 7.6(2))."""
    key = "%d:%d" % (min(user_id, peer_id), max(user_id, peer_id))
    row = conn.execute("SELECT id FROM chats WHERE direct_key = ?", (key,)).fetchone()
    return int(row[0]) if row is not None else None


# --------------------------------------------------------------------------------------------------------------------
# Chat serialisation: the ONE Chat builder (SPEC 7.2)
# --------------------------------------------------------------------------------------------------------------------


def _view_rows(conn: Any, viewer_id: int, chat_ids: Optional[Sequence[int]]) -> List[Dict[str, Any]]:
    """Chat + viewer row + newest visible message id + counters for ONE viewer over all (or the given) chats."""
    sql = (
        "SELECT c.id AS chat_id, c.kind AS kind, c.title AS title, c.description AS description,"
        " c.direct_key AS direct_key, c.is_default AS is_default, c.only_admins_post AS only_admins_post,"
        " c.created_by AS created_by, c.created_at AS created_at, c.last_message_id AS last_message_id,"
        " c.last_activity_at AS last_activity_at, cm.user_id AS viewer_id, cm.role AS my_role,"
        " cm.joined_at AS joined_at, cm.history_from_id AS history_from_id, cm.cleared_before_id AS cleared_before_id,"
        " cm.last_read_id AS last_read_id, cm.muted_until AS muted_until, cm.pinned_at AS pinned_at,"
        " cm.archived AS archived,"
        " (SELECT m.id FROM messages m WHERE m.chat_id = c.id AND {vis} ORDER BY m.id DESC LIMIT 1)"
        " AS last_visible_id, {counters}"
        " FROM chat_members cm JOIN chats c ON c.id = cm.chat_id WHERE cm.user_id = ? AND cm.listed = 1{scope}"
    ).format(vis=db_messages.visible_sql(), counters=db_receipts.counter_columns(), scope="{scope}")
    if chat_ids is None:
        return q_all(conn, sql.format(scope=""), (viewer_id,))
    rows: List[Dict[str, Any]] = []
    for chunk in chunked(list(chat_ids)):
        scope = " AND cm.chat_id IN (%s)" % placeholders(len(chunk))
        rows.extend(q_all(conn, sql.format(scope=scope), [viewer_id] + chunk))
    return rows


def _pin_rows(conn: Any, viewer_id: int, chat_ids: Optional[Sequence[int]]) -> Dict[int, List[int]]:
    """Pinned message ids per chat that ``viewer_id`` may see (SPEC 3.1), newest pin first."""
    sql = (
        "SELECT p.chat_id AS chat_id, p.message_id AS message_id FROM pins p"
        " JOIN chat_members cm ON cm.chat_id = p.chat_id AND cm.user_id = ?"
        " JOIN messages m ON m.id = p.message_id WHERE {vis}{scope}"
        " ORDER BY p.chat_id, p.pinned_at DESC, p.message_id DESC"
    ).format(vis=db_messages.visible_sql(), scope="{scope}")
    out: Dict[int, List[int]] = {}
    chunks: List[Optional[List[int]]] = [None] if chat_ids is None else chunked(list(chat_ids))
    for chunk in chunks:
        if chunk is None:
            rows = q_all(conn, sql.format(scope=""), (viewer_id,))
        else:
            rows = q_all(
                conn, sql.format(scope=" AND p.chat_id IN (%s)" % placeholders(len(chunk))), [viewer_id] + chunk
            )
        for r in rows:
            out.setdefault(r["chat_id"], []).append(r["message_id"])
    return out


def _build(conn: Any, wanted: Dict[int, Optional[List[int]]]) -> Dict[Tuple[int, int], Dict[str, Any]]:
    """The set-based core of :func:`build_chats`: ``wanted`` maps a viewer to its chat ids (``None`` = all chats).

    Per chat: one members query (shared ``members`` list).  Per viewer: one grouped query over all of its chats plus
    one for the pins.  Messages, stars and hidden quotes are read in batched queries for all viewers together.
    """
    rows: Dict[Tuple[int, int], Dict[str, Any]] = {}
    pins: Dict[Tuple[int, int], List[int]] = {}
    for viewer_id, chat_ids in wanted.items():
        for row in _view_rows(conn, viewer_id, chat_ids):
            rows[(viewer_id, row["chat_id"])] = row
        for chat_id, ids in _pin_rows(conn, viewer_id, chat_ids).items():
            pins[(viewer_id, chat_id)] = ids
    if not rows:
        return {}
    members = _member_rows(conn, sorted({chat_id for _, chat_id in rows}))
    payloads = {cid: [member_entry(m) for m in ms] for cid, ms in members.items()}
    needed = {r["last_visible_id"] for r in rows.values() if r["last_visible_id"] is not None}
    for ids in pins.values():
        needed.update(ids)
    parts = db_messages.load_parts(conn, needed)
    viewers = sorted(wanted)
    starred = db_messages.viewer_pairs(conn, "stars", viewers, needed)
    quoted = {p.quoted_id for p in parts.values() if p.quoted_id is not None}
    quote_hidden = db_messages.viewer_pairs(conn, "hidden_messages", viewers, quoted)
    contexts: Dict[Tuple[int, int], db_receipts.StatusCtx] = {}

    def render(viewer_id: int, row: Dict[str, Any], message_id: int) -> Dict[str, Any]:
        p = parts[message_id]
        status = None
        is_sender = p.sender_id == viewer_id
        if is_sender:
            key = (viewer_id, row["chat_id"])
            ctx = contexts.get(key)
            if ctx is None:
                ctx = contexts[key] = db_receipts.status_ctx_from_members(
                    viewer_id, members[row["chat_id"]], row["kind"] == "direct" and is_self_direct(row["direct_key"])
                )
            status = db_receipts.status_of(ctx, message_id)
        floor = max(row["history_from_id"], row["cleared_before_id"])
        state = db_messages.reply_state(p, floor, (viewer_id, p.quoted_id) in quote_hidden)
        return db_messages.render(p, is_sender, (viewer_id, message_id) in starred, state, status)

    out: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for (viewer_id, chat_id), row in rows.items():
        direct = row["kind"] == "direct"
        pinned_ids = pins.get((viewer_id, chat_id), [])
        last_id = row["last_visible_id"]
        out[(viewer_id, chat_id)] = {
            "id": chat_id,
            "kind": row["kind"],
            "title": None if direct else row["title"],
            "description": row["description"],
            "is_default": bool(row["is_default"]),
            "only_admins_post": bool(row["only_admins_post"]),
            "created_by": None if direct else row["created_by"],
            "created_at": row["joined_at"] if direct else row["created_at"],
            "last_activity_at": row["last_activity_at"],
            "peer_id": direct_peer(row["direct_key"], viewer_id) if direct else None,
            "members": payloads[chat_id],
            "pinned_message_ids": list(pinned_ids),
            "pinned_messages": [render(viewer_id, row, mid) for mid in pinned_ids],
            "last_message_id": row["last_message_id"],
            "last_message": render(viewer_id, row, last_id) if last_id is not None else None,
            "me": {
                "unread": row["unread"],
                "unread_mentions": row["unread_mentions"],
                "first_unread_mention_id": row["first_unread_mention_id"],
                "last_read_id": row["last_read_id"],
                "cleared_before_id": row["cleared_before_id"],
                "muted_until": row["muted_until"],
                "pinned": row["pinned_at"] is not None,
                "pinned_at": row["pinned_at"],
                "archived": bool(row["archived"]),
            },
        }
    return out


def build_chats(conn: Any, pairs: Iterable[Tuple[int, int]]) -> Dict[Tuple[int, int], Dict[str, Any]]:
    """THE Chat builder (SPEC 7.2): ``{(viewer_id, chat_id): Chat}`` for ``pairs = [(viewer_id, chat_id), ...]``.

    Runs in the caller's transaction, so call it AFTER all writes of a request.  Pairs where the viewer is not a LISTED
    member of the chat are omitted.  The ``members`` list object is shared by every viewer of a chat (read-only), so
    the hub may encode it once per chat.
    """
    wanted: Dict[int, List[int]] = {}
    for viewer_id, chat_id in pairs:
        wanted.setdefault(viewer_id, []).append(chat_id)
    return _build(conn, {viewer: sorted(set(ids)) for viewer, ids in wanted.items()})


def build_chat(conn: Any, chat_id: int, viewer_id: int) -> Optional[Dict[str, Any]]:
    """One Chat for one viewer (``None`` when the viewer is not a listed member)."""
    return build_chats(conn, [(viewer_id, chat_id)]).get((viewer_id, chat_id))


def chat_update_event(
    conn: Any,
    chat_id: int,
    user_ids: Optional[Iterable[int]] = None,
    chats: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """``ev.chat_update`` with one group per viewer (each Chat built per viewer).  ``user_ids=None`` means every
    listed member.  Built Chats are also stored in ``chats`` (the record's ``chats`` entry) when it is given."""
    if user_ids is None:
        user_ids = [
            r[0] for r in conn.execute("SELECT user_id FROM chat_members WHERE chat_id = ? AND listed = 1", (chat_id,))
        ]
    built = build_chats(conn, [(uid, chat_id) for uid in user_ids])
    if chats is not None:
        chats.update(built)
    groups = [group([viewer], {"chat": built[(viewer, cid)]}) for (viewer, cid) in sorted(built)]
    return event("ev.chat_update", groups) if groups else None


def chat_members_event(
    chat_id: int,
    user_ids: Iterable[int],
    added: Sequence[Dict[str, Any]] = (),
    removed: Sequence[int] = (),
    updated: Sequence[Dict[str, Any]] = (),
) -> Optional[Dict[str, Any]]:
    """``ev.chat_members`` (identical JSON for every recipient); the three arrays are sorted ascending by user id."""
    d = {
        "chat_id": chat_id,
        "added": sorted(added, key=lambda m: m["user_id"]),
        "removed": sorted(removed),
        "updated": sorted(updated, key=lambda m: m["user_id"]),
    }
    recipients = sorted(user_ids)
    return event("ev.chat_members", [group(recipients, d)]) if recipients else None


# --------------------------------------------------------------------------------------------------------------------
# ev.ready
# --------------------------------------------------------------------------------------------------------------------


def _chat_order(chat: Dict[str, Any]) -> Tuple[Any, ...]:
    """SPEC 3.2(7): pinned first (``pinned_at`` DESC), then ``last_activity_at`` DESC, ties by chat id DESC."""
    pinned_at = chat["me"]["pinned_at"]
    return (0 if pinned_at is not None else 1, -(pinned_at or 0.0), -chat["last_activity_at"], -chat["id"])


def build_ready(
    conn: Any,
    user_id: int,
    default_workspace_name: Optional[str] = None,
    default_registration_open: Optional[bool] = None,
) -> Dict[str, Any]:
    """The viewer-specific part of ``ev.ready`` from ONE reader transaction (SPEC 7.3, 7.6(7)).

    Returns ``{instance_id, me, workspace: {name, registration_open}, users, chats, server_time}``; the hub adds
    ``protocol`` and ``limits`` and overlays ``me.online`` / ``users[].online`` / ``last_seen`` from its presence
    capture.  ``chats`` holds every chat in which the caller has a LISTED member row, in the SPEC 3.2(7) list order
    (set-based: one grouped query per viewer, one members query per 400 chats, batched messages).  ``users``, ``me``,
    ``instance_id`` and ``workspace`` come from ``db_users`` (``list_users``, ``get_me``, ``get_instance_id``,
    ``workspace_settings``; ``default_*`` are the ``cfg`` values used while ``meta`` holds no runtime edit).  A disabled
    caller => ``unauthorized``.
    """
    from . import db_users

    caller(conn, user_id)
    chats = sorted(_build(conn, {user_id: None}).values(), key=_chat_order)
    return {
        "instance_id": db_users.get_instance_id(conn),
        "me": db_users.get_me(conn, user_id),
        "workspace": db_users.workspace_settings(
            conn, default_workspace_name or "DeskTalk", bool(default_registration_open)
        ),
        "users": db_users.list_users(conn),
        "chats": chats,
        "server_time": util.now(),
    }


# --------------------------------------------------------------------------------------------------------------------
# Requests: chat.get / chat.open_direct / chat.create_group
# --------------------------------------------------------------------------------------------------------------------


def chat_get(conn: Any, user_id: int, chat_id: int) -> Dict[str, Any]:
    """``chat.get``: ``{"chat": Chat}`` for a listed member, else ``not_member`` (reader function; returns ``res``)."""
    need_id(chat_id, "chat_id")
    caller(conn, user_id)
    require_member(conn, user_id, chat_id)
    return {"chat": build_chat(conn, chat_id, user_id)}


def _system(
    conn: Any,
    chat_id: int,
    ev: str,
    actor_id: Optional[int],
    targets: List[int],
    body: str,
    now: float,
    title: Optional[str] = None,
) -> int:
    return db_messages.insert_system_message(conn, chat_id, ev, actor_id, targets, body, title=title, ts=now)


def chat_open_direct(conn: Any, user_id: int, peer_id: int) -> Dict[str, Any]:
    """``chat.open_direct`` with the dormant/listed rules of SPEC 3.2(3).

    New chat: created dormant (creator ``listed=1``, peer ``listed=0``; a self-chat has one row), ``ev.chat_update`` to
    the caller only; a new chat with a disabled peer => ``invalid_state``.  Existing chat where the caller is listed:
    returned unchanged (``noop``, no events).  Existing dormant chat where the caller is the unlisted peer: the caller's
    row becomes listed, ``ev.chat_update`` to the caller only.  Unknown ``peer_id`` => ``not_found``.
    """
    need_id(peer_id, "user_id")
    caller(conn, user_id)
    peer = conn.execute("SELECT disabled FROM users WHERE id = ?", (peer_id,)).fetchone()
    if peer is None:
        raise ChatError("not_found", "user not found")
    key = "%d:%d" % (min(user_id, peer_id), max(user_id, peer_id))
    now = util.now()
    existing = conn.execute("SELECT id FROM chats WHERE direct_key = ?", (key,)).fetchone()
    chat_id: Optional[int] = int(existing[0]) if existing is not None else None
    if chat_id is None:
        if peer_id != user_id and peer[0]:
            raise ChatError("invalid_state", "this account is disabled", reason="peer_disabled")
        conn.execute("SAVEPOINT open_direct")
        try:
            cur = conn.execute(
                "INSERT INTO chats(kind, title, description, direct_key, is_default, only_admins_post, created_by,"
                " created_at, last_message_id, last_activity_at) VALUES ('direct', NULL, '', ?, 0, 0, ?, ?, NULL, ?)",
                (key, user_id, now, now),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK TO open_direct")
            conn.execute("RELEASE open_direct")
            direct = direct_chat_id(conn, user_id, peer_id)
            if direct is None:
                raise
            chat_id = direct
        else:
            conn.execute("RELEASE open_direct")
            chat_id = int(cur.lastrowid)
            add_member_row(conn, chat_id, user_id, "member", now, 1)
            if peer_id != user_id:
                add_member_row(conn, chat_id, peer_id, "member", now, 0)
            return _opened(conn, user_id, chat_id)
    mine = conn.execute(
        "SELECT listed FROM chat_members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    ).fetchone()
    if mine is None:
        raise ChatError("not_member", "not a member of this chat", chat_id=chat_id)
    if mine[0]:
        chats = {(user_id, chat_id): build_chat(conn, chat_id, user_id)}
        return outcome(
            {"chat": chats[(user_id, chat_id)]}, noop=True, chats=chats, recipients=recipients_of(conn, chat_id)
        )
    conn.execute("UPDATE chat_members SET listed = 1 WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    return _opened(conn, user_id, chat_id)


def _opened(conn: Any, user_id: int, chat_id: int) -> Dict[str, Any]:
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    add_event(events, chat_update_event(conn, chat_id, [user_id], chats))
    return outcome(
        {"chat": chats[(user_id, chat_id)]},
        events,
        [index_entry(conn, chat_id)],
        chats=chats,
        recipients=recipients_of(conn, chat_id),
    )


def chat_create_group(conn: Any, user_id: int, title: Any, member_ids: Any, description: Any = None) -> Dict[str, Any]:
    """``chat.create_group``: the caller becomes admin; ``member_ids`` (<= 200, caller and duplicates dropped) must be
    known (``not_found``) and enabled (``invalid_state``), at most 200 members in total.  The ``created`` system
    message is inserted before the Chats are built, so every member's Chat already holds it as ``last_message``.
    Events: ``ev.chat_update`` to every member then system ``ev.message`` ``created``."""
    clean_title = clean_text(title, "title", _TITLE_MAX, 1)
    clean_description = "" if description is None else clean_text(description, "description", _DESCRIPTION_MAX, 0, True)
    if not isinstance(member_ids, (list, tuple)) or len(member_ids) > MAX_GROUP_MEMBERS:
        raise ChatError("bad_request", "member_ids must be a list of at most %d ids" % MAX_GROUP_MEMBERS)
    ids = [need_id(v, "member_ids") for v in member_ids]
    me = caller(conn, user_id)
    others = [i for i in dict.fromkeys(ids) if i != user_id]
    flags = {}
    for chunk in chunked(others):
        flags.update(
            (r[0], r[1])
            for r in conn.execute(
                "SELECT id, disabled FROM users WHERE id IN (%s)" % placeholders(len(chunk)), chunk
            ).fetchall()
        )
    for uid in others:
        if uid not in flags:
            raise ChatError("not_found", "user not found")
    if any(flags[uid] for uid in others):
        raise ChatError("invalid_state", "this account is disabled", reason="disabled")
    if len(others) + 1 > MAX_GROUP_MEMBERS:
        raise ChatError("invalid_state", "a group holds at most %d members" % MAX_GROUP_MEMBERS, reason="max_members")
    now = util.now()
    cur = conn.execute(
        "INSERT INTO chats(kind, title, description, direct_key, is_default, only_admins_post, created_by, created_at,"
        " last_message_id, last_activity_at) VALUES ('group', ?, ?, NULL, 0, 0, ?, ?, NULL, ?)",
        (clean_title, clean_description, user_id, now, now),
    )
    chat_id = int(cur.lastrowid)
    add_member_row(conn, chat_id, user_id, "admin", now)
    for uid in others:
        add_member_row(conn, chat_id, uid, "member", now)
    body = '%s created group "%s"' % (me["display_name"], clean_title)
    system_id = _system(conn, chat_id, "created", user_id, others, body, now, clean_title)
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    add_event(events, chat_update_event(conn, chat_id, None, chats))
    add_event(events, db_messages.system_message_event(conn, chat_id, system_id))
    return outcome(
        {"chat": chats[(user_id, chat_id)]},
        events,
        [index_entry(conn, chat_id)],
        chats=chats,
        recipients=recipients_of(conn, chat_id),
    )


# --------------------------------------------------------------------------------------------------------------------
# Group administration: update / add / remove / set_admin / leave
# --------------------------------------------------------------------------------------------------------------------


def _require_group_admin(chat: Dict[str, Any]) -> None:
    if chat["my_role"] != "admin":
        raise ChatError("forbidden", "only group admins can do this", chat_id=chat["chat_id"])


def chat_update(
    conn: Any,
    user_id: int,
    chat_id: int,
    title: Any = None,
    description: Any = None,
    only_admins_post: Any = None,
) -> Dict[str, Any]:
    """``chat.update`` (group admin; the default group included).  None of the three fields => ``bad_request``;
    values equal to the stored ones are a no-op.  A title change inserts the ``renamed`` system message.
    Events: ``ev.chat_update`` (all members) then system ``ev.message``."""
    need_id(chat_id, "chat_id")
    if title is None and description is None and only_admins_post is None:
        raise ChatError("bad_request", "nothing to update")
    clean_title = None if title is None else clean_text(title, "title", _TITLE_MAX, 1)
    clean_description = (
        None if description is None else clean_text(description, "description", _DESCRIPTION_MAX, 0, True)
    )
    if only_admins_post is not None:
        need_bool(only_admins_post, "only_admins_post")
    me = caller(conn, user_id)
    chat = require_member(conn, user_id, chat_id)
    if chat["kind"] != "group":
        raise ChatError("invalid_state", "not applicable to a direct chat", chat_id=chat_id)
    _require_group_admin(chat)
    changes: Dict[str, Any] = {}
    if clean_title is not None and clean_title != chat["title"]:
        changes["title"] = clean_title
    if clean_description is not None and clean_description != chat["description"]:
        changes["description"] = clean_description
    if only_admins_post is not None and int(only_admins_post) != chat["only_admins_post"]:
        changes["only_admins_post"] = int(only_admins_post)
    if not changes:
        chats = {(user_id, chat_id): build_chat(conn, chat_id, user_id)}
        return outcome({"chat": chats[(user_id, chat_id)]}, noop=True, chats=chats)
    for column, value in changes.items():
        conn.execute("UPDATE chats SET %s = ? WHERE id = ?" % column, (value, chat_id))
    now = util.now()
    system_id = None
    if "title" in changes:
        body = '%s renamed the group to "%s"' % (me["display_name"], changes["title"])
        system_id = _system(conn, chat_id, "renamed", user_id, [], body, now, changes["title"])
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    add_event(events, chat_update_event(conn, chat_id, None, chats))
    if system_id is not None:
        add_event(events, db_messages.system_message_event(conn, chat_id, system_id))
    return outcome(
        {"chat": chats[(user_id, chat_id)]},
        events,
        [index_entry(conn, chat_id)],
        chats=chats,
        recipients=recipients_of(conn, chat_id),
    )


def chat_add_members(conn: Any, user_id: int, chat_id: int, user_ids: Any) -> Dict[str, Any]:
    """``chat.add_members`` (group admin, 1..50 ids).  Order: unknown user => ``not_found``; direct chat or default
    group => ``invalid_state``; not admin => ``forbidden``; a disabled new member or more than 200 members =>
    ``invalid_state``.  Existing members are skipped silently (all existing => no-op).  New members start fresh.
    Events: ``ev.chat_update`` to each new member then ``ev.chat_members {added}`` to the existing members then system
    ``ev.message`` ``added`` (all current members)."""
    need_id(chat_id, "chat_id")
    if not isinstance(user_ids, (list, tuple)) or not 1 <= len(user_ids) <= MAX_ADD_BATCH:
        raise ChatError("bad_request", "user_ids must be a list of 1..%d ids" % MAX_ADD_BATCH)
    wanted = list(dict.fromkeys(need_id(v, "user_ids") for v in user_ids))
    me = caller(conn, user_id)
    chat = require_member(conn, user_id, chat_id)
    flags = dict(
        conn.execute("SELECT id, disabled FROM users WHERE id IN (%s)" % placeholders(len(wanted)), wanted).fetchall()
    )
    for uid in wanted:
        if uid not in flags:
            raise ChatError("not_found", "user not found")
    if chat["kind"] != "group" or chat["is_default"]:
        raise ChatError("invalid_state", "members cannot be added to this chat", chat_id=chat_id)
    _require_group_admin(chat)
    current = {
        r[0] for r in conn.execute("SELECT user_id FROM chat_members WHERE chat_id = ? AND listed = 1", (chat_id,))
    }
    fresh = [uid for uid in wanted if uid not in current]
    if not fresh:
        chats = {(user_id, chat_id): build_chat(conn, chat_id, user_id)}
        return outcome({"chat": chats[(user_id, chat_id)]}, noop=True, chats=chats)
    if any(flags[uid] for uid in fresh):
        raise ChatError("invalid_state", "this account is disabled", reason="disabled", chat_id=chat_id)
    if len(current) + len(fresh) > MAX_GROUP_MEMBERS:
        raise ChatError(
            "invalid_state",
            "a group holds at most %d members" % MAX_GROUP_MEMBERS,
            reason="max_members",
            chat_id=chat_id,
        )
    now = util.now()
    for uid in fresh:
        add_member_row(conn, chat_id, uid, "member", now)
    names = ", ".join(name_of(conn, uid) for uid in fresh)
    system_id = _system(conn, chat_id, "added", user_id, fresh, "%s added %s" % (me["display_name"], names), now)
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    add_event(events, chat_update_event(conn, chat_id, fresh, chats))
    entries = {m["user_id"]: member_entry(m) for m in _member_rows(conn, [chat_id])[chat_id]}
    add_event(events, chat_members_event(chat_id, sorted(current), added=[entries[u] for u in fresh]))
    add_event(events, db_messages.system_message_event(conn, chat_id, system_id))
    chats[(user_id, chat_id)] = build_chat(conn, chat_id, user_id)
    return outcome(
        {"chat": chats[(user_id, chat_id)]},
        events,
        [index_entry(conn, chat_id)],
        chats=chats,
        recipients=recipients_of(conn, chat_id),
        added=[entries[u] for u in fresh],
    )


def promote_if_needed(conn: Any, chat_id: int, now: float) -> Optional[Dict[str, Any]]:
    """SPEC 3.2(6): when a group has no ENABLED admin left, promote the enabled member with the smallest ``joined_at``
    (tie: lowest ``user_id``) and insert the ``promoted`` system message (``actor_id`` null).  Returns
    ``{user_id, message_id}`` or ``None`` (an enabled admin exists, or nobody could be promoted)."""
    has_admin = conn.execute(
        "SELECT 1 FROM chat_members cm JOIN users u ON u.id = cm.user_id WHERE cm.chat_id = ? AND cm.listed = 1"
        " AND cm.role = 'admin' AND u.disabled = 0 LIMIT 1",
        (chat_id,),
    ).fetchone()
    if has_admin is not None:
        return None
    candidate = conn.execute(
        "SELECT cm.user_id, u.display_name FROM chat_members cm JOIN users u ON u.id = cm.user_id"
        " WHERE cm.chat_id = ? AND cm.listed = 1 AND u.disabled = 0 ORDER BY cm.joined_at, cm.user_id LIMIT 1",
        (chat_id,),
    ).fetchone()
    if candidate is None:
        return None
    conn.execute("UPDATE chat_members SET role = 'admin' WHERE chat_id = ? AND user_id = ?", (chat_id, candidate[0]))
    message_id = _system(conn, chat_id, "promoted", None, [candidate[0]], "%s is now an admin" % candidate[1], now)
    return {"user_id": candidate[0], "message_id": message_id}


def _member_entry_of(conn: Any, chat_id: int, user_id: int) -> Dict[str, Any]:
    rows = [m for m in _member_rows(conn, [chat_id])[chat_id] if m["user_id"] == user_id]
    return member_entry(rows[0])


def _drop_member(conn: Any, chat_id: int, target_id: int, actor_id: int, event_name: str, now: float) -> Dict[str, Any]:
    """Remove a member (shared by remove_member / leave): stars of the chat, the row, promotion, system messages."""
    target = conn.execute(
        "SELECT cm.role, u.display_name FROM chat_members cm JOIN users u ON u.id = cm.user_id"
        " WHERE cm.chat_id = ? AND cm.user_id = ?",
        (chat_id, target_id),
    ).fetchone()
    conn.execute(
        "DELETE FROM stars WHERE user_id = ? AND EXISTS (SELECT 1 FROM messages m WHERE m.id = stars.message_id"
        " AND m.chat_id = ?)",
        (target_id, chat_id),
    )
    conn.execute("DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?", (chat_id, target_id))
    remaining = [
        r[0]
        for r in conn.execute(
            "SELECT user_id FROM chat_members WHERE chat_id = ? AND listed = 1 ORDER BY user_id", (chat_id,)
        )
    ]
    result: Dict[str, Any] = {"remaining": remaining, "promoted": None, "system_ids": []}
    if not remaining:
        return result
    if target[0] == "admin":
        result["promoted"] = promote_if_needed(conn, chat_id, now)
    if event_name == "left":
        body = "%s left" % target[1]
        first = _system(conn, chat_id, "left", target_id, [target_id], body, now)
    else:
        body = "%s removed %s" % (name_of(conn, actor_id), target[1])
        first = _system(conn, chat_id, "removed", actor_id, [target_id], body, now)
    result["system_ids"] = [first]
    if result["promoted"] is not None:
        result["system_ids"].append(result["promoted"]["message_id"])
    return result


def _departure_events(conn: Any, chat_id: int, target_id: int, dropped: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = [event("ev.chat_removed", [group([target_id], {"chat_id": chat_id})])]
    if dropped["remaining"]:
        updated = []
        if dropped["promoted"] is not None:
            updated.append(_member_entry_of(conn, chat_id, dropped["promoted"]["user_id"]))
        add_event(events, chat_members_event(chat_id, dropped["remaining"], removed=[target_id], updated=updated))
        for system_id in dropped["system_ids"]:
            add_event(events, db_messages.system_message_event(conn, chat_id, system_id))
    return events


def chat_remove_member(conn: Any, user_id: int, chat_id: int, target_id: int) -> Dict[str, Any]:
    """``chat.remove_member`` (group admin).  Order: target not a member => ``not_found``; direct chat or default
    group => ``invalid_state``; ``target == caller`` => ``invalid_state`` (reason ``use_leave``); not admin =>
    ``forbidden``.  Deletes the target's stars of the chat; promotes per SPEC 3.2(6).  Events: ``ev.chat_removed``
    (the removed user) then ONE ``ev.chat_members`` (``removed`` + promoted ``updated``) then system ``removed`` /
    ``promoted`` messages (remaining members)."""
    need_id(chat_id, "chat_id")
    need_id(target_id, "user_id")
    caller(conn, user_id)
    chat = require_member(conn, user_id, chat_id)
    member = conn.execute(
        "SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ? AND listed = 1", (chat_id, target_id)
    ).fetchone()
    if member is None:
        raise ChatError("not_found", "user is not a member", chat_id=chat_id)
    if chat["kind"] != "group" or chat["is_default"]:
        raise ChatError("invalid_state", "members cannot be removed from this chat", chat_id=chat_id)
    if target_id == user_id:
        raise ChatError("invalid_state", "use chat.leave to leave a chat", reason="use_leave", chat_id=chat_id)
    _require_group_admin(chat)
    dropped = _drop_member(conn, chat_id, target_id, user_id, "removed", util.now())
    events = _departure_events(conn, chat_id, target_id, dropped)
    chats = {(user_id, chat_id): build_chat(conn, chat_id, user_id)}
    return outcome(
        {"chat": chats[(user_id, chat_id)]},
        events,
        [index_entry(conn, chat_id)],
        chats=chats,
        recipients=recipients_of(conn, chat_id),
        removed=[target_id],
        promoted=dropped["promoted"],
    )


def chat_set_admin(conn: Any, user_id: int, chat_id: int, target_id: int, admin: Any) -> Dict[str, Any]:
    """``chat.set_admin`` (group admin).  Order: target not a member => ``not_found``; direct chat or default group =>
    ``invalid_state``; not admin => ``forbidden``; demoting the last ENABLED admin => ``invalid_state``.  Same role =>
    no-op.  Events: ``ev.chat_members {updated}`` (all members) then system ``promoted`` / ``demoted``."""
    need_id(chat_id, "chat_id")
    need_id(target_id, "user_id")
    need_bool(admin, "admin")
    me = caller(conn, user_id)
    chat = require_member(conn, user_id, chat_id)
    target = conn.execute(
        "SELECT cm.role, u.display_name, u.disabled FROM chat_members cm JOIN users u ON u.id = cm.user_id"
        " WHERE cm.chat_id = ? AND cm.user_id = ? AND cm.listed = 1",
        (chat_id, target_id),
    ).fetchone()
    if target is None:
        raise ChatError("not_found", "user is not a member", chat_id=chat_id)
    if chat["kind"] != "group" or chat["is_default"]:
        raise ChatError("invalid_state", "admins cannot be changed in this chat", chat_id=chat_id)
    _require_group_admin(chat)
    new_role = "admin" if admin else "member"
    if target[0] == new_role:
        chats = {(user_id, chat_id): build_chat(conn, chat_id, user_id)}
        return outcome({"chat": chats[(user_id, chat_id)]}, noop=True, chats=chats)
    if not admin and not target[2]:
        others = conn.execute(
            "SELECT 1 FROM chat_members cm JOIN users u ON u.id = cm.user_id WHERE cm.chat_id = ? AND cm.listed = 1"
            " AND cm.role = 'admin' AND u.disabled = 0 AND cm.user_id != ? LIMIT 1",
            (chat_id, target_id),
        ).fetchone()
        if others is None:
            raise ChatError("invalid_state", "a group needs an admin", reason="last_admin", chat_id=chat_id)
    conn.execute("UPDATE chat_members SET role = ? WHERE chat_id = ? AND user_id = ?", (new_role, chat_id, target_id))
    now = util.now()
    verb = "promoted" if admin else "demoted"
    body = (
        "%s made %s an admin" % (me["display_name"], target[1])
        if admin
        else "%s removed %s as admin" % (me["display_name"], target[1])
    )
    system_id = _system(conn, chat_id, verb, user_id, [target_id], body, now)
    members = [r["user_id"] for r in recipients_of(conn, chat_id)]
    events: List[Dict[str, Any]] = []
    add_event(events, chat_members_event(chat_id, members, updated=[_member_entry_of(conn, chat_id, target_id)]))
    add_event(events, db_messages.system_message_event(conn, chat_id, system_id))
    chats = {(user_id, chat_id): build_chat(conn, chat_id, user_id)}
    return outcome(
        {"chat": chats[(user_id, chat_id)]},
        events,
        [index_entry(conn, chat_id)],
        chats=chats,
        recipients=recipients_of(conn, chat_id),
    )


def chat_leave(conn: Any, user_id: int, chat_id: int) -> Dict[str, Any]:
    """``chat.leave``: a direct chat or the default group => ``invalid_state``.  Deletes the leaver's stars of the
    chat; promotes per SPEC 3.2(6); the last member leaving keeps the chat row (no promotion, no system message).
    Events as ``chat.remove_member`` (``ev.chat_removed`` to the leaver only).  ``res`` is ``{}``."""
    need_id(chat_id, "chat_id")
    caller(conn, user_id)
    chat = require_member(conn, user_id, chat_id)
    if chat["kind"] != "group" or chat["is_default"]:
        raise ChatError("invalid_state", "this chat cannot be left", chat_id=chat_id)
    dropped = _drop_member(conn, chat_id, user_id, user_id, "left", util.now())
    events = _departure_events(conn, chat_id, user_id, dropped)
    return outcome(
        {},
        events,
        [index_entry(conn, chat_id)],
        recipients=recipients_of(conn, chat_id),
        removed=[user_id],
        promoted=dropped["promoted"],
    )


# --------------------------------------------------------------------------------------------------------------------
# Per-user chat state: prefs and clear
# --------------------------------------------------------------------------------------------------------------------


def chat_prefs(
    conn: Any, user_id: int, chat_id: int, muted_until: Any = None, pinned: Any = None, archived: Any = None
) -> Dict[str, Any]:
    """``chat.prefs``: ``muted_until`` (<= now is stored as 0), ``pinned`` (max 3 chats), ``archived``.

    Evaluated on the FINAL state: ``archived`` is applied before ``pinned``; ``pinned:true`` with ``archived:true`` =>
    ``bad_request``; ``pinned:true`` while the chat stays archived or a 4th pinned chat => ``invalid_state``;
    ``archived:true`` clears the pin.  Unchanged values are a no-op.  Event: ``ev.chat_update`` to the actor only."""
    need_id(chat_id, "chat_id")
    if muted_until is None and pinned is None and archived is None:
        raise ChatError("bad_request", "nothing to update")
    if muted_until is not None and (
        isinstance(muted_until, bool) or not isinstance(muted_until, (int, float)) or not 0 <= muted_until <= _MUTE_MAX
    ):
        raise ChatError("bad_request", "muted_until must be a number in range")
    if pinned is not None:
        need_bool(pinned, "pinned")
    if archived is not None:
        need_bool(archived, "archived")
    if pinned is True and archived is True:
        raise ChatError("bad_request", "a chat cannot be pinned and archived")
    caller(conn, user_id)
    chat = require_member(conn, user_id, chat_id)
    now = util.now()
    new_archived = bool(chat["archived"]) if archived is None else archived
    new_pinned_at = chat["pinned_at"]
    if new_archived:
        new_pinned_at = None
    if pinned is False:
        new_pinned_at = None
    elif pinned is True:
        if new_archived:
            raise ChatError("invalid_state", "an archived chat cannot be pinned", chat_id=chat_id)
        if new_pinned_at is None:
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_members WHERE user_id = ? AND listed = 1 AND pinned_at IS NOT NULL",
                (user_id,),
            ).fetchone()[0]
            if count >= MAX_PINNED_CHATS:
                raise ChatError(
                    "invalid_state", "at most %d pinned chats" % MAX_PINNED_CHATS, reason="pin_limit", chat_id=chat_id
                )
            new_pinned_at = now
    new_muted = chat["muted_until"]
    if muted_until is not None:
        new_muted = 0 if muted_until <= now else muted_until
    if (new_muted, new_pinned_at, int(new_archived)) == (chat["muted_until"], chat["pinned_at"], chat["archived"]):
        chats = {(user_id, chat_id): build_chat(conn, chat_id, user_id)}
        return outcome({"chat": chats[(user_id, chat_id)]}, noop=True, chats=chats)
    conn.execute(
        "UPDATE chat_members SET muted_until = ?, pinned_at = ?, archived = ? WHERE chat_id = ? AND user_id = ?",
        (new_muted, new_pinned_at, int(new_archived), chat_id, user_id),
    )
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    add_event(events, chat_update_event(conn, chat_id, [user_id], chats))
    return outcome({"chat": chats[(user_id, chat_id)]}, events, chats=chats, recipients=recipients_of(conn, chat_id))


def chat_clear(conn: Any, user_id: int, chat_id: int) -> Dict[str, Any]:
    """``chat.clear``: ``cleared_before_id = last_read_id = chats.last_message_id``; ``delivered_id`` advances, the
    public read watermark does not.  A chat without messages, or one already cleared up to ``last_message_id``, is a
    no-op.  Events (actor): ``ev.chat_update``, ``ev.read_sync``; ``ev.receipt`` per SPEC 8.2 when ``delivered_id``
    increased."""
    need_id(chat_id, "chat_id")
    caller(conn, user_id)
    chat = require_member(conn, user_id, chat_id)
    last = chat["last_message_id"]
    if last is None or chat["cleared_before_id"] == last:
        chats = {(user_id, chat_id): build_chat(conn, chat_id, user_id)}
        return outcome({"chat": chats[(user_id, chat_id)]}, noop=True, chats=chats)
    conn.execute(
        "UPDATE chat_members SET cleared_before_id = ? WHERE chat_id = ? AND user_id = ?", (last, chat_id, user_id)
    )
    change = db_receipts.advance_marks(conn, chat_id, user_id, delivered_to=last, last_read_to=last)
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    add_event(events, chat_update_event(conn, chat_id, [user_id], chats))
    sync = db_receipts.read_sync_for(conn, chat_id, [user_id])
    add_event(events, db_receipts.read_sync_event(sync))
    receipt = db_receipts.receipt_record(conn, chat_id, user_id, change)
    add_event(events, db_receipts.receipt_event(receipt))
    return outcome(
        {"chat": chats[(user_id, chat_id)]},
        events,
        chats=chats,
        recipients=recipients_of(conn, chat_id),
        read_sync=sync,
        receipt=receipt,
        receipts=[receipt] if receipt else [],
    )


# --------------------------------------------------------------------------------------------------------------------
# admin.update_user (SPEC 7.4): role / disabled / display_name with the last-admin invariants
# --------------------------------------------------------------------------------------------------------------------


def admin_user_chat_ids(conn: Any, user_id: int) -> List[int]:
    """Chat ids the hub must lock (ascending) before ``admin_update_user`` on ``user_id`` (SPEC 7.6(2)): the default
    group plus every non-default group in which the user is a listed group admin.  Compare with the outcome's
    ``touched_chats`` and retry when the set grew in between."""
    rows = conn.execute(
        "SELECT c.id FROM chats c WHERE c.is_default = 1 OR (c.kind = 'group' AND EXISTS (SELECT 1 FROM chat_members cm"
        " WHERE cm.chat_id = c.id AND cm.user_id = ? AND cm.listed = 1 AND cm.role = 'admin')) ORDER BY c.id",
        (user_id,),
    ).fetchall()
    return [r[0] for r in rows]


def admin_update_user(
    conn: Any,
    actor_id: int,
    user_id: int,
    role: Any = None,
    disabled: Any = None,
    display_name: Any = None,
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    """``admin.update_user``.  Order: none of the fields => ``bad_request``; unknown user => ``not_found``; actor not
    an admin => ``forbidden``; demoting or disabling the last active (enabled) admin => ``invalid_state`` (reason
    ``last_admin``); display name taken => ``conflict`` (reason ``name_taken``).  A no-op writes no audit row.

    Effects: ``users`` update; a role change mirrors ``chat_members.role`` in the default group; disabling promotes a
    new admin in every non-default group that lost its last enabled admin (ascending ``chat_id``).  Events:
    ``ev.user_update`` (all) [+ ``ev.me`` to the actor when ``actor == user``]; role change:
    ``ev.chat_members {updated}`` for the default group; disable: a ``hub.revoke`` directive
    (``hub.revoke(user_id, reason='disabled')``), then per promoted group ``ev.chat_members {updated}`` + system
    ``promoted`` message.  ``touched_chats`` lists the chats written.
    """
    from . import db_users

    need_id(user_id, "user_id")
    reject_invalid_text(role, display_name)
    if role is None and disabled is None and display_name is None:
        raise ChatError("bad_request", "nothing to update")
    if role is not None and role not in ("admin", "member"):
        raise ChatError("bad_request", "role must be 'admin' or 'member'")
    if disabled is not None:
        need_bool(disabled, "disabled")
    new_name = None if display_name is None else clean_text(display_name, "display_name", _DISPLAY_NAME_MAX, 1)
    actor = caller(conn, actor_id)
    target = q_one(conn, "SELECT %s FROM users WHERE id = ?" % _USER_COLUMNS, (user_id,))
    if target is None:
        raise ChatError("not_found", "user not found")
    if actor["role"] != "admin":
        raise ChatError("forbidden", "administrators only")
    changes: Dict[str, Any] = {}
    if role is not None and role != target["role"]:
        changes["role"] = role
    if disabled is not None and int(disabled) != target["disabled"]:
        changes["disabled"] = int(disabled)
    if new_name is not None and new_name != target["display_name"]:
        changes["display_name"] = new_name
    if not changes:
        return outcome({"user": db_users.get_user(conn, target["id"])}, noop=True)
    was_active_admin = target["role"] == "admin" and not target["disabled"]
    stays_active_admin = changes.get("role", target["role"]) == "admin" and not changes.get(
        "disabled", target["disabled"]
    )
    if was_active_admin and not stays_active_admin:
        others = conn.execute(
            "SELECT 1 FROM users WHERE role = 'admin' AND disabled = 0 AND id != ? LIMIT 1", (user_id,)
        ).fetchone()
        if others is None:
            raise ChatError("invalid_state", "the last active admin cannot be demoted or disabled", reason="last_admin")
    if "display_name" in changes:
        key = util.display_key(new_name)
        clash = conn.execute(
            "SELECT 1 FROM users WHERE id != ? AND (display_key = ? OR username = ?) LIMIT 1", (user_id, key, key)
        ).fetchone()
        if clash is not None:
            raise ChatError("conflict", "that display name is taken", reason="name_taken")
    if "display_name" in changes:
        conn.execute(
            "UPDATE users SET display_name = ?, display_key = ? WHERE id = ?",
            (new_name, util.display_key(new_name), user_id),
        )
    for column in ("role", "disabled"):
        if column in changes:
            conn.execute("UPDATE users SET %s = ? WHERE id = ?" % column, (changes[column], user_id))
    db_users.audit(conn, actor_id, "admin.update_user", user_id, ip)
    now = util.now()
    events: List[Dict[str, Any]] = []
    touched: List[int] = []
    index: List[Dict[str, Any]] = []
    public = db_users.get_user(conn, user_id)
    events.append(event("ev.user_update", [group(None, {"user": public})]))
    if user_id == actor_id:
        events.append(event("ev.me", [group([actor_id], {"me": db_users.get_me(conn, actor_id)})]))
    everyone = conn.execute("SELECT id FROM chats WHERE is_default = 1").fetchone()
    if "role" in changes and everyone is not None:
        conn.execute(
            "UPDATE chat_members SET role = ? WHERE chat_id = ? AND user_id = ?",
            (changes["role"], everyone[0], user_id),
        )
        if conn.execute("SELECT changes()").fetchone()[0]:
            touched.append(everyone[0])
            members = [r["user_id"] for r in recipients_of(conn, everyone[0])]
            add_event(
                events, chat_members_event(everyone[0], members, updated=[_member_entry_of(conn, everyone[0], user_id)])
            )
            index.append(index_entry(conn, everyone[0]))
    if "disabled" in changes:
        if changes["disabled"]:
            events.append(
                {
                    "t": "hub.revoke",
                    "user_id": user_id,
                    "reason": "disabled",
                    "durable": True,
                    "key": None,
                    "groups": [],
                }
            )
            groups_ = conn.execute(
                "SELECT c.id FROM chats c JOIN chat_members cm ON cm.chat_id = c.id WHERE cm.user_id = ?"
                " AND cm.listed = 1 AND cm.role = 'admin' AND c.kind = 'group' AND c.is_default = 0 ORDER BY c.id",
                (user_id,),
            ).fetchall()
            for (chat_id,) in groups_:
                promoted = promote_if_needed(conn, chat_id, now)
                if promoted is None:
                    continue
                touched.append(chat_id)
                members = [r["user_id"] for r in recipients_of(conn, chat_id)]
                add_event(
                    events,
                    chat_members_event(
                        chat_id, members, updated=[_member_entry_of(conn, chat_id, promoted["user_id"])]
                    ),
                )
                add_event(events, db_messages.system_message_event(conn, chat_id, promoted["message_id"]))
                index.append(index_entry(conn, chat_id))
        for (chat_id,) in conn.execute(
            "SELECT c.id FROM chats c JOIN chat_members cm ON cm.chat_id = c.id WHERE cm.user_id = ?"
            " AND c.kind = 'direct' ORDER BY c.id",
            (user_id,),
        ).fetchall():
            index.append(index_entry(conn, chat_id))
    return outcome({"user": public}, events, index, touched_chats=sorted(touched))
