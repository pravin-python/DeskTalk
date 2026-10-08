"""Message write/read functions, the visibility predicate and Message serialisation (SPEC 3.1, 7.2, 7.4, 7.6).

Every public ``fn(conn, ...)`` is synchronous and runs inside ``Database.run`` (writer, already inside
``BEGIN IMMEDIATE``) or ``Database.run_read`` (reader).  Write functions return an *outcome* dict (``db_chats.outcome``:
``res``, ordered ``events``, ``index`` and the post-write record of SPEC 7.6(6)); read functions return the ``res``
payload directly.

Serialisation is split in two so the hub can encode each distinct per-recipient *variant* once (SPEC 6, 7.2):
:func:`load_parts` builds the viewer-independent :class:`Parts` of a message, :func:`render` and
:func:`serialize_message_variants` turn them into the Message dict of one variant ``(is_sender, starred,
reply_state)``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import db_chats, db_receipts, util

__all__ = [
    "Parts",
    "audience_record",
    "chat_history",
    "derive_mentions",
    "find_visible",
    "floor_sql",
    "insert_system_message",
    "load_parts",
    "message_chat_id",
    "message_event",
    "message_record",
    "msg_delete",
    "msg_edit",
    "msg_forward",
    "msg_info",
    "msg_pin",
    "msg_react",
    "msg_search",
    "msg_send",
    "msg_shared",
    "msg_star",
    "msg_starred",
    "render",
    "reply_state",
    "serialize_for_viewer",
    "serialize_message_variants",
    "system_message_event",
    "validate_emoji",
    "viewer_floors",
    "viewer_pairs",
    "visible_sql",
]

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9._-])@([A-Za-z0-9._-]{3,32})")
_MAX_MENTIONS = 20
_MAX_MENTION_CANDIDATES = 300
_MAX_PINNED_MESSAGES = 5
_MAX_FORWARD_MESSAGES = 20
_MAX_FORWARD_CHATS = 5
_QUOTE_CHARS = 200
_SHARED_KINDS = {
    "media": "m.kind IN ('image', 'video')",
    "files": "m.kind IN ('file', 'audio')",
    "links": "m.kind = 'text' AND (m.body LIKE '%http://%' OR m.body LIKE '%https://%')",
}

#: Variant key of one recipient: ``(is_sender, starred, reply_state)`` with ``reply_state`` in
#: ``none | visible | deleted | unavailable`` (SPEC 7.2).
Variant = Tuple[bool, bool, str]


# --------------------------------------------------------------------------------------------------------------------
# The visibility predicate (SPEC 3.1) - the ONE definition; every query of this package builds on it
# --------------------------------------------------------------------------------------------------------------------


def floor_sql(*extra: str, cm: str = "cm") -> str:
    """SQL expression ``max(history_from_id, cleared_before_id[, extra...])`` of the viewer row aliased ``cm``."""
    cols = ["%s.history_from_id" % cm, "%s.cleared_before_id" % cm]
    cols.extend(extra)
    return "max(%s)" % ", ".join(cols)


def visible_sql(*extra_floors: str, m: str = "m", cm: str = "cm", exclude_hidden: bool = True) -> str:
    """SQL condition ``visible(viewer, m)`` of SPEC 3.1 for message alias ``m`` and viewer ``chat_members`` row ``cm``.

    ``cm`` is a LISTED member row (``listed = 1``), ``m.id`` is above ``max(history_from_id, cleared_before_id)`` (plus
    optional extra lower bounds such as ``cm.last_read_id`` for unread counts) and the viewer has no ``hidden_messages``
    row for it.  ``exclude_hidden=False`` drops only the last clause (used to find a message the viewer already hid).
    """
    cond = "{cm}.listed = 1 AND {m}.id > {floor}".format(cm=cm, m=m, floor=floor_sql(*extra_floors, cm=cm))
    if exclude_hidden:
        cond += (
            " AND NOT EXISTS (SELECT 1 FROM hidden_messages hv WHERE hv.user_id = {cm}.user_id"
            " AND hv.message_id = {m}.id)"
        ).format(cm=cm, m=m)
    return cond


_FIND_SELECT = (
    "SELECT m.id AS id, m.chat_id AS chat_id, m.sender_id AS sender_id, m.client_id AS client_id, m.kind AS kind,"
    " m.body AS body, m.reply_to_id AS reply_to_id, m.forwarded AS forwarded, m.attachment_id AS attachment_id,"
    " m.created_at AS created_at, m.edited_at AS edited_at, m.deleted_at AS deleted_at, c.kind AS chat_kind,"
    " c.direct_key AS direct_key, c.only_admins_post AS only_admins_post, c.is_default AS is_default,"
    " cm.role AS my_role, cm.history_from_id AS history_from_id, cm.cleared_before_id AS cleared_before_id,"
    " cm.last_read_id AS my_last_read_id"
    " FROM messages m JOIN chat_members cm ON cm.chat_id = m.chat_id AND cm.user_id = ?"
    " JOIN chats c ON c.id = m.chat_id WHERE m.id = ? AND "
)


def find_visible(conn: Any, viewer_id: int, message_id: int, allow_hidden: bool = False) -> Dict[str, Any]:
    """Return the message row ``message_id`` joined with the viewer's membership and its chat, or raise ``not_found``.

    ``not_found`` covers unknown ids, chats the viewer is not a listed member of and messages outside ``V(viewer)``
    (SPEC 3.1).  ``allow_hidden=True`` also accepts a message the viewer hid for themselves (idempotent delete-for-me).
    """
    row = db_chats.q_one(conn, _FIND_SELECT + visible_sql(exclude_hidden=not allow_hidden), (viewer_id, message_id))
    if row is None:
        raise db_chats.ChatError("not_found", "message not found", message_id=message_id)
    return row


def message_chat_id(conn: Any, message_id: int) -> Optional[int]:
    """The ``chat_id`` of a message (no authorisation; for the hub to pick the chat lock before ``run``)."""
    row = conn.execute("SELECT chat_id FROM messages WHERE id = ?", (message_id,)).fetchone()
    return int(row[0]) if row is not None else None


# --------------------------------------------------------------------------------------------------------------------
# Serialisation: viewer-independent Parts + per-variant render (SPEC 7.2)
# --------------------------------------------------------------------------------------------------------------------


class Parts:
    """The viewer-independent part of a Message (all but ``client_id``, ``status``, ``starred`` and ``reply_to``).

    ``msg`` is the shared Message dict with neutral values for those four fields; ``replies`` maps the reply states
    ``visible`` / ``deleted`` / ``unavailable`` to the ``reply_to`` object (``None`` when the message quotes nothing).
    """

    __slots__ = ("chat_id", "client_id", "id", "kind", "msg", "quoted_deleted", "quoted_id", "replies", "sender_id")

    def __init__(
        self,
        msg: Dict[str, Any],
        client_id: Optional[str],
        quoted_id: Optional[int],
        quoted_deleted: bool,
        replies: Optional[Dict[str, Dict[str, Any]]],
    ) -> None:
        self.msg = msg
        self.id = msg["id"]
        self.chat_id = msg["chat_id"]
        self.sender_id = msg["sender_id"]
        self.kind = msg["kind"]
        self.client_id = client_id
        self.quoted_id = quoted_id
        self.quoted_deleted = quoted_deleted
        self.replies = replies


_PARTS_SQL = (
    "SELECT m.id AS id, m.chat_id AS chat_id, m.sender_id AS sender_id, m.client_id AS client_id, m.kind AS kind,"
    " m.body AS body, m.reply_to_id AS reply_to_id, m.forwarded AS forwarded, m.attachment_id AS attachment_id,"
    " m.mentions AS mentions, m.system AS system, m.created_at AS created_at, m.edited_at AS edited_at,"
    " m.deleted_at AS deleted_at, a.name AS a_name, a.mime AS a_mime, a.size AS a_size, a.kind AS a_kind,"
    " a.width AS a_width, a.height AS a_height, a.duration AS a_duration, q.id AS q_id, q.sender_id AS q_sender_id,"
    " q.kind AS q_kind, q.body AS q_body, q.deleted_at AS q_deleted_at, qa.name AS q_att_name"
    " FROM messages m LEFT JOIN attachments a ON a.id = m.attachment_id LEFT JOIN messages q ON q.id = m.reply_to_id"
    " LEFT JOIN attachments qa ON qa.id = q.attachment_id WHERE m.id IN (%s)"
)


def _reactions_by_message(conn: Any, ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Grouped reactions per message: groups by smallest ``created_at`` (ties by emoji), users by ``created_at``, id."""
    rows: List[Dict[str, Any]] = []
    for chunk in db_chats.chunked(ids):
        rows.extend(
            db_chats.q_all(
                conn,
                "SELECT message_id, user_id, emoji, created_at FROM reactions WHERE message_id IN (%s)"
                " ORDER BY message_id, created_at, user_id" % db_chats.placeholders(len(chunk)),
                chunk,
            )
        )
    grouped: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        per_msg = grouped.setdefault(r["message_id"], {})
        entry = per_msg.get(r["emoji"])
        if entry is None:
            per_msg[r["emoji"]] = {"first": r["created_at"], "emoji": r["emoji"], "user_ids": [r["user_id"]]}
        else:
            entry["first"] = min(entry["first"], r["created_at"])
            entry["user_ids"].append(r["user_id"])
    out: Dict[int, List[Dict[str, Any]]] = {}
    for mid, per_msg in grouped.items():
        ordered = sorted(per_msg.values(), key=lambda e: (e["first"], e["emoji"]))
        out[mid] = [{"emoji": e["emoji"], "user_ids": e["user_ids"]} for e in ordered]
    return out


