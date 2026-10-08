#!/usr/bin/env python3
"""FreeChat - terminal client.

    python chat_cli.py --host 172.31.1.143 --name pravin
    python chat_cli.py --scan            # LAN pe server dhoondo

Commands chat ke andar:
    /dm <naam> <message>    private message
    /who                    kaun online hai
    /quit                   exit
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from freechat import discovery  # noqa: E402
from freechat.client import ChatClient  # noqa: E402
from freechat.protocol import DEFAULT_PORT, fmt_time  # noqa: E402

# ANSI colors - Windows 10+ terminal bhi support karta hai
C_SYS, C_ME, C_OTHER, C_DM, C_ERR, C_OFF = (
    "\033[90m", "\033[92m", "\033[96m", "\033[95m", "\033[91m", "\033[0m")

if sys.platform == "win32":
    os.system("")  # ANSI escape codes enable karta hai


class CLI:
    def __init__(self, me):
        self.me = me
        self.alive = True

    def on_event(self, ev):
        kind = ev.get("type")
        stamp = fmt_time(ev["ts"]) if ev.get("ts") else ""

        if kind == "welcome":
            online = ", ".join(ev.get("users", [])) or "sirf tum"
            print("{}-- '{}' me aa gaye. Online: {}{}".format(
                C_SYS, ev.get("room", "room"), online, C_OFF))

        elif kind == "msg":
            color = C_ME if ev.get("user") == self.me else C_OTHER
            print("{} {}{}{}: {}".format(stamp, color, ev.get("user", "?"), C_OFF, ev.get("text", "")))

        elif kind == "dm":
            sender, to = ev.get("user", "?"), ev.get("to", "?")
            label = "[DM -> {}]".format(to) if sender == self.me else "[DM from {}]".format(sender)
            print("{} {}{}{} {}".format(stamp, C_DM, label, C_OFF, ev.get("text", "")))

        elif kind == "system":
            print("{}-- {}{}".format(C_SYS, ev.get("text", ""), C_OFF))

        elif kind == "users":
            print("{}-- online: {}{}".format(C_SYS, ", ".join(ev.get("users", [])), C_OFF))

        elif kind == "error":
            print("{}!! {}{}".format(C_ERR, ev.get("text", ""), C_OFF))

        elif kind == "disconnected":
            print("{}-- connection toot gaya.{}".format(C_ERR, C_OFF))
            self.alive = False


def pick_server():
    print("LAN scan kar raha hoon...")
    servers = discovery.scan(timeout=2.0)
    if not servers:
        print("Koi server nahi mila. --host se IP do.")
        return None
    for i, s in enumerate(servers, 1):
        print("  [{}] {}:{}  ({}, {} online)".format(i, s["host"], s["port"], s["room"], s["users"]))
    if len(servers) == 1:
        return servers[0]
    try:
        choice = int(input("Kaunsa? [1]: ").strip() or "1")
        return servers[choice - 1]
    except (ValueError, IndexError):
        return servers[0]


def main():
    ap = argparse.ArgumentParser(description="FreeChat terminal client")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default=os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    ap.add_argument("--scan", action="store_true", help="LAN pe server dhoondo")
    args = ap.parse_args()

    host, port = args.host, args.port
    if args.scan or not host:
        found = pick_server()
        if not found:
            return 1
        host, port = found["host"], found["port"]

    cli = CLI(args.name)
    client = ChatClient(host, port, args.name, cli.on_event)
    try:
        client.connect()
    except OSError as e:
        print("{}Connect nahi hua {}:{} - {}{}".format(C_ERR, host, port, e, C_OFF))
        return 1

    print("{}Connected to {}:{} as '{}'. /quit se nikalo.{}".format(C_SYS, host, port, args.name, C_OFF))

    try:
        while cli.alive:
            try:
                line = input()
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            if line == "/quit":
                break
            if line in ("/who", "/users"):
                client.who()
            elif line.startswith("/dm "):
                rest = line[4:].strip()
                if " " not in rest:
                    print("{}use: /dm <naam> <message>{}".format(C_ERR, C_OFF))
                    continue
                to, body = rest.split(" ", 1)
                client.dm(to, body)
            else:
                client.say(line)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        print("\nBye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
