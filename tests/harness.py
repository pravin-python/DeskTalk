"""Boot a real DeskTalk server for a test suite (SPEC section 12) and hand out ready ``ChatSession`` objects.

``ServerHarness`` runs ``chatd.app.Server`` on a daemon thread, on port 0 of 127.0.0.1, with a throw-away data dir, the
cheap ``scrypt_n``, ``DESKTALK_TEST=1`` semantics and the SPEC 12 default ``test_limits`` (every request bucket lifted,
no handshake/login/registration limits) so a suite can register hundreds of users from one IP.  A suite that tests one
limit names exactly that key in ``test_limits`` (a value of ``None`` restores the production default of the key).

Typical use::

    class MyTests(harness.ServerTestCase):
        test_limits = {"msg.send": [3, 1.0]}

        def test_something(self):
            admin = self.harness.create_admin()
            bob = self.harness.create_user(admin, "bob")
            ...

The text the server printed at startup is kept in ``harness.banner`` (URLs, setup code).

``ServerTestCase`` boots ONE server per test class (``setUpClass``) and registers the cleanup first, so a failed boot
still removes everything.  ``stop()`` aborts every session the harness handed out, stops the server, waits for its
thread, removes the temp dir (retrying: Windows holds files a little longer) and fails when a server thread leaked.

The hub and the REST handlers (``chatd.hub``, ``chatd.api``) may not exist yet; ``start()`` then raises
``unittest.SkipTest`` with the reason, and so does a Python without a usable ``sqlite3``.  Passing ``hub_factory`` /
``routes_factory`` (stubs) removes the need for them.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from typing import Any, Callable, Dict, List, Mapping, Optional, Set

try:  # `unittest discover -s tests -t .` imports this as `tests.harness`
    from . import wsclient
except ImportError:  # `unittest discover -s tests` or running from inside tests/
    import wsclient

from chatd import app, config, util
from chatd.config import Config

DEFAULT_PASSWORD = "Correct-Horse-9"
TEMP_PASSWORD = "Temp-Passw0rd-1"
SCRYPT_N = 1024

#: SPEC 12: the limits every suite starts from; a suite that tests a limit overrides exactly that key.
DEFAULT_TEST_LIMITS: Dict[str, Any] = {
    "*": [100000, 1],
    "ws_handshakes_per_ip_min": 100000,
    "ws_handshakes_per_user_min": 100000,
    "login_a": [100000, 1],
    "login_b": [100000, 1],
    "login_c": [100000, 1],
    "reg_per_ip_hour": 100000,
    "reg_global_hour": 100000,
}


class HarnessError(AssertionError):
    """A setup step of the harness failed (a refused registration, an unusable data dir ...)."""


def stack_problem(custom_hub: bool = False) -> Optional[str]:
    """Why a server cannot boot here (``None`` when it can): no usable ``sqlite3``, or ``hub.py``/``api.py`` missing.

    A module that exists but fails to import is NOT a "problem": that is a real bug and the error propagates.
    """
    from chatd import db

    problem = db.sqlite_problem()
    if problem is not None:
        return "no usable sqlite3 (run the suites with Python 3.11-3.13): %s" % problem
    if custom_hub:
        return None
    for name in ("hub", "api"):
        try:
            importlib.import_module("chatd." + name)
        except ModuleNotFoundError as exc:
            if exc.name == "chatd." + name:
                return "chatd/%s.py is not written yet (the hub owner has not delivered it)" % name
            raise
    return None


def server_env() -> Dict[str, str]:
    """The environment of the server under test (also for subprocess servers): ``DESKTALK_TEST=1``, cheap scrypt."""
    return {"DESKTALK_TEST": "1", "DESKTALK_SCRYPT_N": str(SCRYPT_N)}


def build_config(
    data_dir: str,
    test_limits: Optional[Mapping[str, Any]] = None,
    test_scale: Optional[float] = None,
    settings: Optional[Mapping[str, Any]] = None,
) -> Config:
    """Write ``<data_dir>/config.json`` and load it through ``config.load`` with the SPEC 6.2 test recipe.

    ``settings`` are extra ``config.json`` keys (``max_users``, ``edit_window_s``, ``registration_open`` ...);
    ``test_limits`` are merged over :data:`DEFAULT_TEST_LIMITS` (``None`` removes a key); ``test_scale`` is the timer
    scale of the timer suites (e.g. 0.1).
    """
    limits: Dict[str, Any] = dict(DEFAULT_TEST_LIMITS)
    for key, value in (test_limits or {}).items():
        if value is None:
            limits.pop(key, None)
        else:
            limits[key] = value
    values: Dict[str, Any] = {"allow_sleep": True, "workspace_name": "DeskTalk Test", "log_level": "WARNING"}
    values.update(settings or {})
    values["test_limits"] = limits
    if test_scale is not None:
        values["test_scale"] = test_scale
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(values, handle)
    cfg = config.load(["serve", "--data-dir", data_dir, "--port", "0", "--host", "127.0.0.1"], env=server_env())
    if cfg.warnings:
        raise HarnessError("the test config produced warnings: %s" % "; ".join(cfg.warnings))
    return cfg


def _thread_ids() -> Set[int]:
    return {t.ident for t in threading.enumerate() if t.ident is not None}


def remove_tree(path: str, timeout: float = 10.0) -> None:
    """``rmtree`` that retries for a while (Windows keeps SQLite and log files busy briefly after they closed)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() > deadline:
                shutil.rmtree(path, ignore_errors=True)
                if os.path.exists(path):
                    raise
                return
            time.sleep(0.1)


