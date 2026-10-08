"""Watermarks, ``receipts`` rows, unread counters, status aggregation and receipt routing (SPEC 3.3, 8.1, 8.2).

Every public ``fn(conn, ...)`` is synchronous and runs inside ``Database.run`` (writer) or ``Database.run_read``
(reader).  Write functions return an *outcome* dict (``res``, ordered ``events``, see ``docs/DB_API_chat.md``).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from . import db_chats, db_messages, util

__all__ = [
    "RECEIPT_ROWS_MAX_MEMBERS",
    "StatusCtx",
    "advance_marks",
    "counter_columns",
    "counters",
    "counters_for_viewer",
    "counters_many",
    "read_sync_event",
    "read_sync_for",
    "receipt_audience",
    "receipt_delivered",
    "receipt_event",
    "receipt_read",
    "receipt_record",
    "status_ctx",
    "status_ctx_from_members",
    "status_for",
    "status_of",
]

#: ``receipts`` rows are written only for chats with at most this many current members (SPEC 3.3).
RECEIPT_ROWS_MAX_MEMBERS = 50
_MAX_ITEMS = 50


# --------------------------------------------------------------------------------------------------------------------
# Unread counters (SPEC 8.2)
# --------------------------------------------------------------------------------------------------------------------


def counter_columns() -> str:
    """SQL select-list fragment ``unread``, ``unread_mentions``, ``first_unread_mention_id`` for rows aliased ``cm``.

    The caller's FROM clause must expose ``chat_members`` as ``cm``.  Counts are capped (``LIMIT`` inside a derived
    table) at ``LIMITS.unread_cap`` (1000) and use the one visibility predicate of ``db_messages.visible_sql``.  Each
    subquery is an index range scan on ``messages_chat`` / ``message_mentions_user``, never a table scan.
    """
    cap = int(db_chats.LIMITS.unread_cap)
    vis = db_messages.visible_sql("cm.last_read_id")
    floor = db_messages.floor_sql("cm.last_read_id")
    return (
        "(SELECT COUNT(*) FROM (SELECT 1 FROM messages m WHERE m.chat_id = cm.chat_id AND {vis}"
        " AND m.sender_id != cm.user_id AND m.kind != 'system' AND m.deleted_at IS NULL LIMIT {cap})) AS unread,"
        " (SELECT COUNT(*) FROM (SELECT 1 FROM message_mentions mm JOIN messages m ON m.id = mm.message_id"
        " WHERE mm.user_id = cm.user_id AND mm.message_id > {floor} AND m.chat_id = cm.chat_id AND {vis}"
        " AND m.sender_id != cm.user_id AND m.deleted_at IS NULL LIMIT {cap})) AS unread_mentions,"
        " (SELECT MIN(mm.message_id) FROM message_mentions mm JOIN messages m ON m.id = mm.message_id"
        " WHERE mm.user_id = cm.user_id AND mm.message_id > {floor} AND m.chat_id = cm.chat_id AND {vis}"
        " AND m.sender_id != cm.user_id AND m.deleted_at IS NULL) AS first_unread_mention_id"
    ).format(vis=vis, floor=floor, cap=cap)


def _counters_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "chat_id": row["chat_id"],
        "last_read_id": row["last_read_id"],
        "last_message_id": row["last_message_id"],
        "unread": row["unread"],
        "unread_mentions": row["unread_mentions"],
        "first_unread_mention_id": row["first_unread_mention_id"],
    }


def _counter_rows(conn: Any, where: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
    sql = (
        "SELECT cm.chat_id AS chat_id, cm.user_id AS user_id, cm.last_read_id AS last_read_id,"
        " c.last_message_id AS last_message_id, {cols} FROM chat_members cm JOIN chats c ON c.id = cm.chat_id"
        " WHERE cm.listed = 1 AND {where}"
    ).format(cols=counter_columns(), where=where)
    return db_chats.q_all(conn, sql, params)


def counters_many(conn: Any, chat_id: int, user_ids: Optional[Iterable[int]] = None) -> Dict[int, Dict[str, Any]]:
    """``Counters`` (SPEC 7.2) of several members of ONE chat, as of ``chats.last_message_id`` read in the same call.

    ``user_ids=None`` means every listed member.  Users that are not listed members are absent from the result.
    """
    if user_ids is None:
        rows = _counter_rows(conn, "cm.chat_id = ?", (chat_id,))
    else:
        rows = []
        for chunk in db_chats.chunked(sorted(set(user_ids))):
            where = "cm.chat_id = ? AND cm.user_id IN (%s)" % db_chats.placeholders(len(chunk))
            rows.extend(_counter_rows(conn, where, [chat_id] + chunk))
    return {r["user_id"]: _counters_dict(r) for r in rows}


def counters_for_viewer(conn: Any, user_id: int, chat_ids: Optional[Iterable[int]] = None) -> Dict[int, Dict[str, Any]]:
    """``Counters`` of ONE user for all (or the given) listed chats, set-based (one statement per 400 chat ids)."""
    if chat_ids is None:
        rows = _counter_rows(conn, "cm.user_id = ?", (user_id,))
    else:
        rows = []
        for chunk in db_chats.chunked(sorted(set(chat_ids))):
            where = "cm.user_id = ? AND cm.chat_id IN (%s)" % db_chats.placeholders(len(chunk))
            rows.extend(_counter_rows(conn, where, [user_id] + chunk))
    return {r["chat_id"]: _counters_dict(r) for r in rows}


def counters(conn: Any, user_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
    """``Counters`` of one user in one chat (``None`` when the user is not a listed member)."""
    return counters_many(conn, chat_id, [user_id]).get(user_id)


def read_sync_for(conn: Any, chat_id: int, user_ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
    """``{user_id: Counters}`` of exactly these LISTED members of one chat, as of ``last_message_id`` (the ``read_sync``
    entry of the SPEC 7.6(6) record)."""
    return counters_many(conn, chat_id, list(user_ids))


def read_sync_event(sync: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """An ``ev.read_sync`` event with one group (the user's own ``Counters``) per user of ``sync``."""
    groups = [db_chats.group([uid], sync[uid]) for uid in sorted(sync)]
    return db_chats.event("ev.read_sync", groups) if groups else None


# --------------------------------------------------------------------------------------------------------------------
# Status aggregation (SPEC 8.1)
# --------------------------------------------------------------------------------------------------------------------


class StatusCtx(NamedTuple):
    """What the sender's tick state needs: ``dmin`` = lowest delivered watermark over ``R`` (None when ``R`` is
    empty), ``rmin`` = lowest public read watermark over ``R_read`` (None when ``R_read`` is empty)."""

    self_chat: bool
    dmin: Optional[int]
    rmin: Optional[int]


def status_ctx_from_members(sender_id: int, members: Iterable[Dict[str, Any]], self_chat: bool) -> StatusCtx:
    """Aggregate ``R``/``R_read`` of SPEC 8.1 from member rows.

    Each row needs ``user_id``, ``delivered_id``, ``read_receipt_id``, ``disabled``, ``activated``, ``read_receipts``
    and optionally ``listed``.  The sender, disabled users, never-activated users and unlisted rows are excluded.
    """
    dmin: Optional[int] = None
    rmin: Optional[int] = None
    for m in members:
        if m["user_id"] == sender_id or m["disabled"] or not m["activated"] or not m.get("listed", 1):
            continue
        delivered = m["delivered_id"]
        dmin = delivered if dmin is None else min(dmin, delivered)
        if m["read_receipts"]:
            read = m["read_receipt_id"]
            rmin = read if rmin is None else min(rmin, read)
    return StatusCtx(self_chat, dmin, rmin)


def status_ctx(conn: Any, chat_id: int, sender_id: int) -> StatusCtx:
    """Load the members of ``chat_id`` and aggregate the status context for messages sent by ``sender_id``."""
    chat = db_chats.q_one(conn, "SELECT kind, direct_key FROM chats WHERE id = ?", (chat_id,))
    rows = db_chats.q_all(
        conn,
        "SELECT cm.user_id AS user_id, cm.delivered_id AS delivered_id, cm.read_receipt_id AS read_receipt_id,"
        " cm.listed AS listed, u.disabled AS disabled, u.last_login_at IS NOT NULL AS activated,"
        " u.read_receipts AS read_receipts FROM chat_members cm JOIN users u ON u.id = cm.user_id"
        " WHERE cm.chat_id = ?",
        (chat_id,),
    )
    self_chat = chat is not None and chat["kind"] == "direct" and db_chats.is_self_direct(chat["direct_key"])
    return status_ctx_from_members(sender_id, rows, self_chat)


def status_for(
    recipients: Iterable[Dict[str, Any]], sender_id: int, message_id: int, self_chat: bool = False
) -> Optional[str]:
    """The SPEC 8.1 status of a message for its sender from a ``recipients`` list of the SPEC 7.6(6) record (pure,
    no database access): ``'sent' | 'delivered' | 'read'``, ``None`` in a self-chat."""
    return status_of(status_ctx_from_members(sender_id, recipients, self_chat), message_id)


def status_of(ctx: StatusCtx, message_id: int) -> Optional[str]:
    """``'sent' | 'delivered' | 'read'`` for the sender's own non-system message, ``None`` in a self-chat."""
    if ctx.self_chat:
        return None
    if ctx.dmin is None or message_id > ctx.dmin:
        return "sent"
    if ctx.rmin is not None and message_id <= ctx.rmin:
        return "read"
    return "delivered"


# --------------------------------------------------------------------------------------------------------------------
# Watermarks and receipts rows (SPEC 3.3)
# --------------------------------------------------------------------------------------------------------------------


def _write_receipt_rows(
    conn: Any,
    chat_id: int,
    user_id: int,
    history_from_id: int,
    delivered: Tuple[int, int],
    read: Tuple[int, int],
    now: float,
) -> None:
    """Upsert ``receipts`` rows for the advanced ranges (only chats with <= 50 current members)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM chat_members WHERE chat_id = ? AND listed = 1 LIMIT ?)",
        (chat_id, RECEIPT_ROWS_MAX_MEMBERS + 1),
    ).fetchone()
    if row[0] > RECEIPT_ROWS_MAX_MEMBERS:
        return
    if delivered[1] > delivered[0]:
        conn.execute(
            "INSERT INTO receipts(message_id, user_id, delivered_at, read_at)"
            " SELECT m.id, ?, ?, NULL FROM messages m WHERE m.chat_id = ? AND m.id > ? AND m.id <= ?"
            " AND m.kind != 'system' AND m.sender_id != ?"
            " ON CONFLICT(message_id, user_id) DO UPDATE SET"
            " delivered_at = COALESCE(delivered_at, excluded.delivered_at)",
            (user_id, now, chat_id, max(delivered[0], history_from_id), delivered[1], user_id),
        )
    if read[1] > read[0]:
        conn.execute(
            "INSERT INTO receipts(message_id, user_id, delivered_at, read_at)"
            " SELECT m.id, ?, ?, ? FROM messages m WHERE m.chat_id = ? AND m.id > ? AND m.id <= ?"
            " AND m.kind != 'system' AND m.sender_id != ?"
            " ON CONFLICT(message_id, user_id) DO UPDATE SET"
            " delivered_at = COALESCE(delivered_at, excluded.delivered_at),"
            " read_at = COALESCE(read_at, excluded.read_at)",
            (user_id, now, now, chat_id, max(read[0], history_from_id), read[1], user_id),
        )


def advance_marks(
    conn: Any,
    chat_id: int,
    user_id: int,
    delivered_to: int = 0,
    last_read_to: int = 0,
    public_read_to: int = 0,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Move the three watermarks of one member forward (``max()``), keeping the invariants of SPEC 3.3.

    ``public_read_to`` only counts while ``users.read_receipts = 1``.  ``delivered_id >= read_receipt_id`` and
    ``last_read_id >= read_receipt_id`` always hold afterwards.  ``receipts`` rows are upserted for the advanced
    ranges (chats with <= 50 members).  Returns ``{delivered, read_receipt, last_read}`` as ``(old, new)`` pairs plus
    ``public_changed`` / ``private_changed`` / ``history_from_id``.  Writes nothing when no value moves.
    """
    row = db_chats.q_one(
        conn,
        "SELECT cm.delivered_id AS d, cm.last_read_id AS l, cm.read_receipt_id AS r, cm.history_from_id AS h,"
        " u.read_receipts AS rr FROM chat_members cm JOIN users u ON u.id = cm.user_id"
        " WHERE cm.chat_id = ? AND cm.user_id = ?",
        (chat_id, user_id),
    )
    if row is None:
        raise db_chats.ChatError("not_member", "not a member of this chat", chat_id=chat_id)
    new_r = max(row["r"], public_read_to) if row["rr"] else row["r"]
    new_d = max(row["d"], delivered_to, new_r)
    new_l = max(row["l"], last_read_to, new_r)
    change = {
        "delivered": (row["d"], new_d),
        "read_receipt": (row["r"], new_r),
        "last_read": (row["l"], new_l),
        "public_changed": new_d > row["d"] or new_r > row["r"],
        "private_changed": new_l > row["l"],
        "history_from_id": row["h"],
    }
    if not (change["public_changed"] or change["private_changed"]):
        return change
    conn.execute(
        "UPDATE chat_members SET delivered_id = ?, last_read_id = ?, read_receipt_id = ?"
        " WHERE chat_id = ? AND user_id = ?",
        (new_d, new_l, new_r, chat_id, user_id),
    )
    if change["public_changed"]:
        _write_receipt_rows(
            conn,
            chat_id,
            user_id,
            row["h"],
            (row["d"], new_d),
            (row["r"], new_r),
            now if now is not None else util.now(),
        )
    return change


def receipt_audience(conn: Any, chat_id: int, actor_id: int, change: Dict[str, Any]) -> List[int]:
    """Who receives the ``ev.receipt`` of ``change`` (SPEC 8.2): the actor plus the CURRENT members who authored a
    non-system message inside an advanced range.  Never "all members".  Actor first, then ascending user id."""
    authors: Set[int] = set()
    for lo, hi in (change["delivered"], change["read_receipt"]):
        if hi > lo:
            rows = conn.execute(
                "SELECT DISTINCT m.sender_id FROM messages m JOIN chat_members cm ON cm.chat_id = m.chat_id"
                " AND cm.user_id = m.sender_id AND cm.listed = 1"
                " WHERE m.chat_id = ? AND m.id > ? AND m.id <= ? AND m.kind != 'system'",
                (chat_id, lo, hi),
            ).fetchall()
            authors.update(r[0] for r in rows)
    authors.discard(actor_id)
    return [actor_id] + sorted(authors)


def receipt_record(conn: Any, chat_id: int, user_id: int, change: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ``receipt`` entry of the SPEC 7.6(6) record: ``{chat_id, user_id, delivered_up_to, read_up_to, audience,
    authors}`` (both absolute PUBLIC watermarks) when a public watermark of ``user_id`` really increased, else ``None``
    (also ``None`` in a self-chat, where receipts are no-ops).  ``audience`` is the complete SPEC 8.2 routing list
    (the actor first, then the current authors, no duplicates: nothing has to be added); ``authors`` is the same list
    without the actor."""
    if not change["public_changed"]:
        return None
    chat = db_chats.q_one(conn, "SELECT kind, direct_key FROM chats WHERE id = ?", (chat_id,))
    if chat is not None and chat["kind"] == "direct" and db_chats.is_self_direct(chat["direct_key"]):
        return None
    audience = receipt_audience(conn, chat_id, user_id, change)
    return {
        "chat_id": chat_id,
        "user_id": user_id,
        "delivered_up_to": change["delivered"][1],
        "read_up_to": change["read_receipt"][1],
        "audience": audience,
        "authors": audience[1:],
    }


def receipt_event(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The keyed ``ev.receipt`` of a :func:`receipt_record` (payload = both absolute watermarks; ``key`` is the
    coalescing key ``("receipt", chat_id, user_id)``)."""
    if record is None:
        return None
    payload = {
        "chat_id": record["chat_id"],
        "user_id": record["user_id"],
        "delivered_up_to": record["delivered_up_to"],
        "read_up_to": record["read_up_to"],
    }
    key = ("receipt", record["chat_id"], record["user_id"])
    return db_chats.event("ev.receipt", [db_chats.group(record["audience"], payload)], durable=False, key=key)


# --------------------------------------------------------------------------------------------------------------------
# Requests: receipt.delivered / receipt.read (SPEC 7.4)
# --------------------------------------------------------------------------------------------------------------------


def _delivered_items(items: Any) -> List[Tuple[int, int]]:
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= _MAX_ITEMS:
        raise db_chats.ChatError("bad_request", "items must be a list of 1..%d entries" % _MAX_ITEMS)
    pairs: List[Tuple[int, int]] = []
    for item in items:
        if isinstance(item, dict):
            chat_id, up_to = item.get("chat_id"), item.get("up_to_id")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            chat_id, up_to = item
        else:
            raise db_chats.ChatError("bad_request", "invalid item")
        pairs.append((db_chats.need_id(chat_id, "chat_id"), db_chats.need_uint(up_to, "up_to_id")))
    return pairs


def receipt_delivered(
    conn: Any, user_id: int, items: Any = None, chat_id: Any = None, up_to_id: Any = None
) -> Dict[str, Any]:
    """``receipt.delivered`` in either wire shape (exactly one, else ``bad_request``): ``items`` =
    ``[{chat_id, up_to_id}, ...]`` (1..50) or the single ``chat_id`` + ``up_to_id`` pair.

    ``delivered_id = max(delivered_id, min(up_to_id, chats.last_message_id))``.  A non-member item fails the whole
    request with ``not_member`` carrying ``err.chat_id``.  One keyed ``ev.receipt`` per item whose public value really
    increased; ``receipts`` of the outcome lists them (``receipt`` is the single one for a one-item request).
    ``res`` is ``{}``.
    """
    single = chat_id is not None or up_to_id is not None
    if single == (items is not None):
        raise db_chats.ChatError("bad_request", "send either items or chat_id with up_to_id")
    if single:
        pairs = [(db_chats.need_id(chat_id, "chat_id"), db_chats.need_uint(up_to_id, "up_to_id"))]
    else:
        pairs = _delivered_items(items)
    db_chats.caller(conn, user_id)
    events: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []
    for target, up_to in pairs:
        chat = db_chats.require_member(conn, user_id, target)
        change = advance_marks(conn, target, user_id, delivered_to=min(up_to, chat["last_message_id"] or 0))
        record = receipt_record(conn, target, user_id, change)
        if record is not None:
            records.append(record)
            db_chats.add_event(events, receipt_event(record))
    one = len(pairs) == 1
    return db_chats.outcome(
        {},
        events,
        noop=not events,
        recipients=db_chats.recipients_of(conn, pairs[0][0]) if one else [],
        receipts=records,
        receipt=records[0] if one and records else None,
    )


def receipt_read(conn: Any, user_id: int, chat_id: int, up_to_id: int) -> Dict[str, Any]:
    """``receipt.read``: advance ``last_read_id`` (always), ``delivered_id`` and ``read_receipt_id`` (only while
    ``users.read_receipts = 1``) to ``min(up_to_id, last_message_id)``.  ``res`` is the caller's ``Counters`` as of
    ``last_message_id``; ``ev.receipt`` only when a public value increased, ``ev.read_sync`` (to the actor) only when
    ``last_read_id`` advanced."""
    db_chats.need_id(chat_id, "chat_id")
    db_chats.need_uint(up_to_id, "up_to_id")
    db_chats.caller(conn, user_id)
    chat = db_chats.require_member(conn, user_id, chat_id)
    target = min(up_to_id, chat["last_message_id"] or 0)
    change = advance_marks(conn, chat_id, user_id, delivered_to=target, last_read_to=target, public_read_to=target)
    counted = counters(conn, user_id, chat_id)
    events: List[Dict[str, Any]] = []
    record = receipt_record(conn, chat_id, user_id, change)
    db_chats.add_event(events, receipt_event(record))
    sync: Dict[int, Dict[str, Any]] = {}
    if change["private_changed"]:
        sync[user_id] = counted
        db_chats.add_event(events, read_sync_event(sync))
    return db_chats.outcome(
        counted,
        events,
        noop=not events,
        recipients=db_chats.recipients_of(conn, chat_id),
        read_sync=sync,
        receipt=record,
        receipts=[record] if record else [],
    )
