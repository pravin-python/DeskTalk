"""Newline-delimited JSON wire protocol shared by the server and the clients.

Every message is one line: a JSON object followed by ``"\\n"``. That makes it
trivial to split the TCP byte stream back into messages.

This module also owns all input validation, so the server never has to trust
what a peer sends.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict

from .errors import ProtocolError

Message = Dict[str, Any]

# ---- ports / discovery -----------------------------------------------------
DEFAULT_PORT = 9009          # TCP chat port
DISCOVERY_PORT = 9010        # UDP auto-discovery port
DISCOVERY_MAGIC = "DESKTALK_DISCOVER_V1"
DISCOVERY_REPLY = "DESKTALK_SERVER_V1"

# ---- limits ----------------------------------------------------------------
MAX_LINE = 64 * 1024         # bytes per wire message (spam / abuse guard)
MAX_NAME_LEN = 24
MAX_TEXT_LEN = 4000          # characters per chat message

# ---- heartbeat -------------------------------------------------------------
# The client pings every PING_INTERVAL. The server drops a connection that sent
# nothing for IDLE_TIMEOUT; the client treats a server that sent nothing for
# DEAD_AFTER as dead and reconnects.
PING_INTERVAL = 15.0
IDLE_TIMEOUT = 45.0
DEAD_AFTER = 45.0

# ---- history ---------------------------------------------------------------
HISTORY_ON_JOIN = 30         # messages replayed to a brand-new user
HISTORY_ON_REJOIN = 200      # max messages replayed after a reconnect

# ---- message types ---------------------------------------------------------
# client -> server
T_JOIN = "join"
T_MSG = "msg"
T_DM = "dm"
T_WHO = "who"
T_PING = "ping"
# server -> client
T_WELCOME = "welcome"
T_USERS = "users"
T_SYSTEM = "system"
T_ERROR = "error"
T_PONG = "pong"
# synthetic events produced by the client library itself
EV_DISCONNECTED = "disconnected"
EV_RECONNECTING = "reconnecting"

# ---- join error codes (server closes the connection after sending one) -----
ERR_AUTH = "auth"
ERR_BAD_NAME = "bad_name"
ERR_NAME_TAKEN = "name_taken"


# ---- encoding --------------------------------------------------------------

def scrub(text: str) -> str:
    """Replace characters UTF-8 cannot encode (e.g. lone surrogates from ``"\\ud800"``).

    JSON happily decodes such escapes into a str that then blows up in
    ``.encode("utf-8")`` - one hostile message must not break every receiver.
    """
    return text.encode("utf-8", "replace").decode("utf-8")


def encode(obj: Message) -> bytes:
    """dict -> one wire line. Raises ProtocolError if it cannot be serialised."""
    try:
        return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8", "replace")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Failed to encode message: {}".format(exc))


def decode(line: bytes) -> Message:
    """One wire line -> dict. Raises ProtocolError for anything that is not a JSON object."""
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError: a 64 KB line of "[[[[..." is valid length but nests too deep.
        raise ProtocolError("Invalid message format.")
    if not isinstance(obj, dict):
        raise ProtocolError("Invalid message format.")
    return obj


# ---- validation ------------------------------------------------------------

def clean_name(raw: Any) -> str:
    """Validate and normalise a user name. Raises ProtocolError with a user-facing text."""
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ProtocolError("Name must be a string.")
    name = scrub(raw).strip()[:MAX_NAME_LEN]
    if not name or any(ch.isspace() for ch in name) or not name.isprintable():
        raise ProtocolError("Name cannot be empty or contain spaces.")
    return name


def clean_text(raw: Any) -> str:
    """Validate a chat message body. Returns "" for empty input (callers ignore those)."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ProtocolError("Message must be a string.")
    text = scrub(raw).strip()
    if len(text) > MAX_TEXT_LEN:
        raise ProtocolError("Message is too long (max {} characters).".format(MAX_TEXT_LEN))
    return text


# ---- time ------------------------------------------------------------------

def now_ts() -> float:
    return time.time()


def fmt_time(ts: Any) -> str:
    """Local HH:MM:SS for a unix timestamp; "" if the value is unusable."""
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
