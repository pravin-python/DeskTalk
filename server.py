#!/usr/bin/env python3
"""FreeChat LAN server - asyncio TCP chat hub + UDP auto-discovery.

Chalaane ke liye:
    python server.py                       # sab interfaces, port 9009
    python server.py --port 9009 --name "Pravin ka room"
    python server.py --password secret     # join ke liye shared password
    set FREECHAT_PASSWORD=secret           # (ya env var - process list me nahi dikhta)

Chat history SQLite me save hoti hai (default: server.py ke saath freechat.db).

Sirf standard library. Windows / macOS / Linux sab pe chalta hai.
"""

import argparse
import asyncio
import hmac
import json
import os
import socket
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from freechat.protocol import (  # noqa: E402
    DEFAULT_PORT,
    DISCOVERY_MAGIC,
    DISCOVERY_PORT,
    DISCOVERY_REPLY,
    ERR_AUTH,
    ERR_BAD_NAME,
    ERR_NAME_TAKEN,
    HISTORY_ON_JOIN,
    HISTORY_ON_REJOIN,
    IDLE_TIMEOUT,
    MAX_LINE,
    encode,
    fmt_time,
    now_ts,
)
from freechat.store import HistoryStore  # noqa: E402

OUTBOX_MAX = 256        # ek client ke liye itne messages queue me; zyada = slow client, kick
SEND_TIMEOUT = 10.0     # ek write drain hone ki max der
FLUSH_TIMEOUT = 2.0     # disconnect pe pending messages bhejne ki max der
AUTH_DELAY = 1.0        # galat password pe itni der ruk ke jawab (brute-force slow)


class Client:
    """Ek connection. Bhejna queue ke through hota hai, taaki ek slow client baaki sabko na roke."""

    def __init__(self, writer):
        self.writer = writer
        self.user = None
        self.addr = writer.get_extra_info("peername")
        self.outbox = asyncio.Queue(maxsize=OUTBOX_MAX)
        self.closing = False
        self.task = asyncio.ensure_future(self._pump())

    def send(self, obj):
        """Non-blocking: message queue me daalo. Queue bhar gayi to client slow hai - kick."""
        if self.closing:
            return
        try:
            self.outbox.put_nowait(obj)
        except asyncio.QueueFull:
            self.abort()

    def finish(self):
        """Queue ke saare messages bhej ke connection band karo (jaise error ke baad)."""
        if self.closing:
            return
        self.closing = True
        try:
            self.outbox.put_nowait(None)
        except asyncio.QueueFull:
            self.abort()

    def abort(self):
        self.closing = True
        try:
            self.writer.transport.abort()
        except (OSError, AttributeError):
            pass

    async def _pump(self):
        try:
            while True:
                obj = await self.outbox.get()
                if obj is None:
                    break
                self.writer.write(encode(obj))
                await asyncio.wait_for(self.writer.drain(), SEND_TIMEOUT)
        except (asyncio.TimeoutError, OSError):
            self.abort()
            return
        try:
            self.writer.close()
        except OSError:
            pass


