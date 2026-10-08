"""DeskTalk terminal client.

    python chat_cli.py --host 172.31.1.143 --name pravin
    python chat_cli.py --scan                 # find a server on the LAN
    python chat_cli.py --host 172.31.1.143 --password secret   # or env DESKTALK_PASSWORD

In-chat commands:
    /dm <naam> <message>    private message
    /who                    who is online
    /quit                   exit
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from typing import List, Optional, Sequence

from .. import __version__, discovery
from ..argtypes import port_number
from ..client import ChatClient
from ..discovery import ServerInfo
from ..errors import ProtocolError
from ..log import make_stream_safe, setup_logging
from ..protocol import (
    DEFAULT_PORT,
    EV_DISCONNECTED,
    EV_RECONNECTING,
    T_DM,
    T_ERROR,
    T_MSG,
    T_SYSTEM,
    T_USERS,
    T_WELCOME,
    Message,
    clean_name,
    fmt_time,
)
from .common import default_username, parse_input

log = logging.getLogger(__name__)

# ANSI colours (Windows 10+ terminals support them once enabled below)
C_SYS, C_ME, C_OTHER, C_DM, C_ERR, C_OFF = (
    "\033[90m", "\033[92m", "\033[96m", "\033[95m", "\033[91m", "\033[0m")


def _enable_ansi() -> None:
    if sys.platform == "win32":
        os.system("")  # flips the console into VT mode


class ConsoleView:
    """Renders server events to the terminal."""

    def __init__(self, me: str) -> None:
        self.me = me
        self.alive = True

    @staticmethod
    def _say(color: str, text: str) -> None:
        print("{}{}{}".format(color, text, C_OFF), flush=True)

    def on_event(self, ev: Message) -> None:
        kind = ev.get("type")
        stamp = fmt_time(ev["ts"]) if ev.get("ts") else ""
        text = str(ev.get("text", ""))

        if kind == T_WELCOME:
            users = ev.get("users")
            online = ", ".join(map(str, users)) if isinstance(users, list) and users else "only you"
            if ev.get("rejoined"):
                self._say(C_SYS, "-- Reconnected. Online: " + online)
            else:
                self._say(C_SYS, "-- Joined '{}'. Online: {}".format(ev.get("room", "room"), online))

        elif kind == T_MSG:
            user = str(ev.get("user", "?"))
            color = C_ME if user == self.me else C_OTHER
            print("{} {}{}{}: {}".format(stamp, color, user, C_OFF, text), flush=True)

        elif kind == T_DM:
            sender, to = str(ev.get("user", "?")), str(ev.get("to", "?"))
            label = "[DM -> {}]".format(to) if sender == self.me else "[DM from {}]".format(sender)
            print("{} {}{}{} {}".format(stamp, C_DM, label, C_OFF, text), flush=True)

        elif kind == T_SYSTEM:
            self._say(C_SYS, "-- " + text)

        elif kind == T_USERS:
            users = ev.get("users")
            self._say(C_SYS, "-- online: " + (", ".join(map(str, users)) if isinstance(users, list) else ""))

        elif kind == T_ERROR:
            self._say(C_ERR, "!! " + text)

        elif kind == EV_RECONNECTING:
            self._say(C_SYS, "-- reconnecting attempt #{} (in {:g}s)...".format(
                ev.get("attempt", 0), ev.get("delay", 0)))

        elif kind == EV_DISCONNECTED:
            if ev.get("reconnecting"):
                self._say(C_ERR, "-- Connection lost, trying to reconnect...")
            else:
                self._say(C_ERR, "-- Disconnected. Press Enter to exit.")
                self.alive = False


def pick_server() -> Optional[ServerInfo]:
    """Scan the LAN and let the user choose when several servers answer."""
    print("Scanning LAN for servers...")
    servers: List[ServerInfo] = discovery.scan(timeout=2.0)
    if not servers:
        print("No server found. Specify IP using --host.")
        return None
    for i, s in enumerate(servers, 1):
        print("  [{}] {}:{}  ({}, {} online){}".format(
            i, s.host, s.port, s.room, s.users, "  [password]" if s.locked else ""))
    if len(servers) == 1:
        return servers[0]
    try:
        choice = int(input("Select server [1]: ").strip() or "1")
        return servers[choice - 1]
    except (ValueError, IndexError):
        return servers[0]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="chat_cli.py", description="DeskTalk terminal client")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=port_number, default=DEFAULT_PORT)
    ap.add_argument("--name", default=default_username())
    ap.add_argument("--scan", action="store_true", help="find a server on the LAN")
    ap.add_argument("--password", default=None, help="server password (or env DESKTALK_PASSWORD)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--log-file", default=None, help="write logs to this file")
    ap.add_argument("--version", action="version", version="DeskTalk " + __version__)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    make_stream_safe(sys.stdout)
    make_stream_safe(sys.stdin)  # undecodable input becomes '?', it is never dropped
    setup_logging(verbose=args.verbose, log_file=args.log_file)
    _enable_ansi()

    try:
        name = clean_name(args.name)
    except ProtocolError as exc:
        print("{}Invalid name: {}{}".format(C_ERR, exc, C_OFF))
        return 2

    password = args.password or os.environ.get("DESKTALK_PASSWORD") or ""
    host, port = args.host, args.port
    try:
        if args.scan or not host:
            found = pick_server()
            if found is None:
                return 1
            host, port = found.host, found.port
            if found.locked and not password:
                password = getpass.getpass("Server password: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return 130

    view = ConsoleView(name)
    client = ChatClient(host, port, name, view.on_event, password=password)
    try:
        client.connect()
    except OSError as exc:
        print("{}Failed to connect to {}:{} - {}{}".format(C_ERR, host, port, exc, C_OFF))
        return 1

    print("{}Connected to {}:{} as '{}'. Type /quit to exit.{}".format(C_SYS, host, port, name, C_OFF))
    try:
        return _input_loop(client, view)
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
        print("\nBye.")


def _input_loop(client: ChatClient, view: ConsoleView) -> int:
    while view.alive:
        try:
            line = input()
        except EOFError:
            break
        except UnicodeDecodeError:
            print("{}-- Could not read input (encoding error).{}".format(C_ERR, C_OFF))
            continue

        cmd = parse_input(line)
        if cmd is None:
            continue
        if cmd.kind == "quit":
            break
        if cmd.kind == "error":
            print("{}{}{}".format(C_ERR, cmd.error, C_OFF))
            continue

        if cmd.kind == "who":
            ok = client.who()
        elif cmd.kind == "dm":
            ok = client.dm(cmd.to, cmd.text)
        else:
            ok = client.say(cmd.text)
        if not ok:
            print("{}-- Not connected currently; message not sent.{}".format(C_ERR, C_OFF))
    return 0


if __name__ == "__main__":
    sys.exit(main())