def _pinned_ids(conn: Any, chat_ids: Iterable[int]) -> Set[int]:
    """Ids of all pinned messages of the given chats (<= 5 per chat; ``pins`` has no ``message_id`` index)."""
    found: Set[int] = set()
    for chunk in db_chats.chunked(sorted(set(chat_ids))):
        rows = conn.execute(
            "SELECT message_id FROM pins WHERE chat_id IN (%s)" % db_chats.placeholders(len(chunk)), chunk
        ).fetchall()
        found.update(r[0] for r in rows)
    return found


def _build_parts(row: Dict[str, Any], reactions: List[Dict[str, Any]], pinned: bool) -> Parts:
    deleted = row["deleted_at"] is not None
    attachment = None
    if row["attachment_id"] is not None and not deleted:
        attachment = {
            "id": row["attachment_id"],
            "name": row["a_name"],
            "mime": row["a_mime"],
            "size": row["a_size"],
            "kind": row["a_kind"],
            "url": "/files/%s" % row["attachment_id"],
            "width": row["a_width"],
            "height": row["a_height"],
            "duration": row["a_duration"],
        }
    msg = {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "sender_id": row["sender_id"],
        "client_id": None,
        "kind": row["kind"],
        "body": "" if deleted else row["body"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "deleted": deleted,
        "forwarded": bool(row["forwarded"]),
        "mentions": [] if deleted else json.loads(row["mentions"]),
        "reply_to": None,
        "attachment": attachment,
        "reactions": [] if deleted else reactions,
        "starred": False,
        "pinned": False if deleted else pinned,
        "status": None,
        "system": json.loads(row["system"]) if row["system"] else None,
    }
    quoted_id = None if deleted else row["q_id"]
    replies: Optional[Dict[str, Dict[str, Any]]] = None
    quoted_deleted = False
    if quoted_id is not None:
        quoted_deleted = row["q_deleted_at"] is not None
        live = {
            "id": quoted_id,
            "sender_id": row["q_sender_id"],
            "kind": row["q_kind"],
            "body": "" if quoted_deleted else (row["q_body"] or "")[:_QUOTE_CHARS],
            "attachment_name": None if quoted_deleted else row["q_att_name"],
            "deleted": quoted_deleted,
            "unavailable": False,
        }
        replies = {
            "visible": live,
            "deleted": dict(live, body="", attachment_name=None, deleted=True),
            "unavailable": {
                "id": quoted_id,
                "sender_id": None,
                "kind": "text",
                "body": "",
                "attachment_name": None,
                "deleted": False,
                "unavailable": True,
            },
        }
    return Parts(msg, row["client_id"], quoted_id, quoted_deleted, replies)


def load_parts(conn: Any, message_ids: Iterable[int]) -> Dict[int, Parts]:
    """Load the viewer-independent :class:`Parts` of many messages with a constant number of queries.

    Missing ids are absent from the result.  Reads reactions (by message id), pins (by chat) and the quoted messages
    in set-based queries; never queries per message.
    """
    ids = sorted(set(message_ids))
    rows: List[Dict[str, Any]] = []
    for chunk in db_chats.chunked(ids):
        rows.extend(db_chats.q_all(conn, _PARTS_SQL % db_chats.placeholders(len(chunk)), chunk))
    if not rows:
        return {}
    reactions = _reactions_by_message(conn, [r["id"] for r in rows])
    pinned = _pinned_ids(conn, {r["chat_id"] for r in rows})
    return {r["id"]: _build_parts(r, reactions.get(r["id"], []), r["id"] in pinned) for r in rows}


def reply_state(parts: Parts, floor: int, quote_hidden: bool) -> str:
    """The ``reply_state`` of a recipient (SPEC 7.2): ``none`` | ``visible`` | ``deleted`` | ``unavailable``.

    A quote is ``unavailable`` iff ``quoted.id <= max(history_from_id, cleared_before_id)`` or the viewer hid it.
    """
    if parts.quoted_id is None:
        return "none"
    if parts.quoted_id <= floor or quote_hidden:
        return "unavailable"
    return "deleted" if parts.quoted_deleted else "visible"


def render(
    parts: Parts,
    is_sender: bool = False,
    starred: bool = False,
    state: str = "none",
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """The Message dict of one variant.  Nested objects are shared between variants (read-only); ``client_id`` and
    ``status`` are filled only for the sender's variant, ``starred`` and ``reply_to`` per ``state``."""
    out = dict(parts.msg)
    if is_sender:
        out["client_id"] = parts.client_id
        out["status"] = status
    if starred:
        out["starred"] = True
    if state != "none" and parts.replies is not None:
        out["reply_to"] = parts.replies[state]
    return out


def serialize_message_variants(
    parts: Parts, variants: Iterable[Variant], status: Optional[str] = None
) -> Dict[Variant, Dict[str, Any]]:
    """``{variant: Message}`` for the distinct ``(is_sender, starred, reply_state)`` variants (SPEC 6): the hub
    encodes each value once and hands the same string to every recipient of that variant."""
    return {v: render(parts, v[0], v[1], v[2], status) for v in dict.fromkeys(variants)}


def viewer_pairs(conn: Any, table: str, viewer_ids: Iterable[int], ids: Iterable[int]) -> Set[Tuple[int, int]]:
    """``{(user_id, message_id)}`` of ``stars`` or ``hidden_messages`` for the given viewers and messages."""
    if table not in ("stars", "hidden_messages"):
        raise ValueError("unsupported table")
    viewers = sorted(set(viewer_ids))
    wanted = sorted(set(ids))
    found: Set[Tuple[int, int]] = set()
    for vchunk in db_chats.chunked(viewers, 200):
        for ichunk in db_chats.chunked(wanted, 200):
            sql = "SELECT user_id, message_id FROM %s WHERE user_id IN (%s) AND message_id IN (%s)" % (
                table,
                db_chats.placeholders(len(vchunk)),
                db_chats.placeholders(len(ichunk)),
            )
            found.update((r[0], r[1]) for r in conn.execute(sql, vchunk + ichunk).fetchall())
    return found


def viewer_floors(conn: Any, viewer_id: int, chat_ids: Iterable[int]) -> Dict[int, int]:
    """``{chat_id: max(history_from_id, cleared_before_id)}`` of the viewer's listed memberships."""
    floors: Dict[int, int] = {}
    for chunk in db_chats.chunked(sorted(set(chat_ids))):
        rows = conn.execute(
            "SELECT chat_id, max(history_from_id, cleared_before_id) FROM chat_members"
            " WHERE user_id = ? AND listed = 1 AND chat_id IN (%s)" % db_chats.placeholders(len(chunk)),
            [viewer_id] + chunk,
        ).fetchall()
        floors.update((r[0], r[1]) for r in rows)
    return floors


def render_for_viewer(
    conn: Any, viewer_id: int, parts_list: Sequence[Parts], floors: Optional[Dict[int, int]] = None
) -> List[Dict[str, Any]]:
    """Render ``parts_list`` as the Messages ``viewer_id`` sees (own ``client_id``/``status``/star, quote state)."""
    if not parts_list:
        return []
    starred = {mid for _, mid in viewer_pairs(conn, "stars", [viewer_id], [p.id for p in parts_list])}
    quoted = [p.quoted_id for p in parts_list if p.quoted_id is not None]
    quote_hidden = {mid for _, mid in viewer_pairs(conn, "hidden_messages", [viewer_id], quoted)}
    if floors is None:
        floors = viewer_floors(conn, viewer_id, {p.chat_id for p in parts_list})
    contexts: Dict[int, db_receipts.StatusCtx] = {}
    out: List[Dict[str, Any]] = []
    for p in parts_list:
        is_sender = p.sender_id == viewer_id
        status = None
        if is_sender:
            ctx = contexts.get(p.chat_id)
            if ctx is None:
                ctx = contexts[p.chat_id] = db_receipts.status_ctx(conn, p.chat_id, viewer_id)
            status = db_receipts.status_of(ctx, p.id)
        state = reply_state(p, floors.get(p.chat_id, 0), p.quoted_id in quote_hidden)
        out.append(render(p, is_sender, p.id in starred, state, status))
    return out


def serialize_for_viewer(
    conn: Any, viewer_id: int, message_ids: Sequence[int], floors: Optional[Dict[int, int]] = None
) -> List[Dict[str, Any]]:
    """Messages as seen by ``viewer_id`` in the order of ``message_ids`` (missing ids are skipped).

    Does NOT check visibility: callers pass ids they have already selected through :func:`visible_sql`.
    """
    loaded = load_parts(conn, message_ids)
    return render_for_viewer(conn, viewer_id, [loaded[i] for i in message_ids if i in loaded], floors)


# --------------------------------------------------------------------------------------------------------------------
# System messages
# --------------------------------------------------------------------------------------------------------------------


def insert_system_message(
    conn: Any,
    chat_id: int,
    event: str,
    actor_id: Optional[int],
    target_ids: List[int],
    body: str,
    title: Optional[str] = None,
    message_id: Optional[int] = None,
    bump_activity: bool = True,
    ts: Optional[float] = None,
) -> int:
    """Insert a ``kind='system'`` message and return its id (SPEC 3 rules).

    Always sets ``chats.last_message_id`` to the new id; bumps ``chats.last_activity_at`` only when ``bump_activity``
    is true and ``event != 'joined'`` (registration must not re-sort every user's list).  ``created_at`` is
    ``max(ts or now, chats.last_activity_at)`` so a backwards clock step never yields an older message.
    """
    row = conn.execute("SELECT last_activity_at FROM chats WHERE id = ?", (chat_id,)).fetchone()
    last_activity = float(row[0]) if row is not None else 0.0
    created_at = max(float(ts) if ts is not None else util.now(), last_activity)
    payload = {
        "event": event,
        "actor_id": actor_id,
        "target_ids": [int(t) for t in target_ids],
        "title": title,
        "message_id": message_id,
    }
    cur = conn.execute(
        "INSERT INTO messages(chat_id, sender_id, client_id, kind, body, reply_to_id, forwarded, attachment_id,"
        " mentions, system, created_at) VALUES (?, NULL, NULL, 'system', ?, NULL, 0, NULL, '[]', ?, ?)",
        (chat_id, body, json.dumps(payload, separators=(",", ":")), created_at),
    )
    new_id = int(cur.lastrowid)
    if bump_activity and event != "joined":
        conn.execute(
            "UPDATE chats SET last_message_id = ?, last_activity_at = max(last_activity_at, ?) WHERE id = ?",
            (new_id, created_at, chat_id),
        )
    else:
        conn.execute("UPDATE chats SET last_message_id = ? WHERE id = ?", (new_id, chat_id))
    return new_id


def system_message_event(conn: Any, chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    """``ev.message`` of a system message to every current listed member (one group: system messages have no
    sender, no star and no quote, so there is a single variant).  ``None`` when the chat has no member left."""
    parts = load_parts(conn, [message_id]).get(message_id)
    rows = conn.execute(
        "SELECT user_id FROM chat_members WHERE chat_id = ? AND listed = 1 ORDER BY user_id", (chat_id,)
    ).fetchall()
    if parts is None or not rows:
        return None
    return db_chats.event("ev.message", [db_chats.group([r[0] for r in rows], {"message": render(parts)})])


# --------------------------------------------------------------------------------------------------------------------
# Recipients and per-variant fan-out (SPEC 7.6(6))
# --------------------------------------------------------------------------------------------------------------------


def _users_with(conn: Any, table: str, chat_id: int, message_id: int) -> Set[int]:
    """Listed members of a chat that have a ``stars`` / ``hidden_messages`` row for ``message_id`` (PK seeks)."""
    if table not in ("stars", "hidden_messages"):
        raise ValueError("unsupported table")
    rows = conn.execute(
        "SELECT cm.user_id FROM chat_members cm JOIN %s t ON t.user_id = cm.user_id AND t.message_id = ?"
        " WHERE cm.chat_id = ? AND cm.listed = 1" % table,
        (message_id, chat_id),
    ).fetchall()
    return {r[0] for r in rows}


def audience_record(conn: Any, chat_id: int, message_id: int, quoted_id: Optional[int]) -> Dict[str, Any]:
    """The ``recipients`` / ``hidden`` / ``starred`` / ``quote_hidden`` entries of the SPEC 7.6(6) record, read in the
    caller's transaction (``hidden`` and ``quote_hidden`` hold member ids with a ``hidden_messages`` row for the
    message / the quoted message, ``starred`` the ids with a ``stars`` row)."""
    return {
        "recipients": db_chats.recipients_of(conn, chat_id),
        "hidden": _users_with(conn, "hidden_messages", chat_id, message_id),
        "starred": _users_with(conn, "stars", chat_id, message_id),
        "quote_hidden": _users_with(conn, "hidden_messages", chat_id, quoted_id) if quoted_id is not None else set(),
    }


def _quoted_of(parts: Parts) -> Optional[Dict[str, Any]]:
    """The ``quoted`` entry of the record: the live preview of the quoted message (body empty when it is deleted)."""
    if parts.replies is None:
        return None
    live = parts.replies["visible"]
    return {key: live[key] for key in ("id", "sender_id", "kind", "body", "attachment_name", "deleted")}


def message_record(conn: Any, message_id: int) -> Tuple[Parts, Dict[str, Any]]:
    """Reload ``message_id`` AFTER the writes of the request and assemble the message-scoped entries of the
    SPEC 7.6(6) record: ``recipients``, ``hidden``, ``starred``, ``quote_hidden``, ``quoted``, ``sender_status``
    (plus ``self_chat``).  Returns ``(parts, record)``; ``parts`` is the viewer-independent Message."""
    parts = load_parts(conn, [message_id])[message_id]
    record = audience_record(conn, parts.chat_id, message_id, parts.quoted_id)
    chat = db_chats.q_one(conn, "SELECT kind, direct_key FROM chats WHERE id = ?", (parts.chat_id,))
    self_chat = chat["kind"] == "direct" and db_chats.is_self_direct(chat["direct_key"])
    record["quoted"] = _quoted_of(parts)
    record["self_chat"] = self_chat
    record["sender_status"] = (
        None
        if parts.sender_id is None
        else db_receipts.status_for(record["recipients"], parts.sender_id, parts.id, self_chat)
    )
    return parts, record


def message_event(t: str, parts: Parts, record: Dict[str, Any]) -> Dict[str, Any]:
    """``ev.message`` / ``ev.message_update`` with one group per distinct variant (SPEC 6, 7.6(6)).

    Audience = recipients ``u`` with ``message.id > max(history_from_id, cleared_before_id)`` and ``u`` not in
    ``hidden`` - exactly the SPEC 3.1 predicate.  The sender's variant carries ``client_id`` and
    ``record["sender_status"]``; ``starred`` and ``reply_to`` follow the recipient.
    """
    members: Dict[Variant, List[int]] = {}
    for r in record["recipients"]:
        uid = r["user_id"]
        floor = max(r["history_from_id"], r["cleared_before_id"])
        if parts.id <= floor or uid in record["hidden"]:
            continue
        variant = (
            uid == parts.sender_id,
            uid in record["starred"],
            reply_state(parts, floor, uid in record["quote_hidden"]),
        )
        members.setdefault(variant, []).append(uid)
    rendered = serialize_message_variants(parts, sorted(members), record["sender_status"])
    groups = [db_chats.group(sorted(members[v]), {"message": rendered[v]}) for v in sorted(members)]
    return db_chats.event(t, groups)


def _view_from_record(parts: Parts, record: Dict[str, Any], viewer_id: int) -> Dict[str, Any]:
    """The Message as ``viewer_id`` sees it, built from a record (no queries).  A viewer outside ``recipients`` gets
    the neutral variant."""
    is_sender = viewer_id == parts.sender_id
    for r in record["recipients"]:
        if r["user_id"] == viewer_id:
            floor = max(r["history_from_id"], r["cleared_before_id"])
            state = reply_state(parts, floor, viewer_id in record["quote_hidden"])
            return render(parts, is_sender, viewer_id in record["starred"], state, record["sender_status"])
    return render(parts, is_sender, False, "none", record["sender_status"])


def _unread_users(conn: Any, chat_id: int, message_id: int, only: Optional[Sequence[int]] = None) -> List[int]:
    """Members (optionally restricted to ``only``) for whom ``message_id`` is visible, unread (``id > last_read_id``)
    and not their own: the users whose counters a delete or a mention change affects (SPEC 7.6(6) ``read_sync``)."""
    sql = (
        "SELECT cm.user_id FROM chat_members cm JOIN messages m ON m.id = ? WHERE cm.chat_id = ?"
        " AND m.sender_id IS NOT cm.user_id AND " + visible_sql("cm.last_read_id")
    )
    if only is None:
        return [r[0] for r in conn.execute(sql + " ORDER BY cm.user_id", (message_id, chat_id)).fetchall()]
    found: List[int] = []
    for chunk in db_chats.chunked(sorted(set(only))):
        rows = conn.execute(
            sql + " AND cm.user_id IN (%s) ORDER BY cm.user_id" % db_chats.placeholders(len(chunk)),
            [message_id, chat_id] + chunk,
        ).fetchall()
        found.extend(r[0] for r in rows)
    return found


# --------------------------------------------------------------------------------------------------------------------
# Body, emoji and mention rules
# --------------------------------------------------------------------------------------------------------------------


def _clean_body(body: Any, max_chars: int) -> str:
    """SPEC 7.4 step 7: remove NUL and C0 controls (except newline and tab), strip, enforce ``max_body_chars``.

    The caller decides what an empty result means.
    """
    if not isinstance(body, str):
        raise db_chats.ChatError("bad_request", "body must be a string")
    db_chats.reject_invalid_text(body)
    text = "".join(ch for ch in body if ch >= " " or ch in "\n\t").strip()
    if len(text) > max_chars:
        raise db_chats.ChatError("too_large", "message is too long")
    return text


def validate_emoji(emoji: Any) -> str:
    """SPEC 7.4 ``msg.react`` emoji validation; returns the emoji or raises ``bad_request``."""
    bad = db_chats.ChatError("bad_request", "invalid emoji")
    if not isinstance(emoji, str) or not 1 <= len(emoji) <= 16:
        raise bad
    n = len(emoji)
    has_symbol = False
    i = 0
    while i < n:
        ch = emoji[i]
        cp = ord(ch)
        if ch in "0123456789#*":
            j = i + 1
            if j < n and emoji[j] == "️":
                j += 1
            if j < n and emoji[j] == "⃣":
                has_symbol = True
                i = j + 1
                continue
            raise bad
        if unicodedata.category(ch) in ("So", "Sk"):
            has_symbol = True
        elif not (cp in (0x200D, 0xFE0F, 0x20E3) or 0x1F1E6 <= cp <= 0x1F1FF or 0xE0020 <= cp <= 0xE007F):
            raise bad
        i += 1
    if not has_symbol:
        raise bad
    return emoji


def _mention_candidates(token: str) -> List[str]:
    """The greedy token and, while it ends in ``.`` or ``-``, its one-character trims (length >= 3)."""
    out = [token]
    while len(token) > 3 and token[-1] in ".-":
        token = token[:-1]
        out.append(token)
    return out


def derive_mentions(conn: Any, chat_id: int, sender_id: int, body: str) -> List[int]:
    """Server-derived mentions (SPEC 7.4): ids of current, non-disabled members other than the sender whose username
    appears as an ``@username`` token; at most 20 distinct; none in direct chats.  Ascending user ids."""
    if "@" not in body:
        return []
    kind = conn.execute("SELECT kind FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if kind is None or kind[0] != "group":
        return []
    tokens: List[List[str]] = []
    wanted: Dict[str, None] = {}
    for found in _MENTION_RE.finditer(body):
        cands = [c.lower() for c in _mention_candidates(found.group(1))]
        tokens.append(cands)
        wanted.update((c, None) for c in cands)
        if len(wanted) > _MAX_MENTION_CANDIDATES:
            break
    members: Dict[str, int] = {}
    for chunk in db_chats.chunked(list(wanted)):
        rows = conn.execute(
            "SELECT u.username, u.id FROM users u JOIN chat_members cm ON cm.user_id = u.id AND cm.chat_id = ?"
            " AND cm.listed = 1 WHERE u.disabled = 0 AND u.id != ? AND u.username IN (%s)"
            % db_chats.placeholders(len(chunk)),
            [chat_id, sender_id] + chunk,
        ).fetchall()
        members.update((r[0].lower(), r[1]) for r in rows)
    chosen: List[int] = []
    for cands in tokens:
        uid = next((members[c] for c in cands if c in members), None)
        if uid is not None and uid not in chosen:
            chosen.append(uid)
            if len(chosen) >= _MAX_MENTIONS:
                break
    return sorted(chosen)


def _store_mentions(conn: Any, message_id: int, user_ids: List[int]) -> Tuple[List[int], List[int]]:
    """Make ``message_mentions`` equal ``user_ids``; returns ``(added, removed)``."""
    old = {r[0] for r in conn.execute("SELECT user_id FROM message_mentions WHERE message_id = ?", (message_id,))}
    new = set(user_ids)
    removed = sorted(old - new)
    added = sorted(new - old)
    for uid in removed:
        conn.execute("DELETE FROM message_mentions WHERE message_id = ? AND user_id = ?", (message_id, uid))
    for uid in added:
        conn.execute("INSERT INTO message_mentions(message_id, user_id) VALUES (?, ?)", (message_id, uid))
    return added, removed


# --------------------------------------------------------------------------------------------------------------------
# Posting a message (msg.send / msg.forward share this pipeline)
# --------------------------------------------------------------------------------------------------------------------


def _existing_by_client_id(conn: Any, sender_id: int, client_id: str) -> Optional[Dict[str, Any]]:
    return db_chats.q_one(
        conn, "SELECT id, chat_id FROM messages WHERE sender_id = ? AND client_id = ?", (sender_id, client_id)
    )


def _disabled_peer(conn: Any, kind: str, direct_key: Optional[str], user_id: int) -> bool:
    """True for a direct chat (not the self-chat) whose other member is disabled."""
    if kind != "direct" or db_chats.is_self_direct(direct_key):
        return False
    peer = db_chats.direct_peer(direct_key, user_id)
    row = conn.execute("SELECT disabled FROM users WHERE id = ?", (peer,)).fetchone()
    return row is not None and bool(row[0])


def _sender_marks(
    conn: Any, chat: Dict[str, Any], user_id: int, new_id: int, seen_up_to_id: int, now: float
) -> Dict[str, Any]:
    """SPEC 7.4 "sender marks": delivered always advances; read marks only over messages the sender can be assumed to
    have seen.  Returns the ``db_receipts.advance_marks`` change record."""
    seen = max(chat["last_read_id"], min(seen_up_to_id, new_id - 1))
    floor = max(seen, chat["history_from_id"], chat["cleared_before_id"])
    countable = conn.execute(
        "SELECT 1 FROM messages m WHERE m.chat_id = ? AND m.id > ? AND m.id < ? AND m.sender_id != ?"
        " AND m.kind != 'system' AND m.deleted_at IS NULL AND NOT EXISTS"
        " (SELECT 1 FROM hidden_messages hv WHERE hv.user_id = ? AND hv.message_id = m.id) LIMIT 1",
        (chat["chat_id"], floor, new_id, user_id, user_id),
    ).fetchone()
    read_to = seen if countable is not None else new_id
    return db_receipts.advance_marks(
        conn, chat["chat_id"], user_id, delivered_to=new_id, last_read_to=read_to, public_read_to=read_to, now=now
    )


def _post_message(
    conn: Any,
    user_id: int,
    chat_id: int,
    client_id: str,
    kind: str,
    body: str,
    reply_to_id: Optional[int],
    forwarded: bool,
    attachment_id: Optional[str],
    seen_up_to_id: int,
    now: float,
) -> Dict[str, Any]:
    """Insert one message and run every side effect of SPEC 7.4 ``msg.send`` in the caller's transaction.

    Returns ``{"existing": row}`` when the unique ``(sender_id, client_id)`` index rejected the insert (a race the
    dedupe lookup could not see), otherwise ``{"id", "message", "events", "record", "index_changed"}`` where ``record``
    is the complete SPEC 7.6(6) record of the new message.
    """
    chat = db_chats.require_member(conn, user_id, chat_id)
    dormant = chat["kind"] == "direct" and chat["last_message_id"] is None
    created_at = max(now, chat["last_activity_at"])
    mentions = [] if forwarded else derive_mentions(conn, chat_id, user_id, body)
    conn.execute("SAVEPOINT msg_insert")
    try:
        cur = conn.execute(
            "INSERT INTO messages(chat_id, sender_id, client_id, kind, body, reply_to_id, forwarded, attachment_id,"
            " mentions, system, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                chat_id,
                user_id,
                client_id,
                kind,
                body,
                reply_to_id,
                1 if forwarded else 0,
                attachment_id,
                json.dumps(mentions, separators=(",", ":")),
                created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK TO msg_insert")
        conn.execute("RELEASE msg_insert")
        existing = _existing_by_client_id(conn, user_id, client_id)
        if existing is None or "UNIQUE" not in str(exc):
            raise
        return {"existing": existing}
    conn.execute("RELEASE msg_insert")
    new_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE chats SET last_message_id = ?, last_activity_at = max(last_activity_at, ?) WHERE id = ?",
        (new_id, created_at, chat_id),
    )
    for uid in mentions:
        conn.execute("INSERT INTO message_mentions(message_id, user_id) VALUES (?, ?)", (new_id, uid))

    unlisted: List[int] = []
    if dormant:
        unlisted = [
            r[0] for r in conn.execute("SELECT user_id FROM chat_members WHERE chat_id = ? AND listed = 0", (chat_id,))
        ]
        conn.execute("UPDATE chat_members SET listed = 1 WHERE chat_id = ? AND listed = 0", (chat_id,))
    change = _sender_marks(conn, chat, user_id, new_id, seen_up_to_id, now)
    flipped = [
        r[0]
        for r in conn.execute(
            "SELECT user_id FROM chat_members WHERE chat_id = ? AND listed = 1 AND archived = 1 AND user_id != ?"
            " AND muted_until <= ? ORDER BY user_id",
            (chat_id, user_id, now),
        )
    ]
    if flipped:
        conn.execute(
            "UPDATE chat_members SET archived = 0 WHERE chat_id = ? AND listed = 1 AND archived = 1 AND user_id != ?"
            " AND muted_until <= ?",
            (chat_id, user_id, now),
        )

    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    events: List[Dict[str, Any]] = []
    if unlisted:
        db_chats.add_event(events, db_chats.chat_update_event(conn, chat_id, unlisted, chats))
    parts, record = message_record(conn, new_id)
    events.append(message_event("ev.message", parts, record))
    receipt = db_receipts.receipt_record(conn, chat_id, user_id, change)
    db_chats.add_event(events, db_receipts.receipt_event(receipt))
    sync = db_receipts.read_sync_for(conn, chat_id, [user_id]) if change["private_changed"] else {}
    db_chats.add_event(events, db_receipts.read_sync_event(sync))
    if flipped:
        db_chats.add_event(events, db_chats.chat_update_event(conn, chat_id, flipped, chats))
    record.update({"read_sync": sync, "receipt": receipt, "receipts": [receipt] if receipt else [], "chats": chats})
    return {
        "id": new_id,
        "message": _view_from_record(parts, record, user_id),
        "events": events,
        "record": record,
        "parts": parts,
        "index_changed": bool(unlisted),
    }


def _own_message_view(conn: Any, user_id: int, message_id: int) -> Dict[str, Any]:
    return serialize_for_viewer(conn, user_id, [message_id])[0]


def _deduped(conn: Any, user_id: int, chat_id: int, existing: Dict[str, Any]) -> Dict[str, Any]:
    if existing["chat_id"] != chat_id:
        raise db_chats.ChatError("conflict", "client_id was used for a different chat", chat_id=chat_id)
    return db_chats.outcome(
        {"message": _own_message_view(conn, user_id, existing["id"])}, [], [], deduped=True, noop=True
    )


def _check_client_id(client_id: Any) -> None:
    if not isinstance(client_id, str) or not _CLIENT_ID_RE.match(client_id):
        raise db_chats.ChatError("bad_request", "client_id must be 8..64 characters of [A-Za-z0-9_-]")


def msg_send(
    conn: Any,
    user_id: int,
    chat_id: int,
    client_id: str,
    body: Optional[str] = None,
    attachment_id: Optional[str] = None,
    reply_to_id: Optional[int] = None,
    seen_up_to_id: Optional[int] = None,
) -> Dict[str, Any]:
    """``msg.send`` (SPEC 7.4): the one-run algorithm - shape, caller, membership, permissions, dedupe, reply target,
    attachment, body, insert, mentions, sender marks, dormant listing - and the SPEC 7.3 events.

    Outcome: ``res = {"message": <sender-variant Message>}``; ``events`` in enqueue order (dormant ``ev.chat_update``,
    ``ev.message`` variants, ``ev.receipt``, ``ev.read_sync``, archived-reset ``ev.chat_update``) plus the SPEC 7.6(6)
    record (``recipients``, ``hidden``, ``starred``, ``quote_hidden``, ``quoted``, ``sender_status``, ``read_sync``,
    ``receipt``, ``chats``); ``parts`` is the viewer-independent Message for :func:`serialize_message_variants`.
    ``deduped`` is true when an existing message was returned (no events; the hub refunds the rate-limit token);
    ``index`` lists membership-index entries to replace (a dormant direct chat became listed).
    """
    db_chats.reject_invalid_text(client_id, body, attachment_id)
    db_chats.need_id(chat_id, "chat_id")
    _check_client_id(client_id)
    if body is not None and not isinstance(body, str):
        raise db_chats.ChatError("bad_request", "body must be a string")
    if attachment_id is not None and (not isinstance(attachment_id, str) or len(attachment_id) > 64):
        raise db_chats.ChatError("bad_request", "invalid attachment_id")
    if reply_to_id is not None:
        db_chats.need_id(reply_to_id, "reply_to_id")
    seen = 0 if seen_up_to_id is None else db_chats.need_uint(seen_up_to_id, "seen_up_to_id")
    db_chats.caller(conn, user_id)
    chat = db_chats.require_member(conn, user_id, chat_id)
    if chat["kind"] == "group" and chat["only_admins_post"] and chat["my_role"] != "admin":
        raise db_chats.ChatError("forbidden", "only admins can post in this chat", chat_id=chat_id)
    if _disabled_peer(conn, chat["kind"], chat["direct_key"], user_id):
        raise db_chats.ChatError("invalid_state", "this account is disabled", reason="peer_disabled", chat_id=chat_id)

    existing = _existing_by_client_id(conn, user_id, client_id)
    if existing is not None:
        return _deduped(conn, user_id, chat_id, existing)

    if reply_to_id is not None:
        quoted = find_visible(conn, user_id, reply_to_id)
        if quoted["chat_id"] != chat_id:
            raise db_chats.ChatError("not_found", "message not found", message_id=reply_to_id)
        if quoted["kind"] == "system" or quoted["deleted_at"] is not None:
            raise db_chats.ChatError("invalid_state", "cannot reply to this message", message_id=reply_to_id)
    attachment = None
    if attachment_id is not None:
        attachment = db_chats.q_one(
            conn,
            "SELECT a.id AS id, a.kind AS kind FROM attachments a WHERE a.id = ? AND a.uploader_id = ?"
            " AND NOT EXISTS (SELECT 1 FROM messages WHERE attachment_id = a.id)",
            (attachment_id, user_id),
        )
        if attachment is None:
            raise db_chats.ChatError("not_found", "attachment not found")
    text = _clean_body(body if body is not None else "", db_chats.LIMITS.max_body_chars)
    if not text and attachment is None:
        raise db_chats.ChatError("bad_request", "empty message")

    kind = attachment["kind"] if attachment is not None else "text"
    posted = _post_message(
        conn, user_id, chat_id, client_id, kind, text, reply_to_id, False, attachment_id, seen, util.now()
    )
    if "existing" in posted:
        return _deduped(conn, user_id, chat_id, posted["existing"])
    index = [db_chats.index_entry(conn, chat_id)] if posted["index_changed"] else []
    return db_chats.outcome(
        {"message": posted["message"]}, posted["events"], index, parts=posted["parts"], **posted["record"]
    )


def _forward_client_id(client_id: str, chat_id: int, source_id: int) -> str:
    digest = hashlib.sha256(("%s|%d|%d" % (client_id, chat_id, source_id)).encode("utf-8"))
    return digest.hexdigest()[:32]


def _id_list(value: Any, name: str, limit: int) -> List[int]:
    """Validated, de-duplicated (first occurrence kept) non-empty id list of at most ``limit`` entries."""
    if not isinstance(value, (list, tuple)):
        raise db_chats.ChatError("bad_request", "%s must be a list" % name)
    ids = list(dict.fromkeys(db_chats.need_id(v, name) for v in value))
    if not ids or len(ids) > limit:
        raise db_chats.ChatError("bad_request", "%s must hold 1..%d ids" % (name, limit))
    return ids


def msg_forward(conn: Any, user_id: int, message_ids: Any, chat_ids: Any, client_id: str) -> Dict[str, Any]:
    """``msg.forward`` (SPEC 7.4): atomic, idempotent, several target chats.  Evaluation order (``err.chat_id`` /
    ``err.message_id`` name the first culprit): (1) shape and de-duplication => ``bad_request``; (3) caller disabled =>
    ``unauthorized``; (4) dedupe: when the copy of the FIRST (chat, source) pair of the creation order already exists
    the request is a retry - the existing copies are returned in creation order with no events (``deduped``), and a
    retry whose other copies do not all exist => ``conflict``; (5) targets in ``chat_ids`` order not a member =>
    ``not_member``; (6) sources ascending unknown / not visible => ``not_found``; (7) the first deleted or system
    source => ``invalid_state``; (8) per target in ``chat_ids`` order: ``only_admins_post`` and not admin =>
    ``forbidden``, a disabled direct peer => ``invalid_state``.

    Creation order: each chat in ``chat_ids`` order, each source ascending by id.  A copy keeps ``kind``, ``body`` and
    the same ``attachments`` row, sets ``forwarded=1``, drops the reply and mentions and uses ``client_id =
    sha256("<client_id>|<chat_id>|<source_id>")[:32]``.

    Outcome: ``res = {"messages": [<sender-variant Message>, ...]}`` (creation order); ``events`` are the per-message
    events of ``msg.send`` concatenated in creation order; ``records`` lists one full SPEC 7.6(6) record per CREATED
    message (each with ``message_id`` / ``chat_id`` / ``parts``); the top-level ``chats``, ``read_sync`` and
    ``receipts`` aggregate them; ``deduped`` / ``noop`` are true when nothing was created; ``index`` lists the
    membership-index entries.
    """
    db_chats.reject_invalid_text(client_id)
    sources = sorted(_id_list(message_ids, "message_ids", _MAX_FORWARD_MESSAGES))
    targets = _id_list(chat_ids, "chat_ids", _MAX_FORWARD_CHATS)
    _check_client_id(client_id)
    db_chats.caller(conn, user_id)
    pairs = [(chat_id, source_id) for chat_id in targets for source_id in sources]
    derived = [_forward_client_id(client_id, c, s) for c, s in pairs]
    if _existing_by_client_id(conn, user_id, derived[0]) is not None:
        rows = [_existing_by_client_id(conn, user_id, d) for d in derived]
        if any(r is None or r["chat_id"] != c for r, (c, _) in zip(rows, pairs)):
            raise db_chats.ChatError("conflict", "client_id was used for a different forward")
        views = serialize_for_viewer(conn, user_id, [r["id"] for r in rows])
        return db_chats.outcome({"messages": views}, [], [], deduped=True, noop=True)
    chats = [db_chats.require_member(conn, user_id, cid) for cid in targets]
    found = [find_visible(conn, user_id, mid) for mid in sources]
    for src in found:
        if src["kind"] == "system" or src["deleted_at"] is not None:
            raise db_chats.ChatError("invalid_state", "cannot forward this message", message_id=src["id"])
    for chat in chats:
        if chat["kind"] == "group" and chat["only_admins_post"] and chat["my_role"] != "admin":
            raise db_chats.ChatError("forbidden", "only admins can post in this chat", chat_id=chat["chat_id"])
        if _disabled_peer(conn, chat["kind"], chat["direct_key"], user_id):
            raise db_chats.ChatError(
                "invalid_state", "this account is disabled", reason="peer_disabled", chat_id=chat["chat_id"]
            )

    now = util.now()
    messages: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []
    index: Dict[int, Dict[str, Any]] = {}
    all_chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    all_sync: Dict[int, Dict[str, Any]] = {}
    receipts: List[Dict[str, Any]] = []
    for chat in chats:
        chat_id = chat["chat_id"]
        for src in found:
            posted = _post_message(
                conn,
                user_id,
                chat_id,
                _forward_client_id(client_id, chat_id, src["id"]),
                src["kind"],
                src["body"],
                None,
                True,
                src["attachment_id"],
                0,
                now,
            )
            if "existing" in posted:
                messages.append(_own_message_view(conn, user_id, posted["existing"]["id"]))
                continue
            record = dict(posted["record"], message_id=posted["id"], chat_id=chat_id, parts=posted["parts"])
            messages.append(posted["message"])
            events.extend(posted["events"])
            records.append(record)
            all_chats.update(record["chats"])
            all_sync.update(record["read_sync"])
            receipts.extend(record["receipts"])
            if posted["index_changed"]:
                index[chat_id] = db_chats.index_entry(conn, chat_id)
    created = bool(records)
    return db_chats.outcome(
        {"messages": messages},
        events,
        list(index.values()),
        records=records,
        chats=all_chats,
        read_sync=all_sync,
        receipts=receipts,
        deduped=not created,
        noop=not created,
    )


# --------------------------------------------------------------------------------------------------------------------
# msg.edit / msg.delete / msg.react / msg.star / msg.pin
# --------------------------------------------------------------------------------------------------------------------


def msg_edit(conn: Any, user_id: int, message_id: int, body: str) -> Dict[str, Any]:
    """``msg.edit``: own non-deleted, non-forwarded ``text`` message within ``edit_window_s``.

    Order: shape; caller; ``not_found``; system => ``invalid_state`` (before ownership); someone else's =>
    ``forbidden``; deleted / non-text / forwarded => ``invalid_state``; body checks (``too_large``; empty =>
    ``bad_request``); window => ``window_expired``.  An unchanged normalised body is a no-op (``noop`` true, no events,
    ``edited_at`` untouched).  Mentions are recomputed; ``read_sync`` holds the users whose mention row was added or
    removed and for whom the message is visible and unread.  Outcome ``res = {"message": ...}`` plus the record.
    """
    db_chats.reject_invalid_text(body)
    db_chats.need_id(message_id, "message_id")
    if not isinstance(body, str):
        raise db_chats.ChatError("bad_request", "body must be a string")
    db_chats.caller(conn, user_id)
    row = find_visible(conn, user_id, message_id)
    if row["kind"] == "system":
        raise db_chats.ChatError("invalid_state", "system messages cannot be edited", message_id=message_id)
    if row["sender_id"] != user_id:
        raise db_chats.ChatError("forbidden", "not your message", message_id=message_id)
    if row["deleted_at"] is not None or row["kind"] != "text" or row["forwarded"]:
        raise db_chats.ChatError("invalid_state", "this message cannot be edited", message_id=message_id)
    text = _clean_body(body, db_chats.LIMITS.max_body_chars)
    if not text:
        raise db_chats.ChatError("bad_request", "empty message")
    now = util.now()
    if now - row["created_at"] > db_chats.LIMITS.edit_window_s:
        raise db_chats.ChatError("window_expired", "the edit window has passed", message_id=message_id)
    if text == row["body"]:
        return db_chats.outcome({"message": _own_message_view(conn, user_id, message_id)}, [], [], noop=True)
    mentions = derive_mentions(conn, row["chat_id"], user_id, text)
    conn.execute(
        "UPDATE messages SET body = ?, edited_at = ?, mentions = ? WHERE id = ?",
        (text, now, json.dumps(mentions, separators=(",", ":")), message_id),
    )
    added, removed = _store_mentions(conn, message_id, mentions)
    parts, record = message_record(conn, message_id)
    events = [message_event("ev.message_update", parts, record)]
    affected = _unread_users(conn, row["chat_id"], message_id, added + removed) if (added or removed) else []
    sync = db_receipts.read_sync_for(conn, row["chat_id"], affected) if affected else {}
    db_chats.add_event(events, db_receipts.read_sync_event(sync))
    record["read_sync"] = sync
    return db_chats.outcome({"message": _view_from_record(parts, record, user_id)}, events, [], parts=parts, **record)


def _release_attachment(conn: Any, attachment_id: Optional[str]) -> List[Dict[str, Any]]:
    """Delete the ``attachments`` row when no message references it any more (``db_users.drop_attachment_if_
    unreferenced``); returns ``[{id, path}]`` for the hub, which removes the FILE after commit (SPEC 3 rules)."""
    from . import db_users

    if attachment_id is None:
        return []
    path = db_users.drop_attachment_if_unreferenced(conn, attachment_id)
    return [{"id": attachment_id, "path": path}] if path is not None else []


def _delete_everyone(conn: Any, user_id: int, row: Dict[str, Any]) -> Dict[str, Any]:
    message_id, chat_id = row["id"], row["chat_id"]
    if row["deleted_at"] is not None:
        return db_chats.outcome({"message": _own_message_view(conn, user_id, message_id)}, [], [], noop=True)
    now = util.now()
    is_admin = row["chat_kind"] == "group" and row["my_role"] == "admin"
    if not is_admin and now - row["created_at"] > db_chats.LIMITS.delete_window_s:
        raise db_chats.ChatError("window_expired", "the delete window has passed", message_id=message_id)
    unread_users = _unread_users(conn, chat_id, message_id)
    was_pinned = (
        conn.execute("SELECT 1 FROM pins WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)).fetchone()
        is not None
    )
    conn.execute(
        "UPDATE messages SET deleted_at = ?, body = '', attachment_id = NULL, mentions = '[]', reply_to_id = NULL"
        " WHERE id = ?",
        (now, message_id),
    )
    for table in ("reactions", "message_mentions"):
        conn.execute("DELETE FROM %s WHERE message_id = ?" % table, (message_id,))
    conn.execute(
        "DELETE FROM stars WHERE message_id = ? AND user_id IN (SELECT user_id FROM chat_members WHERE chat_id = ?)",
        (message_id, chat_id),
    )
    conn.execute("DELETE FROM pins WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
    released = _release_attachment(conn, row["attachment_id"])
    parts, record = message_record(conn, message_id)
    events = [message_event("ev.message_update", parts, record)]
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    if was_pinned:
        db_chats.add_event(events, db_chats.chat_update_event(conn, chat_id, None, chats))
    sync = db_receipts.read_sync_for(conn, chat_id, unread_users) if unread_users else {}
    db_chats.add_event(events, db_receipts.read_sync_event(sync))
    record.update({"read_sync": sync, "chats": chats})
    return db_chats.outcome(
        {"message": _view_from_record(parts, record, user_id)},
        events,
        [],
        parts=parts,
        released_attachments=released,
        **record,
    )


def _newest_visible_id(conn: Any, viewer_id: int, chat_id: int) -> Optional[int]:
    row = conn.execute(
        "SELECT m.id FROM messages m JOIN chat_members cm ON cm.chat_id = m.chat_id AND cm.user_id = ?"
        " WHERE m.chat_id = ? AND " + visible_sql() + " ORDER BY m.id DESC LIMIT 1",
        (viewer_id, chat_id),
    ).fetchone()
    return row[0] if row is not None else None


def _delete_for_me(conn: Any, user_id: int, row: Dict[str, Any]) -> Dict[str, Any]:
    message_id, chat_id = row["id"], row["chat_id"]
    hidden = conn.execute(
        "SELECT 1 FROM hidden_messages WHERE user_id = ? AND message_id = ?", (user_id, message_id)
    ).fetchone()
    if hidden is not None:
        return db_chats.outcome({}, [], [], noop=True)
    was_last = _newest_visible_id(conn, user_id, chat_id) == message_id
    was_pinned = (
        conn.execute("SELECT 1 FROM pins WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)).fetchone()
        is not None
    )
    before = db_receipts.counters(conn, user_id, chat_id)
    conn.execute("INSERT INTO hidden_messages(user_id, message_id) VALUES (?, ?)", (user_id, message_id))
    removed = {"chat_id": chat_id, "message_ids": [message_id]}
    events = [db_chats.event("ev.message_removed", [db_chats.group([user_id], removed)])]
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    if was_last or was_pinned:
        db_chats.add_event(events, db_chats.chat_update_event(conn, chat_id, [user_id], chats))
    after = db_receipts.counters(conn, user_id, chat_id)
    sync: Dict[int, Dict[str, Any]] = {}
    if any(before[k] != after[k] for k in ("unread", "unread_mentions", "first_unread_mention_id")):
        sync[user_id] = after
        db_chats.add_event(events, db_receipts.read_sync_event(sync))
    return db_chats.outcome(
        {}, events, [], chats=chats, read_sync=sync, recipients=db_chats.recipients_of(conn, chat_id)
    )


def msg_delete(conn: Any, user_id: int, message_id: int, scope: str) -> Dict[str, Any]:
    """``msg.delete`` (SPEC 7.4).

    ``scope='everyone'``: the sender within ``delete_window_s`` (``window_expired``) or a group admin of that group
    chat at any time; someone else's message by a non-admin => ``forbidden``; already deleted => idempotent
    ``{message}`` (no events).  Side effects: the row stays as a placeholder (``body``, attachment, mentions and reply
    cleared), reactions / pins / stars / mention rows removed, the ``attachments`` row deleted when nothing references
    it any more (``released_attachments`` lists ``{id, path}``: the hub deletes the FILES after commit).  Events:
    ``ev.message_update``, ``ev.chat_update`` (when it was pinned), ``ev.read_sync`` (members who had it unread).
    ``scope='me'``: hides the message for the caller, ``res = {}``; events (actor only): ``ev.message_removed`` then
    ``ev.chat_update`` (``last_message`` or pins changed) then ``ev.read_sync`` (a counter changed).  A system message
    => ``invalid_state`` in both scopes (before ownership).
    """
    db_chats.reject_invalid_text(scope)
    db_chats.need_id(message_id, "message_id")
    if scope not in ("me", "everyone"):
        raise db_chats.ChatError("bad_request", "scope must be 'me' or 'everyone'")
    db_chats.caller(conn, user_id)
    row = find_visible(conn, user_id, message_id, allow_hidden=scope == "me")
    if row["kind"] == "system":
        raise db_chats.ChatError("invalid_state", "system messages cannot be deleted", message_id=message_id)
    if scope == "me":
        return _delete_for_me(conn, user_id, row)
    is_admin = row["chat_kind"] == "group" and row["my_role"] == "admin"
    if row["sender_id"] != user_id and not is_admin:
        raise db_chats.ChatError("forbidden", "not your message", message_id=message_id)
    return _delete_everyone(conn, user_id, row)


def _require_open(row: Dict[str, Any], what: str) -> None:
    if row["kind"] == "system" or row["deleted_at"] is not None:
        raise db_chats.ChatError("invalid_state", "cannot %s this message" % what, message_id=row["id"])


def msg_react(conn: Any, user_id: int, message_id: int, emoji: Optional[str]) -> Dict[str, Any]:
    """``msg.react`` with SET semantics: ``emoji`` replaces the caller's single reaction, ``None`` removes it.

    The same emoji again (or ``None`` without a reaction) is a no-op.  A deleted or system message, or a direct chat
    with a disabled peer, => ``invalid_state``.  Outcome: ``res = {"message": ...}``; ``ev.message_update`` to the
    members for whom the message is visible."""
    db_chats.reject_invalid_text(emoji)
    db_chats.need_id(message_id, "message_id")
    if emoji is not None:
        validate_emoji(emoji)
    db_chats.caller(conn, user_id)
    row = find_visible(conn, user_id, message_id)
    _require_open(row, "react to")
    if _disabled_peer(conn, row["chat_kind"], row["direct_key"], user_id):
        raise db_chats.ChatError("invalid_state", "this account is disabled", reason="peer_disabled")
    current = conn.execute(
        "SELECT emoji FROM reactions WHERE message_id = ? AND user_id = ?", (message_id, user_id)
    ).fetchone()
    if (current[0] if current is not None else None) == emoji:
        return db_chats.outcome({"message": _own_message_view(conn, user_id, message_id)}, [], [], noop=True)
    if emoji is None:
        conn.execute("DELETE FROM reactions WHERE message_id = ? AND user_id = ?", (message_id, user_id))
    else:
        conn.execute(
            "INSERT INTO reactions(message_id, user_id, emoji, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(message_id, user_id) DO UPDATE SET emoji = excluded.emoji, created_at = excluded.created_at",
            (message_id, user_id, emoji, util.now()),
        )
    parts, record = message_record(conn, message_id)
    event = message_event("ev.message_update", parts, record)
    return db_chats.outcome({"message": _view_from_record(parts, record, user_id)}, [event], [], parts=parts, **record)


def msg_star(conn: Any, user_id: int, message_id: int, starred: bool) -> Dict[str, Any]:
    """``msg.star``: per-user, no chat lock.  A deleted or system message => ``invalid_state``.  The unchanged state is
    a no-op.  The single ``ev.message_update`` goes to the actor's connections only."""
    db_chats.need_id(message_id, "message_id")
    db_chats.need_bool(starred, "starred")
    db_chats.caller(conn, user_id)
    row = find_visible(conn, user_id, message_id)
    _require_open(row, "star")
    current = (
        conn.execute("SELECT 1 FROM stars WHERE user_id = ? AND message_id = ?", (user_id, message_id)).fetchone()
        is not None
    )
    if current == starred:
        return db_chats.outcome({"message": _own_message_view(conn, user_id, message_id)}, [], [], noop=True)
    if starred:
        conn.execute(
            "INSERT INTO stars(user_id, message_id, created_at) VALUES (?, ?, ?)", (user_id, message_id, util.now())
        )
    else:
        conn.execute("DELETE FROM stars WHERE user_id = ? AND message_id = ?", (user_id, message_id))
    own = _own_message_view(conn, user_id, message_id)
    update = db_chats.event("ev.message_update", [db_chats.group([user_id], {"message": own})])
    return db_chats.outcome(
        {"message": own},
        [update],
        [],
        recipients=db_chats.recipients_of(conn, row["chat_id"]),
        starred={user_id} if starred else set(),
    )


def msg_pin(conn: Any, user_id: int, chat_id: int, message_id: int, pinned: bool) -> Dict[str, Any]:
    """``msg.pin`` (SPEC 7.4): any member of a direct chat, any member of a group unless ``only_admins_post`` (then
    admins).  The message must belong to ``chat_id`` and be visible (else ``not_found``); at most 5 pins per chat; a
    deleted or system message => ``invalid_state``; pinning a pinned / unpinning an unpinned message is a no-op.
    Events: ``ev.message_update``, ``ev.chat_update`` (all members, per viewer), then (pin only) the system
    ``pinned`` ``ev.message``.  ``res = {"chat": ...}``; ``chats`` holds every member's Chat."""
    db_chats.need_id(chat_id, "chat_id")
    db_chats.need_id(message_id, "message_id")
    db_chats.need_bool(pinned, "pinned")
    db_chats.caller(conn, user_id)
    chat = db_chats.require_member(conn, user_id, chat_id)
    row = find_visible(conn, user_id, message_id)
    if row["chat_id"] != chat_id:
        raise db_chats.ChatError("not_found", "message not found", message_id=message_id)
    if chat["kind"] == "group" and chat["only_admins_post"] and chat["my_role"] != "admin":
        raise db_chats.ChatError("forbidden", "only admins can pin in this chat", chat_id=chat_id)
    _require_open(row, "pin")
    if _disabled_peer(conn, chat["kind"], chat["direct_key"], user_id):
        raise db_chats.ChatError("invalid_state", "this account is disabled", reason="peer_disabled", chat_id=chat_id)
    is_pinned = (
        conn.execute("SELECT 1 FROM pins WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)).fetchone()
        is not None
    )
    if is_pinned == pinned:
        mine = {(user_id, chat_id): db_chats.build_chat(conn, chat_id, user_id)}
        return db_chats.outcome({"chat": mine[(user_id, chat_id)]}, [], [], noop=True, chats=mine)
    now = util.now()
    if pinned:
        count = conn.execute("SELECT COUNT(*) FROM pins WHERE chat_id = ?", (chat_id,)).fetchone()[0]
        if count >= _MAX_PINNED_MESSAGES:
            raise db_chats.ChatError(
                "invalid_state",
                "at most %d pinned messages" % _MAX_PINNED_MESSAGES,
                reason="pin_limit",
                chat_id=chat_id,
            )
        conn.execute(
            "INSERT INTO pins(chat_id, message_id, pinned_by, pinned_at) VALUES (?, ?, ?, ?)",
            (chat_id, message_id, user_id, now),
        )
    else:
        conn.execute("DELETE FROM pins WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
    system_id = None
    if pinned:
        body = "%s pinned a message" % db_chats.name_of(conn, user_id)
        system_id = insert_system_message(conn, chat_id, "pinned", user_id, [], body, message_id=message_id, ts=now)
    parts, record = message_record(conn, message_id)
    events = [message_event("ev.message_update", parts, record)]
    chats: Dict[Tuple[int, int], Dict[str, Any]] = {}
    db_chats.add_event(events, db_chats.chat_update_event(conn, chat_id, None, chats))
    if system_id is not None:
        db_chats.add_event(events, system_message_event(conn, chat_id, system_id))
    record["chats"] = chats
    return db_chats.outcome({"chat": chats[(user_id, chat_id)]}, events, [], parts=parts, **record)


# --------------------------------------------------------------------------------------------------------------------
# Reads: chat.history, msg.info, msg.search, msg.starred, msg.shared
# --------------------------------------------------------------------------------------------------------------------

_PAGE_FROM = (
    "FROM messages m JOIN chat_members cm ON cm.chat_id = m.chat_id AND cm.user_id = ? WHERE m.chat_id = ? AND "
)


def _visible_ids(
    conn: Any, viewer_id: int, chat_id: int, cond: str, params: Sequence[Any], newest_first: bool, limit: int
) -> List[int]:
    sql = "SELECT m.id " + _PAGE_FROM + visible_sql() + (" AND " + cond if cond else "")
    sql += " ORDER BY m.id %s LIMIT ?" % ("DESC" if newest_first else "ASC")
    return [r[0] for r in conn.execute(sql, [viewer_id, chat_id] + list(params) + [limit]).fetchall()]


def _any_visible(conn: Any, viewer_id: int, chat_id: int, cond: str, params: Sequence[Any]) -> bool:
    return bool(_visible_ids(conn, viewer_id, chat_id, cond, params, True, 1))


def _limit(limit: Any, default: int, maximum: int) -> int:
    return default if limit is None else max(1, min(maximum, db_chats.need_int(limit, "limit")))


def chat_history(
    conn: Any,
    user_id: int,
    chat_id: int,
    before_id: Optional[int] = None,
    after_id: Optional[int] = None,
    around_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """``chat.history`` (SPEC 7.4): messages ascending by id from ``V(viewer)``.

    At most one cursor (else ``bad_request``).  ``limit`` is clamped to [1, 100] (default 50).  ``around_id`` must be
    visible (else ``not_found``).  ``has_more_*`` use ``LIMIT n+1`` on ``V`` so hidden messages never create phantom
    pages.  Returns ``{messages, has_more_before, has_more_after}``.
    """
    db_chats.need_id(chat_id, "chat_id")
    if len([c for c in (before_id, after_id, around_id) if c is not None]) > 1:
        raise db_chats.ChatError("bad_request", "at most one of before_id, after_id, around_id")
    if before_id is not None:
        db_chats.need_id(before_id, "before_id")
    if after_id is not None:
        db_chats.need_uint(after_id, "after_id")
    if around_id is not None:
        db_chats.need_id(around_id, "around_id")
    count = _limit(limit, 50, 100)
    db_chats.caller(conn, user_id)
    chat = db_chats.require_member(conn, user_id, chat_id)
    floors = {chat_id: max(chat["history_from_id"], chat["cleared_before_id"])}
    more_before = more_after = False
    if around_id is not None:
        if not _any_visible(conn, user_id, chat_id, "m.id = ?", [around_id]):
            raise db_chats.ChatError("not_found", "message not found", message_id=around_id)
        n_before = count // 2
        n_after = count - 1 - n_before
        older = _visible_ids(conn, user_id, chat_id, "m.id < ?", [around_id], True, n_before + 1)
        newer = _visible_ids(conn, user_id, chat_id, "m.id > ?", [around_id], False, n_after + 1)
        more_before, more_after = len(older) > n_before, len(newer) > n_after
        ids = older[:n_before][::-1] + [around_id] + newer[:n_after]
    elif after_id is not None:
        found = _visible_ids(conn, user_id, chat_id, "m.id > ?", [after_id], False, count + 1)
        more_after = len(found) > count
        ids = found[:count]
        more_before = _any_visible(conn, user_id, chat_id, "m.id <= ?", [after_id])
    elif before_id is not None:
        found = _visible_ids(conn, user_id, chat_id, "m.id < ?", [before_id], True, count + 1)
        more_before = len(found) > count
        ids = found[:count][::-1]
        more_after = _any_visible(conn, user_id, chat_id, "m.id >= ?", [before_id])
    else:
        found = _visible_ids(conn, user_id, chat_id, "", [], True, count + 1)
        more_before = len(found) > count
        ids = found[:count][::-1]
    return {
        "messages": serialize_for_viewer(conn, user_id, ids, floors),
        "has_more_before": more_before,
        "has_more_after": more_after,
    }


def msg_info(conn: Any, user_id: int, message_id: int) -> Dict[str, Any]:
    """``msg.info``: own, non-deleted, non-system message.  Order: ``not_found``, system => ``invalid_state``, someone
    else's => ``forbidden``, deleted => ``invalid_state``.  ``recipients`` = current members except the sender and
    except members that joined after the message (``history_from_id >= id``), disabled ones included; ``delivered`` /
    ``read`` from the watermarks, timestamps from ``receipts`` rows (``null`` when no row exists)."""
    db_chats.need_id(message_id, "message_id")
    db_chats.caller(conn, user_id)
    row = find_visible(conn, user_id, message_id)
    if row["kind"] == "system":
        raise db_chats.ChatError("invalid_state", "system messages have no receipts", message_id=message_id)
    if row["sender_id"] != user_id:
        raise db_chats.ChatError("forbidden", "not your message", message_id=message_id)
    if row["deleted_at"] is not None:
        raise db_chats.ChatError("invalid_state", "message was deleted", message_id=message_id)
    members = db_chats.q_all(
        conn,
        "SELECT user_id, delivered_id, read_receipt_id FROM chat_members WHERE chat_id = ? AND listed = 1"
        " AND user_id != ? AND history_from_id < ? ORDER BY user_id",
        (row["chat_id"], user_id, message_id),
    )
    stamps = {
        r["user_id"]: r
        for r in db_chats.q_all(
            conn, "SELECT user_id, delivered_at, read_at FROM receipts WHERE message_id = ?", (message_id,)
        )
    }
    recipients = []
    for m in members:
        stamp = stamps.get(m["user_id"])
        recipients.append(
            {
                "user_id": m["user_id"],
                "delivered": m["delivered_id"] >= message_id,
                "read": m["read_receipt_id"] >= message_id,
                "delivered_at": stamp["delivered_at"] if stamp else None,
                "read_at": stamp["read_at"] if stamp else None,
            }
        )
    return {"message": _own_message_view(conn, user_id, message_id), "recipients": recipients}


def _busy(exc: sqlite3.OperationalError) -> db_chats.ChatError:
    return db_chats.ChatError("server_busy", "the query took too long, try again", retry_after=2.0)


def _page(conn: Any, viewer_id: int, rows: List[Any], limit: int) -> Tuple[List[Dict[str, Any]], bool]:
    """Serialise ``rows`` (``(id, chat_id)`` newest first, ``limit + 1`` of them) as viewer ``viewer_id``."""
    chosen = rows[:limit]
    return serialize_for_viewer(conn, viewer_id, [r[0] for r in chosen]), len(rows) > limit


def msg_search(
    conn: Any,
    user_id: int,
    q: str,
    chat_id: Optional[int] = None,
    before_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """``msg.search``: ``fold(body) LIKE ? ESCAPE '\\'`` over ``V(viewer)``, non-deleted, non-system, newest first.

    ``q`` is stripped and must have 2..64 code points; ``limit`` is clamped to [1, 50] (default 30); ``total``
    (capped at 1000) is present only on the first page of a chat-scoped search.  A query interrupted by the hub's
    time box answers ``server_busy``.  Returns ``{results: [{message, chat_id}], has_more, total?}``."""
    db_chats.reject_invalid_text(q)
    if not isinstance(q, str):
        raise db_chats.ChatError("bad_request", "q must be a string")
    q = q.strip()
    if not 2 <= len(q) <= 64:
        raise db_chats.ChatError("bad_request", "q must be 2..64 characters")
    if chat_id is not None:
        db_chats.need_id(chat_id, "chat_id")
    if before_id is not None:
        db_chats.need_id(before_id, "before_id")
    count = _limit(limit, 30, 50)
    db_chats.caller(conn, user_id)
    if chat_id is not None:
        db_chats.require_member(conn, user_id, chat_id)
    pattern = "%" + q.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where = [
        "cm.user_id = ?",
        visible_sql(),
        "m.deleted_at IS NULL",
        "m.kind != 'system'",
        "fold(m.body) LIKE ? ESCAPE '\\'",
    ]
    params: List[Any] = [user_id, pattern]
    if chat_id is not None:
        where.append("m.chat_id = ?")
        params.append(chat_id)
    base = "FROM messages m JOIN chat_members cm ON cm.chat_id = m.chat_id WHERE " + " AND ".join(where)
    try:
        page_sql = "SELECT m.id, m.chat_id " + base + (" AND m.id < ?" if before_id is not None else "")
        rows = conn.execute(
            page_sql + " ORDER BY m.id DESC LIMIT ?",
            params + ([before_id] if before_id is not None else []) + [count + 1],
        ).fetchall()
        total = None
        if chat_id is not None and before_id is None:
            total = conn.execute("SELECT COUNT(*) FROM (SELECT 1 " + base + " LIMIT 1000)", params).fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc).lower():
            raise _busy(exc)
        raise
    messages, has_more = _page(conn, user_id, rows, count)
    out: Dict[str, Any] = {
        "results": [{"message": m, "chat_id": m["chat_id"]} for m in messages],
        "has_more": has_more,
    }
    if total is not None:
        out["total"] = total
    return out


def msg_starred(
    conn: Any, user_id: int, chat_id: Optional[int] = None, before_id: Optional[int] = None, limit: Optional[int] = None
) -> Dict[str, Any]:
    """``msg.starred``: the caller's starred, visible, non-deleted messages newest first by message id.
    Returns ``{messages, has_more}``; a ``chat_id`` the caller is not in => ``not_member``."""
    if chat_id is not None:
        db_chats.need_id(chat_id, "chat_id")
    if before_id is not None:
        db_chats.need_id(before_id, "before_id")
    count = _limit(limit, 30, 50)
    db_chats.caller(conn, user_id)
    if chat_id is not None:
        db_chats.require_member(conn, user_id, chat_id)
    where = ["s.user_id = ?", visible_sql(), "m.deleted_at IS NULL"]
    params: List[Any] = [user_id]
    if chat_id is not None:
        where.append("m.chat_id = ?")
        params.append(chat_id)
    if before_id is not None:
        where.append("m.id < ?")
        params.append(before_id)
    rows = conn.execute(
        "SELECT m.id, m.chat_id FROM stars s JOIN messages m ON m.id = s.message_id"
        " JOIN chat_members cm ON cm.chat_id = m.chat_id AND cm.user_id = s.user_id WHERE "
        + " AND ".join(where)
        + " ORDER BY s.message_id DESC LIMIT ?",
        params + [count + 1],
    ).fetchall()
    messages, has_more = _page(conn, user_id, rows, count)
    return {"messages": messages, "has_more": has_more}


def msg_shared(
    conn: Any, user_id: int, chat_id: int, kind: str, before_id: Optional[int] = None, limit: Optional[int] = None
) -> Dict[str, Any]:
    """``msg.shared``: ``media`` (image, video), ``files`` (file, audio) or ``links`` (text containing http(s)://) of
    a chat, newest first, visible, non-deleted, non-system.  Returns ``{messages, has_more}``."""
    db_chats.reject_invalid_text(kind)
    db_chats.need_id(chat_id, "chat_id")
    if kind not in _SHARED_KINDS:
        raise db_chats.ChatError("bad_request", "kind must be media, files or links")
    if before_id is not None:
        db_chats.need_id(before_id, "before_id")
    count = _limit(limit, 30, 50)
    db_chats.caller(conn, user_id)
    db_chats.require_member(conn, user_id, chat_id)
    params: List[Any] = [user_id, chat_id]
    extra = ""
    if before_id is not None:
        extra = " AND m.id < ?"
        params.append(before_id)
    try:
        rows = conn.execute(
            "SELECT m.id, m.chat_id "
            + _PAGE_FROM
            + visible_sql()
            + " AND m.deleted_at IS NULL AND ("
            + _SHARED_KINDS[kind]
            + ")"
            + extra
            + " ORDER BY m.id DESC LIMIT ?",
            params + [count + 1],
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc).lower():
            raise _busy(exc)
        raise
    messages, has_more = _page(conn, user_id, rows, count)
    return {"messages": messages, "has_more": has_more}
