"""Server lifecycle with a stub hub: start/stop, background tasks, graceful shutdown (SPEC 2.2 - 2.6, 6.2)."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import io
import json
import os
import shutil
import signal
import socket
import ssl
import tempfile
import threading
import time
import unittest
from typing import Any, List, Optional
from unittest import mock

try:  # `unittest discover -s tests -t .` imports this module as part of the `tests` package
    from . import test_transport_support as support
except ImportError:  # `unittest discover -s tests` or running from inside tests/
    import test_transport_support as support

from chatd import app, auth, tlsutil, util
from chatd.app import Server, format_urls, prepare_data_dir
from chatd.config import Config
from chatd.http import Request, Response, Router, json_response


class ServerHub(support.StubHub):
    """The stub hub plus the hooks app.py drives (SPEC 6.1)."""

    def __init__(self) -> None:
        super().__init__()
        self.shutdown_calls = 0
        self.online: List[int] = []
        self.storage_bytes = -1
        self.order: List[str] = []

    @property
    def external_changes(self) -> int:
        return self.counters["external_change_calls"]

    @property
    def revalidations(self) -> int:
        return self.counters["revalidate_calls"]

    async def start(self) -> None:
        self.order.append("start")
        self.started = True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.stopping = True

    def online_user_ids(self) -> List[int]:
        return list(self.online)


def stub_routes(router: Router, hub: Any, database: Any, cfg: Config) -> None:
    async def info(req: Request) -> Response:
        return json_response(200, {"name": cfg.workspace_name, "tls": cfg.tls})

    router.add("GET", "/api/info", info)


class ServerBase(unittest.TestCase):
    scale = 1.0
    overrides: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="dt-server-")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.addCleanup(support.reset_scale)
        self.data = os.path.join(tempfile.mkdtemp(dir=self.tmp), "data")
        self.hub = ServerHub()
        self.server: Optional[Server] = None

    def make_server(self, **overrides: Any) -> Server:
        values = {
            "log_level": "WARNING",
            "host": "127.0.0.1",
            "port": 0,
            "data_dir": self.data,
            "scrypt_n": 1024,
            "allow_sleep": True,
            "test_scale": self.scale,
        }
        values.update(self.overrides)
        values.update(overrides)
        cfg = Config(**values)
        server = Server(cfg, hub_factory=lambda db, c: self.hub, routes_factory=stub_routes)
        self.addCleanup(self._stop, server)
        return server

    @staticmethod
    def _stop(server: Server) -> None:
        server.stop()
        server.join(15)

    def boot(self, **overrides: Any) -> Server:
        self.server = self.make_server(**overrides)
        self.server.start()
        return self.server

    def fetch(self, path: str, port: Optional[int] = None) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", port or self.server.port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def add_user(self, user_id: int = 1) -> None:
        self.sql(
            "INSERT INTO users(id, username, display_name, display_key, pw_hash, created_at) VALUES(?,?,?,?,?,1)",
            user_id,
            "user%02d" % user_id,
            "User %d" % user_id,
            "user %d" % user_id,
            "x",
        )

    def sql(self, statement: str, *params: Any) -> Any:
        def write(conn: Any) -> Any:
            return conn.execute(statement, params).fetchall()

        return self.server.db.run_sync(write)

    def query(self, statement: str, *params: Any) -> Any:
        return self.sql(statement, *params)


class LifecycleTests(ServerBase):
    def test_start_serves_and_stop_is_graceful_and_idempotent(self) -> None:
        server = self.boot()
        self.assertGreater(server.port, 0)
        status, headers, body = self.fetch("/healthz")
        self.assertEqual((status, body), (200, b"ok"))
        status, _, body = self.fetch("/api/info")
        self.assertEqual(json.loads(body), {"name": "DeskTalk", "tls": False})
        for sub in ("uploads/.tmp", "logs", "control", "backups"):
            self.assertTrue(os.path.isdir(os.path.join(self.data, *sub.split("/"))), sub)
        info = util.read_lock_info(self.data)
        self.assertEqual((info["pid"], info["port"], info["tls"]), (os.getpid(), server.port, False))
        threads = [threading.Thread(target=server.stop) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(server.join(15))
        server.stop()
        self.assertEqual(self.hub.shutdown_calls, 1)
        self.assertTrue(support.wait_until(lambda: not self._port_open(server.port), 5))
        self.assertIsNone(util.read_lock_info(self.data))
        self.assertFalse(
            os.path.exists(os.path.join(self.data, "chat.db-wal"))
            and os.path.getsize(os.path.join(self.data, "chat.db-wal")) > 0
        )

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            return False

    def test_stop_before_start_completes_does_not_hang(self) -> None:
        server = self.make_server()
        server.stop()
        server.start()
        self.assertTrue(server.join(15))

    def test_restart_on_the_same_data_dir_keeps_the_database(self) -> None:
        first = self.boot()
        self.sql("INSERT INTO meta(key, value) VALUES('marker', 'kept')")
        first.stop()
        first.join(15)
        second = self.make_server()
        second.start()
        self.server = second
        self.assertEqual(self.query("SELECT value FROM meta WHERE key='marker'"), [("kept",)])

    def test_second_instance_exits_73_with_pid_and_port(self) -> None:
        first = self.boot()
        other = self.make_server()
        with self.assertRaises(util.AlreadyRunning) as caught:
            other.start()
        self.assertEqual(caught.exception.code, 73)
        self.assertEqual((caught.exception.pid, caught.exception.port), (os.getpid(), first.port))
        self.assertIn("pid %d" % os.getpid(), str(caught.exception))
        self.assertEqual(self.fetch("/healthz")[0], 200)

    def test_setup_code_is_created_and_removed_once_a_user_exists(self) -> None:
        self.scale = 0.1
        self.boot(test_scale=0.1)
        path = os.path.join(self.data, "setup_code.txt")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            self.assertRegex(fh.read().strip(), r"^[A-Za-z0-9_-]{8}$")
        self.add_user()
        self.assertTrue(support.wait_until(lambda: not os.path.exists(path), 5))

    def test_public_attributes_and_hub_start_before_the_sockets_are_bound(self) -> None:
        seen = {}

        async def start() -> None:
            seen["http_before_start"] = server.http is not None
            seen["lock_held"] = util.read_lock_info(self.data) is not None
            self.hub.started = True

        self.hub.start = start  # type: ignore[assignment]
        server = self.make_server()
        server.start()
        self.assertEqual(seen, {"http_before_start": False, "lock_held": True})
        self.assertIs(server.hub, self.hub)
        self.assertEqual(os.path.abspath(str(server.cfg.data_dir)), os.path.abspath(self.data))
        self.assertTrue(hasattr(server.db, "run_sync"))
        self.assertEqual(self.hub.counters["dropped_ephemeral"], 0)

    def test_healthz_is_503_once_the_hub_is_stopping(self) -> None:
        server = self.boot()
        self.assertEqual(self.fetch("/healthz")[0], 200)
        self.hub.stopping = True
        status, headers, body = self.fetch("/healthz")
        self.assertEqual((status, headers["Retry-After"]), (503, "5"))
        self.assertIn(b"unavailable", body)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        conn.request("HEAD", "/healthz")
        self.assertEqual(conn.getresponse().status, 503)
        conn.close()
        self.hub.stopping = False
        self.assertEqual(self.fetch("/healthz")[0], 200)

    def test_serve_installs_the_file_log_and_removes_it_again(self) -> None:
        import logging

        before = list(logging.getLogger().handlers)
        server = self.boot(log_level="INFO")
        self.assertTrue(os.path.isdir(os.path.join(self.data, "logs")))
        server.stop()
        server.join(15)
        with open(os.path.join(self.data, "logs", "desktalk.log"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("shutting down", text)
        self.assertIn("stopped", text)
        self.assertEqual(logging.getLogger().handlers, before)

    def test_unusable_data_dir_is_a_fatal_error(self) -> None:
        blocker = os.path.join(self.tmp, "a-file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        cfg = Config(host="127.0.0.1", port=0, data_dir=os.path.join(blocker, "data"))
        with self.assertRaises(util.FatalError) as caught:
            prepare_data_dir(cfg)
        self.assertEqual(caught.exception.code, 78)
        self.assertIn("doctor", str(caught.exception))

    def test_port_in_use_is_reported(self) -> None:
        taker = socket.socket()
        taker.bind(("127.0.0.1", 0))
        taker.listen(1)
        self.addCleanup(taker.close)
        server = self.make_server(port=taker.getsockname()[1])
        with self.assertRaises(util.FatalError) as caught:
            server.start()
        self.assertIn("cannot listen", str(caught.exception))
        self.assertEqual(caught.exception.code, 1)
        self.assertIsNone(util.read_lock_info(self.data))

    def test_newer_schema_is_refused_with_78(self) -> None:
        server = self.boot()
        self.sql("UPDATE meta SET value='999' WHERE key='schema_version'")
        server.stop()
        server.join(15)
        again = self.make_server()
        with self.assertRaises(util.FatalError) as caught:
            again.start()
        self.assertEqual(caught.exception.code, 78)
        self.assertIsNone(util.read_lock_info(self.data))

    def test_stale_stop_request_is_deleted_at_boot(self) -> None:
        os.makedirs(os.path.join(self.data, "control"))
        stale = os.path.join(self.data, "control", "stop.request")
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write("stop")
        self.scale = 0.1
        server = self.boot(test_scale=0.1)
        self.assertFalse(os.path.exists(stale))
        time.sleep(0.6)
        self.assertEqual(self.fetch("/healthz")[0], 200)
        self.assertFalse(server.join(0.01))

    def test_stop_request_file_stops_the_server_and_is_consumed(self) -> None:
        self.scale = 0.1
        server = self.boot(test_scale=0.1)
        request = os.path.join(self.data, "control", "stop.request")
        with open(request, "w", encoding="utf-8") as fh:
            fh.write("stop")
        self.assertTrue(server.join(10))
        self.assertFalse(os.path.exists(request))
        self.assertEqual(self.hub.shutdown_calls, 1)

    def test_pending_temp_uploads_are_removed_at_boot(self) -> None:
        tmp = os.path.join(self.data, "uploads", ".tmp")
        os.makedirs(tmp)
        for name in ("a.part", "b.part"):
            with open(os.path.join(tmp, name), "wb") as fh:
                fh.write(b"x")
        self.boot()
        self.assertEqual(os.listdir(tmp), [])

    def test_signal_handler_stops_a_server_on_the_main_thread(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("needs the main thread")
        server = self.make_server()
        previous = signal.getsignal(signal.SIGINT)
        timer = threading.Timer(1.5, signal.raise_signal, [signal.SIGINT])
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        server.run()
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        self.assertEqual(self.hub.shutdown_calls, 1)


class TaskTests(ServerBase):
    scale = 0.05

    def setUp(self) -> None:
        super().setUp()
        util.set_test_scale(self.scale)

    def test_typing_sweeper_is_ticked_every_second_scaled(self) -> None:
        self.boot()
        self.assertTrue(support.wait_until(lambda: self.hub.typing_sweeps >= 5, 5))

    def test_revalidation_runs_every_five_minutes_scaled(self) -> None:
        util.set_test_scale(0.01)
        self.boot(test_scale=0.01)
        self.assertTrue(support.wait_until(lambda: self.hub.revalidations >= 1, 10))

    def test_external_change_fires_for_reload_file_and_other_connections_only(self) -> None:
        self.boot()
        time.sleep(0.6)
        base = self.hub.external_changes
        for i in range(40):  # in-process commits on the writer connection never bump its own data_version
            self.sql("INSERT INTO meta(key, value) VALUES(?, 'v')", "k%d" % i)
        time.sleep(0.6)
        self.assertEqual(self.hub.external_changes, base)
        conn = self.server.db.connect_extra()
        try:
            conn.execute("INSERT INTO meta(key, value) VALUES('other', 'process')")
        finally:
            conn.close()
        self.assertTrue(support.wait_until(lambda: self.hub.external_changes > base, 5))
        base = self.hub.external_changes
        time.sleep(0.5)
        self.assertEqual(self.hub.external_changes, base)
        reload_file = os.path.join(self.data, "control", "reload")
        with open(reload_file, "w", encoding="utf-8") as fh:
            fh.write("1")
        self.assertTrue(support.wait_until(lambda: self.hub.external_changes > base, 5))

    def test_heartbeat_stores_last_seen_of_online_users(self) -> None:
        self.boot()
        self.add_user()
        self.hub.online = [1]
        self.assertTrue(
            support.wait_until(lambda: self.query("SELECT last_seen_at FROM users WHERE id=1")[0][0] is not None, 10)
        )

    def test_shutdown_stores_last_seen_for_online_users(self) -> None:
        server = self.boot()
        self.add_user()
        self.hub.online = [1]
        server.stop()
        server.join(15)
        import sqlite3

        conn = sqlite3.connect(os.path.join(self.data, "chat.db"))
        try:
            self.assertIsNotNone(conn.execute("SELECT last_seen_at FROM users WHERE id=1").fetchone()[0])
        finally:
            conn.close()

    def test_automatic_backup_runs_once_and_is_recorded(self) -> None:
        self.scale = 0.01
        util.set_test_scale(0.01)
        self.boot(test_scale=0.01)
        backups = os.path.join(self.data, "backups")
        self.assertTrue(support.wait_until(lambda: any(n.startswith("auto-") for n in os.listdir(backups)), 10))
        self.assertTrue(
            support.wait_until(lambda: bool(self.query("SELECT value FROM meta WHERE key='last_backup_at'")), 5)
        )
        before = sorted(n for n in os.listdir(backups) if n.startswith("auto-"))
        time.sleep(1.5)  # several 15-minute ticks at this scale: the 20 h rule keeps it at one file
        self.assertEqual(sorted(n for n in os.listdir(backups) if n.startswith("auto-")), before)
        self.assertEqual(len(before), 1)

    def test_orphan_sweep_removes_old_unattached_uploads(self) -> None:
        self.scale = 0.01
        util.set_test_scale(0.01)
        self.boot(test_scale=0.01)
        self.add_user()
        att = "ab" + "0" * 30
        folder = os.path.join(self.data, "uploads", "ab")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, att)
        with open(path, "wb") as fh:
            fh.write(b"orphan")
        self.sql(
            "INSERT INTO attachments(id, uploader_id, name, mime, kind, size, path, created_at) "
            "VALUES(?,1,'x','application/octet-stream','file',6,?,?)",
            att,
            "ab/" + att,
            time.time() - 3 * 3600,
        )
        self.assertTrue(support.wait_until(lambda: not os.path.exists(path), 10))
        self.assertEqual(self.query("SELECT COUNT(*) FROM attachments"), [(0,)])

    def test_hourly_job_purges_sessions_and_refreshes_storage(self) -> None:
        server = self.boot()
        self.add_user()
        self.sql(
            "INSERT INTO sessions(token_hash, user_id, created_at, last_used_at, expires_at) VALUES('old',1,1,1,2)"
        )
        self.sql(
            "INSERT INTO attachments(id, uploader_id, name, mime, kind, size, path, created_at) "
            "VALUES(?,1,'x','application/octet-stream','file',1234,'cd/x',?)",
            "cd" + "0" * 30,
            time.time(),
        )
        asyncio.run_coroutine_threadsafe(server._hourly(), server._loop).result(10)
        self.assertEqual(self.query("SELECT COUNT(*) FROM sessions"), [(0,)])
        self.assertEqual(self.hub.storage_bytes, 1234)


class ShutdownTests(ServerBase):
    def test_websockets_get_1001_restart_and_idle_http_does_not_block(self) -> None:
        server = self.boot()
        hub = self.hub
        self.add_user()
        token, digest = auth.new_session_token()
        now = time.time()
        self.sql(
            "INSERT INTO sessions(token_hash, user_id, created_at, last_used_at, expires_at) VALUES(?,1,?,?,?)",
            digest,
            now,
            now,
            now + 3600,
        )
        clients = [support.RawWsClient(server.port, "fc_session=" + token) for _ in range(3)]
        idle = [support.connect(server.port) for _ in range(5)]
        for client in clients:
            client.send_text("x")
            client.recv_text()
        for sock in idle:
            sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            self.assertEqual(support.read_response(sock)[0], 200)
        start = time.monotonic()
        server.stop()
        self.assertTrue(server.join(15))
        self.assertLess(time.monotonic() - start, 8)
        for client in clients:
            self.assertEqual(client.recv_close(), 1001)
            client.close()
        for sock in idle:
            self.assertTrue(support.closed_by_peer(sock, 3))
            sock.close()
        self.assertEqual(hub.shutdown_calls, 1)

    def test_listener_closes_at_once_even_when_the_hub_shuts_down_slowly(self) -> None:
        server = self.boot()

        async def slow_shutdown() -> None:
            await asyncio.sleep(2)

        self.hub.shutdown = slow_shutdown  # type: ignore[assignment]
        server.stop()
        self.assertTrue(support.wait_until(lambda: not LifecycleTests._port_open(server.port), 1.0))
        self.assertTrue(server.join(20))

    def test_requests_after_stopping_get_server_error(self) -> None:
        server = self.boot()
        sock = support.connect(server.port)
        sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertEqual(support.read_response(sock)[0], 200)
        server.http.stopping = True
        sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        result = support.read_response(sock)
        self.assertTrue(result is None or result[0] == 500)
        sock.close()
        server.http.stopping = False


@unittest.skipIf(tlsutil.find_openssl() is None, "openssl is not available")
class TlsTests(ServerBase):
    def test_https_serves_and_cert_files_exist(self) -> None:
        server = self.boot(tls=True)
        self.assertTrue(server.cfg.tls)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", server.port, timeout=10, context=ctx)
        conn.request("GET", "/api/info")
        resp = conn.getresponse()
        self.assertEqual((resp.status, json.loads(resp.read())["tls"]), (200, True))
        conn.close()
        for name in ("cert.pem", "key.pem", "meta.json"):
            self.assertTrue(os.path.exists(os.path.join(self.data, "tls", name)))
        self.assertEqual(util.read_lock_info(self.data)["tls"], True)

    def test_tls_init_and_serve_tls_produce_the_same_files(self) -> None:
        from chatd import __main__ as cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["tls-init", "--data-dir", self.data]), 0, err.getvalue())
        names = sorted(os.listdir(os.path.join(self.data, "tls")))
        self.assertEqual(names, ["cert.pem", "key.pem", "meta.json"])
        with open(os.path.join(self.data, "tls", "cert.pem"), "rb") as fh:
            cert = fh.read()
        server = self.boot(tls=True)  # the certificate from tls-init is current: serve --tls keeps it
        self.assertTrue(server.cfg.tls)
        self.assertEqual(sorted(os.listdir(os.path.join(self.data, "tls"))), names)
        with open(os.path.join(self.data, "tls", "cert.pem"), "rb") as fh:
            self.assertEqual(fh.read(), cert)
        self.assertEqual(tlsutil.cert_fingerprint(self.data) in out.getvalue(), True)

    def test_plain_http_request_to_the_tls_port_is_rejected_cleanly(self) -> None:
        server = self.boot(tls=True)
        sock = support.connect(server.port)
        sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertTrue(support.closed_by_peer(sock, 5))
        sock.close()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as raw, ctx.wrap_socket(raw) as tls:
            tls.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            self.assertEqual(support.read_response(tls)[0], 200)

    def test_redirect_listener_answers_301_only(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        redirect_port = probe.getsockname()[1]
        probe.close()
        server = self.boot(tls=True, redirect_port=redirect_port)
        status, headers, _ = support.exchange(
            redirect_port, b"GET /anything?x=1 HTTP/1.1\r\nHost: chat.local:%d\r\nCookie: a=b\r\n\r\n" % redirect_port
        )
        self.assertEqual(status, 301)
        self.assertEqual(headers["location"], "https://chat.local:%d/" % server.port)
        self.assertNotIn("set-cookie", headers)
        self.assertEqual(support.exchange(redirect_port, b"GET / HTTP/1.1\r\n\r\n")[0], 400)

    def test_tls_failure_falls_back_to_plain_http(self) -> None:
        with mock.patch.object(tlsutil, "find_openssl", return_value=None):
            server = self.boot(tls=True)
        self.assertFalse(server.cfg.tls)
        self.assertEqual(self.fetch("/healthz")[0], 200)
        self.assertEqual(json.loads(self.fetch("/api/info")[2])["tls"], False)


class BannerTests(unittest.TestCase):
    def test_urls_for_all_interfaces(self) -> None:
        cfg = Config(host="0.0.0.0", port=8765)
        lines = format_urls(cfg, 8765, "172.31.1.5", ["10.0.0.2", "192.168.56.1"])
        self.assertEqual(lines[0], "Share this link (open it in a browser on any device on this network):")
        self.assertEqual(lines[1], "    http://172.31.1.5:8765/")
        self.assertIn("Other adapters (may not be reachable by phones):", lines)
        self.assertIn("    http://192.168.56.1:8765/", lines)
        self.assertEqual(lines[-1], "On this PC: http://localhost:8765/")

    def test_no_network_address_yet(self) -> None:
        lines = format_urls(Config(host="0.0.0.0"), 8765, None, [])
        self.assertTrue(lines[0].startswith("No network address yet"))

    def test_specific_host_and_tls_and_default_ports(self) -> None:
        self.assertEqual(
            format_urls(Config(host="127.0.0.1"), 9000, None, []), ["Share this link:", "    http://127.0.0.1:9000/"]
        )
        cfg = Config(host="0.0.0.0", tls=True)
        self.assertEqual(format_urls(cfg, 443, "10.1.1.1", [])[1], "    https://10.1.1.1/")
        self.assertEqual(format_urls(Config(host="0.0.0.0"), 80, "10.1.1.1", [])[1], "    http://10.1.1.1/")

    def test_only_other_addresses(self) -> None:
        lines = format_urls(Config(host="0.0.0.0"), 8765, None, ["10.0.0.2", "10.0.0.3"])
        self.assertEqual(lines[1], "    http://10.0.0.2:8765/")
        self.assertIn("    http://10.0.0.3:8765/", lines)


class FileLimitTests(unittest.TestCase):
    def test_raise_file_limit_never_raises(self) -> None:
        app.raise_file_limit()


if __name__ == "__main__":
    unittest.main()