class ServerHarness:
    """One running server plus the sessions made for it.  See the module docstring."""

    def __init__(
        self,
        test_limits: Optional[Mapping[str, Any]] = None,
        test_scale: Optional[float] = None,
        settings: Optional[Mapping[str, Any]] = None,
        hub_factory: Optional[Callable[[Any, Config], Any]] = None,
        routes_factory: Optional[Callable[..., None]] = None,
        data_dir: Optional[str] = None,
    ) -> None:
        self.test_limits = dict(test_limits or {})
        self.test_scale = test_scale
        self.settings = dict(settings or {})
        self.hub_factory = hub_factory
        self.routes_factory = routes_factory
        self.sessions: List[wsclient.ChatSession] = []
        self.leaked_threads: List[str] = []
        self.banner = ""
        self.server: Optional[app.Server] = None
        self.cfg: Optional[Config] = None
        self._root: Optional[str] = None
        self._external_dir = data_dir
        self._counter = 0
        self._threads_before: Set[int] = set()

    # ---- lifecycle ---------------------------------------------------------------------------------------------

    @property
    def data_dir(self) -> str:
        """The data directory (created by :meth:`start` or :meth:`seed_admin`, removed by :meth:`stop`)."""
        if self._external_dir is not None:
            return self._external_dir
        if self._root is None:
            self._root = tempfile.mkdtemp(prefix="dtk-test-")
        return os.path.join(self._root, "data")

    @property
    def port(self) -> int:
        """The bound port of the running server."""
        if self.server is None:
            raise HarnessError("the server is not running")
        return self.server.port

    @property
    def hub(self) -> Any:
        return self.server.hub if self.server is not None else None

    @property
    def db(self) -> Any:
        return self.server.db if self.server is not None else None

    @property
    def counters(self) -> Dict[str, int]:
        """``server.hub.counters`` (SPEC 6.1): the in-process hook for "no control-poll re-validation" assertions."""
        return self.server.hub.counters

    def start(self) -> "ServerHarness":
        """Boot the server (skips the test with the reason when the stack is not available)."""
        reason = stack_problem(self.hub_factory is not None and self.routes_factory is not None)
        if reason is not None:
            raise unittest.SkipTest(reason)
        self._threads_before = _thread_ids()
        self.cfg = build_config(self.data_dir, self.test_limits, self.test_scale, self.settings)
        self.server = app.Server(self.cfg, self.hub_factory, self.routes_factory)
        with contextlib.redirect_stdout(io.StringIO()) as printed:  # the startup banner (it carries the setup code)
            self.server.start()
        self.banner = printed.getvalue()
        return self

    def stop(self, remove_data: bool = True) -> None:
        """Abort the sessions, stop the server, wait for it, remove the temp dir; fail on leaked server threads.

        Safe to call twice and after a failed :meth:`start`.  ``remove_data=False`` keeps the data dir (for
        :meth:`restart`).
        """
        for session in self.sessions:
            session.abort()
        self.sessions = []
        problem: Optional[str] = None
        server, self.server = self.server, None
        if server is not None:
            server.stop()
            if not server.join(20):
                problem = "the server thread did not stop within 20 s"
            util.set_test_scale(1.0)
            self.leaked_threads = self._wait_for_thread_exit()
            if self.leaked_threads and problem is None:
                problem = "threads still alive after stop(): %s" % ", ".join(sorted(self.leaked_threads))
        if remove_data and self._root is not None:
            remove_tree(self._root)
            self._root = None
        if problem is not None:
            raise HarnessError(problem)

    def _wait_for_thread_exit(self, timeout: float = 3.0) -> List[str]:
        deadline = time.monotonic() + timeout
        while True:
            alive = [
                t.name
                for t in threading.enumerate()
                if t.ident not in self._threads_before and t.name != "wsclient-reader" and t.is_alive()
            ]
            if not alive or time.monotonic() > deadline:
                return alive
            time.sleep(0.05)

    def restart(self) -> "ServerHarness":
        """Stop and start again on the SAME data dir (reconnect, recovery and shutdown suites); the port changes."""
        self.stop(remove_data=False)
        return self.start()

    def __enter__(self) -> "ServerHarness":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---- identities --------------------------------------------------------------------------------------------

    def unique(self, stem: str = "user") -> str:
        """A fresh username ``<stem><n>`` (3..32 chars, ``[a-z0-9]``)."""
        self._counter += 1
        return "%s%d" % (stem, self._counter)

    def anonymous(self) -> wsclient.ChatSession:
        """A session with no identity (``/api/info``, registration attempts, negative handshakes)."""
        session = wsclient.ChatSession(self.port, name="anonymous")
        self.sessions.append(session)
        return session

    def setup_code(self, timeout: float = 5.0) -> str:
        """The one-time setup code the server wrote to ``<data>/setup_code.txt`` (SPEC 4.3)."""
        path = os.path.join(self.data_dir, "setup_code.txt")
        deadline = time.monotonic() + timeout
        while True:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    code = handle.read().strip()
                if code:
                    return code
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise HarnessError("no setup_code.txt in %s: the first admin already exists" % self.data_dir)
            time.sleep(0.05)

    def create_admin(
        self,
        username: str = "boss",
        password: str = DEFAULT_PASSWORD,
        display_name: str = "The Boss",
        connect: bool = True,
    ) -> wsclient.ChatSession:
        """The first admin through the REAL setup flow: read the setup code, ``POST /api/register``, connect."""
        session = wsclient.ChatSession(self.port, name=username)
        self.sessions.append(session)
        response = session.register(username, display_name, password, setup_code=self.setup_code())
        if response.status != 201:
            raise HarnessError("setup registration of %r failed: %r" % (username, response))
        return session.connect() if connect else session

    def seed_admin(
        self, username: str = "boss", password: str = DEFAULT_PASSWORD, display_name: str = "The Boss"
    ) -> None:
        """Create the first admin with ``maintenance.create_admin`` BEFORE :meth:`start` (no setup code is involved,
        the account is never-activated until its first login).  Needs the config, hence the data dir only."""
        from chatd import maintenance

        os.makedirs(self.data_dir, exist_ok=True)
        maintenance.create_admin(self.data_dir, username, password, display_name=display_name, scrypt_n=SCRYPT_N)

    def login(self, username: str, password: str = DEFAULT_PASSWORD, connect: bool = True) -> wsclient.ChatSession:
        """Log an existing user in (``POST /api/login``) and connect."""
        session = wsclient.ChatSession(self.port, name=username)
        self.sessions.append(session)
        response = session.login(username, password)
        if response.status != 200:
            raise HarnessError("login of %r failed: %r" % (username, response))
        return session.connect() if connect else session

    def create_user(
        self,
        admin: wsclient.ChatSession,
        username: Optional[str] = None,
        display_name: Optional[str] = None,
        password: str = DEFAULT_PASSWORD,
        role: str = "member",
        activate: bool = True,
        connect: bool = True,
    ) -> wsclient.ChatSession:
        """An ordinary user through ``admin.create_user`` (the account starts with a temporary password and
        ``must_change_password``).

        ``activate`` performs the first login and the forced password change to ``password`` (the user is then
        activated, SPEC 8.1) and ``connect`` opens the socket.  With ``activate=False`` nothing else happens: the
        returned session is not logged in; its ``username`` is set and the temporary password is ``TEMP_PASSWORD``
        (a never-activated user, or a ``must_change_password`` flow under test).
        """
        name = username or self.unique("user")
        shown = display_name or "User %s" % name
        temp = TEMP_PASSWORD if activate or password == DEFAULT_PASSWORD else password
        res = admin.request(
            "admin.create_user", {"username": name, "display_name": shown, "password": temp, "role": role}
        )
        if not res.get("ok"):
            raise HarnessError("admin.create_user %r failed: %r" % (name, res.get("err")))
        session = wsclient.ChatSession(self.port, name=name)
        session.username, session.password = name, temp
        self.sessions.append(session)
        if not activate:
            return session
        response = session.login(name, temp)
        if response.status != 200:
            raise HarnessError("first login of %r failed: %r" % (name, response))
        response = session.change_password(temp, password)
        if response.status != 204:
            raise HarnessError("forced password change of %r failed: %r" % (name, response))
        session.password = password
        return session.connect() if connect else session

    def create_users(
        self, admin: wsclient.ChatSession, count: int, stem: str = "user", **kwargs: Any
    ) -> List[wsclient.ChatSession]:
        """``count`` activated, connected users named ``<stem><n>``."""
        return [self.create_user(admin, self.unique(stem), **kwargs) for _ in range(count)]

    def open_registration(self, admin: wsclient.ChatSession) -> str:
        """Turn self-registration on (``admin.settings``) and return the join code (SPEC 4.3)."""
        res = admin.request("admin.settings", {"registration_open": True})
        if not res.get("ok"):
            raise HarnessError("admin.settings failed: %r" % (res.get("err"),))
        return res["d"]["workspace"]["join_code"]

    def register(
        self,
        join_code: str,
        username: Optional[str] = None,
        password: str = DEFAULT_PASSWORD,
        display_name: Optional[str] = None,
        connect: bool = True,
    ) -> wsclient.ChatSession:
        """A user through ``POST /api/register`` with the join code (registration must be open)."""
        name = username or self.unique("user")
        session = wsclient.ChatSession(self.port, name=name)
        self.sessions.append(session)
        response = session.register(name, display_name or "User %s" % name, password, join_code=join_code)
        if response.status != 201:
            raise HarnessError("registration of %r failed: %r" % (name, response))
        return session.connect() if connect else session


class ServerTestCase(unittest.TestCase):
    """Base class: one server per test class, configured through class attributes.

    ``test_limits`` / ``test_scale`` / ``settings`` / ``hub_factory`` / ``routes_factory`` are the
    :class:`ServerHarness` arguments; ``self.harness`` is the running harness.  Everything is cleaned up even when
    ``setUpClass`` fails half way or the server is skipped.
    """

    test_limits: Dict[str, Any] = {}
    test_scale: Optional[float] = None
    settings: Dict[str, Any] = {}
    hub_factory: Optional[Callable[[Any, Config], Any]] = None
    routes_factory: Optional[Callable[..., None]] = None
    harness: ServerHarness

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.harness = ServerHarness(cls.test_limits, cls.test_scale, cls.settings, cls.hub_factory, cls.routes_factory)
        cls.addClassCleanup(cls.harness.stop)
        cls.harness.start()
