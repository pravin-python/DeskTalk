"""RFC 6455 server side on ``asyncio`` streams (SPEC 6, 7.5 ws caps, 8.4 liveness inputs).

Contract with the hub (SPEC 6 / 6.1 / 6.2):

* ``upgrade_response(req, hub)`` validates the handshake and the caps and returns the ``101`` :class:`Response`
  whose ``upgrade`` callable runs the frame loop and ``await hub.serve(ws, req.session)``.
* :class:`WebSocket` offers ``recv()``, ``send_text()``, ``close()``, ``closed``, ``remote_addr``, ``user_agent``,
  ``session``, ``last_rx`` (monotonic) and ``last_rx_wall`` (wall clock of the same instant).

Receive side: client frames must be masked, RSV bits must be 0, control frames are <= 125 bytes and unfragmented,
the declared length is validated before a single payload byte is read (non-minimal encodings 1002, MSB set or over
the 256 KiB message budget 1009), text is validated as UTF-8 incrementally (1007), binary is refused (1003).

Send side: one writer task per connection is the only caller of ``writer.write/drain``.  There are three frame
classes (SPEC 6), selected by the arguments of ``send_text(text, durable=True, key=None)``: *durable*
(``durable=True``, no key), *keyed* (``durable=True`` with a ``key``: ``ev.receipt``; replaces the older queued frame
of that key, is never dropped and counts as durable for the 512-frame limit) and *ephemeral* (``durable=False``:
typing/presence; coalesced by ``key`` too, dropped first when more than 256 frames are queued, every drop counted in
``hub.counters['dropped_ephemeral']``).  A replacing frame moves to the tail, so the relative order of everything
still queued is the enqueue order.  ``close`` flushes the queue first for the codes 1000/1001/4001/4003/4008
(bounded by 2 s) and closes at once for protocol failures and back-pressure.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import itertools
import logging
import re
import ssl
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from . import util
from .http import Connection, Request, Response, error_response

log = logging.getLogger("chatd.ws")

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ---- limits (SPEC 6) --------------------------------------------------------------------------------------------
MAX_MESSAGE_BYTES = 256 * 1024
MAX_CONTROL_PAYLOAD = 125
PING_INTERVAL_S = 25.0
LIVENESS_TIMEOUT_S = 60.0
DRAIN_TIMEOUT_S = 10.0
CLOSE_WAIT_S = 2.0
CLOSE_FLUSH_S = 2.0
FINISH_WAIT_S = 5.0
WRITE_HIGH_WATER = 256 * 1024
EPHEMERAL_SHED_FRAMES = 256
MAX_DURABLE_FRAMES = 512
MAX_QUEUED_BYTES = 2 * 1024 * 1024
GLOBAL_QUEUED_BYTES = 128 * 1024 * 1024
MAX_INBOX_MESSAGES = 256
WRITE_BATCH_FRAMES = 64
WRITE_BATCH_BYTES = 256 * 1024
MAX_PENDING_PONGS = 16
WS_PER_USER = 8
WS_PER_IP = 30
WS_TOTAL = 800
HANDSHAKES_PER_IP_MIN = 20
HANDSHAKES_PER_USER_MIN = 30
RATE_TABLE_MAX = 10000

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_VALID_OPCODES = frozenset((OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG))
_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{22}==$")
#: ``ws.close`` codes that first flush the queued frames (an ``ev.kicked`` must reach the client before its 4001).
FLUSH_CLOSE_CODES = frozenset((1000, 1001, 4001, 4003, 4008))
_RX_COUNTER = itertools.count()  # tie-break for last_rx: time.monotonic() has a ~15 ms tick on Windows
_FRAME_CACHE_SIZE = 8
_FRAME_CACHE_MIN = 1024


class _ProtocolError(Exception):
    """A violation that closes the connection with ``code`` (``reason`` is sent, never logged with payloads)."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


