"""LAN auto-discovery over UDP broadcast.

A client broadcasts a probe; every server answers with its room name and TCP
port. Both directions are built/parsed here so the format lives in one place.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import List, Optional

from .protocol import DISCOVERY_MAGIC, DISCOVERY_PORT, DISCOVERY_REPLY

log = logging.getLogger(__name__)

MAX_ROOM_LEN = 64


@dataclass(frozen=True)
class ServerInfo:
    """A DeskTalk server found on the LAN."""

    host: str
    port: int
    room: str
    users: int
    locked: bool  # True if the server requires a password


# ---- wire format -----------------------------------------------------------

def build_probe() -> bytes:
    return DISCOVERY_MAGIC.encode("utf-8")


def is_probe(data: bytes) -> bool:
    return data.decode("utf-8", "ignore").strip() == DISCOVERY_MAGIC


def build_reply(room: str, port: int, users: int, locked: bool) -> bytes:
    return json.dumps({
        "magic": DISCOVERY_REPLY,
        "room": room,
        "port": port,
        "users": users,
        "locked": locked,
    }).encode("utf-8")


def parse_reply(data: bytes, host: str) -> Optional[ServerInfo]:
    """Parse a server reply. Returns None for anything malformed or foreign.

    UDP is unauthenticated: any host on the LAN can send us garbage, so every
    field is validated and nothing here is allowed to raise.
    """
    try:
        info = json.loads(data.decode("utf-8"))
        if not isinstance(info, dict) or info.get("magic") != DISCOVERY_REPLY:
            return None
        port = info.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            return None
        users = info.get("users", 0)
        users = users if isinstance(users, int) and not isinstance(users, bool) and users >= 0 else 0
        return ServerInfo(
            host=host,
            port=port,
            room=str(info.get("room", ""))[:MAX_ROOM_LEN],
            users=users,
            locked=info.get("locked") is True,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


# ---- scanning --------------------------------------------------------------

def broadcast_addresses() -> List[str]:
    """Global broadcast + the (assumed /24) broadcast address of every local interface."""
    addrs = ["255.255.255.255"]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            parts = ip.split(".")
            addrs.append(".".join(parts[:3] + ["255"]))
    except OSError as exc:
        log.debug("could not enumerate local interfaces: %s", exc)
    return list(dict.fromkeys(addrs))


def scan(timeout: float = 1.5) -> List[ServerInfo]:
    """Look for servers on the LAN for ``timeout`` seconds. Never raises."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        log.warning("LAN scan: socket creation failed (%s)", exc)
        return []

    found = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)

        probe = build_probe()
        for addr in broadcast_addresses():
            try:
                sock.sendto(probe, (addr, DISCOVERY_PORT))
            except OSError as exc:
                log.debug("probe to %s failed: %s", addr, exc)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows: an ICMP "port unreachable" for one of our probes surfaces
                # on the *next* recvfrom as WSAECONNRESET. It is not fatal - keep listening.
                continue
            except OSError as exc:
                log.debug("scan receive stopped: %s", exc)
                break
            server = parse_reply(data, addr[0])
            if server is not None:
                found[server.host] = server
    except OSError as exc:
        log.warning("LAN scan failed: %s", exc)
    finally:
        sock.close()

    return sorted(found.values(), key=lambda s: s.host)
