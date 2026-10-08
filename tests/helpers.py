"""Shared test helpers: run a real ChatServer on a thread, collect client events."""

import asyncio
import queue
import threading
import time
import unittest

from desktalk.client import ChatClient
from desktalk.protocol import MAX_LINE
from desktalk.server.hub import ChatServer
from desktalk.store import open_store


class ServerThread:
    """Runs a ChatServer on its own asyncio loop (in a separate thread)."""

    def __init__(self, store, password=None, port=0):
        self.store = store
        self.password = password
        self.port = port
        self.chat = None
        self.srv = None
        self.loop = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("server did not start")
        return self

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.chat = ChatServer("test", self.port, self.store, self.password)

        async def boot():
            self.srv = await asyncio.start_server(self.chat.handle, "127.0.0.1", self.port, limit=MAX_LINE)
            self.port = self.srv.sockets[0].getsockname()[1]
            self.chat.port = self.port

        self.loop.run_until_complete(boot())
        self._ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()
        self._stopped.set()

    def call(self, coro, timeout=5):
        """Run a coroutine on the server loop and return its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self):
        if self._stopped.is_set():
            return

        async def shutdown():
            self.srv.close()
            for c in list(self.chat.clients):
                c.abort()
            for _ in range(100):  # let every handle() coroutine finish
                if not self.chat.clients:
                    break
                await asyncio.sleep(0.02)

        self.call(shutdown())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._stopped.wait(5)

    def online(self):
        return self.chat.users()


class Events:
    """Callable event sink for ChatClient with a blocking ``wait``."""

    def __init__(self):
        self.q = queue.Queue()
        self.seen = []

    def __call__(self, ev):
        self.q.put(ev)

    def drain(self):
        while True:
            try:
                self.seen.append(self.q.get_nowait())
            except queue.Empty:
                return

    def wait(self, pred, timeout=8.0):
        for ev in self.seen:
            if pred(ev):
                return ev
        end = time.time() + timeout
        while time.time() < end:
            try:
                ev = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            self.seen.append(ev)
            if pred(ev):
                return ev
        tail = [(e.get("type"), str(e.get("text", ""))[:40]) for e in self.seen[-6:]]
        raise AssertionError("event not received ({} seen); last: {}".format(len(self.seen), tail))


def of_type(kind, **match):
    def pred(ev):
        return ev.get("type") == kind and all(ev.get(k) == v for k, v in match.items())
    return pred


def msg_with(text):
    return lambda ev: ev.get("type") == "msg" and ev.get("text") == text


class ServerCase(unittest.TestCase):
    """Base class: fresh in-memory store, helpers to start servers and join clients."""

    def setUp(self):
        self.store = open_store(":memory:")
        self.servers = []
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        for s in self.servers:
            try:
                s.stop()
            except Exception:
                pass
        self.store.close()

    def start_server(self, **kw):
        s = ServerThread(self.store, **kw).start()
        self.servers.append(s)
        return s

    def join(self, srv, name, password="", **kw):
        ev = Events()
        c = ChatClient("127.0.0.1", srv.port, name, ev, password=password, **kw)
        c.connect()
        self.clients.append(c)
        return c, ev