class ChatServer:
    def __init__(self, room_name, port, store, password=None):
        self.room_name = room_name
        self.port = port
        self.store = store
        self.password = password or None
        self.clients = set()

    # ---------- helpers ----------

    def users(self):
        return sorted(c.user for c in self.clients if c.user)

    def find(self, user):
        low = user.lower()
        for c in self.clients:
            if c.user and c.user.lower() == low:
                return c
        return None

    def broadcast(self, obj, skip=None):
        for c in list(self.clients):
            if c is not skip and c.user:
                c.send(obj)

    def push_users(self):
        self.broadcast({"type": "users", "users": self.users()})

    def log(self, text):
        print("[" + fmt_time(now_ts()) + "] " + text, flush=True)

    # ---------- connection lifecycle ----------

    async def handle(self, reader, writer):
        client = Client(writer)
        self.clients.add(client)
        self.log("connect {}".format(client.addr))
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    self.log("idle timeout {}".format(client.user or client.addr))
                    break
                except ValueError:  # line stream limit se badi - stream ab bharosemand nahi
                    client.send({"type": "error", "text": "Message bahut bada hai."})
                    client.finish()
                    break
                if not line:
                    break
                if len(line) > MAX_LINE:
                    client.send({"type": "error", "text": "Message bahut bada hai."})
                    continue
                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except (ValueError, UnicodeDecodeError):
                    client.send({"type": "error", "text": "Invalid message format."})
                    continue
                if isinstance(msg, dict):
                    await self.dispatch(client, msg)
        except (ConnectionError, OSError):
            pass
        finally:
            await self.disconnect(client)

    async def disconnect(self, client):
        self.clients.discard(client)
        client.finish()
        try:
            await asyncio.wait_for(client.task, FLUSH_TIMEOUT)
        except asyncio.TimeoutError:
            client.abort()
        if client.user:
            self.log("left {}".format(client.user))
            self.broadcast({
                "type": "system",
                "text": client.user + " chat se nikal gaya.",
                "ts": now_ts(),
            })
            self.push_users()

    # ---------- message routing ----------

    async def dispatch(self, client, msg):
        kind = msg.get("type")

        if kind == "join":
            await self.on_join(client, msg)
            return

        if not client.user:
            client.send({"type": "error", "text": "Pehle join karo."})
            return

        if kind == "ping":
            client.send({"type": "pong", "ts": now_ts()})

        elif kind == "msg":
            text = str(msg.get("text", "")).strip()
            if not text:
                return
            ts = now_ts()
            out = {"type": "msg", "user": client.user, "text": text, "ts": ts}
            try:
                out["id"] = self.store.add(client.user, text, ts)
            except sqlite3.Error as e:
                self.log("history save fail: {}".format(e))
            self.broadcast(out)

        elif kind == "dm":
            to = str(msg.get("to", "")).strip()
            text = str(msg.get("text", "")).strip()
            if not text:
                return
            target = self.find(to)
            if not target:
                client.send({"type": "error", "text": "'" + to + "' naam ka koi online nahi hai."})
                return
            payload = {
                "type": "dm",
                "user": client.user,
                "to": target.user,
                "text": text,
                "ts": now_ts(),
            }
            target.send(payload)
            if target is not client:
                client.send(payload)

        elif kind == "who":
            client.send({"type": "users", "users": self.users()})

    def reject_join(self, client, code, text):
        client.send({"type": "error", "code": code, "text": text})
        client.finish()

    async def on_join(self, client, msg):
        if client.user:
            client.send({"type": "error", "text": "Already joined."})
            return

        if self.password is not None:
            given = str(msg.get("password", "")).encode("utf-8")
            if not hmac.compare_digest(given, self.password.encode("utf-8")):
                self.log("galat password {}".format(client.addr))
                await asyncio.sleep(AUTH_DELAY)
                self.reject_join(client, ERR_AUTH, "Password galat hai (ya is server ko password chahiye).")
                return

        name = str(msg.get("user", "")).strip()[:24]
        if not name or any(ch.isspace() for ch in name):
            self.reject_join(client, ERR_BAD_NAME, "Naam khaali ya space-wala nahi ho sakta.")
            return
        if self.find(name):
            self.reject_join(client, ERR_NAME_TAKEN,
                             "'" + name + "' naam already use me hai, dusra try karo.")
            return

        try:
            since_id = int(msg.get("since_id") or 0)
        except (TypeError, ValueError):
            since_id = 0

        client.user = name
        self.log("join {} from {}".format(name, client.addr))
        client.send({
            "type": "welcome",
            "room": self.room_name,
            "user": name,
            "users": self.users(),
            "ts": now_ts(),
        })
        try:
            if since_id < 0 or since_id > self.store.last_id():
                since_id = 0  # client ka id purana/galat (db reset) - fresh history do
            if since_id:
                history = self.store.recent(HISTORY_ON_REJOIN, since_id)
            else:
                history = self.store.recent(HISTORY_ON_JOIN)
        except sqlite3.Error as e:
            self.log("history read fail: {}".format(e))
            history = []
        for old in history:
            client.send(old)
        self.broadcast(
            {"type": "system", "text": name + " chat me aaya.", "ts": now_ts()},
            skip=client,
        )
        self.push_users()


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
            "locked": self.server.password is not None,
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
    default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "freechat.db")
    ap = argparse.ArgumentParser(description="FreeChat LAN server")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: sab interfaces)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default=socket.gethostname() + " room", help="room ka naam")
    ap.add_argument("--password", default=None,
                    help="join ke liye shared password (ya env FREECHAT_PASSWORD)")
    ap.add_argument("--db", default=default_db,
                    help="history SQLite file (default: freechat.db); ':memory:' = history save nahi")
    ap.add_argument("--no-discovery", action="store_true", help="UDP auto-discovery band karo")
    args = ap.parse_args()

    password = args.password or os.environ.get("FREECHAT_PASSWORD") or None
    store = HistoryStore(args.db)
    chat = ChatServer(args.name, args.port, store, password)
    srv = await asyncio.start_server(chat.handle, args.host, args.port, limit=MAX_LINE)

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
    print("  Password: {}".format("ON" if password else "OFF (koi bhi join kar sakta hai)"))
    print("  History : {}".format("RAM only" if args.db == ":memory:" else args.db))
    print("  Dost ko ye address do:")
    for ip in local_ips() or ["<apna LAN IP>"]:
        print("      {}:{}".format(ip, args.port))
    print("")
    print("  Band karne ke liye Ctrl+C")
    print("")

    try:
        async with srv:
            await srv.serve_forever()
    finally:
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer band.")
