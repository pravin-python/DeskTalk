"""One client connection on the server side."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..errors import ProtocolError
from ..protocol import Message, encode

log = logging.getLogger(__name__)

OUTBOX_MAX = 256       # queued messages per client; beyond that the client is too slow -> kick
SEND_TIMEOUT = 10.0    # max seconds a single write may take to drain
FLUSH_TIMEOUT = 2.0    # max seconds to flush pending messages when disconnecting


class Connection:
    """A connected client.

    All sending goes through a bounded queue drained by its own task, so one slow
    or hung client can never stall the broadcast to everybody else. If its queue
    overflows or a write times out, the client is aborted.
    """

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.user: Optional[str] = None
        self.addr: Any = writer.get_extra_info("peername")
        self._outbox: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(maxsize=OUTBOX_MAX)
        self._closing = False
        self.task = asyncio.ensure_future(self._pump())

    @property
    def label(self) -> str:
        """Human-readable identity for log lines."""
        return self.user or str(self.addr)

    # ---- sending (all non-blocking) ----

    def send(self, obj: Message) -> None:
        """Queue a message. Never raises."""
        try:
            data = encode(obj)
        except ProtocolError as exc:
            log.error("dropping unencodable message for %s: %s", self.label, exc)
            return
        self.send_bytes(data)

    def send_bytes(self, data: bytes) -> None:
        """Queue an already-encoded line (lets a broadcast encode once for everyone)."""
        if self._closing:
            return
        try:
            self._outbox.put_nowait(data)
        except asyncio.QueueFull:
            log.warning("%s is too slow (outbox full) - disconnecting", self.label)
            self.abort()

    def finish(self) -> None:
        """Flush what is queued, then close. Nothing can be sent afterwards."""
        if self._closing:
            return
        self._closing = True
        try:
            self._outbox.put_nowait(None)
        except asyncio.QueueFull:
            self.abort()

    def abort(self) -> None:
        """Drop the connection immediately, discarding unsent data."""
        self._closing = True
        try:
            self.writer.transport.abort()
        except Exception as exc:  # noqa: BLE001 - transport may already be gone
            log.debug("abort of %s: %s", self.label, exc)

    async def wait_closed(self, timeout: float = FLUSH_TIMEOUT) -> None:
        """Wait for the writer task to finish flushing; abort if it takes too long."""
        try:
            await asyncio.wait_for(self.task, timeout)
        except asyncio.TimeoutError:
            self.abort()

    # ---- writer task ----

    async def _pump(self) -> None:
        try:
            while True:
                data = await self._outbox.get()
                if data is None:
                    break
                self.writer.write(data)
                await asyncio.wait_for(self.writer.drain(), SEND_TIMEOUT)
        except asyncio.CancelledError:
            self.abort()
            raise
        except (asyncio.TimeoutError, OSError) as exc:
            log.debug("write to %s failed: %r", self.label, exc)
            self.abort()
            return
        except Exception:  # noqa: BLE001 - keep a bug here from becoming an unretrieved task error
            log.exception("writer task for %s crashed", self.label)
            self.abort()
            return
        try:
            self.writer.close()
        except OSError as exc:
            log.debug("close of %s: %s", self.label, exc)
