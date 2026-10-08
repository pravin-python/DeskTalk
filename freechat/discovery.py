"""LAN pe chal rahe FreeChat servers dhoondhne ke liye UDP broadcast scan."""

import json
import socket
import time

from .protocol import DISCOVERY_MAGIC, DISCOVERY_PORT, DISCOVERY_REPLY


def broadcast_addresses():
    """Global broadcast + har local subnet ka broadcast address."""
    addrs = ["255.255.255.255"]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            parts = ip.split(".")
            addrs.append(".".join(parts[:3] + ["255"]))  # /24 maan ke
    except OSError:
        pass
    return list(dict.fromkeys(addrs))


def scan(timeout=1.5):
    """LAN scan karo. Return: list of {"host", "port", "room", "users", "locked"}."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)

    payload = DISCOVERY_MAGIC.encode("utf-8")
    for addr in broadcast_addresses():
        try:
            sock.sendto(payload, (addr, DISCOVERY_PORT))
        except OSError:
            continue

    found = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            info = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if info.get("magic") != DISCOVERY_REPLY:
            continue
        host = addr[0]
        found[host] = {
            "host": host,
            "port": int(info.get("port", 0)),
            "room": str(info.get("room", "")),
            "users": int(info.get("users", 0)),
            "locked": bool(info.get("locked", False)),
        }

    sock.close()
    return sorted(found.values(), key=lambda s: s["host"])
