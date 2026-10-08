#!/usr/bin/env python3
"""FreeChat LAN server - asyncio TCP chat hub + UDP auto-discovery.

Chalaane ke liye:
    python server.py                       # sab interfaces, port 9009
    python server.py --port 9009 --name "Pravin ka room"

Sirf standard library. Windows / macOS / Linux sab pe chalta hai.
"""

import argparse
import asyncio
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from freechat.protocol import (  # noqa: E402
    DEFAULT_PORT,
    DISCOVERY_MAGIC,
    DISCOVERY_PORT,
    DISCOVERY_REPLY,
    MAX_LINE,
    encode,
    fmt_time,
    now_ts,
)

MAX_HISTORY = 100


class Client:
    def __init__(self, writer):
        self.writer = writer
        self.user = None
        self.addr = writer.get_extra_info("peername")

    async def send(self, obj):
        try:
            self.writer.write(encode(obj))
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass  # cleanup disconnect handler me hota hai


class ChatServer:
    def __init__(self, room_name, port):
        self.room_name = room_name
        self.port = port
        self.clients = set()
        self.history = []

    # ---------- helpers ----------

    def users(self):
        return sorted(c.user for c in self.clients if c.user)

    def find(self, user):
        low = user.lower()
        for c in self.clients:
            if c.user and c.user.lower() == low:
                return c
        return None

    async def broadcast(self, obj, skip=None):
        for c in list(self.clients):
            if c is not skip and c.user:
                await c.send(obj)

    async def push_users(self):
        await self.broadcast({"type": "users", "users": self.users()})

    def log(self, text):
        print("[" + fmt_time(now_ts()) + "] " + text, flush=True)

    # ---------- connection lifecycle ----------

    async def handle(self, reader, writer):
        client = Client(writer)
        self.clients.add(client)
        self.log("connect {}".format(client.addr))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > MAX_LINE:
                    await client.send({"type": "error", "text": "Message bahut bada hai."})
                    continue
                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except (ValueError, UnicodeDecodeError):
                    await client.send({"type": "error", "text": "Invalid message format."})
                    continue
                if isinstance(msg, dict):
                    await self.dispatch(client, msg)
        except (ConnectionError, OSError):
            pass
        finally:
            await self.disconnect(client)

    async def disconnect(self, client):
        self.clients.discard(client)
        try:
            client.writer.close()
        except OSError:
            pass
        if client.user:
            self.log("left {}".format(client.user))
            await self.broadcast({
                "type": "system",
                "text": client.user + " chat se nikal gaya.",
                "ts": now_ts(),
            })
            await self.push_users()

    # ---------- message routing ----------

    async def dispatch(self, client, msg):
        kind = msg.get("type")

        if kind == "join":
            await self.on_join(client, msg)
            return

        if not client.user:
            await client.send({"type": "error", "text": "Pehle join karo."})
            return

        if kind == "msg":
            text = str(msg.get("text", "")).strip()
            if not text:
                return
            out = {"type": "msg", "user": client.user, "text": text, "ts": now_ts()}
            self.history.append(out)
            del self.history[:-MAX_HISTORY]
            await self.broadcast(out)

        elif kind == "dm":
            to = str(msg.get("to", "")).strip()
            text = str(msg.get("text", "")).strip()
            target = self.find(to)
            if not target:
                await client.send({"type": "error", "text": "'" + to + "' naam ka koi online nahi hai."})
                return
            payload = {
                "type": "dm",
                "user": client.user,
                "to": target.user,
                "text": text,
                "ts": now_ts(),
            }
            await target.send(payload)
            if target is not client:
                await client.send(payload)

        elif kind == "who":
            await client.send({"type": "users", "users": self.users()})

        elif kind == "ping":
            await client.send({"type": "pong", "ts": now_ts()})

    async def on_join(self, client, msg):
        if client.user:
            await client.send({"type": "error", "text": "Already joined."})
            return
        name = str(msg.get("user", "")).strip()[:24]
        if not name or any(ch.isspace() for ch in name):
            await client.send({"type": "error", "text": "Naam khaali ya space-wala nahi ho sakta."})
            return
        if self.find(name):
            await client.send({
                "type": "error",
                "text": "'" + name + "' naam already use me hai, dusra try karo.",
            })
            return

        client.user = name
        self.log("join {} from {}".format(name, client.addr))
        await client.send({
            "type": "welcome",
            "room": self.room_name,
            "user": name,
            "users": self.users(),
            "ts": now_ts(),
        })
        for old in self.history[-30:]:
            await client.send(old)
        await self.broadcast(
            {"type": "system", "text": name + " chat me aaya.", "ts": now_ts()},
            skip=client,
        )
        await self.push_users()


# ---------- UDP discovery responder ----------

class DiscoveryProtocol(asyncio.DatagramProtocol):
    """Client broadcast bhejta hai, server apna port + naam wapas bhejta hai."""

    def __init__(self, server):
        self.server = server
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.decode("utf-8", "ignore").strip() != DISCOVERY_MAGIC:
            return
        reply = json.dumps({
            "magic": DISCOVERY_REPLY,
            "room": self.server.room_name,
            "port": self.server.port,
            "users": len(self.server.users()),
        }).encode("utf-8")
        if self.transport:
            self.transport.sendto(reply, addr)


def local_ips():
    """Is machine ke LAN IPs - dost ko dene ke liye."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # koi packet nahi jaata, sirf routing table dekhta hai
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


async def main():
    ap = argparse.ArgumentParser(description="FreeChat LAN server")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: sab interfaces)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default=socket.gethostname() + " room", help="room ka naam")
    ap.add_argument("--no-discovery", action="store_true", help="UDP auto-discovery band karo")
    args = ap.parse_args()

    chat = ChatServer(args.name, args.port)
    srv = await asyncio.start_server(chat.handle, args.host, args.port)

    if not args.no_discovery:
        loop = asyncio.get_running_loop()
        try:
            await loop.create_datagram_endpoint(
                lambda: DiscoveryProtocol(chat),
                local_addr=("0.0.0.0", DISCOVERY_PORT),
                allow_broadcast=True,
            )
            print("Auto-discovery ON (UDP {})".format(DISCOVERY_PORT))
        except OSError as e:
            print("Discovery start nahi hua ({}) - manual IP se connect karo".format(e))

    print("")
    print("  FreeChat server chal raha hai: '{}'".format(chat.room_name))
    print("  TCP port: {}".format(args.port))
    print("  Dost ko ye address do:")
    for ip in local_ips() or ["<apna LAN IP>"]:
        print("      {}:{}".format(ip, args.port))
    print("")
    print("  Band karne ke liye Ctrl+C")
    print("")

    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer band.")
