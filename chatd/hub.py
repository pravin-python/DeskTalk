"""The realtime hub: WebSocket protocol, presence, typing, rate limits and fan-out (SPEC 6.1, 7, 8).

``Hub(db, cfg)`` is constructed by ``app.py`` inside the running loop and driven by

* the WebSocket transport: ``await hub.serve(ws, session)`` for every authenticated socket (SPEC 6),
* the REST layer (``api.py``): ``create_user``, ``revoke``, ``broadcast_user`` and ``is_online``,
* the background tasks of SPEC 2.4: ``sweep_typing`` (1 s), ``external_change`` (control poll), ``revalidate_all``
  (5 min), ``online_user_ids`` (heartbeat), ``shutdown``; ``storage_bytes`` is assigned by ``app.py`` (hourly).

Design in one paragraph.  Every request is validated synchronously in the read loop (SPEC 7.1, 7.1.1 step 1), charged
against its token buckets (7.5) and then runs as an own task: mutating requests as *detached* tasks tracked in
``_inflight`` (SPEC 7.6(5)): a closing socket never cancels a commit, it merely loses the ``res``.  A write takes the
chat locks in ascending order, runs ONE ``Database.run``, installs the membership index and enqueues the ready-made
fan-out plan the db layer returned (``out["events"]``, see ``docs/DB_API_chat.md``) in one block without an ``await``,
so enqueue order equals commit order and every event precedes its ``res`` (7.6(1), (4)).  The hub never queries the
database between commit and fan-out.

Authorisation is never cached here: role, ``disabled`` and membership are re-read by the db function inside the same
transaction as the action.  The only membership copy is the typing index of 7.6(6), used for typing relays and drops.

Python 3.8 compatible, standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import platform
import re
import shutil
import time
from collections import deque
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Deque,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
)

from . import __version__, auth, db_chats, db_messages, db_receipts, db_users, util
from . import db as dbmod

log = logging.getLogger("chatd.hub")

__all__ = ["Hub"]

PROTOCOL = 1
MAX_ID = 2**53 - 1
MAX_MUTE = 4102444800
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")

# ---- protocol limits (SPEC 6, 7.1, 7.6, 8) --------------------------------------------------------------------------
MAX_IN_FLIGHT = 64
MALFORMED_LIMIT = 20
BACKLOG_CAP = 400
RECEIPT_BACKLOG_CAP = 2000
READY_CONCURRENCY = 4
TYPING_TTL_S = 7.0
TYPING_REFRESH_S = 1.0
PRESENCE_GRACE_S = 8.0
VIOLATION_LIMIT = 3
VIOLATION_WINDOW_S = 60.0
INFLIGHT_WINDOW_S = 10.0
SHUTDOWN_WAIT_S = 5.0
SEARCH_WAIT_S = 5.0
SEARCH_BOX_S = 2.0
SEEN_CACHE_S = 300.0
CLOSE_UNAUTHORIZED = 4001
CLOSE_RATE_LIMITED = 4008
EVERYONE_LOCK = 0  # lock key of the default chat (it may not exist yet when the first user registers)

#: ``[count, window_s]`` of every SPEC 7.5 bucket (the names are those of ``test_limits``, SPEC 2.1).
DEFAULT_LIMITS: Dict[str, Tuple[int, float]] = {
    "global_user": (100, 10.0),
    "global_conn": (30, 5.0),
    "msg.send": (20, 10.0),
    "msg.forward": (5, 10.0),
    "typing": (5, 1.0),
    "receipt": (10, 1.0),
    "msg.search": (5, 10.0),
    "read": (20, 10.0),
    "msg.react": (30, 10.0),
    "msg.pin": (10, 60.0),
    "chat.create_group": (5, 60.0),
    "chat.add_members": (20, 60.0),
    "chat.open_direct": (30, 60.0),
    "profile.update": (5, 60.0),
    "admin.create_user": (30, 60.0),
    "admin.reset_password": (30, 60.0),
    "admin_other": (60, 60.0),
}
#: Buckets counted per connection; every other bucket is per user across all connections.
CONN_BUCKETS = frozenset(("global_conn", "typing", "receipt"))

_ADMIN_NAME_MAX = 400
_TEXT_MAX = 4000
_PASSWORD_MAX = 4096


# --------------------------------------------------------------------------------------------------------------
# Errors and request arguments
# --------------------------------------------------------------------------------------------------------------


#: A protocol failure raised by the hub itself: the db layer's own error type (``db_chats.ChatError`` is a subclass),
#: so one ``except dbmod.RequestError`` covers every failure that maps to an ``err`` object (SPEC 7.1.1).
_Fail = dbmod.RequestError


def _bad(msg: str, reason: Optional[str] = None) -> _Fail:
    return _Fail("bad_request", msg, reason)


class _Args:
    """Typed accessors over the ``d`` object of a request: every failure is a ``bad_request`` naming the field.

    An explicit JSON ``null`` counts as "absent" for optional fields (``msg.react`` handles its ``emoji`` itself).
    """

    __slots__ = ("d",)

    def __init__(self, d: Dict[str, Any]) -> None:
        self.d = d

    def _get(self, name: str, required: bool) -> Any:
        value = self.d.get(name)
        if value is None and required:
            raise _bad("%s is required" % name)
        return value

    def ident(self, name: str, required: bool = True) -> Optional[int]:
        """An id: ``int`` (not ``bool``) in ``[1, 2^53-1]``."""
        value = self._get(name, required)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ID:
            raise _bad("%s must be an id" % name)
        return value

    def uint(self, name: str, required: bool = False) -> Optional[int]:
        """A watermark or cursor: ``int`` (not ``bool``) in ``[0, 2^53-1]``."""
        value = self._get(name, required)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_ID:
            raise _bad("%s must be a non-negative integer" % name)
        return value

    def limit(self, name: str = "limit") -> Optional[int]:
        """A page size: any ``int`` (the db function clamps it); anything else is ``bad_request``."""
        value = self.d.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise _bad("%s must be an integer" % name)
        return value

    def text(self, name: str, required: bool, max_len: int = _TEXT_MAX) -> Optional[str]:
        """A string of at most ``max_len`` code points (normalisation and the real limits are the db function's)."""
        value = self._get(name, required)
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > max_len:
            raise _bad("%s must be a string of at most %d characters" % (name, max_len))
        return value

    def flag(self, name: str, required: bool = False) -> Optional[bool]:
        value = self._get(name, required)
        if value is None:
            return None
        if not isinstance(value, bool):
            raise _bad("%s must be true or false" % name)
        return value

    def number(self, name: str, low: float, high: float, required: bool = False) -> Optional[float]:
        value = self._get(name, required)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise _bad("%s must be a number" % name)
        if not low <= value <= high:
            raise _bad("%s is out of range" % name)
        return value

    def enum(self, name: str, choices: Tuple[str, ...], required: bool = True) -> Optional[str]:
        value = self._get(name, required)
        if value is None:
            return None
        if not isinstance(value, str) or value not in choices:
            raise _bad("%s must be one of %s" % (name, ", ".join(choices)))
        return value

    def id_list(self, name: str, low: int, high: int) -> List[int]:
        """A required list of ``low..high`` ids (duplicates are kept: the db function de-duplicates)."""
        value = self._get(name, True)
        if not isinstance(value, list) or not low <= len(value) <= high:
            raise _bad("%s must be a list of %d..%d ids" % (name, low, high))
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= MAX_ID:
                raise _bad("%s must contain only ids" % name)
        return list(value)

    def client_id(self, name: str = "client_id") -> str:
        value = self._get(name, True)
        if not isinstance(value, str) or not CLIENT_ID_RE.match(value):
            raise _bad("%s must be 8..64 characters of [A-Za-z0-9_-]" % name)
        return value


# --------------------------------------------------------------------------------------------------------------
# Request validators (SPEC 7.1.1 step 1: shape, type, range; the db functions re-check everything)
# --------------------------------------------------------------------------------------------------------------

Validator = Callable[[_Args], Dict[str, Any]]


def _v_none(a: _Args) -> Dict[str, Any]:
    return {}


def _v_chat(a: _Args) -> Dict[str, Any]:
    return {"chat_id": a.ident("chat_id")}


def _v_open_direct(a: _Args) -> Dict[str, Any]:
    return {"user_id": a.ident("user_id")}


def _v_create_group(a: _Args) -> Dict[str, Any]:
    return {
        "title": a.text("title", True, _ADMIN_NAME_MAX),
        "member_ids": a.id_list("member_ids", 0, 200),
        "description": a.text("description", False),
    }


def _v_chat_update(a: _Args) -> Dict[str, Any]:
    k = {
        "chat_id": a.ident("chat_id"),
        "title": a.text("title", False, _ADMIN_NAME_MAX),
        "description": a.text("description", False),
        "only_admins_post": a.flag("only_admins_post"),
    }
    if k["title"] is None and k["description"] is None and k["only_admins_post"] is None:
        raise _bad("nothing to update")
    return k


def _v_add_members(a: _Args) -> Dict[str, Any]:
    return {"chat_id": a.ident("chat_id"), "user_ids": a.id_list("user_ids", 1, 50)}


def _v_chat_user(a: _Args) -> Dict[str, Any]:
    return {"chat_id": a.ident("chat_id"), "user_id": a.ident("user_id")}


def _v_set_admin(a: _Args) -> Dict[str, Any]:
    return {"chat_id": a.ident("chat_id"), "user_id": a.ident("user_id"), "admin": a.flag("admin", True)}


def _v_prefs(a: _Args) -> Dict[str, Any]:
    k = {
        "chat_id": a.ident("chat_id"),
        "muted_until": a.number("muted_until", 0, MAX_MUTE),
        "pinned": a.flag("pinned"),
        "archived": a.flag("archived"),
    }
    if k["muted_until"] is None and k["pinned"] is None and k["archived"] is None:
        raise _bad("nothing to update")
    if k["pinned"] is True and k["archived"] is True:
        raise _bad("a chat cannot be pinned and archived at once")
    return k


def _v_history(a: _Args) -> Dict[str, Any]:
    k = {
        "chat_id": a.ident("chat_id"),
        "before_id": a.ident("before_id", False),
        "after_id": a.uint("after_id"),
        "around_id": a.ident("around_id", False),
        "limit": a.limit(),
    }
    if sum(1 for name in ("before_id", "after_id", "around_id") if k[name] is not None) > 1:
        raise _bad("at most one of before_id, after_id and around_id")
    return k


def _v_send(a: _Args) -> Dict[str, Any]:
    return {
        "chat_id": a.ident("chat_id"),
        "client_id": a.client_id(),
        "body": a.text("body", False, 1 << 20),
        "attachment_id": a.text("attachment_id", False, 64),
        "reply_to_id": a.ident("reply_to_id", False),
        "seen_up_to_id": a.uint("seen_up_to_id"),
    }


def _v_edit(a: _Args) -> Dict[str, Any]:
    return {"message_id": a.ident("message_id"), "body": a.text("body", True, 1 << 20)}


def _v_delete(a: _Args) -> Dict[str, Any]:
    return {"message_id": a.ident("message_id"), "scope": a.enum("scope", ("me", "everyone"))}


def _v_react(a: _Args) -> Dict[str, Any]:
    if "emoji" not in a.d:
        raise _bad("emoji is required (null removes the reaction)")
    emoji = a.d["emoji"]
    if emoji is not None:
        db_messages.validate_emoji(emoji)
    return {"message_id": a.ident("message_id"), "emoji": emoji}


def _v_star(a: _Args) -> Dict[str, Any]:
    return {"message_id": a.ident("message_id"), "starred": a.flag("starred", True)}


def _v_pin(a: _Args) -> Dict[str, Any]:
    return {"chat_id": a.ident("chat_id"), "message_id": a.ident("message_id"), "pinned": a.flag("pinned", True)}


def _v_forward(a: _Args) -> Dict[str, Any]:
    message_ids = list(dict.fromkeys(a.id_list("message_ids", 0, 1000)))
    chat_ids = list(dict.fromkeys(a.id_list("chat_ids", 0, 1000)))
    client_id = a.client_id()
    if not 1 <= len(message_ids) <= 20:
        raise _bad("message_ids must hold 1..20 ids")
    if not 1 <= len(chat_ids) <= 5:
        raise _bad("chat_ids must hold 1..5 ids")
    return {"message_ids": message_ids, "chat_ids": chat_ids, "client_id": client_id}


def _v_message(a: _Args) -> Dict[str, Any]:
    return {"message_id": a.ident("message_id")}


def _v_search(a: _Args) -> Dict[str, Any]:
    return {
        "q": a.text("q", True, 1000),
        "chat_id": a.ident("chat_id", False),
        "before_id": a.ident("before_id", False),
        "limit": a.limit(),
    }


def _v_starred(a: _Args) -> Dict[str, Any]:
    return {"chat_id": a.ident("chat_id", False), "before_id": a.ident("before_id", False), "limit": a.limit()}


def _v_shared(a: _Args) -> Dict[str, Any]:
    return {
        "chat_id": a.ident("chat_id"),
        "kind": a.enum("kind", ("media", "files", "links")),
        "before_id": a.ident("before_id", False),
        "limit": a.limit(),
    }


def _v_delivered(a: _Args) -> Dict[str, Any]:
    items = a.d.get("items")
    single = a.d.get("chat_id") is not None or a.d.get("up_to_id") is not None
    if items is not None and single:
        raise _bad("send either chat_id and up_to_id or items, not both")
    if items is None:
        if not single:
            raise _bad("chat_id and up_to_id (or items) are required")
        return {"items": [{"chat_id": a.ident("chat_id"), "up_to_id": a.uint("up_to_id", True)}]}
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise _bad("items must be a list of 1..50 entries")
    checked = []
    for item in items:
        if not isinstance(item, dict):
            raise _bad("items must contain objects")
        entry = _Args(item)
        checked.append({"chat_id": entry.ident("chat_id"), "up_to_id": entry.uint("up_to_id", True)})
    return {"items": checked}


def _v_read(a: _Args) -> Dict[str, Any]:
    return {"chat_id": a.ident("chat_id"), "up_to_id": a.uint("up_to_id", True)}


def _v_typing(a: _Args) -> Dict[str, Any]:
    return {"chat_id": a.ident("chat_id"), "state": a.enum("state", ("typing", "recording", "stop"))}


def _v_profile(a: _Args) -> Dict[str, Any]:
    k = {
        "display_name": a.text("display_name", False, _ADMIN_NAME_MAX),
        "status_text": a.text("status_text", False, _ADMIN_NAME_MAX),
        "read_receipts": a.flag("read_receipts"),
        "show_last_seen": a.flag("show_last_seen"),
    }
    fields = {name: value for name, value in k.items() if value is not None}
    if not fields:
        raise _bad("nothing to update")
    return {"fields": fields}


def _v_audit(a: _Args) -> Dict[str, Any]:
    return {"limit": a.limit()}


def _v_create_user(a: _Args) -> Dict[str, Any]:
    return {
        "username": a.text("username", True, _ADMIN_NAME_MAX),
        "display_name": a.text("display_name", True, _ADMIN_NAME_MAX),
        "password": a.text("password", True, _PASSWORD_MAX),
        "role": a.enum("role", ("admin", "member"), False),
    }


def _v_update_user(a: _Args) -> Dict[str, Any]:
    k = {
        "user_id": a.ident("user_id"),
        "role": a.enum("role", ("admin", "member"), False),
        "disabled": a.flag("disabled"),
        "display_name": a.text("display_name", False, _ADMIN_NAME_MAX),
    }
    if k["role"] is None and k["disabled"] is None and k["display_name"] is None:
        raise _bad("nothing to update")
    return k


def _v_reset_password(a: _Args) -> Dict[str, Any]:
    return {"user_id": a.ident("user_id"), "new_password": a.text("new_password", True, _PASSWORD_MAX)}


def _v_settings(a: _Args) -> Dict[str, Any]:
    return {
        "workspace_name": a.text("workspace_name", False, _ADMIN_NAME_MAX),
        "registration_open": a.flag("registration_open"),
        "rotate_join_code": a.flag("rotate_join_code"),
    }


class _Spec(NamedTuple):
    """One request type: its validator, handler method (``""``: answered inline), kind and its token buckets (7.5)."""

    validate: Validator
    handler: str
    mutating: bool
    buckets: Tuple[str, ...] = ()
    globals_: bool = True


_READ = ("read",)

#: Every request of SPEC 7.4.  ``mutating`` requests run as detached tasks; ``buckets`` are charged in addition to the
#: two global buckets (``globals_=False``: ``ping``, ``typing``, ``receipt.*`` and ``admin.*`` are exempt, SPEC 7.5).
REQUESTS: Dict[str, _Spec] = {
    "ping": _Spec(_v_none, "", False, (), False),
    "chat.get": _Spec(_v_chat, "_h_chat_get", False, _READ),
    "chat.open_direct": _Spec(_v_open_direct, "_h_open_direct", True, ("chat.open_direct",)),
    "chat.create_group": _Spec(_v_create_group, "_h_create_group", True, ("chat.create_group",)),
    "chat.update": _Spec(_v_chat_update, "_h_chat_update", True),
    "chat.add_members": _Spec(_v_add_members, "_h_add_members", True, ("chat.add_members",)),
    "chat.remove_member": _Spec(_v_chat_user, "_h_remove_member", True),
    "chat.set_admin": _Spec(_v_set_admin, "_h_set_admin", True),
    "chat.leave": _Spec(_v_chat, "_h_leave", True),
    "chat.prefs": _Spec(_v_prefs, "_h_prefs", True),
    "chat.clear": _Spec(_v_chat, "_h_clear", True),
    "chat.history": _Spec(_v_history, "_h_history", False, _READ),
    "msg.send": _Spec(_v_send, "_h_send", True, ("msg.send",)),
    "msg.edit": _Spec(_v_edit, "_h_edit", True),
    "msg.delete": _Spec(_v_delete, "_h_delete", True),
    "msg.react": _Spec(_v_react, "_h_react", True, ("msg.react",)),
    "msg.star": _Spec(_v_star, "_h_star", True),
    "msg.pin": _Spec(_v_pin, "_h_pin", True, ("msg.pin",)),
    "msg.forward": _Spec(_v_forward, "_h_forward", True, ("msg.forward",)),
    "msg.info": _Spec(_v_message, "_h_info", False, _READ),
    "msg.search": _Spec(_v_search, "_h_search", False, ("msg.search",)),
    "msg.starred": _Spec(_v_starred, "_h_starred", False, _READ),
    "msg.shared": _Spec(_v_shared, "_h_shared", False, _READ),
    "receipt.delivered": _Spec(_v_delivered, "_h_delivered", True, ("receipt",), False),
    "receipt.read": _Spec(_v_read, "_h_read", True, ("receipt",), False),
    "typing": _Spec(_v_typing, "", False, ("typing",), False),
    "profile.update": _Spec(_v_profile, "_h_profile", True, ("profile.update",)),
    "admin.users": _Spec(_v_none, "_h_admin_users", False, ("admin_other",), False),
    "admin.create_user": _Spec(_v_create_user, "_h_admin_create_user", True, ("admin.create_user",), False),
    "admin.update_user": _Spec(_v_update_user, "_h_admin_update_user", True, ("admin_other",), False),
    "admin.reset_password": _Spec(_v_reset_password, "_h_admin_reset_password", True, ("admin.reset_password",), False),
    "admin.settings": _Spec(_v_settings, "_h_admin_settings", True, ("admin_other",), False),
    "admin.stats": _Spec(_v_none, "_h_admin_stats", False, ("admin_other",), False),
    "admin.audit": _Spec(_v_audit, "_h_admin_audit", False, ("admin_other",), False),
}


# --------------------------------------------------------------------------------------------------------------
# Token buckets and the connection record
# --------------------------------------------------------------------------------------------------------------


class _Bucket:
    """A token bucket evaluated with ``loop.time()`` (SPEC 7.5): capacity ``count``, refill ``count / window``/s."""

    __slots__ = ("capacity", "window", "rate", "tokens", "stamp")

    def __init__(self, count: float, window: float, now: float) -> None:
        self.capacity = float(count)
        self.window = window
        self.rate = count / window if window > 0 else float("inf")
        self.tokens = float(count)
        self.stamp = now

    def _refill(self, now: float) -> None:
        if now > self.stamp:
            self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now

    def shortfall(self, need: int, now: float) -> float:
        """``0.0`` when ``need`` tokens are available, else the ``retry_after`` seconds (2 decimals, rounded up)."""
        self._refill(now)
        if self.tokens >= need - 1e-9:
            return 0.0
        if self.rate <= 0:
            return 3600.0
        return math.ceil((need - self.tokens) / self.rate * 100) / 100

    def take(self, need: int) -> None:
        self.tokens -= need

    def refund(self, count: int, now: float) -> None:
        self._refill(now)
        self.tokens = min(self.capacity, self.tokens + count)


class _Conn:
    """One authenticated WebSocket as the hub sees it: ``pending`` -> ``live`` -> ``closed`` (SPEC 7.6(7))."""

    __slots__ = (
        "ws", "id", "user_id", "token_hash", "ip", "state", "backlog", "backlog_receipts", "buckets", "violation_at",
        "violations", "inflight", "tasks", "malformed", "typing_keys", "closing",
    )

    def __init__(self, ws: Any, conn_id: int, session: Dict[str, Any]) -> None:
        self.ws = ws
        self.id = conn_id
        self.user_id = int(session["user_id"])
        self.token_hash = session.get("token_hash")
        self.ip = str(session.get("ip") or getattr(ws, "remote_addr", "") or "")
        self.state = "pending"
        self.backlog: List[str] = []
        self.backlog_receipts: Dict[Any, str] = {}
        self.buckets: Dict[str, _Bucket] = {}
        self.violation_at: Dict[str, float] = {}
        self.violations: Deque[float] = deque()
        self.inflight = 0
        self.tasks: Set["asyncio.Task[Any]"] = set()
        self.malformed = 0
        self.typing_keys: Set[Tuple[int, int]] = set()
        self.closing = False


def _event(kind: str, user_ids: Optional[Iterable[int]], data: Dict[str, Any]) -> Dict[str, Any]:
    """A one-group durable fan-out event in the shape of the db layer's plans (``None`` = every connection)."""
    return {"t": kind, "cls": "durable", "durable": True, "key": None, "groups": [{"user_ids": user_ids, "d": data}]}


def _fingerprint(user: Dict[str, Any]) -> Tuple[Any, ...]:
    """The fields of a ``User`` whose change ``external_change`` announces (SPEC 2.4)."""
    return (
        user["role"], bool(user["disabled"]), user["display_name"], user["status_text"], bool(user["read_receipts"]),
        bool(user["activated"]),
    )


# --------------------------------------------------------------------------------------------------------------
# Read functions that run on a reader thread (the only SQL in this module)
# --------------------------------------------------------------------------------------------------------------


def _baseline_read(conn: Any) -> Dict[str, Any]:
    """Everything ``Hub.start`` needs from one snapshot: users, membership index, the default chat and its newest id."""
    row = conn.execute("SELECT id, last_message_id FROM chats WHERE is_default = 1").fetchone()
    return {
        "users": db_users.list_users(conn),
        "index": db_chats.load_membership_index(conn),
        "everyone": int(row[0]) if row is not None else None,
        "hi": int(row[1] or 0) if row is not None else 0,
    }


def _external_read(conn: Any, old_members: Dict[int, str], hi: int, fanned: Set[int]) -> Dict[str, Any]:
    """The state ``external_change`` diffs against its copy (SPEC 2.4), from one reader snapshot.

    ``old_members`` is the hub's copy of the default chat's members (user id -> chat role); ``hi`` the newest
    default-chat message id already examined and ``fanned`` the system message ids the hub itself announced.  Returns
    the users, the default chat's index entry, the ``members[]`` entries of the members that joined or changed role,
    and the ready ``ev.message`` events of the system messages nobody announced yet.
    """
    users = db_users.list_users(conn)
    row = conn.execute("SELECT id FROM chats WHERE is_default = 1").fetchone()
    result: Dict[str, Any] = {"users": users, "everyone": None, "entry": None, "members": {}, "system": [], "hi": hi}
    if row is None:
        return result
    chat_id = int(row[0])
    entry = db_chats.index_entry(conn, chat_id)
    result["everyone"], result["entry"] = chat_id, entry
    wanted = [uid for uid, role in entry["members"].items() if old_members.get(uid) != role]
    if wanted:
        chat = db_chats.build_chat(conn, chat_id, wanted[0])
        if chat is not None:
            result["members"] = {m["user_id"]: m for m in chat["members"] if m["user_id"] in wanted}
    found = conn.execute(
        "SELECT id FROM messages WHERE chat_id = ? AND kind = 'system' AND id > ? ORDER BY id LIMIT 200",
        (chat_id, hi),
    ).fetchall()
    for (message_id,) in found:
        result["hi"] = max(result["hi"], message_id)
        if message_id not in fanned:
            event = db_messages.system_message_event(conn, chat_id, message_id)
            if event is not None:
                result["system"].append((message_id, event))
    return result


# --------------------------------------------------------------------------------------------------------------
# The hub
# --------------------------------------------------------------------------------------------------------------


class Hub:
    """The realtime hub (SPEC 6.1).  Constructed inside the running loop by ``app.py``."""

    def __init__(self, db: Any, cfg: Any) -> None:
        self.db = db
        self.cfg = cfg
        self.stopping = False
        self.storage_bytes = 0
        self.counters: Dict[str, int] = {"external_change_calls": 0, "revalidate_calls": 0, "dropped_ephemeral": 0}
        self._specs: Dict[str, Tuple[_Spec, Optional[Callable[..., Awaitable[Dict[str, Any]]]]]] = {
            name: (spec, getattr(self, spec.handler) if spec.handler else None) for name, spec in REQUESTS.items()
        }
        self._started = False
        self._started_at = time.monotonic()
        self._start_lock: Optional["asyncio.Lock"] = None
        self._ready_sem: Optional["asyncio.Semaphore"] = None
        self._search_sem: Optional["asyncio.Semaphore"] = None
        self._conn_ids = 0
        self._conns: Dict[int, Set[_Conn]] = {}
        self._all: Set[_Conn] = set()
        self._locks: Dict[int, "asyncio.Lock"] = {}
        self._inflight: Set["asyncio.Task[Any]"] = set()
        self._background: Set["asyncio.Task[Any]"] = set()
        self._user_buckets: Dict[int, Dict[str, _Bucket]] = {}
        self._searching: Set[int] = set()
        self._index: Dict[int, Dict[str, Any]] = {}
        self._online: Set[int] = set()
        self._grace: Dict[int, "asyncio.TimerHandle"] = {}
        self._share_seen: Dict[int, bool] = {}
        self._seen_wall: Dict[int, float] = {}
        self._typing: Dict[Tuple[int, int], Dict[int, Tuple[str, float]]] = {}
        self._relayed: Dict[Tuple[int, int], Tuple[str, float]] = {}
        self._users: Dict[int, Tuple[Any, ...]] = {}
        self._everyone_id: Optional[int] = None
        self._everyone_hi = 0
        self._fanned_system: Set[int] = set()
        self._limits_payload = {
            "max_upload_bytes": cfg.max_upload_bytes,
            "max_body_chars": cfg.max_body_chars,
            "edit_window_s": cfg.edit_window_s,
            "delete_window_s": cfg.delete_window_s,
            "max_pinned_chats": 3,
            "max_pinned_messages": 5,
            "max_group_members": 200,
            "max_forward_messages": 20,
            "max_forward_chats": 5,
        }
        db_chats.configure_limits(
            max_body_chars=cfg.max_body_chars,
            edit_window_s=cfg.edit_window_s,
            delete_window_s=cfg.delete_window_s,
            unread_cap=cfg.limit("unread_cap", 1000),
        )

    # ==========================================================================================================
    # Lifecycle (SPEC 6.1)
    # ==========================================================================================================

    async def start(self) -> None:
        """Build the membership index and the baselines of the control-poll diff from one reader snapshot (idempotent).

        ``app.py`` may call this before binding the sockets; every other entry point calls it lazily, so the hub also
        works when nobody did.
        """
        if self._started:
            return
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
            self._ready_sem = asyncio.Semaphore(READY_CONCURRENCY)
            self._search_sem = asyncio.Semaphore(1)
        async with self._start_lock:
            if self._started:
                return
            base = await self.db.run_read(_baseline_read)
            self._index = base["index"]
            self._users = {u["id"]: _fingerprint(u) for u in base["users"]}
            self._everyone_id = base["everyone"]
            self._everyone_hi = base["hi"]
            self._started = True

    async def shutdown(self) -> None:
        """Stop accepting work and wait up to 5 s for in-flight tasks; ``app.py`` closes the sockets (SPEC 2.4)."""
        self.stopping = True
        for handle in self._grace.values():
            handle.cancel()
        self._grace.clear()
        pending = [t for t in (self._inflight | self._background) if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=util.scaled(SHUTDOWN_WAIT_S))

    def online_user_ids(self) -> List[int]:
        """Users with at least one open connection (the heartbeat writes their ``last_seen_at``)."""
        return sorted(self._conns)

    def is_online(self, user_id: int) -> bool:
        """``True`` while ``user_id`` counts as online (an open connection or inside the 8 s grace period)."""
        return user_id in self._online

    async def _ensure_started(self) -> None:
        if not self._started:
            await self.start()

    # ==========================================================================================================
    # Tasks
    # ==========================================================================================================

    def _spawn(
        self, coro: Coroutine[Any, Any, Any], tracked: Optional[Set["asyncio.Task[Any]"]] = None, quiet: bool = False
    ) -> "asyncio.Task[Any]":
        """Create a task that is referenced until done (the loop keeps only weak references)."""
        task = asyncio.get_running_loop().create_task(coro)
        (self._background if tracked is None else tracked).add(task)

        def done(finished: "asyncio.Task[Any]") -> None:
            self._inflight.discard(finished)
            self._background.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()  # always retrieved: asyncio must not warn about it
            if error is not None and not quiet:
                log.error("background task failed: %s", type(error).__name__, exc_info=error)

        task.add_done_callback(done)
        return task

    def _launch(self, coro: Coroutine[Any, Any, Any]) -> "asyncio.Task[Any]":
        """A detached operation (SPEC 7.6(5)): tracked in ``_inflight``; its waiter awaits ``asyncio.shield`` of it."""
        return self._spawn(coro, self._inflight, quiet=True)

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    # ==========================================================================================================
    # Connections: pending -> live (SPEC 7.6(7))
    # ==========================================================================================================

    async def serve(self, ws: Any, session: Dict[str, Any]) -> None:
        """Run one authenticated socket until it closes: ``ev.ready`` first, then the request loop (SPEC 6.1)."""
        await self._ensure_started()
        if self.stopping:
            ws.close(1001, "restart")
            return
        self._conn_ids += 1
        conn = _Conn(ws, self._conn_ids, session)
        self._register(conn)
        log.info("ws open user=%d ip=%s", conn.user_id, util.safe_log_value(conn.ip))
        try:
            if await self._open(conn):
                await self._pump(conn)
        finally:
            await self._cleanup(conn)

    async def _open(self, conn: _Conn) -> bool:
        """Build the snapshot and make the connection live; ``False`` when it must be dropped instead."""
        sem = self._ready_sem
        assert sem is not None
        try:
            async with sem:
                built = await self.db.run_read(
                    db_chats.build_ready, conn.user_id, self.cfg.workspace_name, self.cfg.registration_open
                )
        except dbmod.RequestError as exc:
            if exc.code == "unauthorized":
                self._kick(conn, "disabled")
            else:
                log.error("building ev.ready failed: %s", exc.code)
                conn.ws.close(1011, "server error")
            return False
        except (dbmod.ServerBusy, dbmod.DatabaseClosed):
            conn.ws.close(1013, "busy")
            return False
        except Exception as exc:  # noqa: BLE001 - one broken snapshot must close this socket only
            log.error("building ev.ready failed: %s", type(exc).__name__, exc_info=True)
            conn.ws.close(1011, "server error")
            return False
        if conn.ws.closed or conn.closing:
            return False
        self._go_live(conn, built)  # no await from here until the connection is live
        return True

    def _go_live(self, conn: _Conn, built: Dict[str, Any]) -> None:
        """The no-``await`` block of SPEC 7.6(7): capture presence, queue ``ev.ready``, flush the backlog, go live."""
        wall = time.time()
        if self._seen_wall:
            self._seen_wall = {u: t for u, t in self._seen_wall.items() if wall - t < SEEN_CACHE_S}
        for user in built["users"]:
            uid = user["id"]
            user["online"] = uid in self._online
            seen = self._seen_wall.get(uid)
            if seen is not None and not user["online"] and user["last_seen"] is not None and seen > user["last_seen"]:
                user["last_seen"] = seen
        me = built["me"]
        me["online"] = True
        self._share_seen[conn.user_id] = bool(me.get("show_last_seen", True))
        payload = {
            "protocol": PROTOCOL,
            "instance_id": built["instance_id"],
            "me": me,
            "workspace": built["workspace"],
            "users": built["users"],
            "chats": built["chats"],
            "server_time": built["server_time"],
            "limits": self._limits_payload,
        }
        ws = conn.ws
        ws.send_text(util.json_dumps({"t": "ev.ready", "d": payload}, ensure_ascii=False))
        for text in conn.backlog:
            ws.send_text(text)
        for key, text in conn.backlog_receipts.items():
            self._send_live(conn, text, "keyed", key)
        conn.backlog = []
        conn.backlog_receipts = {}
        conn.state = "live"

    async def _pump(self, conn: _Conn) -> None:
        """The read loop: every received text frame goes through :meth:`_on_frame` in arrival order."""
        while True:
            text = await conn.ws.recv()
            if text is None or conn.closing:
                return
            try:
                self._on_frame(conn, text)
            except Exception as exc:  # noqa: BLE001 - a bug while handling one frame must not end the connection
                log.error("frame handling failed: %s", type(exc).__name__, exc_info=True)

    async def _cleanup(self, conn: _Conn) -> None:
        conn.state = "closed"
        conn.closing = True
        tasks = [t for t in conn.tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._drop_typing_of(conn)
        self._unregister(conn)
        if not conn.ws.closed:
            conn.ws.close(1000, "")
        log.info("ws close user=%d ip=%s", conn.user_id, util.safe_log_value(conn.ip))

    # ---- presence (SPEC 8.4) ---------------------------------------------------------------------------------------

    def _register(self, conn: _Conn) -> None:
        uid = conn.user_id
        self._conns.setdefault(uid, set()).add(conn)
        self._all.add(conn)
        handle = self._grace.pop(uid, None)
        if handle is not None:
            handle.cancel()
        if uid not in self._online:
            self._online.add(uid)
            self._broadcast_presence(uid, True, None)

    def _unregister(self, conn: _Conn) -> None:
        uid = conn.user_id
        self._all.discard(conn)
        mine = self._conns.get(uid)
        if mine is None:
            return
        mine.discard(conn)
        if mine:
            return
        del self._conns[uid]
        if self.stopping:
            return
        wall = float(getattr(conn.ws, "last_rx_wall", time.time()))
        self._grace[uid] = asyncio.get_running_loop().call_later(
            util.scaled(PRESENCE_GRACE_S), self._finalize_presence, uid, wall
        )

    def _finalize_presence(self, uid: int, wall: float) -> None:
        """The grace period ended with no new connection: broadcast ``offline``, persist ``last_seen`` (SPEC 8.4)."""
        self._grace.pop(uid, None)
        if uid in self._conns or self.stopping:
            return
        self._online.discard(uid)
        self._seen_wall[uid] = wall
        self._broadcast_presence(uid, False, wall if self._share_seen.get(uid, True) else None)
        self._spawn(self._store_last_seen(uid, wall))

    async def _store_last_seen(self, uid: int, wall: float) -> None:
        try:
            await self.db.run(db_users.set_last_seen, [uid], wall)
        except dbmod.DatabaseClosed:
            log.debug("last_seen not stored: the database is closed")

    def _broadcast_presence(self, uid: int, online: bool, last_seen: Optional[float]) -> None:
        if self.stopping:
            return
        text = util.json_dumps(
            {"t": "ev.presence", "d": {"user_id": uid, "online": online, "last_seen": last_seen}}, ensure_ascii=False
        )
        key = ("presence", uid)
        for conn in list(self._all):
            if conn.state == "live":
                conn.ws.send_text(text, durable=False, key=key)

    # ==========================================================================================================
    # Sending
    # ==========================================================================================================

    @staticmethod
    def _send_live(conn: _Conn, text: str, cls: str, key: Any) -> None:
        """Queue a frame on a live socket in its SPEC 6 class: durable, keyed (``ev.receipt``) or ephemeral."""
        if cls == "ephemeral":
            conn.ws.send_text(text, durable=False, key=key)
        else:
            conn.ws.send_text(text, durable=True, key=key if cls == "keyed" else None)

    def _send(self, conn: _Conn, text: str, cls: str, key: Any) -> None:
        """Hand one frame to a connection: live ones get it at once, pending ones keep it in their backlog.

        A pending connection keeps durable frames in ``backlog`` (cap 400) and the newest ``ev.receipt`` per key in
        ``backlog_receipts`` (cap 2000 keys); ephemeral frames are never kept (SPEC 7.6(7)).
        """
        if conn.state == "live":
            self._send_live(conn, text, cls, key)
        elif conn.state == "pending":
            if cls == "durable":
                if len(conn.backlog) >= BACKLOG_CAP:
                    self._overflow(conn)
                else:
                    conn.backlog.append(text)
            elif cls == "keyed":
                if key not in conn.backlog_receipts and len(conn.backlog_receipts) >= RECEIPT_BACKLOG_CAP:
                    self._overflow(conn)
                else:
                    conn.backlog_receipts[key] = text

    def _overflow(self, conn: _Conn) -> None:
        conn.closing = True
        conn.backlog = []
        conn.backlog_receipts = {}
        conn.ws.close(1013, "overloaded")

    def _deliver(self, user_ids: Optional[Iterable[int]], text: str, cls: str, key: Any) -> None:
        """Enqueue ``text`` on every connection of ``user_ids`` (``None``: every connection); one try per connection."""
        if user_ids is None:
            targets: List[_Conn] = list(self._all)
        else:
            targets = [c for uid in user_ids for c in self._conns.get(uid, ())]
        for conn in targets:
            try:
                self._send(conn, text, cls, key)
            except Exception as exc:  # noqa: BLE001 - SPEC 7.6(5): one broken connection never stops the fan-out
                log.error("enqueue failed: %s", type(exc).__name__, exc_info=True)

    def _fanout(self, events: Iterable[Dict[str, Any]]) -> None:
        """Enqueue a fan-out plan in order (SPEC 7.6(1)); each audience group is serialised once."""
        for event in events:
            kind = event["t"]
            if not kind.startswith("ev."):
                self._directive(event)
                continue
            cls, key = event["cls"], event.get("key")
            for group in event["groups"]:
                data = group["d"]
                self._decorate(kind, data)
                text = util.json_dumps({"t": kind, "d": data}, ensure_ascii=False)
                self._deliver(group["user_ids"], text, cls, key)
            self._note_fanned(kind, event)

    def _decorate(self, kind: str, data: Dict[str, Any]) -> None:
        """Add the truthful ``online`` flag the db layer cannot know (``ev.user_update`` / ``ev.me``)."""
        if kind == "ev.user_update":
            user = data["user"]
            user["online"] = user["id"] in self._online
            self._users[user["id"]] = _fingerprint(user)
        elif kind == "ev.me":
            data["me"]["online"] = True

    def _note_fanned(self, kind: str, event: Dict[str, Any]) -> None:
        """Remember the default chat's system messages announced here so ``external_change`` does not repeat them."""
        if kind != "ev.message" or self._everyone_id is None:
            return
        for group in event["groups"]:
            message = group["d"].get("message")
            if message and message.get("kind") == "system" and message.get("chat_id") == self._everyone_id:
                self._fanned_system.add(message["id"])

    def _directive(self, event: Dict[str, Any]) -> None:
        if event["t"] == "hub.revoke":
            self._revoke_now(event["user_id"], event["reason"])
        else:
            log.error("unknown fan-out directive %s", util.safe_log_value(event["t"]))

    def _reply(self, conn: _Conn, rid: Optional[str], payload: Dict[str, Any], ok: bool = True) -> None:
        """Send the ``res`` of a request that carried an ``id`` (a request without one never gets an answer)."""
        if rid is None:
            return
        frame: Dict[str, Any] = {"t": "res", "id": rid, "ok": ok}
        frame["d" if ok else "err"] = payload
        conn.ws.send_text(util.json_dumps(frame, ensure_ascii=False))

    # ==========================================================================================================
    # Frame handling (SPEC 7.1)
    # ==========================================================================================================

    def _malformed(self, conn: _Conn) -> None:
        conn.malformed += 1
        err = {"code": "bad_request", "msg": "malformed frame"}
        conn.ws.send_text(util.json_dumps({"t": "res", "id": None, "ok": False, "err": err}))
        if conn.malformed >= MALFORMED_LIMIT:
            conn.closing = True
            conn.ws.close(1008, "too many malformed frames")

    def _on_frame(self, conn: _Conn, text: str) -> None:
        try:
            obj = util.json_loads_strict(text)
        except ValueError:
            self._malformed(conn)
            return
        if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
            self._malformed(conn)
            return
        conn.malformed = 0
        name = obj["t"]
        rid = obj.get("id")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 64:
            rid = None
        if util.has_lone_surrogate(obj):
            if rid is not None and util.has_lone_surrogate(rid):
                rid = None
            self._reply(conn, rid, _bad("text is not valid unicode", "invalid_text").to_err(), False)
            return
        if self.stopping:
            self._reply(conn, rid, {"code": "server_error", "msg": "the server is restarting"}, False)
            return
        entry = self._specs.get(name) if len(name) <= 32 else None
        if entry is None:
            self._reply(conn, rid, _bad("unknown request type").to_err(), False)
            return
        spec, handler = entry
        data = obj.get("d", {}) if "d" in obj else {}
        try:
            if not isinstance(data, dict):
                raise _bad("d must be an object")
            args = spec.validate(_Args(data))
        except dbmod.RequestError as exc:
            self._reply(conn, rid, exc.to_err(), False)
            return
        if name == "ping":
            self._reply(conn, rid, {"now": util.now()})
            return
        if name == "typing":
            if self._charge(conn, spec, args) is None:
                self._typing_frame(conn, args)
            self._reply(conn, rid, {})
            return
        assert handler is not None
        if conn.inflight >= MAX_IN_FLIGHT:
            self._refuse(conn, rid, "inflight", util.scaled(INFLIGHT_WINDOW_S), 1.0)
            return
        refused = self._charge(conn, spec, args)
        if refused is not None:
            self._refuse(conn, rid, refused[1], refused[2], refused[0])
            return
        conn.inflight += 1
        task = self._spawn(self._execute(conn, name, handler, rid, args), self._inflight if spec.mutating else None)
        if not spec.mutating:
            conn.tasks.add(task)
        task.add_done_callback(lambda finished, c=conn: self._request_done(c, finished))

    @staticmethod
    def _request_done(conn: _Conn, task: "asyncio.Task[Any]") -> None:
        conn.inflight -= 1
        conn.tasks.discard(task)

    async def _execute(
        self,
        conn: _Conn,
        name: str,
        handler: Callable[..., Awaitable[Dict[str, Any]]],
        rid: Optional[str],
        args: Dict[str, Any],
    ) -> None:
        """Run one request and answer it; every failure becomes a ``res`` (SPEC 7.1.1), never an exception."""
        try:
            self._reply(conn, rid, await handler(conn, args))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to a res below; unexpected ones are logged with a traceback
            err = self._error_of(exc)
            if err is None:
                log.error("request %s failed: %s", name, type(exc).__name__, exc_info=True)
                err = {"code": "server_error", "msg": "internal error"}
            self._reply(conn, rid, err, False)
            if err["code"] == "unauthorized":
                self._revoke_now(conn.user_id, "disabled")  # SPEC 7.1.1 (e): the account was disabled meanwhile

    @staticmethod
    def _error_of(exc: BaseException) -> Optional[Dict[str, Any]]:
        """The ``err`` object of a known failure, ``None`` for a bug (SPEC 7.1.1)."""
        if isinstance(exc, dbmod.RequestError):
            return exc.to_err()
        if isinstance(exc, dbmod.ServerBusy):
            return {"code": "server_busy", "msg": "the server is busy, retry shortly", "retry_after": exc.retry_after}
        if isinstance(exc, dbmod.DatabaseClosed):
            return {"code": "server_error", "msg": "the server is restarting"}
        return None

    # ---- rate limits (SPEC 7.5) -----------------------------------------------------------------------------------

    def _limit_spec(self, name: str) -> Tuple[int, float]:
        limits = self.cfg.test_limits
        spec = limits.get(name, limits.get("*"))
        count, window = spec if spec is not None else DEFAULT_LIMITS[name]
        return int(count), util.scaled(float(window))

    def _bucket(self, conn: _Conn, name: str, now: float) -> _Bucket:
        store = conn.buckets if name in CONN_BUCKETS else self._user_buckets.setdefault(conn.user_id, {})
        bucket = store.get(name)
        if bucket is None:
            count, window = self._limit_spec(name)
            bucket = store[name] = _Bucket(count, window, now)
        return bucket

    def _charge(self, conn: _Conn, spec: _Spec, args: Dict[str, Any]) -> Optional[Tuple[float, str, float]]:
        """Take a request's tokens all-or-nothing: ``None`` when granted, else ``(retry_after, bucket, window)``."""
        now = self._now()
        needs: List[Tuple[str, int]] = []
        if spec.globals_:
            needs.extend((("global_user", 1), ("global_conn", 1)))
        needs.extend((bucket, 1) for bucket in spec.buckets)
        if spec.handler == "_h_forward":
            needs.append(("msg.send", len(args["chat_ids"])))
        entries = [(name, self._bucket(conn, name, now), count) for name, count in needs]
        worst: Optional[Tuple[float, str, float]] = None
        for name, bucket, count in entries:
            wait = bucket.shortfall(count, now)
            if wait > 0 and (worst is None or wait > worst[0]):
                worst = (wait, name, bucket.window)
        if worst is not None:
            return worst
        for _, bucket, count in entries:
            bucket.take(count)
        return None

    def _refund(self, conn: _Conn, items: Iterable[Tuple[str, int]]) -> None:
        now = self._now()
        for name, count in items:
            self._bucket(conn, name, now).refund(count, now)

    def _refuse(self, conn: _Conn, rid: Optional[str], bucket: str, window: float, retry_after: float) -> None:
        """Answer ``rate_limited`` and count the violation; the third one within 60 s closes the socket with 4008."""
        err = {"code": "rate_limited", "msg": "too many requests, slow down", "retry_after": retry_after}
        self._reply(conn, rid, err, False)
        now = self._now()
        last = conn.violation_at.get(bucket)
        if last is not None and now - last < window:
            return
        conn.violation_at[bucket] = now
        conn.violations.append(now)
        cutoff = now - util.scaled(VIOLATION_WINDOW_S)
        while conn.violations and conn.violations[0] < cutoff:
            conn.violations.popleft()
        if len(conn.violations) >= VIOLATION_LIMIT:
            log.info("closing a connection of user %d that keeps hitting rate limits", conn.user_id)
            conn.closing = True
            conn.ws.close(CLOSE_RATE_LIMITED, "rate limited")

    # ==========================================================================================================
    # Writing: chat locks, one Database.run, index, fan-out (SPEC 7.6)
    # ==========================================================================================================

    def _chat_lock(self, chat_id: Optional[int]) -> "asyncio.Lock":
        key = EVERYONE_LOCK if chat_id is None or chat_id == self._everyone_id else chat_id
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    @contextlib.asynccontextmanager
    async def _locked(self, chat_ids: Iterable[Optional[int]]) -> AsyncIterator[None]:
        """Hold the locks of ``chat_ids`` (ascending: two forwards ``[1, 2]`` and ``[2, 1]`` cannot deadlock)."""
        keys = sorted({EVERYONE_LOCK if c is None or c == self._everyone_id else c for c in chat_ids})
        held: List["asyncio.Lock"] = []
        try:
            for key in keys:
                lock = self._chat_lock(None if key == EVERYONE_LOCK else key)
                await lock.acquire()
                held.append(lock)
            yield
        finally:
            for lock in reversed(held):
                lock.release()

    async def _write(self, chat_ids: Iterable[int], fn: Callable[..., Any], *args: Any) -> Dict[str, Any]:
        """The common mutation: lock, ``Database.run(fn, *args)``, install the index, enqueue the events (no await)."""
        async with self._locked(chat_ids):
            out = await self.db.run(fn, *args)
            self._apply(out)
        return out

    def _apply(self, out: Dict[str, Any]) -> None:
        """Everything that follows a commit, in one block without an ``await`` (SPEC 7.6(1), (6))."""
        for entry in out.get("index") or ():
            self._index[entry["chat_id"]] = entry
        self._fanout(out["events"])
        for item in out.get("released_attachments") or ():
            self._spawn(self._remove_file(item.get("path")))

    async def _remove_file(self, relative: Any) -> None:
        """Delete a released attachment's file after COMMIT (SPEC 3 rules; Windows retry rules of 2.6)."""
        if not isinstance(relative, str) or not relative:
            return
        root = os.path.abspath(str(self.cfg.uploads_dir))
        path = os.path.abspath(os.path.join(root, relative))
        try:
            inside = os.path.commonpath([root, path]) == root and path != root
        except ValueError:
            inside = False
        if not inside:
            log.error("refusing to delete a file outside the uploads directory")
            return
        await asyncio.get_running_loop().run_in_executor(None, util.retry_file_op, os.remove, path)

    # ==========================================================================================================
    # Request handlers: connection, chats
    # ==========================================================================================================

    async def _h_chat_get(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return await self.db.run_read(db_chats.chat_get, conn.user_id, k["chat_id"])

    async def _h_open_direct(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        existing = await self.db.run_read(db_chats.direct_chat_id, conn.user_id, k["user_id"])
        out = await self._write([existing] if existing else [], db_chats.chat_open_direct, conn.user_id, k["user_id"])
        return out["res"]

    async def _h_create_group(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            [], db_chats.chat_create_group, conn.user_id, k["title"], k["member_ids"], k["description"]
        )
        return out["res"]

    async def _h_chat_update(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            [k["chat_id"]], db_chats.chat_update, conn.user_id, k["chat_id"], k["title"], k["description"],
            k["only_admins_post"],
        )
        return out["res"]

    async def _h_add_members(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            [k["chat_id"]], db_chats.chat_add_members, conn.user_id, k["chat_id"], k["user_ids"]
        )
        return out["res"]

    async def _h_remove_member(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            [k["chat_id"]], db_chats.chat_remove_member, conn.user_id, k["chat_id"], k["user_id"]
        )
        return out["res"]

    async def _h_set_admin(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            [k["chat_id"]], db_chats.chat_set_admin, conn.user_id, k["chat_id"], k["user_id"], k["admin"]
        )
        return out["res"]

    async def _h_leave(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write([k["chat_id"]], db_chats.chat_leave, conn.user_id, k["chat_id"])
        return out["res"]

    async def _h_prefs(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            [], db_chats.chat_prefs, conn.user_id, k["chat_id"], k["muted_until"], k["pinned"], k["archived"]
        )
        return out["res"]

    async def _h_clear(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write([], db_chats.chat_clear, conn.user_id, k["chat_id"])
        return out["res"]

    async def _h_history(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return await self.db.run_read(
            db_messages.chat_history, conn.user_id, k["chat_id"], k["before_id"], k["after_id"], k["around_id"],
            k["limit"],
        )

    # ==========================================================================================================
    # Request handlers: messages
    # ==========================================================================================================

    async def _h_send(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            [k["chat_id"]], db_messages.msg_send, conn.user_id, k["chat_id"], k["client_id"], k["body"],
            k["attachment_id"], k["reply_to_id"], k["seen_up_to_id"],
        )
        if out["deduped"]:
            self._refund(conn, [("msg.send", 1)])
        return out["res"]

    async def _h_forward(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            k["chat_ids"], db_messages.msg_forward, conn.user_id, k["message_ids"], k["chat_ids"], k["client_id"]
        )
        if out["deduped"]:
            self._refund(conn, [("msg.forward", 1), ("msg.send", len(k["chat_ids"]))])
        return out["res"]

    async def _write_message(
        self, conn: _Conn, message_id: int, fn: Callable[..., Any], *args: Any, lock: bool = True
    ) -> Dict[str, Any]:
        """A write on one message: lock the chat the message belongs to (an unknown id takes no lock and fails)."""
        chat_id = await self.db.run_read(db_messages.message_chat_id, message_id) if lock else None
        return await self._write([chat_id] if chat_id else [], fn, conn.user_id, message_id, *args)

    async def _h_edit(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return (await self._write_message(conn, k["message_id"], db_messages.msg_edit, k["body"]))["res"]

    async def _h_delete(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        everyone = k["scope"] == "everyone"
        out = await self._write_message(conn, k["message_id"], db_messages.msg_delete, k["scope"], lock=everyone)
        return out["res"]

    async def _h_react(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return (await self._write_message(conn, k["message_id"], db_messages.msg_react, k["emoji"]))["res"]

    async def _h_star(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write_message(conn, k["message_id"], db_messages.msg_star, k["starred"], lock=False)
        return out["res"]

    async def _h_pin(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._write(
            [k["chat_id"]], db_messages.msg_pin, conn.user_id, k["chat_id"], k["message_id"], k["pinned"]
        )
        return out["res"]

    async def _h_info(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return await self.db.run_read(db_messages.msg_info, conn.user_id, k["message_id"])

    async def _h_starred(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return await self.db.run_read(
            db_messages.msg_starred, conn.user_id, k["chat_id"], k["before_id"], k["limit"]
        )

    async def _h_search(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return await self._guarded_search(
            conn, db_messages.msg_search, k["q"], k["chat_id"], k["before_id"], k["limit"]
        )

    async def _h_shared(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return await self._guarded_search(
            conn, db_messages.msg_shared, k["chat_id"], k["kind"], k["before_id"], k["limit"]
        )

    async def _guarded_search(self, conn: _Conn, fn: Callable[..., Any], *args: Any) -> Dict[str, Any]:
        """``msg.search`` / ``msg.shared`` (SPEC 2.3): one per user, one global, time-boxed, never queued for long."""
        uid = conn.user_id
        if uid in self._searching:
            raise _Fail("rate_limited", "a search is already running", retry_after=1.0)
        self._searching.add(uid)
        try:
            if not await self._take_search_slot():
                raise _Fail("server_busy", "searching is busy, retry shortly", retry_after=2.0)
            try:
                return await self.db.run_read(fn, uid, *args, interrupt_after=util.scaled(SEARCH_BOX_S))
            except dbmod.ServerBusy:
                raise _Fail("server_busy", "the search took too long, narrow it down", retry_after=2.0)
            finally:
                assert self._search_sem is not None
                self._search_sem.release()
        finally:
            self._searching.discard(uid)

    async def _take_search_slot(self) -> bool:
        """Acquire the global search semaphore, waiting at most 5 s; ``False`` on timeout.

        Not ``wait_for(sem.acquire(), ...)``: when the timeout or a cancellation races with a successful acquire that
        form can leak the permit, and a leaked permit would disable every later search.
        """
        sem = self._search_sem
        assert sem is not None
        waiter = asyncio.ensure_future(sem.acquire())
        try:
            await asyncio.wait({waiter}, timeout=util.scaled(SEARCH_WAIT_S))
        except asyncio.CancelledError:
            self._give_up(waiter, sem)
            raise
        if waiter.done() and not waiter.cancelled():
            return True
        self._give_up(waiter, sem)
        return False

    @staticmethod
    def _give_up(waiter: "asyncio.Future[Any]", sem: "asyncio.Semaphore") -> None:
        """Abandon a pending ``acquire``; hand the permit back when it was granted after all."""
        if not waiter.cancel() and not waiter.cancelled() and waiter.exception() is None:
            sem.release()

    # ==========================================================================================================
    # Request handlers: receipts, typing, profile
    # ==========================================================================================================

    async def _h_delivered(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return (await self._write([], db_receipts.receipt_delivered, conn.user_id, k["items"]))["res"]

    async def _h_read(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        return (await self._write([], db_receipts.receipt_read, conn.user_id, k["chat_id"], k["up_to_id"]))["res"]

    async def _h_profile(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        uid = conn.user_id
        out = await self.db.run(db_users.profile_update, uid, k["fields"])
        me = out["me"]
        me["online"] = True
        self._share_seen[uid] = bool(me["show_last_seen"])
        if out["changed"]:
            public = {key: value for key, value in me.items() if key not in ("show_last_seen", "must_change_password")}
            self._fanout(
                [
                    _event("ev.user_update", None, {"user": public}),
                    _event("ev.me", [uid], {"me": dict(me)}),
                ]
            )
        return {"me": me}

    # ---- typing (SPEC 8.3) ----------------------------------------------------------------------------------------

    def _typing_frame(self, conn: _Conn, k: Dict[str, Any]) -> None:
        """Apply a ``typing`` frame: ignore rules of SPEC 7.4, then the per-connection entry and the relay rules."""
        uid, chat_id, state = conn.user_id, k["chat_id"], k["state"]
        entry = self._index.get(chat_id)
        if entry is None or uid not in entry["members"] or conn.state != "live":
            return
        if entry["self_chat"] or (entry["kind"] == "direct" and entry["peer_disabled"]):
            return
        if entry["only_admins_post"] and entry["members"].get(uid) != "admin":
            return
        key = (chat_id, uid)
        now = self._now()
        if state == "stop":
            entries = self._typing.get(key)
            if entries is not None:
                entries.pop(conn.id, None)
                if not entries:
                    del self._typing[key]
            conn.typing_keys.discard(key)
        else:
            self._typing.setdefault(key, {})[conn.id] = (state, now + util.scaled(TYPING_TTL_S))
            conn.typing_keys.add(key)
        self._typing_relay(key, now, refresh=True)

    def _typing_relay(self, key: Tuple[int, int], now: float, refresh: bool = False) -> None:
        """Relay the effective state of ``(chat, user)`` when it changed or, for a ``refresh`` frame, >= 1 s passed."""
        entries = self._typing.get(key)
        states = {state for state, _ in entries.values()} if entries else set()
        effective = "recording" if "recording" in states else ("typing" if states else None)
        previous = self._relayed.get(key)
        if effective is None:
            if previous is not None:
                del self._relayed[key]
                self._typing_send(key, "stop")
            return
        if previous is not None and previous[0] == effective and not (
            refresh and now - previous[1] >= util.scaled(TYPING_REFRESH_S)
        ):
            return
        self._relayed[key] = (effective, now)
        self._typing_send(key, effective)

    def _typing_send(self, key: Tuple[int, int], state: str) -> None:
        """``ev.typing`` to every live connection of every other current member; never to the typist (SPEC 8.3)."""
        chat_id, typist = key
        entry = self._index.get(chat_id)
        if entry is None or self.stopping:
            return
        text = util.json_dumps(
            {"t": "ev.typing", "d": {"chat_id": chat_id, "user_id": typist, "state": state}}, ensure_ascii=False
        )
        frame_key = ("typing", chat_id, typist)
        for uid in entry["members"]:
            if uid == typist:
                continue
            for conn in self._conns.get(uid, ()):
                if conn.state == "live":
                    conn.ws.send_text(text, durable=False, key=frame_key)

    def _drop_typing_of(self, conn: _Conn) -> None:
        """A closing connection drops only its own typing entries (SPEC 8.3)."""
        now = self._now()
        for key in list(conn.typing_keys):
            entries = self._typing.get(key)
            if entries is not None:
                entries.pop(conn.id, None)
                if not entries:
                    del self._typing[key]
            self._typing_relay(key, now)
        conn.typing_keys.clear()

    def sweep_typing(self) -> None:
        """Expire typing entries whose TTL ended; emit ``stop`` for each ``(chat, user)`` whose last entry is gone."""
        if not self._typing:
            return
        now = self._now()
        for key in list(self._typing):
            entries = self._typing[key]
            for conn_id in [c for c, (_, expires) in entries.items() if expires <= now]:
                del entries[conn_id]
            entry = self._index.get(key[0])
            if entry is None or key[1] not in entry["members"]:
                entries.clear()  # the typist left the chat meanwhile
            if not entries:
                del self._typing[key]
            self._typing_relay(key, now)

    # ==========================================================================================================
    # Request handlers: admin (SPEC 7.4)
    # ==========================================================================================================

    async def _h_admin_users(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        users = await self.db.run_read(db_users.admin_users, conn.user_id)
        for user in users:
            user["online"] = user["id"] in self._online
        return {"users": users}

    async def _h_admin_create_user(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        await self.db.run_read(db_users.require_admin, conn.user_id)  # cheap refusal before the hash is paid for
        username = db_users.normalize_username(k["username"])
        display_name = db_users.normalize_display_name(k["display_name"])
        if username is None:
            raise _bad("username must be 3-32 characters of a-z, 0-9, '.', '_' or '-'")
        if display_name is None:
            raise _bad("display name must be 1-40 characters")
        problem = auth.check_password_policy(k["password"], username, display_name)
        if problem is not None:
            raise _bad(problem, "weak_password")
        pw_hash = await auth.hash_password(k["password"])
        user = await self.create_user(
            {
                "username": username,
                "display_name": display_name,
                "pw_hash": pw_hash,
                "role": k["role"] or "member",
                "activated": False,
                "must_change_password": True,
                "setup_code": None,
                "check_setup_code": False,
                "actor_id": conn.user_id,
                "ip": conn.ip,
            }
        )
        return {"user": user}

    async def _h_admin_update_user(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        target = k["user_id"]
        ids = await self.db.run_read(db_chats.admin_user_chat_ids, target)
        out: Dict[str, Any] = {}
        for attempt in range(3):
            async with self._locked(ids):
                again = await self.db.run_read(db_chats.admin_user_chat_ids, target)
                if set(again) <= set(ids) or attempt == 2:
                    out = await self.db.run(
                        db_chats.admin_update_user, conn.user_id, target, k["role"], k["disabled"], k["display_name"],
                        conn.ip,
                    )
                    self._apply(out)
                    break
            ids = sorted(set(ids) | set(again))  # a group gained the target as admin meanwhile: lock it as well
        user = out["res"]["user"]
        if k["disabled"] and not out["noop"]:
            await self.db.run(db_users.revoke_user_sessions, target)  # re-enabling must not revive old cookies
        user["online"] = user["id"] in self._online
        return {"user": user}

    async def _h_admin_reset_password(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        await self.db.run_read(db_users.require_admin, conn.user_id)
        target = await self.db.run_read(db_users.get_user, k["user_id"])
        if target is None:
            raise _Fail("not_found", "user not found")
        problem = auth.check_password_policy(k["new_password"], target["username"], target["display_name"])
        if problem is not None:
            raise _bad(problem, "weak_password")
        pw_hash = await auth.hash_password(k["new_password"])
        done = await self.db.run(db_users.admin_reset_password, conn.user_id, k["user_id"], pw_hash, conn.ip)
        self._revoke_now(done["user_id"], "revoked")
        return {}

    async def _h_admin_settings(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        args = (
            conn.user_id, self.cfg.workspace_name, self.cfg.registration_open, conn.ip, k["workspace_name"],
            k["registration_open"], k["rotate_join_code"],
        )
        if k["workspace_name"] is None and k["registration_open"] is None and k["rotate_join_code"] is None:
            out = await self.db.run_read(db_users.admin_settings, *args)  # `{}` is a read: no write, no audit, no event
        else:
            out = await self.db.run(db_users.admin_settings, *args)
        workspace = out["workspace"]
        if out["changed"]:
            public = {"name": workspace["name"], "registration_open": workspace["registration_open"]}
            self._fanout([_event("ev.workspace", None, public)])
        return {"workspace": workspace}

    async def _h_admin_stats(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        stats = await self.db.run_read(db_users.admin_stats, conn.user_id)
        host = await asyncio.get_running_loop().run_in_executor(None, self._host_stats)
        stats.update(host)
        stats.update(
            online=len(self._online),
            storage_bytes=self.storage_bytes,
            uptime_s=round(time.monotonic() - self._started_at, 1),
            python=platform.python_version(),
            version=__version__,
        )
        return stats

    def _host_stats(self) -> Dict[str, Any]:
        """Blocking host facts for ``admin.stats`` (run in an executor): file size, free disk, LAN URLs."""
        facts: Dict[str, Any] = {"db_bytes": 0, "disk_free_bytes": 0, "urls": []}
        try:
            facts["db_bytes"] = os.path.getsize(str(self.cfg.db_path))
        except OSError:
            log.debug("database size unavailable")
        try:
            facts["disk_free_bytes"] = shutil.disk_usage(str(self.cfg.data_dir)).free
        except OSError:
            log.debug("disk usage unavailable")
        info = util.read_lock_info(str(self.cfg.data_dir)) or {}
        port = info.get("port") or self.cfg.port
        primary, others = util.lan_addresses()
        addresses = ([primary] if primary else []) + list(others)
        facts["urls"] = ["%s://%s:%s/" % (self.cfg.scheme, address, port) for address in addresses]
        return facts

    async def _h_admin_audit(self, conn: _Conn, k: Dict[str, Any]) -> Dict[str, Any]:
        entries = await self.db.run_read(db_users.admin_audit, conn.user_id, 100 if k["limit"] is None else k["limit"])
        return {"entries": entries}

    # ==========================================================================================================
    # Registration, sessions, kicks (called by api.py and the admin handlers)
    # ==========================================================================================================

    async def create_user(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Register an account and broadcast it (SPEC 6.1, 3.2(4)); returns the new ``User``.

        Holds the ``Everyone`` lock around ``db.register_user`` and the fan-out ``ev.user_update`` -> ``ev.chat_members
        {added}`` -> system ``ev.message``.  Raises ``db.RequestError`` exactly as the db function does.
        """
        await self._ensure_started()
        return await asyncio.shield(self._launch(self._create_user(spec)))

    async def _create_user(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        async with self._locked([None]):
            out = await self.db.run(
                db_users.register_user, spec, self.cfg.max_users, str(self.cfg.data_dir), self.cfg.workspace_name,
                self.cfg.registration_open,
            )
            chat_id = out["chat_id"]
            if out["first_user"]:
                self._everyone_id = chat_id
            entry = out["index"]
            self._index[chat_id] = entry
            user = out["user"]
            user["online"] = False
            events = [_event("ev.user_update", None, {"user": user})]
            others = [uid for uid in entry["members"] if uid != user["id"]]
            for event in (db_chats.chat_members_event(chat_id, others, added=[out["member"]]), out["message_event"]):
                if event is not None:
                    events.append(event)
            self._fanout(events)
        if out["first_user"]:
            await asyncio.get_running_loop().run_in_executor(None, auth.clear_setup_code, str(self.cfg.data_dir))
        return user

    def broadcast_user(self, user: Dict[str, Any]) -> None:
        """``ev.user_update`` to every connection (the first login of a never-activated user, SPEC 4.2)."""
        self._fanout([_event("ev.user_update", None, {"user": dict(user)})])

    async def revoke(
        self,
        user_id: int,
        reason: str,
        token_hash: Optional[str] = None,
        except_token_hash: Optional[str] = None,
    ) -> None:
        """Send ``ev.kicked`` and close ``4001`` on the matching connections of ``user_id`` at once (SPEC 6.1)."""
        self._revoke_now(user_id, reason, token_hash, except_token_hash)

    def _revoke_now(
        self,
        user_id: int,
        reason: str,
        token_hash: Optional[str] = None,
        except_token_hash: Optional[str] = None,
    ) -> int:
        """Synchronous core of :meth:`revoke` (the fan-out of ``admin.update_user`` calls it between two events)."""
        kicked = 0
        for conn in list(self._conns.get(user_id, ())):
            if token_hash is not None and conn.token_hash != token_hash:
                continue
            if except_token_hash is not None and conn.token_hash == except_token_hash:
                continue
            self._kick(conn, reason)
            kicked += 1
        return kicked

    def _kick(self, conn: _Conn, reason: str) -> None:
        conn.closing = True
        conn.ws.send_text(util.json_dumps({"t": "ev.kicked", "d": {"reason": reason}}))
        conn.ws.close(CLOSE_UNAUTHORIZED, reason)

    async def revalidate_all(self) -> None:
        """Close the sockets whose session expired or vanished (``revoked``) and those of disabled users (SPEC 2.4)."""
        await self._ensure_started()
        self.counters["revalidate_calls"] += 1
        pairs = sorted({(c.user_id, c.token_hash) for c in self._all if c.token_hash})
        if not pairs:
            return
        statuses = await self.db.run_read(db_users.session_statuses, pairs)
        for conn in list(self._all):
            status = statuses.get(conn.token_hash)
            if status in ("revoked", "disabled") and not conn.closing:
                self._kick(conn, status)

    # ==========================================================================================================
    # Changes made by another process (SPEC 2.4)
    # ==========================================================================================================

    async def external_change(self) -> None:
        """The database changed under us (CLI ``create-admin`` / ``reset-password``): kick, then diff and announce.

        Emits exactly what the same action would have emitted through the hub: a new account -> ``ev.user_update``,
        ``ev.chat_members {added}`` to the other ``Everyone`` members, the system ``ev.message``; a role change ->
        ``ev.user_update`` + ``ev.chat_members {updated}``; any other row change -> ``ev.user_update``.
        """
        await self._ensure_started()
        self.counters["external_change_calls"] += 1
        await self.revalidate_all()
        await asyncio.shield(self._launch(self._diff_external()))

    async def _diff_external(self) -> None:
        had_users = bool(self._users)
        async with self._locked([None]):
            old = self._index.get(self._everyone_id) if self._everyone_id is not None else None
            old_members = dict(old["members"]) if old is not None else {}
            state = await self.db.run_read(
                _external_read, old_members, self._everyone_hi, set(self._fanned_system)
            )
            events: List[Dict[str, Any]] = []
            entry = state["entry"]
            added_ids = [uid for uid in (entry["members"] if entry else {}) if uid not in old_members]
            for user in state["users"]:
                uid = user["id"]
                known = self._users.get(uid)
                if known is not None and known == _fingerprint(user):
                    continue
                events.append(_event("ev.user_update", None, {"user": user}))
                if entry is None or uid not in state["members"]:
                    continue
                member = state["members"][uid]
                if uid in added_ids:
                    chat_event = db_chats.chat_members_event(state["everyone"], list(old_members), added=[member])
                else:
                    everyone = list(entry["members"])
                    chat_event = db_chats.chat_members_event(state["everyone"], everyone, updated=[member])
                if chat_event is not None:
                    events.append(chat_event)
            if entry is not None:
                self._index[state["everyone"]] = entry
                self._everyone_id = state["everyone"]
            events.extend(event for _, event in state["system"])
            self._fanout(events)
            self._everyone_hi = state["hi"]
            self._fanned_system = {i for i in self._fanned_system if i > self._everyone_hi}
        if state["users"] and not had_users:
            await asyncio.get_running_loop().run_in_executor(None, auth.clear_setup_code, str(self.cfg.data_dir))
