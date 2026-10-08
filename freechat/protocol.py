"""Newline-delimited JSON protocol shared by server and clients.

Har message ek line hota hai: JSON object + "\n".
Isse TCP stream ko aasani se message boundaries me tod sakte hain.
"""

import json
import time

# Default ports
DEFAULT_PORT = 9009          # TCP chat port
DISCOVERY_PORT = 9010        # UDP auto-discovery port
DISCOVERY_MAGIC = "FREECHAT_DISCOVER_V1"
DISCOVERY_REPLY = "FREECHAT_SERVER_V1"

MAX_LINE = 64 * 1024         # 64 KB per message, spam/abuse guard


def encode(obj: dict) -> bytes:
    """dict -> ek line of bytes."""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes) -> dict:
    """ek line of bytes -> dict. Galat JSON pe ValueError."""
    return json.loads(line.decode("utf-8").strip())


def now_ts() -> float:
    return time.time()


def fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))
