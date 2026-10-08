"""Thread-based chat client core. GUI aur CLI dono isko use karte hain."""

import socket
import threading

from .protocol import decode, encode


class ChatClient:
    """Ek background thread me socket padhta hai aur on_event callback maarta hai.

    on_event(dict) kisi bhi thread se call ho sakta hai - GUI me isko
    main thread pe marshal karna zaroori hai.
    """

    def __init__(self, host, port, user, on_event):
        self.host = host
        self.port = int(port)
        self.user = user
        self.on_event = on_event
        self.sock = None
        self.thread = None
        self._running = False
        self._lock = threading.Lock()

    # ---------- lifecycle ----------

    def connect(self, timeout=6.0):
        """Server se judo aur join bhejo. Fail hone pe OSError uthata hai."""
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(None)
        self._running = True
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()
        self.send({"type": "join", "user": self.user})

    def close(self):
        self._running = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ---------- sending ----------

    def send(self, obj):
        with self._lock:
            if not self.sock:
                return False
            try:
                self.sock.sendall(encode(obj))
                return True
            except OSError:
                return False

    def say(self, text):
        return self.send({"type": "msg", "text": text})

    def dm(self, to, text):
        return self.send({"type": "dm", "to": to, "text": text})

    def who(self):
        return self.send({"type": "who"})

    # ---------- receiving ----------

    def _reader(self):
        buf = b""
        try:
            while self._running:
                chunk = self.sock.recv(4096) if self.sock else b""
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        self.on_event(decode(line))
                    except (ValueError, UnicodeDecodeError):
                        continue
        except OSError:
            pass
        finally:
            if self._running:
                self._running = False
                self.on_event({"type": "disconnected"})
