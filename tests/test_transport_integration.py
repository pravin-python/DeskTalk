"""The transport with the REAL hub, api, auth and database: register, WebSocket, upload, control poll, shutdown."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, Optional, Tuple
from unittest import mock

try:  # `unittest discover -s tests -t .` imports this module as part of the `tests` package
    from . import test_transport_support as support
except ImportError:  # `unittest discover -s tests` or running from inside tests/
    import test_transport_support as support

from chatd import __main__ as cli
from chatd import config, util
from chatd.app import Server

PASSWORD = "correct-horse-battery-staple"


class RealStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="dt-real-")
        cls.data = os.path.join(cls.tmp, "data")
        env = {"DESKTALK_TEST": "1", "DESKTALK_SCRYPT_N": "1024"}
        cfg = config.load(
            ["serve", "--data-dir", cls.data, "--port", "0", "--host", "127.0.0.1", "--allow-sleep"], env
        ).replace(log_level="WARNING", test_scale=0.2)
        cls.server = Server(cfg)
        cls.server.start()
        cls.port = cls.server.port

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.server.join(30)
        util.set_test_scale(1.0)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        cookie: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            sent = {"X-Requested-With": "desktalk"}
            if isinstance(body, (dict, list)):
                sent["Content-Type"] = "application/json"
                body = json.dumps(body)
            if cookie:
                sent["Cookie"] = cookie
            sent.update(headers or {})
            conn.request(method, path, body, sent)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_00_first_admin_registers_with_the_setup_code(self) -> None:
        status, _, body = self.call("GET", "/api/info")
        self.assertEqual((status, json.loads(body)["needs_setup"]), (200, True))
        with open(os.path.join(self.data, "setup_code.txt"), encoding="utf-8") as fh:
            code = fh.read().strip()
        spec = {"username": "boss", "display_name": "Boss", "password": PASSWORD}
        self.assertEqual(self.call("POST", "/api/register", dict(spec, setup_code="wrongcode"))[0], 403)
        status, headers, body = self.call("POST", "/api/register", dict(spec, setup_code=code))
        self.assertEqual(status, 201, body)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertNotIn("Secure", headers["Set-Cookie"])
        self.assertTrue(support.wait_until(lambda: not os.path.exists(os.path.join(self.data, "setup_code.txt")), 5))
        self.assertFalse(json.loads(self.call("GET", "/api/info")[2])["needs_setup"])

    def sign_in(self) -> str:
        status, headers, body = self.call("POST", "/api/login", {"username": "boss", "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return headers["Set-Cookie"].split(";")[0]

    def test_01_websocket_ready_message_events_before_res_and_receipts(self) -> None:
        cookie = self.sign_in()
        client = support.RawWsClient(self.port, cookie)
        self.addCleanup(client.close)
        self.assertEqual(client.response[0], 101)
        ready = json.loads(client.recv_text())
        self.assertEqual(ready["t"], "ev.ready")
        chat_id = ready["d"]["chats"][0]["id"]
        client.send_text(
            json.dumps(
                {"t": "msg.send", "id": "m1", "d": {"chat_id": chat_id, "client_id": "client-0001", "body": "hi"}}
            )
        )
        frames = []
        while True:
            frame = json.loads(client.recv_text())
            frames.append(frame)
            if frame["t"] == "res":
                break
        names = [f["t"] for f in frames]
        self.assertEqual(names[0], "ev.message")
        self.assertEqual(names[-1], "res")
        self.assertTrue(frames[-1]["ok"])
        self.assertIn("ev.receipt", names)  # keyed frame, enqueued before the res
        self.assertLess(names.index("ev.receipt"), names.index("res"))

    def test_02_upload_and_download_through_the_real_access_rule(self) -> None:
        cookie = self.sign_in()
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        status, _, body = self.call(
            "POST", "/api/upload", png, cookie, {"X-File-Name": "pic.png", "Content-Type": "text/html"}
        )
        self.assertEqual(status, 201, body)
        attachment = json.loads(body)["attachment"]
        self.assertEqual((attachment["mime"], attachment["kind"]), ("image/png", "image"))
        status, headers, data = self.call("GET", attachment["url"], cookie=cookie)
        self.assertEqual((status, data), (200, png))
        self.assertEqual(headers["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(self.call("GET", attachment["url"])[0], 401)
        status, _, _ = self.call("GET", attachment["url"], cookie=cookie, headers={"Range": "bytes=0-7"})
        self.assertEqual(status, 206)

    def test_03_control_poll_reaches_the_hub_only_for_other_processes(self) -> None:
        hub = self.server.hub
        cookie = self.sign_in()
        client = support.RawWsClient(self.port, cookie)
        self.addCleanup(client.close)
        ready = json.loads(client.recv_text())
        chat_id = ready["d"]["chats"][0]["id"]
        time.sleep(1.0)
        base = hub.counters["external_change_calls"]
        for i in range(60):  # commits of the in-process writer never fire the poll
            client.send_text(
                json.dumps(
                    {
                        "t": "msg.send",
                        "id": "s%d" % i,
                        "d": {"chat_id": chat_id, "client_id": "burst-%04d" % i, "body": "x"},
                    }
                )
            )
        seen = 0
        deadline = time.monotonic() + 20
        while seen < 60 and time.monotonic() < deadline:
            if json.loads(client.recv_text()).get("t") == "res":
                seen += 1
        self.assertEqual(seen, 60)
        time.sleep(1.5)
        self.assertEqual(hub.counters["external_change_calls"], base)
        out, err = io.StringIO(), io.StringIO()
        env = {"DESKTALK_TEST": "1", "DESKTALK_SCRYPT_N": "1024"}
        stdin = io.StringIO(PASSWORD + "\n")
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(mock.patch.object(sys, "stdin", stdin))
            code = cli.main(["create-admin", "second", "--password-stdin", "--data-dir", self.data])
        self.assertEqual(code, 0, err.getvalue())
        seen = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            frame = json.loads(client.recv_text())
            if frame["t"] in ("ev.user_update", "ev.chat_members", "ev.chat_update", "ev.message"):
                seen.append(frame["t"])
            if frame["t"] == "ev.message" and frame["d"]["message"]["kind"] == "system":
                break
        self.assertEqual(seen, ["ev.user_update", "ev.chat_members", "ev.message"])
        self.assertEqual(hub.counters["external_change_calls"], base + 1)

    def test_04_kicked_user_gets_ev_kicked_before_close_4001(self) -> None:
        cookie = self.sign_in()
        client = support.RawWsClient(self.port, cookie)
        self.addCleanup(client.close)
        json.loads(client.recv_text())
        status, _, _ = self.call("POST", "/api/logout", cookie=cookie)
        self.assertEqual(status, 204)
        kicked = json.loads(client.recv_text())
        self.assertEqual((kicked["t"], kicked["d"]["reason"]), ("ev.kicked", "logout"))
        self.assertEqual(client.recv_close(), 4001)

    def test_99_shutdown_closes_sockets_with_1001(self) -> None:
        cookie = self.sign_in()
        client = support.RawWsClient(self.port, cookie)
        self.addCleanup(client.close)
        json.loads(client.recv_text())
        self.server.stop()
        self.assertTrue(self.server.join(30))
        self.assertEqual(client.recv_close(), 1001)


if __name__ == "__main__":
    unittest.main()
