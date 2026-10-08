"""Shared helpers of the hub / REST test suites (no tests live here).

* :class:`HubServer` boots the real :class:`chatd.app.Server` in a background thread on a free port with a temp data
  dir, a low scrypt cost and the lifted test limits of SPEC 12 (individual keys are restored per suite).
* :class:`HubTestCase` boots one server per test class, registers the first admin and offers :meth:`new_user`
  (registered through ``/api/register`` with the join code, connected, cleaned up after the test).
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import sys
import tempfile
import threading
import unittest
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chatd import util  # noqa: E402
from chatd.app import Server  # noqa: E402
from chatd.config import Config  # noqa: E402

try:  # `unittest discover -s tests -t .` imports the suites as part of the `tests` package
    from .wsclient import ChatSession, Frame
except ImportError:  # `unittest discover -s tests` or running a file from inside tests/
    from wsclient import ChatSession, Frame  # type: ignore[no-redef]

PASSWORD = "correct horse battery"

#: The `Server` test helper's default `test_limits` (SPEC 12): every request limit lifted.
OPEN_LIMITS: Dict[str, Any] = {
    "*": [100000, 1],
    "ws_handshakes_per_ip_min": 100000,
    "ws_handshakes_per_user_min": 100000,
    "login_a": [100000, 1],
    "login_b": [100000, 1],
    "login_c": [100000, 1],
    "reg_per_ip_hour": 100000,
    "reg_global_hour": 100000,
}

_names = itertools.count(1)


class HubServer:
    """A running server for the tests; ``limits`` override single keys of :data:`OPEN_LIMITS`."""

    def __init__(self, scale: float = 1.0, limits: Optional[Dict[str, Any]] = None, **overrides: Any) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dt-hub-")
        self.data_dir = os.path.join(self.tmp, "data")
        merged = dict(OPEN_LIMITS)
        merged.update(limits or {})
        values: Dict[str, Any] = {
            "host": "127.0.0.1",
            "port": 0,
            "data_dir": self.data_dir,
            "scrypt_n": 1024,
            "allow_sleep": True,
            "test_scale": scale,
            "test_limits": merged,
        }
        values.update(overrides)
        self.cfg = Config(**values)
        self.server = Server(self.cfg)

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def hub(self) -> Any:
        return self.server.hub

    def start(self) -> "HubServer":
        self.server.start()
        return self

    def stop(self) -> None:
        self.server.stop()
        self.server.join(20)
        util.set_test_scale(1.0)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def setup_code(self) -> str:
        with open(os.path.join(self.data_dir, "setup_code.txt"), encoding="utf-8") as handle:
            return handle.read().strip()

    def call(self, coro: Any, timeout: float = 10.0) -> Any:
        """Run a coroutine on the server's event loop and return its result (to inspect or poke hub state)."""
        loop = self.server._loop
        assert loop is not None
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def run(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Run a plain function on the server's event loop thread (hub internals are loop-confined)."""

        async def wrapper() -> Any:
            return func()

        return self.call(wrapper(), timeout)


class HubTestCase(unittest.TestCase):
    """One server per test class; ``admin`` is the connected first admin, ``join_code`` opens registration."""

    scale = 1.0
    limits: Dict[str, Any] = {}
    overrides: Dict[str, Any] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = HubServer(cls.scale, cls.limits, **cls.overrides).start()
        cls.port = cls.srv.port
        cls.admin = ChatSession(cls.port, name="admin")
        response = cls.admin.register("root1", "Root One", PASSWORD, setup_code=cls.srv.setup_code())
        assert response.status == 201, response
        cls.admin.connect()
        settings = cls.admin.request("admin.settings", {"registration_open": True})
        assert settings["ok"], settings
        cls.join_code = settings["d"]["workspace"]["join_code"]
        cls._sessions: List[ChatSession] = []

    @classmethod
    def tearDownClass(cls) -> None:
        cls.admin.abort()
        for session in cls._sessions:
            session.abort()
        cls.srv.stop()

    # ---- helpers -------------------------------------------------------------------------------------------------

    @property
    def hub(self) -> Any:
        return self.srv.hub

    def new_user(self, prefix: str = "user", connect: bool = True) -> ChatSession:
        """Register a fresh member through the REST API (join code) and connect its socket."""
        number = next(_names)
        session = ChatSession(self.port, name="%s%d" % (prefix, number))
        response = session.register(
            "%s%d" % (prefix, number), "%s %d" % (prefix.capitalize(), number), PASSWORD, join_code=self.join_code
        )
        self.assertEqual(response.status, 201, response)
        self._sessions.append(session)
        self.addCleanup(session.abort)
        if connect:
            session.connect()
        return session

    def another_tab(self, session: ChatSession) -> ChatSession:
        """A second connected socket of the same account."""
        tab = session.clone(session.name + "-tab")
        self._sessions.append(tab)
        self.addCleanup(tab.abort)
        return tab.connect()

    def everyone_id(self) -> int:
        return next(c["id"] for c in self.admin.ready["chats"] if c["is_default"])

    def make_group(self, owner: ChatSession, *members: ChatSession, title: str = "Team") -> int:
        res = owner.request("chat.create_group", {"title": title, "member_ids": [m.me["id"] for m in members]})
        self.assertTrue(res["ok"], res)
        return res["d"]["chat"]["id"]

    def send(self, session: ChatSession, chat_id: int, body: str = "hello there", **extra: Any) -> Frame:
        data = {"chat_id": chat_id, "client_id": "cid-%s-%d" % (session.name, next(_names)), "body": body}
        data.update(extra)
        res = session.request("msg.send", data)
        self.assertTrue(res["ok"], res)
        return res

    def barrier(self, *sessions: ChatSession) -> None:
        """Wait until everything enqueued for these sessions so far has arrived (ping round trip, SPEC 7.6(4))."""
        for session in sessions:
            res = session.request("ping", {})
            self.assertTrue(res["ok"], res)

    def assertErr(self, res: Frame, code: str, reason: Optional[str] = None) -> Dict[str, Any]:
        self.assertFalse(res.get("ok"), res)
        err = res["err"]
        self.assertEqual(err["code"], code, res)
        if reason is not None:
            self.assertEqual(err.get("reason"), reason, res)
        return err


def start_thread(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread
