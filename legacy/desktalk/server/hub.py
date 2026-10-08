"""The chat hub: join/auth, message routing, history, presence."""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any, Callable, Dict, List, Optional, Set

from ..errors import ProtocolError, StoreError
from ..protocol import (
    ERR_AUTH,
    ERR_BAD_NAME,
    ERR_NAME_TAKEN,
    HISTORY_ON_JOIN,
    HISTORY_ON_REJOIN,
    IDLE_TIMEOUT,
    MAX_LINE,
    T_DM,
    T_ERROR,
    T_JOIN,
    T_MSG,
    T_PING,
    T_PONG,
    T_SYSTEM,
    T_USERS,
    T_WELCOME,
    T_WHO,
    Message,
    clean_name,
    clean_text,
    decode,
    encode,
    now_ts,
    scrub,
)
from .connection import FLUSH_TIMEOUT, Connection

log = logging.getLogger(__name__)

AUTH_DELAY = 1.0   # seconds to stall before answering a wrong password (slows brute force)

Handler = Callable[[Connection, Message], None]


class ChatServer:
    """Owns the set of connections and routes messages between them.

    ``store`` must offer ``add / last_id / recent / close`` (see desktalk.store).
    ``password=None`` means an open room.
    """

    def __init__(self, room_name: str, port: int, store: Any, password: Optional[str] = None) -> None:
        self.room_name = room_name
        self.port = port
        self.store = store
        self.password = password or None
        self.clients: Set[Connection] = set()
        self._handlers: Dict[str, Handler] = {
            T_PING: self._on_ping,
            T_MSG: self._on_msg,
            T_DM: self._on_dm,
            T_WHO: self._on_who,
        }

    # ---------- queries ----------

    @property
    def locked(self) -> bool:
        return self.password is not None

    def users(self) -> List[str]:
        return sorted(c.user for c in self.clients if c.user)

    def find(self, user: str) -> Optional[Connection]:
        low = user.lower()
        for c in self.clients:
            if c.user and c.user.lower() == low:
                return c
        return None

    # ---------- sending ----------

    def broadcast(self, obj: Message, skip: Optional[Connection] = None) -> None:
        """Send to every joined client (encoding only once)."""
        try:
            data = encode(obj)
        except ProtocolError as exc:
            log.error("broadcast dropped, cannot encode %r: %s", obj.get("type"), exc)
            return
        for c in list(self.clients):
            if c is not skip and c.user:
                c.send_bytes(data)

    def push_users(self) -> None:
        self.broadcast({"type": T_USERS, "users": self.users()})

    @staticmethod
    def _error(conn: Connection, text: str, code: Optional[str] = None) -> None:
        obj: Message = {"type": T_ERROR, "text": text}
        if code:
            obj["code"] = code
        conn.send(obj)

    def _reject_join(self, conn: Connection, code: str, text: str) -> None:
        """Tell the client why joining failed, then close the connection."""
        self._error(conn, text, code)
        conn.finish()

    # ---------- connection lifecycle ----------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """asyncio.start_server callback - one invocation per client."""
        conn = Connection(writer)
        self.clients.add(conn)
        log.info("connect %s", conn.addr)
        try:
            await self._read_loop(conn, reader)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            log.debug("connection error for %s: %r", conn.label, exc)
        except Exception:  # noqa: BLE001 - one client must never take the server down
            log.exception("unexpected error for %s", conn.label)
        finally:
            await self._disconnect(conn)

    async def _read_loop(self, conn: Connection, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                log.info("idle timeout: %s", conn.label)
                return
            except ValueError:
                # Line exceeded the stream limit; the stream can no longer be trusted.
                self._error(conn, "Message is too large.")
                conn.finish()
                return
            if not line:
                return  # peer closed
            if len(line) > MAX_LINE:
                self._error(conn, "Message is too large.")
                continue
            try:
                msg = decode(line)
            except ProtocolError as exc:
                self._error(conn, str(exc))
                continue
            try:
                await self._dispatch(conn, msg)
            except Exception:  # noqa: BLE001 - a handler bug must not drop the client
                log.exception("handler failed for %s (type=%r)", conn.label, msg.get("type"))
                self._error(conn, "Internal server error, please try again.")

    async def _disconnect(self, conn: Connection) -> None:
        self.clients.discard(conn)
        try:
            conn.finish()
            await conn.wait_closed()
            if conn.user:
                log.info("left %s", conn.user)
                self.broadcast({"type": T_SYSTEM, "text": conn.user + " left the chat.", "ts": now_ts()})
                self.push_users()
        except asyncio.CancelledError:
            conn.abort()
            raise
        except Exception:  # noqa: BLE001
            log.exception("error while disconnecting %s", conn.label)

    async def shutdown(self, reason: str = "Server is shutting down.") -> None:
        """Tell everyone, flush, and close all connections."""
        conns = list(self.clients)
        self.broadcast({"type": T_SYSTEM, "text": reason, "ts": now_ts()})
        for c in conns:
            c.finish()
        if conns:
            await asyncio.wait([c.task for c in conns], timeout=FLUSH_TIMEOUT)
        for c in conns:
            c.abort()

    # ---------- message routing ----------

    async def _dispatch(self, conn: Connection, msg: Message) -> None:
        kind = msg.get("type")
        if kind == T_JOIN:
            await self._on_join(conn, msg)
            return
        if not conn.user:
            self._error(conn, "Please join first.")
            return
        handler = self._handlers.get(kind) if isinstance(kind, str) else None
        if handler is None:
            self._error(conn, "Unknown message type.")
            return
        handler(conn, msg)

    def _on_ping(self, conn: Connection, msg: Message) -> None:
        conn.send({"type": T_PONG, "ts": now_ts()})

    def _on_who(self, conn: Connection, msg: Message) -> None:
        conn.send({"type": T_USERS, "users": self.users()})

    def _on_msg(self, conn: Connection, msg: Message) -> None:
        try:
            text = clean_text(msg.get("text"))
        except ProtocolError as exc:
            self._error(conn, str(exc))
            return
        if not text:
            return
        ts = now_ts()
        out: Message = {"type": T_MSG, "user": conn.user, "text": text, "ts": ts}
        try:
            out["id"] = self.store.add(conn.user, text, ts)
        except StoreError as exc:
            log.warning("history save failed: %s", exc)  # still deliver, just without an id
        self.broadcast(out)

    def _on_dm(self, conn: Connection, msg: Message) -> None:
        to = msg.get("to")
        if not isinstance(to, str):
            self._error(conn, "Recipient name required for DM.")
            return
        try:
            text = clean_text(msg.get("text"))
        except ProtocolError as exc:
            self._error(conn, str(exc))
            return
        if not text:
            return
        to = scrub(to).strip()
        target = self.find(to)
        if target is None:
            self._error(conn, "No user named '" + to + "' is online.")
            return
        payload: Message = {"type": T_DM, "user": conn.user, "to": target.user, "text": text, "ts": now_ts()}
        target.send(payload)
        if target is not conn:
            conn.send(payload)

    # ---------- join ----------

    def _password_ok(self, msg: Message) -> bool:
        if self.password is None:
            return True
        given = msg.get("password")
        given = scrub(given) if isinstance(given, str) else ""
        return hmac.compare_digest(given.encode("utf-8"), scrub(self.password).encode("utf-8"))

    def _history_for(self, since_id: int) -> List[Message]:
        """Room history for a (re)joining client; [] if the store fails."""
        try:
            if since_id < 0 or since_id > self.store.last_id():
                since_id = 0  # client id is stale/invalid (e.g. db was reset) - give fresh history
            limit = HISTORY_ON_REJOIN if since_id else HISTORY_ON_JOIN
            return self.store.recent(limit, since_id)
        except StoreError as exc:
            log.warning("history read failed: %s", exc)
            return []

    @staticmethod
    def _parse_since(raw: Any) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            return 0
        return raw

    async def _on_join(self, conn: Connection, msg: Message) -> None:
        if conn.user:
            self._error(conn, "Already joined.")
            return

        if not self._password_ok(msg):
            log.warning("wrong password from %s", conn.addr)
            await asyncio.sleep(AUTH_DELAY)
            self._reject_join(conn, ERR_AUTH, "Incorrect password (or password required).")
            return

        try:
            name = clean_name(msg.get("user"))
        except ProtocolError as exc:
            self._reject_join(conn, ERR_BAD_NAME, str(exc))
            return
        if self.find(name):
            self._reject_join(conn, ERR_NAME_TAKEN, "Name '" + name + "' is already taken, try another.")
            return

        history = self._history_for(self._parse_since(msg.get("since_id")))

        conn.user = name  # no await between find() and this line, so names stay unique
        log.info("join %s from %s", name, conn.addr)
        conn.send({
            "type": T_WELCOME,
            "room": self.room_name,
            "user": name,
            "users": self.users(),
            "ts": now_ts(),
        })
        for old in history:
            conn.send(old)
        self.broadcast({"type": T_SYSTEM, "text": name + " joined the chat.", "ts": now_ts()}, skip=conn)
        self.push_users()
