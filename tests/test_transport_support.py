"""Shared helpers of the transport test-suites (no tests live here).

* :class:`FakeAuth` - stands in for ``chatd.auth`` (token ``tok-<user_id>`` authenticates user ``<user_id>``).
* :class:`Harness` - runs a real :class:`chatd.http.HttpServer` with a :class:`chatd.http.Router` in a background
  thread on port 0 of 127.0.0.1.
* raw-socket helpers: :func:`exchange`, :func:`parse_response`, :class:`RawWsClient` (masking, fragments, close).
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import struct
import sys
import threading
import time
import weakref
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chatd import util  # noqa: E402
from chatd.config import Config  # noqa: E402
from chatd.http import HttpServer, Request, Response, Router  # noqa: E402

HOST_HEADER = b"Host: 127.0.0.1\r\n"


class FakeAuth:
    """A minimal ``auth`` module: cookie ``fc_session=tok-<id>`` is user ``<id>``; ``tok-bad`` is unknown."""

    def __init__(self, must_change: Optional[set] = None, reissue: Optional[set] = None) -> None:
        self.must_change = must_change or set()
        self.reissue = reissue or set()
        self.calls: List[Tuple[str, str]] = []

    def parse_cookie(self, header: str) -> Optional[str]:
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fc_session" and value:
                return value
        return None

    async def authenticate(self, db: Any, token: str, ip: str, user_agent: str) -> Optional[dict]:
        self.calls.append((token, ip))
        if not token.startswith("tok-") or token == "tok-bad":
            return None
        uid = int(token[4:])
        return {
            "user_id": uid,
            "token_hash": "h%d" % uid,
            "ip": ip,
            "user_agent": user_agent,
            "must_change_password": uid in self.must_change,
            "reissue_cookie": uid in self.reissue,
        }

    def cookie_header(self, token: str, secure: bool, max_age: int) -> str:
        return "fc_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (
            token,
            max_age,
            "; Secure" if secure else "",
        )

    def clear_cookie_header(self, secure: bool) -> str:
        return "fc_session=; Path=/; Max-Age=0"


def make_config(data_dir: str, **overrides: Any) -> Config:
    """A Config for tests: loopback, port 0, small scrypt (irrelevant here), data dir ``data_dir``."""
    values: Dict[str, Any] = {"host": "127.0.0.1", "port": 0, "data_dir": data_dir, "scrypt_n": 1024}
    values.update(overrides)
    return Config(**values)


class Harness:
    """A running HTTP server for tests.  ``routes`` is called with the Router before the server starts."""

    def __init__(
        self,
        cfg: Config,
        routes: Optional[Callable[[Router, "Harness"], None]] = None,
        auth: Optional[FakeAuth] = None,
        ssl_context: Any = None,
        db: Any = None,
    ) -> None:
        self.cfg = cfg
        self.auth = auth or FakeAuth()
        self.router = Router(cfg, db, auth_module=self.auth)
        if routes is not None:
            routes(self.router, self)
        self.server = HttpServer(cfg, self.router, ssl_context)
        self.port = 0
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test thread
            self._error = exc
            self._ready.set()
        finally:
            loop.close()

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        self.port = await self.server.start()
        self._ready.set()
        await self._stop.wait()
        self.server.close_listeners()
        self.server.abort_connections()
        await self.server.wait_connections(3)
        await self.server.wait_closed(2)

    def start(self) -> "Harness":
        self._thread.start()
        self._ready.wait(10)
        if self._error is not None:
            raise self._error
        return self

    def stop(self) -> None:
        if self.loop is not None and self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)

    def call(self, coro: Awaitable[Any], timeout: float = 10) -> Any:
        """Run a coroutine on the server loop and return its result (for inspecting server state)."""
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def __enter__(self) -> "Harness":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def connect(port: int, timeout: float = 5.0) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


_LEFTOVER: "weakref.WeakKeyDictionary[Any, bytes]" = weakref.WeakKeyDictionary()


def read_response(sock: socket.socket, head_only: bool = False) -> Optional[Tuple[int, Dict[str, str], bytes]]:
    """Read one response from ``sock`` (bytes of a following pipelined response are kept for the next call).

    ``None`` when the peer closed before a complete status line and header block arrived.
    """
    data = _LEFTOVER.pop(sock, b"")
    while b"\r\n\r\n" not in data:
        try:
            chunk = sock.recv(65536)
        except (ConnectionError, socket.timeout):
            return None
        if not chunk:
            return None
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    status, headers = parse_head(head)
    length = 0 if head_only else int(headers.get("content-length", "0"))
    body = rest
    while len(body) < length:
        try:
            chunk = sock.recv(65536)
        except (ConnectionError, socket.timeout):
            break
        if not chunk:
            break
        body += chunk
    if body[length:]:
        _LEFTOVER[sock] = body[length:]
    return status, headers, body[:length]


def parse_head(head: bytes) -> Tuple[int, Dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        key = name.strip().lower()
        headers[key] = headers[key] + ", " + value.strip() if key in headers else value.strip()
    return status, headers


def exchange(
    port: int, raw: bytes, timeout: float = 5.0, head_only: bool = False
) -> Optional[Tuple[int, Dict[str, str], bytes]]:
    """Send ``raw`` on a fresh connection and return the first response (or ``None`` if none arrived)."""
    sock = connect(port, timeout)
    try:
        sock.sendall(raw)
        return read_response(sock, head_only)
    finally:
        sock.close()


def get(port: int, path: str, extra: bytes = b"", host: bytes = HOST_HEADER) -> Tuple[int, Dict[str, str], bytes]:
    raw = b"GET " + path.encode("ascii") + b" HTTP/1.1\r\n" + host + extra + b"Connection: close\r\n\r\n"
    result = exchange(port, raw)
    assert result is not None, "no response"
    return result


def closed_by_peer(sock: socket.socket, timeout: float = 5.0) -> bool:
    """True when the peer closes the connection (EOF or reset) within ``timeout`` seconds."""
    sock.settimeout(timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return False
        except ConnectionError:
            return True
        if not chunk:
            return True
    return False


# --------------------------------------------------------------------------------------------------------------
# Raw WebSocket client
# --------------------------------------------------------------------------------------------------------------


def mask_payload(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def build_frame(
    opcode: int,
    payload: bytes,
    fin: bool = True,
    masked: bool = True,
    rsv: int = 0,
    length_form: Optional[str] = None,
    declared_length: Optional[int] = None,
    key: bytes = b"\x01\x02\x03\x04",
) -> bytes:
    """Build a client frame; ``length_form`` ('7', '16', '64') forces an encoding, ``declared_length`` lies."""
    n = len(payload) if declared_length is None else declared_length
    form = length_form or ("7" if n < 126 else "16" if n < 65536 else "64")
    b1 = (0x80 if fin else 0) | (rsv << 4) | opcode
    mbit = 0x80 if masked else 0
    if form == "7":
        head = struct.pack("!BB", b1, mbit | n)
    elif form == "16":
        head = struct.pack("!BBH", b1, mbit | 126, n)
    else:
        head = struct.pack("!BBQ", b1, mbit | 127, n)
    if masked:
        return head + key + mask_payload(payload, key)
    return head + payload


class RawWsClient:
    """A blocking WebSocket test client on a raw socket."""

    def __init__(self, port: int, cookie: str = "fc_session=tok-1", extra: bytes = b"", timeout: float = 5.0) -> None:
        self.sock = connect(port, timeout)
        self.port = port
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            (
                "GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\nCookie: %s\r\n" % (key, cookie)
            ).encode("ascii")
            + extra
            + b"\r\n"
        )
        self.sock.sendall(req)
        self.buf = b""
        self.response = self._read_head()

    def _read_head(self) -> Tuple[int, Dict[str, str]]:
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        return parse_head(head)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send_text(self, text: str) -> None:
        self.send(build_frame(0x1, text.encode("utf-8")))

    def _need(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def recv_frame(self) -> Tuple[int, bytes]:
        b1, b2 = self._need(2)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._need(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._need(8))[0]
        return b1 & 0x0F, self._need(length)

    def recv_text(self) -> str:
        while True:
            opcode, payload = self.recv_frame()
            if opcode == 0x1:
                return payload.decode("utf-8")
            if opcode == 0x9:
                self.send(build_frame(0xA, payload))

    def recv_close(self) -> Optional[int]:
        """Read frames until a close frame; returns its code (``None`` for an empty payload or EOF)."""
        try:
            while True:
                opcode, payload = self.recv_frame()
                if opcode == 0x8:
                    return struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
        except (EOFError, ConnectionError, socket.timeout):
            return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class StubHub:
    """The hub as the transport sees it: records connections, echoes text, lets tests drive the socket."""

    def __init__(self) -> None:
        self.stopping = False
        self.sockets: List[Any] = []
        self.received: List[str] = []
        self.sessions: List[dict] = []
        self.finished = threading.Event()
        self.echo = True
        self.on_connect: Optional[Callable[[Any], None]] = None
        self.counters: Dict[str, int] = {"external_change_calls": 0, "revalidate_calls": 0, "dropped_ephemeral": 0}
        self.started = False
        self.typing_sweeps = 0

    async def start(self) -> None:
        self.started = True

    def sweep_typing(self) -> None:
        self.typing_sweeps += 1

    async def serve(self, ws: Any, session: dict) -> None:
        self.sockets.append(ws)
        self.sessions.append(session)
        if self.on_connect is not None:
            self.on_connect(ws)
        while True:
            text = await ws.recv()
            if text is None:
                break
            self.received.append(text)
            if self.echo:
                ws.send_text("echo:" + text)
        self.finished.set()

    async def shutdown(self) -> None:
        self.stopping = True

    def online_user_ids(self) -> List[int]:
        return sorted({s.session["user_id"] for s in self.sockets if not s.closed})

    async def revalidate_all(self) -> None:
        self.counters["revalidate_calls"] += 1

    async def external_change(self) -> None:
        self.counters["external_change_calls"] += 1


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def ok_handler(text: str = "ok") -> Callable[[Request], Awaitable[Response]]:
    async def handler(req: Request) -> Response:
        return Response(200, [("Content-Type", "text/plain")], text.encode("utf-8"))

    return handler


def reset_scale() -> None:
    util.set_test_scale(1.0)
