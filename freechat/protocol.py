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

# Heartbeat: client har PING_INTERVAL pe ping bhejta hai; server IDLE_TIMEOUT tak
# kuch na aaye to client ko hata deta hai; client DEAD_AFTER tak server se kuch na
# aaye to connection mara hua maan ke reconnect karta hai.
PING_INTERVAL = 15.0
IDLE_TIMEOUT = 45.0
DEAD_AFTER = 45.0

HISTORY_ON_JOIN = 30         # naye user ko itne purane messages milte hain
HISTORY_ON_REJOIN = 200      # reconnect pe jo miss hua wo (max) itna

# Join fail hone pe server error me "code" bhejta hai aur connection band kar deta hai.
ERR_AUTH = "auth"
ERR_BAD_NAME = "bad_name"
ERR_NAME_TAKEN = "name_taken"


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
