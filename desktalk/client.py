"""Thread-based chat client core, shared by the GUI and the CLI."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Optional

from .errors import ProtocolError
from .protocol import (
    DEAD_AFTER,
    ERR_AUTH,
    ERR_BAD_NAME,
    ERR_NAME_TAKEN,
    EV_DISCONNECTED,
    EV_RECONNECTING,
    MAX_LINE,
    PING_INTERVAL,
    T_DM,
    T_ERROR,
    T_JOIN,
    T_MSG,
    T_PING,
    T_PONG,
    T_WELCOME,
    T_WHO,
    Message,
    decode,
    encode,
)

log = logging.getLogger(__name__)

READ_TICK = 5.0            # recv timeout, so the heartbeat checks keep running
RECONNECT_MAX_DELAY = 10.0
FATAL_CODES = frozenset({ERR_AUTH, ERR_BAD_NAME, ERR_NAME_TAKEN})

EventHandler = Callable[[Message], None]


class ChatClient:
    """Reads the socket on a background thread and reports through ``on_event``.

    ``on_event(dict)`` is called from that thread - a GUI must marshal it onto
    its own thread. An exception raised by ``on_event`` is logged and swallowed
    so a buggy handler can never kill the connection.

    If the connection drops (and ``auto_reconnect`` is on) the client reconnects
    by itself, rejoins and asks the server only for the messages it missed.
    Extra events it produces:

    * ``{"type": "disconnected", "reconnecting": bool, "reason": str}``
    * ``{"type": "reconnecting", "attempt": int, "delay": float}``
    * ``{"type": "welcome", ..., "rejoined": True}`` after a reconnect

    A wrong password, a bad name or a name already in use are *fatal*: no
    reconnect, ``disconnected`` with ``reconnecting=False``.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        on_event: EventHandler,
        password: str = "",
        auto_reconnect: bool = True,
    ) -> None:
        self.host = host
        self.port = int(port)  # ValueError for junk - callers validate first
        self.user = user
        self.password = password or ""
        self.on_event = on_event
        self.auto_reconnect = auto_reconnect
        self.sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None
        self.last_id = 0                    # newest room message id seen (for rejoin catch-up)
        self._welcomed = False              # has a welcome ever arrived?
        self._fatal: Optional[str] = None   # text of a fatal join error
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------- lifecycle ----------

    def connect(self, timeout: float = 6.0) -> None:
        """Connect and join. The first attempt raises OSError on failure."""
        self._stop.clear()
        self._fatal = None
        sock = self._open(timeout)
        self.thread = threading.Thread(target=self._run, args=(sock,), name="desktalk-client", daemon=True)
        self.thread.start()

    def close(self) -> None:
        """Disconnect for good. Idempotent."""
        self._stop.set()
        with self._lock:
            sock, self.sock = self.sock, None
        self._shutdown(sock)

    @property
    def connected(self) -> bool:
        return self.sock is not None

    # ---------- sending ----------

    def send(self, obj: Message) -> bool:
        """Send one message. False if there is no live connection or the send failed."""
        try:
            data = encode(obj)
        except ProtocolError as exc:
            log.warning("not sending unencodable message: %s", exc)
            return False
        with self._lock:
            sock = self.sock
            if sock is None:
                return False
            try:
                sock.sendall(data)
                return True
            except OSError as exc:
                # A partial write corrupts the stream: drop the socket so we reconnect cleanly.
                log.debug("send failed (%s); dropping socket", exc)
                self.sock = None
                self._shutdown(sock)
                return False

    def say(self, text: str) -> bool:
        return self.send({"type": T_MSG, "text": text})

    def dm(self, to: str, text: str) -> bool:
        return self.send({"type": T_DM, "to": to, "text": text})

    def who(self) -> bool:
        return self.send({"type": T_WHO})

    # ---------- internals ----------

    @staticmethod
    def _shutdown(sock: Optional[socket.socket]) -> None:
        if sock is None:
            return
        for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
            try:
                action()
            except OSError:
                pass

    def _emit(self, ev: Message) -> None:
        if self._stop.is_set():
            return
        try:
            self.on_event(ev)
        except Exception:  # noqa: BLE001 - a UI bug must not kill the network thread
            log.exception("on_event handler raised for %r event", ev.get("type"))

    def _open(self, timeout: float = 6.0) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except UnicodeError as exc:  # e.g. host "a..b" fails IDNA encoding
            raise OSError("Invalid server address {!r}: {}".format(self.host, exc))
        sock.settimeout(READ_TICK)
        with self._lock:
            self.sock = sock
        self.send({
            "type": T_JOIN,
            "user": self.user,
            "password": self.password,
            "since_id": self.last_id,
        })
        return sock

    def _drop(self, sock: socket.socket) -> None:
        with self._lock:
            if self.sock is sock:
                self.sock = None
        self._shutdown(sock)

    def _run(self, sock: socket.socket) -> None:
        """Thread entry point; guarantees the UI hears about a crash."""
        try:
            self._session_loop(sock)
        except Exception as exc:  # noqa: BLE001
            log.exception("client thread crashed")
            self._emit({
                "type": EV_DISCONNECTED,
                "reconnecting": False,
                "reason": "Internal error: {}".format(exc),
            })
            self.close()

    def _session_loop(self, sock: Optional[socket.socket]) -> None:
        """Read loop + reconnect loop."""
        attempt = 0
        while not self._stop.is_set():
            if sock is not None:
                joined = self._pump(sock)
                self._drop(sock)
                sock = None
                if self._stop.is_set():
                    return
                if self._fatal or not self.auto_reconnect:
                    self._emit({
                        "type": EV_DISCONNECTED,
                        "reconnecting": False,
                        "reason": self._fatal or "Server connection lost.",
                    })
                    return
                if joined:
                    attempt = 0
                self._emit({"type": EV_DISCONNECTED, "reconnecting": True, "reason": "Connection lost."})

            attempt += 1
            delay = min(RECONNECT_MAX_DELAY, float(2 ** min(attempt - 1, 4)))
            self._emit({"type": EV_RECONNECTING, "attempt": attempt, "delay": delay})
            if self._stop.wait(delay):
                return
            try:
                sock = self._open()
            except OSError as exc:
                log.debug("reconnect attempt %d failed: %s", attempt, exc)
                sock = None

    def _pump(self, sock: socket.socket) -> bool:
        """Read one connection until it ends. True if a welcome arrived on it."""
        buf = b""
        joined = False
        last_rx = last_ping = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_rx > DEAD_AFTER:
                log.info("server silent for %.0fs - treating connection as dead", DEAD_AFTER)
                break
            if now - last_ping >= PING_INTERVAL:
                last_ping = now
                self.send({"type": T_PING})
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                log.debug("recv failed: %s", exc)
                break
            if not chunk:
                break
            last_rx = time.monotonic()
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    ev = decode(line)
                except ProtocolError:
                    continue
                if self._handle(ev) and ev.get("type") == T_WELCOME:
                    joined = True
            if len(buf) > MAX_LINE * 2:  # a "line" that never ends: the peer is broken
                log.warning("server sent an oversized line - dropping connection")
                break
        return joined

    def _handle(self, ev: Message) -> bool:
        """Process one server event. True if it was forwarded to the UI."""
        kind = ev.get("type")
        if kind == T_PONG:
            return False

        if kind == T_MSG and isinstance(ev.get("id"), int):
            self.last_id = max(self.last_id, ev["id"])

        elif kind == T_WELCOME:
            ev["rejoined"] = self._welcomed
            self._welcomed = True

        elif kind == T_ERROR:
            code = ev.get("code")
            if isinstance(code, str) and code in FATAL_CODES:
                if code == ERR_NAME_TAKEN and self._welcomed:
                    return False  # our own dead connection is still registered server-side; retry
                self._fatal = str(ev.get("text") or "Could not join room.")

        self._emit(ev)
        return True
