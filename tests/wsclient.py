"""WebSocket test client and the ``ChatSession`` helper of SPEC section 12 (shared by every suite that talks to a server).

Layers
------
* :func:`encode_frame` / :func:`fragment_frames` - build client frames, including deliberately broken ones
  (unmasked, reserved bits, non-minimal or lying length fields, raw bytes).
* :class:`WebSocketClient` - one blocking socket plus a reader thread.  It answers server pings (``auto_pong``),
  reassembles fragmented messages, completes the close handshake and records everything the server did wrong
  (``violations``: a masked frame, reserved bits, a non-minimal length ...).
* :class:`ChatSession` - the HTTP side (``register``/``login``/``logout``/``upload`` through ``http.client``, always
  with ``X-Requested-With: desktalk``, session cookie kept) and the protocol side of SPEC 7 on top of a
  ``WebSocketClient``:

  ``request(t, d, timeout=5) -> res frame`` (never raises on ``ok:false``), ``send(t, d)`` (no ``id``: fire and forget),
  ``events`` (append-only list of ``(seq, frame)``), ``mark() -> int``, ``wait_event(name, pred, timeout, since)``,
  ``expect_none(name, pred, since)``, ``raw(text_or_bytes)``, ``close_code``, ``auto_pong``.

Sequence numbers
----------------
Every JSON frame the server sends (``res`` and ``ev.*`` alike) gets the next number of ONE counter, the moment it
arrives, starting with the socket's first frame.  ``mark()`` returns the number the NEXT frame will get, so
``wait_event(..., since=mark())`` sees only what arrives afterwards.  ``Frame.seq`` tells the order of an event relative
to a ``res`` ("events before ``res``", SPEC 7.6(4)); the close of the socket takes a number too (``close_seq``), which is
how "``ev.kicked`` precedes the 4001 close" is asserted.  Events are queued from the moment the socket opens, so events
that precede ``res`` are never lost, and ``ev.ready`` is always the first one.

``expect_none`` is the barrier of SPEC 7.6(4): it sends a ``ping`` and waits for its ``res``; everything that was enqueued
for this connection before then has arrived, so a matching event after ``since`` is a real violation.  Every
"must NOT receive" assertion is written with it, never with ``time.sleep``.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_TIMEOUT = 5.0
SESSION_COOKIE = "fc_session"
CSRF_HEADER = ("X-Requested-With", "desktalk")
_VALID_OPCODES = frozenset((OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG))

Headers = Mapping[str, Optional[str]]


class HandshakeError(Exception):
    """The server answered the WebSocket upgrade with something other than ``101`` (or a bad accept key)."""

    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        super().__init__("WebSocket handshake refused: HTTP %d %s" % (status, body[:200].decode("utf-8", "replace")))
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def code(self) -> Optional[str]:
        """The ``error.code`` of a JSON refusal body, if there is one."""
        try:
            return json.loads(self.body.decode("utf-8"))["error"]["code"]
        except (ValueError, KeyError, TypeError):
            return None


class ClientTimeout(AssertionError):
    """A wait ran out of time (an ``AssertionError`` so unittest reports a failure, not an error)."""


class ConnectionClosedError(AssertionError):
    """The socket is closed (or the server went away) while the test still needed it."""


# --------------------------------------------------------------------------------------------------------------
# Frame building
# --------------------------------------------------------------------------------------------------------------


def mask_bytes(data: bytes, key: bytes) -> bytes:
    """XOR ``data`` with the repeating 4-byte ``key`` (big-integer XOR: fast even for megabyte payloads)."""
    n = len(data)
    if n == 0:
        return b""
    stream = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(n, "big")


def encode_frame(
    opcode: int,
    payload: bytes = b"",
    *,
    fin: bool = True,
    masked: bool = True,
    rsv: int = 0,
    length_form: Optional[str] = None,
    declared_length: Optional[int] = None,
    key: Optional[bytes] = None,
) -> bytes:
    """One client frame.  The keyword arguments are the misbehaviours of the negative tests.

    ``masked=False`` omits the mask (RFC 6455: the server must close 1002); ``rsv`` sets the reserved bits (1..7);
    ``length_form`` forces the length encoding (``"7"``, ``"16"`` or ``"64"``; a non-minimal one must be refused with
    1002); ``declared_length`` writes a length different from ``len(payload)`` (a huge value with no payload tests that
    the server validates the length before it reads, SPEC 6); ``key`` pins the mask key.
    """
    length = len(payload) if declared_length is None else declared_length
    form = length_form or ("7" if length < 126 else "16" if length < 65536 else "64")
    first = (0x80 if fin else 0) | ((rsv & 7) << 4) | (opcode & 0x0F)
    mask_bit = 0x80 if masked else 0
    if form == "7":
        if length > 125:
            raise ValueError("a 7-bit length cannot express %d" % length)
        head = struct.pack("!BB", first, mask_bit | length)
    elif form == "16":
        head = struct.pack("!BBH", first, mask_bit | 126, length)
    elif form == "64":
        head = struct.pack("!BBQ", first, mask_bit | 127, length)
    else:
        raise ValueError("length_form must be '7', '16' or '64'")
    if not masked:
        return head + payload
    mask_key = key if key is not None else os.urandom(4)
    return head + mask_key + mask_bytes(payload, mask_key)


def fragment_frames(opcode: int, payload: bytes, sizes: Sequence[int], **options: Any) -> List[bytes]:
    """Split one message into a first frame (``opcode``, ``FIN=0``), continuations and a last ``FIN=1`` frame.

    ``sizes`` are the lengths of the leading fragments; the remainder of ``payload`` is the last one.  ``options`` are
    passed to :func:`encode_frame` (e.g. ``masked=False``).
    """
    frames: List[bytes] = []
    start = 0
    for index, size in enumerate(sizes):
        chunk = payload[start:start + size]
        start += size
        frames.append(encode_frame(opcode if index == 0 else OP_CONT, chunk, fin=False, **options))
    frames.append(encode_frame(opcode if not sizes else OP_CONT, payload[start:], fin=True, **options))
    return frames


def close_payload(code: int, reason: str = "") -> bytes:
    """The payload of a close frame."""
    return struct.pack("!H", code) + reason.encode("utf-8")


# --------------------------------------------------------------------------------------------------------------
# The socket client
# --------------------------------------------------------------------------------------------------------------


def _merge_headers(defaults: List[Tuple[str, str]], overrides: Optional[Headers]) -> List[Tuple[str, str]]:
    """Apply ``overrides`` (case-insensitive name; ``None`` removes the header) to ``defaults``; extras are appended."""
    result = list(defaults)
    for name, value in (overrides or {}).items():
        lowered = name.lower()
        result = [(n, v) for n, v in result if n.lower() != lowered]
        if value is not None:
            result.append((name, value))
    return result


def _parse_head(head: bytes) -> Tuple[int, Dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        key = name.strip().lower()
        headers[key] = headers[key] + ", " + value.strip() if key in headers else value.strip()
    return status, headers


class WebSocketClient:
    """A connected WebSocket: blocking sends, a reader thread, optional per-message callback.

    Without ``on_message`` completed messages are queued for :meth:`recv_message`.  The reader answers pings while
    ``auto_pong`` is true, echoes a received close frame and ends at EOF.  Observations for assertions:

    * ``close_code`` / ``close_reason`` - ``None`` while open; the received close code once there is one; ``1005`` for
      a close frame without status and ``1006`` for a TCP close without any close frame (the browser conventions).
    * ``pings`` / ``pongs`` - payloads received; ``violations`` - rule breaks by the server (masked frame, reserved
      bits, fragmented or oversized control frame, non-minimal length, unknown opcode).
    """

    def __init__(
        self,
        sock: socket.socket,
        leftover: bytes = b"",
        on_message: Optional[Callable[[int, bytes], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        auto_pong: bool = True,
    ) -> None:
        self.sock = sock
        self.auto_pong = auto_pong
        self.pings: List[bytes] = []
        self.pongs: List[bytes] = []
        self.violations: List[str] = []
        self.last_rx = time.monotonic()
        self.status = 101
        self.response_headers: Dict[str, str] = {}
        self._buf = leftover
        self._on_message = on_message
        self._on_close = on_close
        self._send_lock = threading.Lock()
        self._state = threading.Condition()
        self._received_close: Optional[Tuple[int, str]] = None
        self._sent_close = False
        self._ended = False
        self._inbox: List[Tuple[int, bytes]] = []
        self._thread = threading.Thread(target=self._read_loop, name="wsclient-reader", daemon=True)

    # ---- opening -----------------------------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        port: int,
        *,
        host: str = "127.0.0.1",
        path: str = "/ws",
        cookie: Optional[str] = None,
        origin: Optional[str] = None,
        headers: Optional[Headers] = None,
        on_message: Optional[Callable[[int, bytes], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        auto_pong: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> "WebSocketClient":
        """Connect and upgrade.  ``headers`` overrides or (value ``None``) removes any handshake header, which is how
        the negative handshake tests are written; a refusal raises :class:`HandshakeError` with the status."""
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            defaults = [
                ("Host", "%s:%d" % (host, port)),
                ("Upgrade", "websocket"),
                ("Connection", "Upgrade"),
                ("Sec-WebSocket-Version", "13"),
                ("Sec-WebSocket-Key", key),
            ]
            if cookie is not None:
                defaults.append(("Cookie", cookie))
            if origin is not None:
                defaults.append(("Origin", origin))
            merged = _merge_headers(defaults, headers)
            sent_key = next((v for n, v in merged if n.lower() == "sec-websocket-key"), "")
            lines = ["GET %s HTTP/1.1" % path] + ["%s: %s" % kv for kv in merged]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionClosedError("the server closed the connection during the WebSocket handshake")
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            status, response_headers = _parse_head(head)
            if status != 101:
                length = int(response_headers.get("content-length", "0") or 0)
                while len(rest) < length:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                raise HandshakeError(status, response_headers, rest[:length])
            expected = base64.b64encode(hashlib.sha1(sent_key.encode("ascii") + GUID).digest()).decode("ascii")
            if response_headers.get("sec-websocket-accept") != expected:
                raise HandshakeError(status, response_headers, b"bad Sec-WebSocket-Accept")
        except BaseException:
            sock.close()
            raise
        sock.settimeout(None)
        client = cls(sock, rest, on_message, on_close, auto_pong)
        client.response_headers = response_headers
        client._thread.start()
        return client

    # ---- sending -----------------------------------------------------------------------------------------------

    def send_raw(self, data: bytes, ignore_errors: bool = False) -> None:
        """Write bytes to the socket as they are (hand-built or deliberately broken frames).

        ``ignore_errors`` swallows ``OSError``: the server may already have closed (for example 1009 before it read
        the end of an oversized message), which the test then asserts through ``close_code``.
        """
        try:
            with self._send_lock:
                self.sock.sendall(data)
        except OSError as exc:
            if not ignore_errors:
                raise ConnectionClosedError("cannot send: %s" % type(exc).__name__)

    def send_frame(self, opcode: int, payload: bytes = b"", ignore_errors: bool = False, **options: Any) -> None:
        """Send one frame built by :func:`encode_frame` (``options`` are its misbehaviour keywords)."""
        self.send_raw(encode_frame(opcode, payload, **options), ignore_errors)

    def send_text(self, text: str, **options: Any) -> None:
        self.send_frame(OP_TEXT, text.encode("utf-8"), **options)

    def send_binary(self, data: bytes, **options: Any) -> None:
        self.send_frame(OP_BINARY, data, **options)

    def send_fragments(self, text: str, sizes: Sequence[int], ping_between: bool = False, **options: Any) -> None:
        """Send ``text`` as several fragments; ``ping_between`` interleaves a ping (allowed between fragments)."""
        frames = fragment_frames(OP_TEXT, text.encode("utf-8"), sizes, **options)
        for index, frame in enumerate(frames):
            self.send_raw(frame)
            if ping_between and index < len(frames) - 1:
                self.send_frame(OP_PING, b"between")

    def ping(self, payload: bytes = b"") -> None:
        self.send_frame(OP_PING, payload)

    def send_close(self, code: Optional[int] = 1000, reason: str = "") -> None:
        """Send a close frame (``code=None`` sends one without a status)."""
        with self._state:
            self._sent_close = True
        self.send_frame(OP_CLOSE, b"" if code is None else close_payload(code, reason), ignore_errors=True)

    # ---- receiving ---------------------------------------------------------------------------------------------

    def recv_message(self, timeout: float = DEFAULT_TIMEOUT) -> Tuple[int, bytes]:
        """The next complete data message ``(opcode, payload)`` (only without ``on_message``)."""
        with self._state:
            if not self._state.wait_for(lambda: self._inbox or self._ended, timeout):
                raise ClientTimeout("no message within %.1f s" % timeout)
            if self._inbox:
                return self._inbox.pop(0)
        raise ConnectionClosedError("the socket closed (code %s) before a message arrived" % self.close_code)

    def wait_pong(self, payload: Optional[bytes] = None, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """Wait until a pong (with this payload, when given) has arrived."""
        with self._state:
            ok = self._state.wait_for(
                lambda: any(payload is None or p == payload for p in self.pongs) or self._ended, timeout
            )
            matching = [p for p in self.pongs if payload is None or p == payload]
        if matching:
            return matching[0]
        if not ok:
            raise ClientTimeout("no pong within %.1f s" % timeout)
        raise ConnectionClosedError("the socket closed before the pong arrived")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_frame(self) -> Tuple[bool, int, bytes]:
        b1, b2 = self._recv_exact(2)
        fin, rsv, opcode = bool(b1 & 0x80), (b1 >> 4) & 7, b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
            if length < 126:
                self.violations.append("non-minimal 16-bit length")
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
            if length < 65536:
                self.violations.append("non-minimal 64-bit length")
        key = self._recv_exact(4) if b2 & 0x80 else None
        if key is not None:
            self.violations.append("masked server frame")
        payload = self._recv_exact(length)
        if key is not None:
            payload = mask_bytes(payload, key)
        if rsv:
            self.violations.append("reserved bits set")
        if opcode not in _VALID_OPCODES:
            self.violations.append("unknown opcode %d" % opcode)
        if opcode >= OP_CLOSE and (not fin or length > 125):
            self.violations.append("fragmented or oversized control frame")
        return fin, opcode, payload

    def _read_loop(self) -> None:
        message_opcode: Optional[int] = None
        parts: List[bytes] = []
        try:
            while True:
                fin, opcode, payload = self._read_frame()
                self.last_rx = time.monotonic()
                if opcode == OP_PING:
                    with self._state:
                        self.pings.append(payload)
                        self._state.notify_all()
                    if self.auto_pong:
                        self.send_frame(OP_PONG, payload, ignore_errors=True)
                elif opcode == OP_PONG:
                    with self._state:
                        self.pongs.append(payload)
                        self._state.notify_all()
                elif opcode == OP_CLOSE:
                    self._on_close_frame(payload)
                elif opcode in (OP_TEXT, OP_BINARY):
                    message_opcode, parts = opcode, [payload]
                elif opcode == OP_CONT:
                    parts.append(payload)
                if opcode in (OP_TEXT, OP_BINARY, OP_CONT) and fin and message_opcode is not None:
                    self._deliver(message_opcode, b"".join(parts))
                    message_opcode, parts = None, []
        except (EOFError, OSError, struct.error):
            pass
        finally:
            self._finish()

    def _on_close_frame(self, payload: bytes) -> None:
        code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
        reason = payload[2:].decode("utf-8", "replace")
        with self._state:
            first = self._received_close is None
            if first:
                self._received_close = (code, reason)
            answer = not self._sent_close
            self._sent_close = True
            self._state.notify_all()
        if first and answer:
            self.send_frame(OP_CLOSE, payload[:2], ignore_errors=True)

    def _deliver(self, opcode: int, data: bytes) -> None:
        if self._on_message is not None:
            self._on_message(opcode, data)
            return
        with self._state:
            self._inbox.append((opcode, data))
            self._state.notify_all()

    def _finish(self) -> None:
        with self._state:
            self._ended = True
            self._state.notify_all()
        if self._on_close is not None:
            self._on_close()

    # ---- closing -----------------------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once the connection has ended (EOF or reset)."""
        return self._ended

    @property
    def close_code(self) -> Optional[int]:
        with self._state:
            if self._received_close is not None:
                return self._received_close[0]
            return 1006 if self._ended else None

    @property
    def close_reason(self) -> Optional[str]:
        with self._state:
            return self._received_close[1] if self._received_close is not None else None

    def wait_closed(self, timeout: float = DEFAULT_TIMEOUT) -> int:
        """Wait for the connection to end; returns :attr:`close_code`."""
        with self._state:
            if not self._state.wait_for(lambda: self._ended, timeout):
                raise ClientTimeout("the server did not close the connection within %.1f s" % timeout)
        code = self.close_code
        assert code is not None
        return code

    def close(self, code: Optional[int] = 1000, reason: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
        """Close handshake: send a close frame, wait for the server's, then release the socket."""
        if not self._ended:
            self.send_close(code, reason)
            with self._state:
                self._state.wait_for(lambda: self._ended, timeout)
        self.abort()

    def abort(self) -> None:
        """Drop the TCP connection without a close frame (the "browser tab died" case)."""
        for action in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                action()
            except OSError:
                pass


# --------------------------------------------------------------------------------------------------------------
# HTTP side
# --------------------------------------------------------------------------------------------------------------


class HttpResponse:
    """A finished HTTP response: ``status``, lower-case ``headers`` (repeated ones joined with ``", "``),
    ``set_cookies`` (every ``Set-Cookie`` value), ``body``."""

    def __init__(self, status: int, headers: Dict[str, str], set_cookies: List[str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.set_cookies = set_cookies
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def json(self) -> Any:
        """The parsed JSON body, or ``None`` when the body is empty or not JSON."""
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError:
            return None

    @property
    def error(self) -> Optional[Dict[str, Any]]:
        """The ``error`` object of a failed ``/api/*`` call, else ``None``."""
        body = self.json
        err = body.get("error") if isinstance(body, dict) else None
        return err if isinstance(err, dict) else None

    @property
    def code(self) -> Optional[str]:
        err = self.error
        return err.get("code") if err else None

    def __repr__(self) -> str:
        return "<HttpResponse %d %s>" % (self.status, self.body[:120])


class Frame(dict):
    """A JSON frame received from the server: a ``dict`` plus its arrival number ``seq`` and monotonic time ``at``."""

    seq: int = -1
    at: float = 0.0


class ChatSession:
    """One user's HTTP identity plus (after :meth:`connect`) one WebSocket, as described in the module docstring.

    ``ChatSession(port)`` knows nobody yet: call :meth:`login` or :meth:`register`, then :meth:`connect`.  Several
    sessions of the same user (tabs) are separate objects that share nothing but the account; :meth:`clone` makes one
    that reuses this session's cookie.  All waits raise :class:`ClientTimeout` / :class:`ConnectionClosedError`
    (both ``AssertionError``) with the last received frames in the message.
    """

    def __init__(self, port: int, host: str = "127.0.0.1", name: str = "") -> None:
        self.port = port
        self.host = host
        self.name = name
        self.token: Optional[str] = None
        self.username: Optional[str] = None
        self.password: Optional[str] = None
        self.me: Optional[Dict[str, Any]] = None
        self.ws: Optional[WebSocketClient] = None
        self.frames: List[Frame] = []
        self.events: List[Tuple[int, Frame]] = []
        self.responses: List[Frame] = []
        self.garbage: List[bytes] = []
        self.close_seq: Optional[int] = None
        self._auto_pong = True
        self._seq = 0
        self._event_seqs: List[int] = []
        self._res_by_id: Dict[Any, Frame] = {}
        self._next_id = 0
        self._cv = threading.Condition()

    # ---- HTTP --------------------------------------------------------------------------------------------------

    @property
    def cookie_header(self) -> Optional[str]:
        return "%s=%s" % (SESSION_COOKIE, self.token) if self.token else None

    def http(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        data: Optional[bytes] = None,
        headers: Optional[Headers] = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        """One HTTP exchange on a fresh connection.

        ``body`` (any JSON-able value) is sent as ``application/json``; ``data`` is sent as given (set the
        ``Content-Type`` through ``headers``).  ``headers`` overrides or - with ``None`` - removes any header, so
        ``{"X-Requested-With": None}`` is the CSRF negative test.  The session cookie goes along when there is one;
        a ``Set-Cookie`` of ``fc_session`` updates it (an empty value or ``Max-Age=0`` clears it).
        """
        payload = data
        defaults = [("Host", "%s:%d" % (self.host, self.port)), ("Accept", "application/json"), CSRF_HEADER]
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            defaults.append(("Content-Type", "application/json"))
        if self.cookie_header:
            defaults.append(("Cookie", self.cookie_header))
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            merged = _merge_headers(defaults, headers)
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in merged:
                conn.putheader(name, value)
            if payload is not None and not any(n.lower() == "content-length" for n, _ in merged):
                conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders(payload)
            raw = conn.getresponse()
            content = raw.read()
            response_headers: Dict[str, str] = {}
            cookies: List[str] = []
            for name, value in raw.getheaders():
                key = name.lower()
                if key == "set-cookie":
                    cookies.append(value)
                response_headers[key] = response_headers[key] + ", " + value if key in response_headers else value
            result = HttpResponse(raw.status, response_headers, cookies, content)
        finally:
            conn.close()
        self._absorb_cookies(result)
        return result

    def get(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.http("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> HttpResponse:
        return self.http("POST", path, body if body is not None else {}, **kwargs)

    def _absorb_cookies(self, response: HttpResponse) -> None:
        for line in response.set_cookies:
            first, _, rest = line.partition(";")
            name, _, value = first.strip().partition("=")
            if name != SESSION_COOKIE:
                continue
            cleared = not value or "max-age=0" in rest.lower().replace(" ", "")
            self.token = None if cleared else value

    def register(
        self,
        username: str,
        display_name: str,
        password: str,
        setup_code: Optional[str] = None,
        join_code: Optional[str] = None,
        **kwargs: Any,
    ) -> HttpResponse:
        """``POST /api/register``; on ``201`` the session cookie and ``me`` are kept."""
        self.username, self.password = username, password
        fields: Dict[str, Any] = {"username": username, "display_name": display_name, "password": password}
        if setup_code is not None:
            fields["setup_code"] = setup_code
        if join_code is not None:
            fields["join_code"] = join_code
        return self._authenticated(self.http("POST", "/api/register", fields, **kwargs), 201)

    def login(self, username: str, password: str, **kwargs: Any) -> HttpResponse:
        """``POST /api/login``; on ``200`` the session cookie and ``me`` are kept."""
        self.username, self.password = username, password
        body = {"username": username, "password": password}
        return self._authenticated(self.http("POST", "/api/login", body, **kwargs), 200)

    def _authenticated(self, response: HttpResponse, success: int) -> HttpResponse:
        if response.status == success and isinstance(response.json, dict):
            self.me = response.json.get("me")
        return response

    def logout(self) -> HttpResponse:
        return self.post("/api/logout")

    def change_password(self, old_password: str, new_password: str) -> HttpResponse:
        return self.post("/api/password", {"old_password": old_password, "new_password": new_password})

    def upload(
        self,
        name: str,
        content: bytes,
        meta: Optional[Dict[str, Any]] = None,
        headers: Optional[Headers] = None,
        content_type: str = "application/octet-stream",
    ) -> HttpResponse:
        """``POST /api/upload`` of ``content`` as file ``name`` (percent-encoded UTF-8 in ``X-File-Name``)."""
        extra: Dict[str, Optional[str]] = {
            "Content-Type": content_type,
            "X-File-Name": urllib.parse.quote(name, safe=""),
        }
        if meta is not None:
            extra["X-Meta"] = json.dumps(meta)
        extra.update(headers or {})
        return self.http("POST", "/api/upload", data=content, headers=extra, timeout=30.0)

    def clone(self, name: str = "") -> "ChatSession":
        """A new, unconnected session of the same user (same cookie), e.g. a second tab or device."""
        other = ChatSession(self.port, self.host, name or self.name)
        other.token, other.me = self.token, self.me
        other.username, other.password = self.username, self.password
        return other

    # ---- WebSocket ---------------------------------------------------------------------------------------------

    @property
    def auto_pong(self) -> bool:
        """Answer server pings automatically (default).  The SPEC 6 liveness tests switch it off."""
        return self._auto_pong

    @auto_pong.setter
    def auto_pong(self, value: bool) -> None:
        self._auto_pong = value
        if self.ws is not None:
            self.ws.auto_pong = value

    def connect(
        self,
        *,
        wait_ready: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        origin: Optional[str] = None,
        headers: Optional[Headers] = None,
        cookie: Optional[str] = None,
    ) -> "ChatSession":
        """Open ``/ws`` with the session cookie (``cookie`` replaces the header value) and start queueing frames.

        ``wait_ready`` waits for ``ev.ready``.  A refused upgrade raises :class:`HandshakeError`.  Frame, event and
        response lists are kept across reconnects (a new connection keeps counting); use a new session to start clean.
        """
        self.ws = WebSocketClient.open(
            self.port,
            host=self.host,
            cookie=cookie if cookie is not None else self.cookie_header,
            origin=origin,
            headers=headers,
            on_message=self._on_message,
            on_close=self._on_close,
            auto_pong=self._auto_pong,
            timeout=timeout,
        )
        self.close_seq = None
        if wait_ready:
            self.wait_event("ev.ready", timeout=timeout)
        return self

    def _on_message(self, opcode: int, data: bytes) -> None:
        try:
            parsed = json.loads(data.decode("utf-8")) if opcode == OP_TEXT else None
        except ValueError:
            parsed = None
        with self._cv:
            if not isinstance(parsed, dict):
                self.garbage.append(data)
            else:
                frame = Frame(parsed)
                frame.seq, frame.at = self._seq, time.monotonic()
                self._seq += 1
                self.frames.append(frame)
                kind = frame.get("t")
                if kind == "res":
                    self.responses.append(frame)
                    self._res_by_id.setdefault(frame.get("id"), frame)
                elif isinstance(kind, str) and kind.startswith("ev."):
                    self.events.append((frame.seq, frame))
                    self._event_seqs.append(frame.seq)
            self._cv.notify_all()

    def _on_close(self) -> None:
        with self._cv:
            self.close_seq = self._seq
            self._seq += 1
            self._cv.notify_all()

    @property
    def close_code(self) -> Optional[int]:
        """The close code the server sent (``None`` while open; 1005/1006 as for browsers)."""
        return self.ws.close_code if self.ws is not None else None

    @property
    def ready(self) -> Dict[str, Any]:
        """The ``d`` of the most recent ``ev.ready`` (waits up to 5 s for the first one)."""
        self.wait_event("ev.ready")
        return self.events_matching("ev.ready")[-1]["d"]

    @property
    def user_id(self) -> int:
        return self.ready["me"]["id"]

    def mark(self) -> int:
        """The sequence number the next frame will get (``since=session.mark()`` sees only later events)."""
        with self._cv:
            return self._seq

    def _next_request_id(self) -> str:
        self._next_id += 1
        return "c%d" % self._next_id

    def _send_json(self, obj: Dict[str, Any], ignore_errors: bool = False) -> None:
        if self.ws is None:
            raise ConnectionClosedError("the session is not connected")
        self.ws.send_text(json.dumps(obj, separators=(",", ":")), ignore_errors=ignore_errors)

    def send(self, t: str, d: Optional[Dict[str, Any]] = None) -> None:
        """Fire and forget: a request frame without ``id`` (the server never answers it, SPEC 7.1)."""
        self._send_json({"t": t, "d": d if d is not None else {}})

    def request_nowait(self, t: str, d: Optional[Dict[str, Any]] = None, ignore_errors: bool = False) -> str:
        """Send a request with a fresh ``id`` and return that id without waiting (cancel-safety tests)."""
        rid = self._next_request_id()
        self._send_json({"t": t, "id": rid, "d": d if d is not None else {}}, ignore_errors)
        return rid

    def request(self, t: str, d: Optional[Dict[str, Any]] = None, timeout: float = DEFAULT_TIMEOUT) -> Frame:
        """Send ``t`` with ``d`` and return its ``res`` frame; an ``ok:false`` answer is returned, never raised."""
        return self.wait_res(self.request_nowait(t, d), timeout)

    def wait_res(self, rid: Any, timeout: float = DEFAULT_TIMEOUT) -> Frame:
        """The ``res`` frame with ``id == rid`` (``None`` waits for an answer to a malformed frame, SPEC 7.1)."""
        with self._cv:
            self._cv.wait_for(lambda: rid in self._res_by_id or self._closed(), timeout)
            frame = self._res_by_id.get(rid)
            if frame is not None:
                return frame
            closed = self._closed()
        what = "res id=%r" % (rid,)
        if closed:
            raise ConnectionClosedError(self._explain("socket closed before " + what))
        raise ClientTimeout(self._explain("no %s within %.1f s" % (what, timeout)))

    def _closed(self) -> bool:
        return self.ws is None or self.ws.closed

    def raw(self, data: Any, ignore_errors: bool = False) -> None:
        """Write verbatim.  ``str``: one masked text frame with exactly that text (malformed JSON, odd shapes);
        ``bytes``: raw bytes on the socket, e.g. :func:`encode_frame` output with a misbehaviour."""
        if self.ws is None:
            raise ConnectionClosedError("the session is not connected")
        if isinstance(data, str):
            self.ws.send_text(data, ignore_errors=ignore_errors)
        else:
            self.ws.send_raw(bytes(data), ignore_errors)

    # ---- waiting for events ------------------------------------------------------------------------------------

    @staticmethod
    def _event_name(name: Optional[str]) -> Optional[str]:
        if name is None or name == "*":
            return None
        return name if name.startswith("ev.") else "ev." + name

    def events_matching(
        self, name: Optional[str], pred: Optional[Callable[[Frame], bool]] = None, since: int = 0
    ) -> List[Frame]:
        """Snapshot of every event named ``name`` (``None``/``"*"``: any) with ``seq >= since`` that satisfies ``pred``."""
        wanted = self._event_name(name)
        with self._cv:
            start = bisect.bisect_left(self._event_seqs, since)
            candidates = [frame for _, frame in self.events[start:]]
        return [f for f in candidates if (wanted is None or f.get("t") == wanted) and (pred is None or pred(f))]

    def wait_event(
        self,
        name: Optional[str],
        pred: Optional[Callable[[Frame], bool]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        since: int = 0,
    ) -> Frame:
        """The first event named ``name`` (``"ev.message"`` or ``"message"``) with ``seq >= since`` for which
        ``pred(frame)`` holds.  It removes nothing: a later call finds it again."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                found = self._first_event(name, pred, since)
                if found is not None:
                    return found
                if self._closed():
                    raise ConnectionClosedError(self._explain("socket closed while waiting for event %r" % (name,)))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClientTimeout(self._explain("no event %r within %.1f s" % (name, timeout)))
                self._cv.wait(remaining)

    def _first_event(
        self, name: Optional[str], pred: Optional[Callable[[Frame], bool]], since: int
    ) -> Optional[Frame]:
        wanted = self._event_name(name)
        for _, frame in self.events[bisect.bisect_left(self._event_seqs, since):]:
            if (wanted is None or frame.get("t") == wanted) and (pred is None or pred(frame)):
                return frame
        return None

    def expect_none(
        self,
        name: Optional[str],
        pred: Optional[Callable[[Frame], bool]] = None,
        since: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Assert that no matching event arrived since ``since`` (SPEC 7.6(4) barrier: a ``ping`` round trip)."""
        res = self.request("ping", {}, timeout)
        if not res.get("ok"):
            raise AssertionError("the barrier ping failed: %r" % (res,))
        late = self.events_matching(name, pred, since)
        if late:
            raise AssertionError("unexpected event %s after seq %d: %r" % (name, since, late[0]))

    def _explain(self, message: str) -> str:
        with self._cv:
            tail = ["%s%s" % (f.get("t"), "(%s)" % f.get("id") if f.get("t") == "res" else "") for f in self.frames[-8:]]
        code = self.ws.close_code if self.ws is not None else None
        return "%s [%s; close_code=%s; last frames: %s]" % (message, self.name or "session", code, ", ".join(tail) or "none")

    # ---- closing -----------------------------------------------------------------------------------------------

    def wait_closed(self, timeout: float = DEFAULT_TIMEOUT) -> int:
        """Wait until the server has closed the connection; returns the close code."""
        if self.ws is None:
            raise ConnectionClosedError("the session is not connected")
        return self.ws.wait_closed(timeout)

    def close(self) -> None:
        """Graceful close handshake (idempotent)."""
        if self.ws is not None:
            self.ws.close()

    def abort(self) -> None:
        """Drop the connection without a close frame (idempotent)."""
        if self.ws is not None:
            self.ws.abort()

    def __enter__(self) -> "ChatSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.abort()
