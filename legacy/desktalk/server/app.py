"""Server entry point: argument parsing, startup, graceful shutdown.

    python server.py                         # all interfaces, port 9009
    python server.py --port 9009 --name "Pravin ka room"
    python server.py --password secret       # shared join password
    DESKTALK_PASSWORD=secret python server.py  # same, but hidden from the process list
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import os
import socket
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .. import __version__
from ..argtypes import port_number
from ..log import make_stream_safe, setup_logging
from ..protocol import DEFAULT_PORT, DISCOVERY_PORT, MAX_LINE
from ..store import open_store
from .hub import ChatServer
from .responder import DiscoveryResponder

log = logging.getLogger(__name__)

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "desktalk.db")
_ADDR_IN_USE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    room_name: str = ""
    password: Optional[str] = None
    db_path: str = DEFAULT_DB
    discovery: bool = True
    verbose: bool = False
    log_file: Optional[str] = None


# ---------- CLI ----------

def parse_args(argv: Optional[Sequence[str]] = None) -> ServerConfig:
    ap = argparse.ArgumentParser(prog="server.py", description="DeskTalk LAN server")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--port", type=port_number, default=DEFAULT_PORT)
    ap.add_argument("--name", default=socket.gethostname() + " room", help="room name")
    ap.add_argument("--password", default=None,
                    help="shared join password (or env DESKTALK_PASSWORD)")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="history SQLite file (default: desktalk.db); ':memory:' = RAM only")
    ap.add_argument("--no-discovery", action="store_true", help="disable UDP auto-discovery")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--log-file", default=None, help="also write logs to this file")
    ap.add_argument("--version", action="version", version="DeskTalk " + __version__)
    args = ap.parse_args(argv)
    return ServerConfig(
        host=args.host,
        port=args.port,
        room_name=args.name,
        password=args.password or os.environ.get("DESKTALK_PASSWORD") or None,
        db_path=args.db,
        discovery=not args.no_discovery,
        verbose=args.verbose,
        log_file=args.log_file,
    )


# ---------- helpers ----------

def local_ips() -> List[str]:
    """This machine's LAN addresses - what you give to friends."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packet is sent; this only consults the routing table
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
    """Route asyncio's own error reports through logging; ignore benign socket resets."""
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return  # a peer vanished mid-write (very common on Windows) - not an error
    log.error("asyncio: %s", context.get("message", "unhandled error"), exc_info=exc)


def _print_banner(cfg: ServerConfig, chat: ChatServer, persistent: bool) -> None:
    lines = [
        "",
        "  DeskTalk server running: '{}'".format(chat.room_name),
        "  TCP port: {}".format(cfg.port),
        "  Password: {}".format("ON" if chat.locked else "OFF (anyone can join)"),
        "  History : {}".format(cfg.db_path if persistent else "RAM only (cleared on restart)"),
        "  Share this address with others:",
    ]
    lines += ["      {}:{}".format(ip, cfg.port) for ip in local_ips() or ["<your LAN IP>"]]
    lines += ["", "  Press Ctrl+C to stop", ""]
    print("\n".join(lines), flush=True)


async def _start_discovery(chat: ChatServer) -> Optional[asyncio.BaseTransport]:
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: DiscoveryResponder(chat),
            local_addr=("0.0.0.0", DISCOVERY_PORT),
            allow_broadcast=True,
        )
    except OSError as exc:
        log.warning("Discovery failed to start (%s) - connect using manual IP", exc)
        return None
    log.info("Auto-discovery ON (UDP %d)", DISCOVERY_PORT)
    return transport


# ---------- run ----------

async def serve(cfg: ServerConfig) -> None:
    """Run until cancelled; always releases sockets and the history DB on the way out."""
    asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)

    store = open_store(cfg.db_path)
    chat = ChatServer(cfg.room_name, cfg.port, store, cfg.password)
    server: Optional[asyncio.AbstractServer] = None
    discovery: Optional[asyncio.BaseTransport] = None
    try:
        server = await asyncio.start_server(chat.handle, cfg.host, cfg.port, limit=MAX_LINE)
        if cfg.discovery:
            discovery = await _start_discovery(chat)
        _print_banner(cfg, chat, store.persistent)
        await server.serve_forever()
    finally:
        try:
            if server is not None:
                server.close()
            await chat.shutdown()
            if discovery is not None:
                discovery.close()
        except Exception:  # noqa: BLE001 - never let cleanup mask the original exit reason
            log.exception("error during shutdown")
        finally:
            store.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console entry point. Returns the process exit code."""
    cfg = parse_args(argv)
    make_stream_safe(sys.stdout)
    setup_logging(verbose=cfg.verbose, log_file=cfg.log_file)
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        print("\nServer stopped.")
        return 0
    except OSError as exc:
        if exc.errno in _ADDR_IN_USE:
            log.error("Port %d is already in use - specify another --port, or stop the existing server.", cfg.port)
        else:
            log.error("Failed to start server: %s", exc)
        return 1
    except Exception:  # noqa: BLE001
        log.exception("server crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
