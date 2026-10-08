"""UDP discovery responder: answers LAN probes with this server's address."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Tuple

from ..discovery import build_reply, is_probe
from .hub import ChatServer

log = logging.getLogger(__name__)


class DiscoveryResponder(asyncio.DatagramProtocol):
    """A client broadcasts a probe; we answer with room name, TCP port and user count."""

    def __init__(self, hub: ChatServer) -> None:
        self.hub = hub
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Tuple[Any, ...]) -> None:
        # An exception escaping a protocol callback only lands in the loop's error
        # handler; catch it here so a bad packet is a log line, not noise.
        try:
            if self.transport is None or not is_probe(data):
                return
            reply = build_reply(self.hub.room_name, self.hub.port, len(self.hub.users()), self.hub.locked)
            self.transport.sendto(reply, addr)
        except Exception:  # noqa: BLE001
            log.exception("discovery reply to %s failed", addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("discovery socket error: %s", exc)