# --------------------------------------------------------------------------------------------------------------
# Frame helpers
# --------------------------------------------------------------------------------------------------------------


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """An unmasked, unfragmented server frame (minimal length encoding)."""
    n = len(payload)
    if n < 126:
        head = bytes((0x80 | opcode, n))
    elif n < 65536:
        head = bytes((0x80 | opcode, 126)) + n.to_bytes(2, "big")
    else:
        head = bytes((0x80 | opcode, 127)) + n.to_bytes(8, "big")
    return head + payload


def unmask(data: bytes, key: bytes) -> bytes:
    """XOR ``data`` with the repeating 4-byte ``key`` (big-integer XOR: fast for 256 KiB payloads)."""
    n = len(data)
    if n == 0:
        return b""
    stream = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(n, "big")


def accept_key(client_key: str) -> str:
    """The ``Sec-WebSocket-Accept`` value for a client key."""
    return base64.b64encode(hashlib.sha1(client_key.encode("ascii") + GUID).digest()).decode("ascii")


def _valid_close_code(code: int) -> bool:
    return 1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999


def _close_payload(code: int, reason: str) -> bytes:
    raw = reason.encode("utf-8")[:123]
    while raw:
        try:
            raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    return code.to_bytes(2, "big") + raw


_frame_cache: "deque[Tuple[str, bytes]]" = deque(maxlen=_FRAME_CACHE_SIZE)


def _text_frame(text: str) -> bytes:
    """Encode ``text`` once: the hub hands the same ``str`` to every recipient of a variant, so recent large
    frames are shared by identity and 300 connections hold one ``bytes`` object instead of 300 copies."""
    if len(text) >= _FRAME_CACHE_MIN:
        for cached_text, cached in _frame_cache:
            if cached_text is text:
                return cached
    frame = encode_frame(OP_TEXT, text.encode("utf-8"))
    if len(text) >= _FRAME_CACHE_MIN:
        _frame_cache.append((text, frame))
    return frame


class _Entry:
    __slots__ = ("frame", "size", "durable", "key", "dead")

    def __init__(self, frame: bytes, durable: bool, key: Any) -> None:
        self.frame = frame
        self.size = len(frame)
        self.durable = durable
        self.key = key
        self.dead = False


# --------------------------------------------------------------------------------------------------------------
# The connection
# --------------------------------------------------------------------------------------------------------------


