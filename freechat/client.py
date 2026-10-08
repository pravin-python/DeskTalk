"""Thread-based chat client core. GUI aur CLI dono isko use karte hain."""

import socket
import threading
import time

from .protocol import (
    DEAD_AFTER,
    ERR_AUTH,
    ERR_BAD_NAME,
    ERR_NAME_TAKEN,
    PING_INTERVAL,
    decode,
    encode,
)

READ_TICK = 5.0          # recv itne second me timeout hota hai taaki heartbeat check chalta rahe
RECONNECT_MAX_DELAY = 10.0
FATAL_CODES = {ERR_AUTH, ERR_BAD_NAME, ERR_NAME_TAKEN}


class ChatClient:
    """Ek background thread me socket padhta hai aur on_event callback maarta hai.

    on_event(dict) kisi bhi thread se call ho sakta hai - GUI me isko
    main thread pe marshal karna zaroori hai.

    Connection toot jaaye to (auto_reconnect=True pe) khud dobara judta hai aur
    wapas join karke miss hue messages le leta hai. Extra events:
        {"type": "disconnected", "reconnecting": bool, "reason": str}
        {"type": "reconnecting", "attempt": int, "delay": float}
        {"type": "welcome", ..., "rejoined": True}   # reconnect ke baad
    Password galat / naam bura / naam already-use pe reconnect NAHI hota
    (disconnected, reconnecting=False).
    """

    def __init__(self, host, port, user, on_event, password="", auto_reconnect=True):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password or ""
        self.on_event = on_event
        self.auto_reconnect = auto_reconnect
        self.sock = None
        self.thread = None
        self.last_id = 0          # last dekha hua room message id (rejoin pe history ke liye)
        self._welcomed = False    # kabhi welcome mila?
        self._fatal = None        # fatal join error ka text
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------- lifecycle ----------

    def connect(self, timeout=6.0):
        """Server se judo aur join bhejo. Pehla connect fail ho to OSError uthata hai."""
        self._stop.clear()
        self._fatal = None
        sock = self._open(timeout)
        self.thread = threading.Thread(target=self._run, args=(sock,), daemon=True)
        self.thread.start()

    def close(self):
        self._stop.set()
        with self._lock:
            sock, self.sock = self.sock, None
        self._shutdown(sock)

    @property
    def connected(self):
        return self.sock is not None

    # ---------- sending ----------

    def send(self, obj):
        with self._lock:
            sock = self.sock
            if not sock:
                return False
            try:
                sock.sendall(encode(obj))
                return True
            except OSError:
                # adhura likha ho sakta hai - stream kharab, socket band karo (reconnect hoga)
                self.sock = None
                self._shutdown(sock)
                return False

    def say(self, text):
        return self.send({"type": "msg", "text": text})

    def dm(self, to, text):
        return self.send({"type": "dm", "to": to, "text": text})

    def who(self):
        return self.send({"type": "who"})

    # ---------- internals ----------

    @staticmethod
    def _shutdown(sock):
        if not sock:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _emit(self, ev):
        if not self._stop.is_set():
            self.on_event(ev)

    def _open(self, timeout=6.0):
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(READ_TICK)
        with self._lock:
            self.sock = sock
        self.send({
            "type": "join",
            "user": self.user,
            "password": self.password,
            "since_id": self.last_id,
        })
        return sock

    def _drop(self, sock):
        with self._lock:
            if self.sock is sock:
                self.sock = None
        self._shutdown(sock)

    def _run(self, sock):
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
                        "type": "disconnected",
                        "reconnecting": False,
                        "reason": self._fatal or "Server se connection toot gaya.",
                    })
                    return
                if joined:
                    attempt = 0
                self._emit({
                    "type": "disconnected",
                    "reconnecting": True,
                    "reason": "Connection toot gaya.",
                })

            attempt += 1
            delay = min(RECONNECT_MAX_DELAY, float(2 ** min(attempt - 1, 4)))
            self._emit({"type": "reconnecting", "attempt": attempt, "delay": delay})
            if self._stop.wait(delay):
                return
            try:
                sock = self._open()
            except OSError:
                sock = None

    def _pump(self, sock):
        """Ek connection ka read loop. True return karta hai agar is session me welcome mila."""
        buf = b""
        joined = False
        last_rx = last_ping = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_rx > DEAD_AFTER:
                break  # server se koi awaaz nahi - connection mara hua hai
            if now - last_ping >= PING_INTERVAL:
                last_ping = now
                self.send({"type": "ping"})
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
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
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                if self._handle(ev):
                    if ev.get("type") == "welcome":
                        joined = True
        return joined

    def _handle(self, ev):
        """Event ko process karo. True = on_event ko forward kiya gaya."""
        kind = ev.get("type")
        if kind == "pong":
            return False

        if kind == "msg" and isinstance(ev.get("id"), int):
            self.last_id = max(self.last_id, ev["id"])

        elif kind == "welcome":
            ev["rejoined"] = self._welcomed
            self._welcomed = True

        elif kind == "error" and ev.get("code") in FATAL_CODES:
            if ev["code"] == ERR_NAME_TAKEN and self._welcomed:
                return False  # purana (mara hua) connection abhi server pe baaki hai - retry hoga
            self._fatal = ev.get("text") or "Join nahi ho paya."

        self._emit(ev)
        return True