class WebSocket:
    """One upgraded connection (SPEC 6).  Created and driven by :class:`WsRegistry`; the hub only uses the API."""

    def __init__(
        self, conn: Connection, session: Dict[str, Any], user_agent: str, registry: "WsRegistry"
    ) -> None:
        self._reader = conn
        self._writer = conn.writer
        self._registry = registry
        self.session = session
        self.remote_addr = conn.remote_addr
        self.user_agent = user_agent
        self.last_rx = time.monotonic()
        self.last_rx_wall = time.time()
        self._rx_seq = next(_RX_COUNTER)
        self.closed = False

        self._inbox: Deque[str] = deque()
        self._inbox_event = asyncio.Event()
        self._space_event = asyncio.Event()
        self._eof = False

        self._entries: Deque[_Entry] = deque()
        self._keyed: Dict[Any, _Entry] = {}
        self._ctrl: Deque[bytes] = deque()
        self._wake = asyncio.Event()
        self._live = 0
        self._durable = 0
        self._bytes = 0
        self._close_request: Optional[Tuple[int, str]] = None
        self._close_sent = False
        self._peer_closed = asyncio.Event()
        self._tasks: Set["asyncio.Task[None]"] = set()
        self._write_task: Optional["asyncio.Task[None]"] = None
        self._dead = False  # the transport is gone: nothing more can be written or read
        self._flush_timer: Optional["asyncio.TimerHandle"] = None
        self.counters: Optional[Dict[str, int]] = None  # ``hub.counters`` (set by the registry)

    # ---- API used by the hub -----------------------------------------------------------------------------------

    async def recv(self) -> Optional[str]:
        """The next text message, or ``None`` once the connection is closed (queued messages are delivered first)."""
        while True:
            if self._inbox:
                item = self._inbox.popleft()
                self._space_event.set()
                return item
            if self._eof:
                return None
            self._inbox_event.clear()
            await self._inbox_event.wait()

    def send_text(self, text: str, durable: bool = True, key: Any = None) -> None:
        """Queue a text frame without blocking.  Never raises into the hub.

        ``durable=True`` without ``key``: durable frame.  ``durable=True`` with ``key``: keyed frame (``ev.receipt``).
        ``durable=False``: ephemeral frame (``key`` coalesces it).  See the module docstring for the rules.
        """
        if self.closed:
            return
        try:
            frame = _text_frame(text)
        except (UnicodeEncodeError, AttributeError):
            log.error("refusing to send a frame that is not valid text")
            return
        entry = _Entry(frame, durable, key)
        if key is not None:
            older = self._keyed.pop(key, None)
            if older is not None:
                self._kill(older)  # a replacement does not grow the queue
            self._keyed[key] = entry
        if self._live > EPHEMERAL_SHED_FRAMES:
            if not durable:
                self._count_dropped(1)
                if key is not None and self._keyed.get(key) is entry:
                    del self._keyed[key]
                return
            self._shed_ephemeral()
        self._entries.append(entry)
        self._live += 1
        self._bytes += entry.size
        self._registry.queued_bytes += entry.size
        if durable:
            self._durable += 1
            if self._durable > MAX_DURABLE_FRAMES or self._bytes > MAX_QUEUED_BYTES:
                self.overflow()
                return
        if self._registry.queued_bytes > GLOBAL_QUEUED_BYTES:
            self._registry.shed()
            return
        self._wake.set()

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the connection.

        The codes in :data:`FLUSH_CLOSE_CODES` first deliver the frames already queued (through the writer task,
        bounded by 2 s) and then send the Close frame; every other code (protocol failures, back-pressure) sends it
        at once and discards the queue.  ``recv()`` returns ``None`` from now on, ``send_text`` is a no-op.
        """
        if self.closed:
            return
        self.closed = True
        self._finish_input()
        if code in FLUSH_CLOSE_CODES:
            self._close_request = (code, reason)
            self._flush_timer = asyncio.get_running_loop().call_later(util.scaled(CLOSE_FLUSH_S), self._flush_expired)
        else:
            self._close_now(code, reason)
        self._wake.set()

    def overflow(self) -> None:
        """Close with 1013 right now, discarding everything still queued (queue or byte budget exceeded)."""
        if self._close_sent:
            return
        self.closed = True
        self._finish_input()
        self._close_now(1013, "overloaded")
        self._wake.set()

    def _close_now(self, code: int, reason: str) -> None:
        self._drop_queue()
        self._ctrl.clear()
        self._close_request = (code, reason)

    def _drop_queue(self) -> None:
        for entry in list(self._entries):
            self._kill(entry)
        self._entries.clear()
        self._keyed.clear()

    def _flush_expired(self) -> None:
        """The 2 s flush bound of ``close`` ran out: whatever is still queued is abandoned, the Close frame follows."""
        if not self._close_sent:
            self._drop_queue()
            self._wake.set()

    def _count_dropped(self, n: int) -> None:
        if self.counters is not None and n:
            self.counters["dropped_ephemeral"] = self.counters.get("dropped_ephemeral", 0) + n

    # ---- queue accounting --------------------------------------------------------------------------------------

    def _kill(self, entry: _Entry) -> None:
        if entry.dead:
            return
        entry.dead = True
        self._live -= 1
        self._bytes -= entry.size
        self._registry.queued_bytes -= entry.size
        if entry.durable:
            self._durable -= 1
        if entry.key is not None and self._keyed.get(entry.key) is entry:
            del self._keyed[entry.key]

    def _shed_ephemeral(self) -> None:
        """Drop every queued ephemeral frame (keyed and durable frames are never dropped)."""
        dropped = 0
        for entry in self._entries:
            if not entry.durable and not entry.dead:
                self._kill(entry)
                dropped += 1
        self._count_dropped(dropped)

    @property
    def queued_bytes(self) -> int:
        return self._bytes

    def _take_batch(self) -> List[bytes]:
        batch: List[bytes] = []
        while self._ctrl:
            batch.append(self._ctrl.popleft())
        size = 0
        while self._entries and len(batch) < WRITE_BATCH_FRAMES and size < WRITE_BATCH_BYTES:
            entry = self._entries.popleft()
            if entry.dead:
                continue
            self._kill(entry)
            batch.append(entry.frame)
            size += entry.size
        if not batch and self._close_request is not None and not self._close_sent and not self._entries:
            code, reason = self._close_request
            batch.append(encode_frame(OP_CLOSE, _close_payload(code, reason)))
            self._close_sent = True
        return batch

    # ---- input side bookkeeping --------------------------------------------------------------------------------

    def _finish_input(self) -> None:
        self._eof = True
        self._inbox_event.set()
        self._space_event.set()

    def _touch(self) -> None:
        self.last_rx = time.monotonic()
        self.last_rx_wall = time.time()
        self._rx_seq = next(_RX_COUNTER)

    @property
    def rx_order(self) -> Tuple[float, int]:
        """Sort key of "how recently did this connection receive something" (exact even within one clock tick)."""
        return self.last_rx, self._rx_seq

    # ---- tasks -------------------------------------------------------------------------------------------------

    def _spawn(self, coro: Any) -> "asyncio.Task[None]":
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def start(self) -> None:
        transport = self._writer.transport
        if transport is not None:
            transport.set_write_buffer_limits(high=WRITE_HIGH_WATER)
        self._spawn(self._read_loop())
        self._write_task = self._spawn(self._write_loop())
        self._spawn(self._ping_loop())

    async def finish(self) -> None:
        """Wait for the close handshake to complete (bounded) and tear the socket down."""
        if not self.closed:
            self.close(1000, "")
        try:
            if self._write_task is not None and not self._dead:
                try:
                    await asyncio.wait_for(asyncio.shield(self._write_task), util.scaled(FINISH_WAIT_S))
                except asyncio.TimeoutError:
                    log.debug("websocket writer did not finish in time")
            if self._close_sent and not self._peer_closed.is_set() and not self._dead:
                try:
                    await asyncio.wait_for(self._peer_closed.wait(), util.scaled(CLOSE_WAIT_S))
                except asyncio.TimeoutError:
                    log.debug("peer did not answer the close frame")
        finally:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
            for task in list(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
            self._abort_transport()

    def _abort_transport(self) -> None:
        self.closed = True
        self._dead = True
        self._finish_input()
        self._wake.set()
        transport = self._writer.transport
        if transport is not None and not transport.is_closing():
            transport.abort()

    # ---- writer ------------------------------------------------------------------------------------------------

    async def _write_loop(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                if self._dead:
                    return
                while True:
                    batch = self._take_batch()
                    if not batch:
                        break
                    for frame in batch:
                        self._writer.write(frame)
                    await asyncio.wait_for(self._writer.drain(), util.scaled(DRAIN_TIMEOUT_S))
                    if self._close_sent:
                        return
        except asyncio.TimeoutError:
            log.info("closing a connection whose peer stopped reading (1013)")
            self._abort_transport()
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            log.debug("websocket write failed: %s", type(exc).__name__)
            self._abort_transport()

    async def _ping_loop(self) -> None:
        while not self.closed:
            await asyncio.sleep(util.scaled(PING_INTERVAL_S))
            if len(self._ctrl) < MAX_PENDING_PONGS:
                self._ctrl.append(encode_frame(OP_PING, b""))
                self._wake.set()

    # ---- reader ------------------------------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            await self._frames()
        except _ProtocolError as err:
            log.info("closing a connection (%d: %s)", err.code, err.reason)
            self.closed = True
            self._close_now(err.code, err.reason)
            self._wake.set()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError):
            self._abort_transport()
        finally:
            self._finish_input()
            self._peer_closed.set()

    async def _frames(self) -> None:
        reader = self._reader
        decoder: Optional["codecs.IncrementalDecoder"] = None
        parts: List[str] = []
        assembled = 0
        while True:
            b1, b2 = await reader.readexactly(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            if b1 & 0x70:
                raise _ProtocolError(1002, "reserved bits set")
            if opcode not in _VALID_OPCODES:
                raise _ProtocolError(1002, "unknown opcode")
            if not b2 & 0x80:
                raise _ProtocolError(1002, "client frames must be masked")
            length = b2 & 0x7F
            control = opcode >= OP_CLOSE
            if control and (not fin or length > MAX_CONTROL_PAYLOAD):
                raise _ProtocolError(1002, "invalid control frame")
            if length == 126:
                length = int.from_bytes(await reader.readexactly(2), "big")
                if length < 126:
                    raise _ProtocolError(1002, "non-minimal length")
            elif length == 127:
                length = int.from_bytes(await reader.readexactly(8), "big")
                if length >> 63:
                    raise _ProtocolError(1009, "message too big")
                if length < 65536:
                    raise _ProtocolError(1002, "non-minimal length")
            if not control and length > MAX_MESSAGE_BYTES - assembled:
                raise _ProtocolError(1009, "message too big")
            if opcode == OP_BINARY:
                raise _ProtocolError(1003, "binary frames are not supported")
            mask = await reader.readexactly(4)
            payload = unmask(await reader.readexactly(length), mask) if length else b""
            self._touch()

            if opcode == OP_PING:
                if len(self._ctrl) < MAX_PENDING_PONGS:
                    self._ctrl.append(encode_frame(OP_PONG, payload))
                    self._wake.set()
            elif opcode == OP_PONG:
                continue
            elif opcode == OP_CLOSE:
                self._on_close_frame(payload)
                return
            else:
                if opcode == OP_CONT:
                    if decoder is None:
                        raise _ProtocolError(1002, "unexpected continuation")
                else:
                    if decoder is not None:
                        raise _ProtocolError(1002, "interleaved data frames")
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    parts, assembled = [], 0
                assembled += length
                try:
                    parts.append(decoder.decode(payload, final=fin))
                except UnicodeDecodeError:
                    raise _ProtocolError(1007, "invalid UTF-8")
                if fin:
                    decoder = None
                    await self._deliver("".join(parts))
                    parts, assembled = [], 0

    async def _deliver(self, text: str) -> None:
        if self.closed:
            return
        while len(self._inbox) >= MAX_INBOX_MESSAGES and not self._eof:
            self._space_event.clear()
            await self._space_event.wait()
        self._inbox.append(text)
        self._inbox_event.set()

    def _on_close_frame(self, payload: bytes) -> None:
        if len(payload) == 1:
            raise _ProtocolError(1002, "invalid close frame")
        code = 1000
        if len(payload) >= 2:
            code = int.from_bytes(payload[:2], "big")
            if not _valid_close_code(code):
                raise _ProtocolError(1002, "invalid close code")
            try:
                payload[2:].decode("utf-8")
            except UnicodeDecodeError:
                raise _ProtocolError(1007, "invalid UTF-8 in close reason")
        self._peer_closed.set()
        if not self.closed:
            self.closed = True
            self._close_request = (code, "")
        self._finish_input()
        self._wake.set()


# --------------------------------------------------------------------------------------------------------------
# Registry: caps, handshake rate limits, per-user replacement, liveness
# --------------------------------------------------------------------------------------------------------------


class WsRegistry:
    """Every live WebSocket of one server, the connection caps and the handshake rate limits (SPEC 6).

    Caps and windows read ``cfg.test_limits`` (``ws_per_user``, ``ws_per_ip``, ``ws_total``,
    ``ws_handshakes_per_ip_min``, ``ws_handshakes_per_user_min``) and go through :func:`util.scaled`.
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.sockets: Set[WebSocket] = set()
        self.queued_bytes = 0
        self._by_ip: Dict[str, int] = {}
        self._handshakes: Dict[Tuple[str, Any], Deque[float]] = {}
        self._tasks: Set["asyncio.Task[None]"] = set()

    # ---- limits ------------------------------------------------------------------------------------------------

    @property
    def per_user(self) -> int:
        return int(self.cfg.limit("ws_per_user", WS_PER_USER))

    @property
    def per_ip(self) -> int:
        return int(self.cfg.limit("ws_per_ip", WS_PER_IP))

    @property
    def total(self) -> int:
        return int(self.cfg.limit("ws_total", WS_TOTAL))

    def count(self) -> int:
        return len(self.sockets)

    def ip_count(self, ip: str) -> int:
        return self._by_ip.get(ip, 0)

    def handshake_retry_after(self, ip: str, user_id: int) -> Optional[float]:
        """Record a handshake attempt; return the ``Retry-After`` seconds when a per-minute limit is exceeded."""
        window = util.scaled(60.0)
        now = time.monotonic()
        limits = (
            (("ip", ip), int(self.cfg.limit("ws_handshakes_per_ip_min", HANDSHAKES_PER_IP_MIN))),
            (("user", user_id), int(self.cfg.limit("ws_handshakes_per_user_min", HANDSHAKES_PER_USER_MIN))),
        )
        if len(self._handshakes) > RATE_TABLE_MAX:
            self._handshakes = {k: v for k, v in self._handshakes.items() if v and now - v[-1] < window}
        wait = 0.0
        for key, limit in limits:
            stamps = self._handshakes.setdefault(key, deque())
            while stamps and now - stamps[0] >= window:
                stamps.popleft()
            if len(stamps) >= limit:
                wait = max(wait, stamps[0] + window - now)
        if wait > 0:
            return wait
        for key, _ in limits:
            self._handshakes[key].append(now)
        return None

    # ---- membership --------------------------------------------------------------------------------------------

    def _register(self, ws: WebSocket) -> None:
        """Add ``ws``; when its user already has the maximum, the connection with the oldest ``last_rx`` is replaced."""
        user_id = ws.session["user_id"]
        mine = [s for s in self.sockets if s.session["user_id"] == user_id and not s.closed]
        while len(mine) >= self.per_user:
            victim = min(mine, key=lambda s: s.rx_order)
            victim.close(4003, "replaced")
            mine.remove(victim)
        self.sockets.add(ws)
        self._by_ip[ws.remote_addr] = self._by_ip.get(ws.remote_addr, 0) + 1

    def _unregister(self, ws: WebSocket) -> None:
        if ws in self.sockets:
            self.sockets.discard(ws)
            self.queued_bytes -= ws.queued_bytes
            left = self._by_ip.get(ws.remote_addr, 1) - 1
            if left > 0:
                self._by_ip[ws.remote_addr] = left
            else:
                self._by_ip.pop(ws.remote_addr, None)

    def shed(self) -> None:
        """The global send budget is exhausted: close the connection with the largest queue with 1013."""
        if self.sockets:
            max(self.sockets, key=lambda s: s.queued_bytes).overflow()

    def check_liveness(self) -> int:
        """Close every connection silent for more than 60 s with 1001 (called every 10 s, SPEC 2.4)."""
        limit = util.scaled(LIVENESS_TIMEOUT_S)
        now = time.monotonic()
        stale = [s for s in self.sockets if not s.closed and now - s.last_rx > limit]
        for ws in stale:
            ws.close(1001, "idle timeout")
        return len(stale)

    async def close_all(self, code: int = 1001, reason: str = "restart", timeout: float = 3.0) -> None:
        """Close every connection and give the close frames up to ``timeout`` seconds to leave (shutdown)."""
        for ws in list(self.sockets):
            ws.close(code, reason)
        deadline = time.monotonic() + timeout
        while self.sockets and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        for ws in list(self.sockets):
            ws._abort_transport()

    # ---- running a connection ----------------------------------------------------------------------------------

    async def run(self, req: Request, hub: Any, conn: Connection) -> None:
        """Frame loop of one upgraded connection; returns after ``hub.serve`` and the close handshake are done."""
        session = dict(req.session or {})
        if (
            self.count() >= self.total
            or self.ip_count(conn.remote_addr) >= self.per_ip
        ):
            conn.writer.write(encode_frame(OP_CLOSE, _close_payload(1013, "too many connections")))
            return
        ws = WebSocket(conn, session, req.headers.get("user-agent", "")[:200], self)
        counters = getattr(hub, "counters", None)
        ws.counters = counters if isinstance(counters, dict) else None
        self._register(ws)
        ws.start()
        try:
            await hub.serve(ws, session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a hub bug must close this socket, not the server; logged with traceback
            log.exception("hub.serve failed for user %s", session.get("user_id"))
            ws.close(1011, "server error")
        finally:
            try:
                await ws.finish()
            finally:
                self._unregister(ws)


def _registry_for(req: Request) -> WsRegistry:
    server = req.server
    registry = getattr(server, "ws_registry", None)
    if registry is None:
        registry = WsRegistry(server.cfg)
        server.ws_registry = registry
    return registry


def _token_list(value: str) -> List[str]:
    return [t.strip().lower() for t in value.split(",") if t.strip()]


async def upgrade_response(req: Request, hub: Any) -> Response:
    """Validate the WebSocket handshake and caps (SPEC 6); return the ``101`` response or the refusal.

    The caller (the Router, ``auth='cookie'``) has already run ``check_request_origin`` and the session check, so
    ``req.session`` is set.  The returned response's ``upgrade`` callable owns the socket from here on.
    """
    registry = _registry_for(req)
    session = req.session
    if session is None:
        return error_response(401, "unauthorized", "sign in required")
    if req.method != "GET" or req.version != "1.1":
        return error_response(400, "bad_request", "a WebSocket upgrade must be an HTTP/1.1 GET")
    if "websocket" not in _token_list(req.headers.get("upgrade", "")) or "upgrade" not in _token_list(
        req.headers.get("connection", "")
    ):
        return error_response(400, "bad_request", "not a WebSocket upgrade request")
    if req.headers.get("sec-websocket-version", "").strip() != "13":
        resp = error_response(400, "bad_request", "unsupported WebSocket version")
        resp.add_header("Sec-WebSocket-Version", "13")
        return resp
    key = req.headers.get("sec-websocket-key", "").strip()
    if not _KEY_RE.match(key):
        return error_response(400, "bad_request", "invalid Sec-WebSocket-Key")
    if registry.count() >= registry.total or registry.ip_count(req.remote_addr) >= registry.per_ip:
        return error_response(503, "unavailable", "too many WebSocket connections", retry_after=5)
    wait = registry.handshake_retry_after(req.remote_addr, session["user_id"])
    if wait is not None:
        return error_response(429, "rate_limited", "too many connection attempts", retry_after=wait)

    async def run(reader: Any, writer: Any) -> None:
        await registry.run(req, hub, reader)

    headers = [
        ("Upgrade", "websocket"),
        ("Connection", "Upgrade"),
        ("Sec-WebSocket-Accept", accept_key(key)),
    ]
    return Response(101, headers, upgrade=run)
