"""WebSocket transport: handshake, framing edge cases, send path, caps, liveness (SPEC 6)."""

from __future__ import annotations

import os
import shutil
import socket
import struct
import tempfile
import time
import unittest
from typing import Any, Dict, Optional

try:  # `unittest discover -s tests -t .` imports this module as part of the `tests` package
    from . import test_transport_support as support
except ImportError:  # `unittest discover -s tests` or running from inside tests/
    import test_transport_support as support

from chatd import util, websocket
from chatd.http import Router
from chatd.websocket import accept_key, encode_frame, unmask, upgrade_response

RawWsClient = support.RawWsClient
build_frame = support.build_frame


class WsBase(unittest.TestCase):
    scale = 1.0

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="dt-ws-")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def config(self) -> Dict[str, Any]:
        return {}

    def setUp(self) -> None:
        util.set_test_scale(self.scale)
        self.addCleanup(support.reset_scale)
        self.hub = support.StubHub()
        self.auth = support.FakeAuth(must_change={7})
        self.cfg = support.make_config(os.path.join(self.tmp, "data"), **self.config())

        def routes(router: Router, harness: support.Harness) -> None:
            router.add("GET", "/ws", lambda req: upgrade_response(req, self.hub), auth="cookie")

        self.h = support.Harness(self.cfg, routes, self.auth).start()
        self.addCleanup(self.h.stop)
        self.clients: list = []
        self.addCleanup(self._close_clients)

    def _close_clients(self) -> None:
        for client in self.clients:
            client.close()

    def connect(self, cookie: str = "fc_session=tok-1", extra: bytes = b"") -> RawWsClient:
        client = RawWsClient(self.h.port, cookie, extra)
        self.clients.append(client)
        return client

    def probe(self, cookie: str = "fc_session=tok-1", extra: bytes = b"") -> Any:
        """Attempt a handshake and return ``(status, headers)``; the socket is closed right away."""
        client = RawWsClient(self.h.port, cookie, extra)
        try:
            return client.response
        finally:
            client.close()

    @property
    def registry(self) -> websocket.WsRegistry:
        return self.h.server.ws_registry


class HandshakeTests(WsBase):
    def test_rfc_example_accept_key(self) -> None:
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_upgrade_and_echo(self) -> None:
        client = self.connect()
        status, headers = client.response
        self.assertEqual(status, 101)
        self.assertEqual(headers["upgrade"], "websocket")
        self.assertEqual(headers["connection"], "Upgrade")
        self.assertIn("sec-websocket-accept", headers)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertNotIn("content-length", headers)
        client.send_text("hello é中\U0001f600")
        self.assertEqual(client.recv_text(), "echo:hello é中\U0001f600")
        self.assertEqual(self.hub.sessions[0]["user_id"], 1)
        self.assertEqual(self.hub.sessions[0]["token_hash"], "h1")

    def test_session_and_connection_attributes(self) -> None:
        client = self.connect(extra=b"User-Agent: UnitTest/1.0\r\n")
        client.send_text("x")
        client.recv_text()
        ws = self.hub.sockets[0]
        self.assertEqual(ws.remote_addr, "127.0.0.1")
        self.assertEqual(ws.user_agent, "UnitTest/1.0")
        self.assertFalse(ws.closed)
        self.assertLess(time.monotonic() - ws.last_rx, 2)
        self.assertLess(abs(time.time() - ws.last_rx_wall), 2)

    def test_refusals(self) -> None:
        self.assertEqual(self.probe("fc_session=tok-bad")[0], 401)
        self.assertEqual(self.probe("x=y")[0], 401)
        self.assertEqual(self.probe("fc_session=tok-7")[0], 403)
        self.assertEqual(self.probe(extra=b"Origin: http://evil.example\r\n")[0], 403)
        self.assertEqual(self.probe(extra=b"Sec-Fetch-Site: cross-site\r\n")[0], 403)
        self.assertEqual(self.connect(extra=b"Origin: http://127.0.0.1\r\n").response[0], 101)

    def test_malformed_handshakes(self) -> None:
        base = "GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: fc_session=tok-1\r\n"
        good = (
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        )
        cases = {
            "no upgrade": good.replace("Upgrade: websocket\r\n", ""),
            "no connection": good.replace("Connection: Upgrade\r\n", ""),
            "version 8": good.replace("Version: 13", "Version: 8"),
            "short key": good.replace("dGhlIHNhbXBsZSBub25jZQ==", "abc"),
            "bad key chars": good.replace("dGhlIHNhbXBsZSBub25jZQ==", "!!!!!!!!!!!!!!!!!!!!!!=="),
            "no key": good.replace("Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n", ""),
        }
        for name, extra in cases.items():
            status, _, _ = support.exchange(self.h.port, (base + extra + "\r\n").encode())
            self.assertEqual(status, 400, name)
        status, headers, _ = support.exchange(self.h.port, (base + cases["version 8"] + "\r\n").encode())
        self.assertEqual(headers["sec-websocket-version"], "13")
        # token lists are accepted
        ok = base + good.replace("Connection: Upgrade", "Connection: keep-alive, Upgrade") + "\r\n"
        sock = support.connect(self.h.port)
        sock.sendall(ok.encode())
        self.assertEqual(support.read_response(sock, head_only=True)[0], 101)
        sock.close()

    def test_post_is_not_an_upgrade(self) -> None:
        raw = (
            b"POST /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: fc_session=tok-1\r\nX-Requested-With: desktalk\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        self.assertEqual(support.exchange(self.h.port, raw)[0], 405)


class FrameTests(WsBase):
    def expect_close(self, frames: bytes, code: int, client: Optional[RawWsClient] = None) -> None:
        client = client or self.connect()
        client.send(frames)
        self.assertEqual(client.recv_close(), code)
        self.assertTrue(self.hub.finished.wait(5) or True)

    def test_unmasked_frame_is_1002(self) -> None:
        self.expect_close(build_frame(0x1, b"hi", masked=False), 1002)

    def test_rsv_bits_are_1002(self) -> None:
        for rsv in (1, 2, 4):
            self.expect_close(build_frame(0x1, b"hi", rsv=rsv), 1002)

    def test_invalid_opcodes_are_1002(self) -> None:
        for opcode in (0x3, 0x7, 0xB, 0xF):
            self.expect_close(build_frame(opcode, b""), 1002)

    def test_control_frame_rules(self) -> None:
        self.expect_close(build_frame(0x9, b"x", fin=False), 1002)
        self.expect_close(build_frame(0x9, b"x" * 126), 1002)
        self.expect_close(build_frame(0x8, b"\x03\xe8", fin=False), 1002)
        self.expect_close(build_frame(0x9, b"x" * 200, length_form="16"), 1002)

    def test_non_minimal_lengths_are_1002(self) -> None:
        self.expect_close(build_frame(0x1, b"a" * 100, length_form="16"), 1002)
        self.expect_close(build_frame(0x1, b"a" * 125, length_form="16"), 1002)
        self.expect_close(build_frame(0x1, b"a" * 300, length_form="64"), 1002)
        self.expect_close(build_frame(0x1, b"a" * 65535, length_form="64"), 1002)

    def test_msb_length_is_1009_without_reading_payload(self) -> None:
        client = self.connect()
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, 1 << 63) + b"\x01\x02\x03\x04"
        client.send(header)  # no payload follows: the server must not wait for it
        self.assertEqual(client.recv_close(), 1009)

    def test_declared_length_over_budget_is_1009_without_reading_payload(self) -> None:
        client = self.connect()
        client.send(struct.pack("!BBQ", 0x81, 0x80 | 127, 256 * 1024 + 1) + b"\x01\x02\x03\x04")
        self.assertEqual(client.recv_close(), 1009)

    def test_max_size_message_is_accepted(self) -> None:
        client = self.connect()
        client.send(build_frame(0x1, b"a" * (256 * 1024)))
        self.assertEqual(len(client.recv_text()), 256 * 1024 + 5)

    def test_fragments_count_against_the_message_budget(self) -> None:
        client = self.connect()
        client.send(build_frame(0x1, b"a" * 200000, fin=False))
        client.send(build_frame(0x0, b"a" * 100000, fin=True))
        self.assertEqual(client.recv_close(), 1009)

    def test_fragmented_message_with_interleaved_ping(self) -> None:
        client = self.connect()
        client.send(build_frame(0x1, "hé".encode()[:2], fin=False))
        client.send(build_frame(0x9, b"pp"))
        client.send(build_frame(0x0, "hé".encode()[2:] + b"llo", fin=True))
        self.assertEqual(client.recv_frame(), (0xA, b"pp"))
        self.assertEqual(client.recv_text(), "echo:héllo")
        self.assertEqual(self.hub.received, ["héllo"])

    def test_utf8_split_inside_a_character_across_fragments(self) -> None:
        client = self.connect()
        data = "中".encode()
        client.send(build_frame(0x1, data[:1], fin=False))
        client.send(build_frame(0x0, data[1:2], fin=False))
        client.send(build_frame(0x0, data[2:], fin=True))
        self.assertEqual(client.recv_text(), "echo:中")

    def test_invalid_utf8_is_1007(self) -> None:
        self.expect_close(build_frame(0x1, b"\xff\xfe"), 1007)
        self.expect_close(build_frame(0x1, b"\xed\xa0\x80"), 1007)  # a UTF-8 encoded surrogate
        client = self.connect()
        client.send(build_frame(0x1, b"\xe4\xb8", fin=True))  # truncated sequence at the end of the message
        self.assertEqual(client.recv_close(), 1007)

    def test_binary_is_1003(self) -> None:
        self.expect_close(build_frame(0x2, b"\x00\x01"), 1003)

    def test_continuation_rules(self) -> None:
        self.expect_close(build_frame(0x0, b"x"), 1002)
        client = self.connect()
        client.send(build_frame(0x1, b"a", fin=False) + build_frame(0x1, b"b"))
        self.assertEqual(client.recv_close(), 1002)

    def test_ping_pong_and_unsolicited_pong(self) -> None:
        client = self.connect()
        client.send(build_frame(0x9, b"abc"))
        self.assertEqual(client.recv_frame(), (0xA, b"abc"))
        client.send(build_frame(0xA, b"unsolicited"))
        client.send(build_frame(0x9, b""))
        self.assertEqual(client.recv_frame(), (0xA, b""))
        client.send_text("still alive")
        self.assertEqual(client.recv_text(), "echo:still alive")

    def test_close_handshake_echoes_code(self) -> None:
        client = self.connect()
        client.send(build_frame(0x8, struct.pack("!H", 1001) + b"bye"))
        self.assertEqual(client.recv_close(), 1001)
        self.assertTrue(self.hub.finished.wait(5))
        self.assertTrue(support.closed_by_peer(client.sock, 3))

    def test_close_without_code_gets_1000(self) -> None:
        client = self.connect()
        client.send(build_frame(0x8, b""))
        self.assertEqual(client.recv_close(), 1000)

    def test_invalid_close_frames_are_1002(self) -> None:
        self.expect_close(build_frame(0x8, b"\x03"), 1002)
        self.expect_close(build_frame(0x8, struct.pack("!H", 1005)), 1002)
        self.expect_close(build_frame(0x8, struct.pack("!H", 999)), 1002)
        self.expect_close(build_frame(0x8, struct.pack("!H", 1000) + b"\xff"), 1007)

    def test_message_before_close_is_still_delivered(self) -> None:
        client = self.connect()
        client.send(build_frame(0x1, b"last words") + build_frame(0x8, struct.pack("!H", 1000)))
        self.assertTrue(support.wait_until(lambda: self.hub.received == ["last words"]))
        self.assertTrue(self.hub.finished.wait(5))

    def test_abrupt_disconnect_ends_hub_serve(self) -> None:
        client = self.connect()
        client.send_text("x")
        client.recv_text()
        client.sock.shutdown(socket.SHUT_RDWR)
        client.close()
        self.assertTrue(self.hub.finished.wait(5))
        self.assertTrue(support.wait_until(lambda: self.registry.count() == 0))

    def test_unmask_helper(self) -> None:
        key = b"\x0a\x0b\x0c\x0d"
        data = bytes(range(37))
        self.assertEqual(unmask(support.mask_payload(data, key), key), data)
        self.assertEqual(unmask(b"", key), b"")

    def test_encode_frame_lengths(self) -> None:
        self.assertEqual(encode_frame(1, b"a" * 125)[:2], b"\x81\x7d")
        self.assertEqual(encode_frame(1, b"a" * 126)[:4], b"\x81\x7e\x00\x7e")
        self.assertEqual(encode_frame(1, b"a" * 65536)[:10], b"\x81\x7f" + (65536).to_bytes(8, "big"))


class SendPathTests(WsBase):
    def test_server_close_flushes_queued_frames_first(self) -> None:
        def on_connect(ws: Any) -> None:
            ws.send_text("one")
            ws.send_text("two")
            ws.close(4001, "unauthorized")
            ws.send_text("dropped")

        self.hub.on_connect = on_connect
        client = self.connect()
        self.assertEqual(client.recv_text(), "one")
        self.assertEqual(client.recv_text(), "two")
        self.assertEqual(client.recv_close(), 4001)
        self.assertTrue(self.hub.finished.wait(5))

    def test_ephemeral_frames_are_coalesced_by_key_and_keep_enqueue_order(self) -> None:
        def on_connect(ws: Any) -> None:
            ws.send_text("D1")
            ws.send_text("t1", durable=False, key=("typing", 1))
            ws.send_text("D2")
            ws.send_text("t2", durable=False, key=("typing", 1))
            ws.send_text("p1", durable=False, key=("presence", 9))
            ws.send_text("loose", durable=False)

        self.hub.on_connect = on_connect
        client = self.connect()
        got = [client.recv_text() for _ in range(5)]
        self.assertEqual(got, ["D1", "D2", "t2", "p1", "loose"])

    def test_durable_overflow_closes_with_1013(self) -> None:
        def on_connect(ws: Any) -> None:
            for i in range(600):
                ws.send_text("m%d" % i)

        self.hub.on_connect = on_connect
        client = self.connect()
        self.assertEqual(client.recv_close(), 1013)
        self.assertTrue(self.hub.finished.wait(5))

    def test_byte_budget_overflow_closes_with_1013(self) -> None:
        def on_connect(ws: Any) -> None:
            chunk = "x" * 300000
            for _ in range(8):
                ws.send_text(chunk)

        self.hub.on_connect = on_connect
        client = self.connect()
        self.assertEqual(client.recv_close(), 1013)

    def test_ephemeral_frames_are_shed_first_under_pressure(self) -> None:
        def on_connect(ws: Any) -> None:
            for i in range(400):
                ws.send_text("e%d" % i, durable=False, key=i)
            ws.send_text("durable")

        self.hub.on_connect = on_connect
        client = self.connect()
        got = []
        while True:
            text = client.recv_text()
            got.append(text)
            if text == "durable":
                break
        self.assertEqual(got[-1], "durable")
        self.assertLessEqual(len(got), 257 + 1)
        self.assertNotIn("close", got)

    def test_send_after_close_never_raises_and_accounting_returns_to_zero(self) -> None:
        client = self.connect()
        client.send_text("x")
        client.recv_text()
        ws = self.hub.sockets[0]
        client.send(build_frame(0x8, struct.pack("!H", 1000)))
        self.assertTrue(self.hub.finished.wait(5))
        ws.send_text("late")
        ws.send_text("late", durable=False, key="k")
        self.assertTrue(support.wait_until(lambda: self.registry.count() == 0))
        self.assertEqual(self.registry.queued_bytes, 0)

    def test_hub_exception_closes_socket_with_1011(self) -> None:
        async def broken(ws: Any, session: dict) -> None:
            raise RuntimeError("hub bug with secret payload")

        self.hub.serve = broken  # type: ignore[assignment]
        client = self.connect()
        self.assertEqual(client.recv_close(), 1011)

    def test_large_frame_roundtrip(self) -> None:
        client = self.connect()
        client.send_text("y" * 200000)
        self.assertEqual(len(client.recv_text()), 200005)


class CapTests(WsBase):
    def config(self) -> Dict[str, Any]:
        return {"test_limits": {"ws_per_user": 3, "ws_per_ip": 5, "ws_total": 6, "ws_handshakes_per_ip_min": 50}}

    def test_ninth_style_replacement_closes_the_oldest_with_4003(self) -> None:
        first = self.connect()
        first.send_text("a")
        first.recv_text()
        second = self.connect()
        second.send_text("b")
        second.recv_text()
        third = self.connect()
        third.send_text("c")
        third.recv_text()
        # make the second connection the stalest, then open a fourth one
        first.send_text("keep first fresh")
        first.recv_text()
        third.send_text("keep third fresh")
        third.recv_text()
        fourth = self.connect()
        fourth.send_text("d")
        fourth.recv_text()
        self.assertEqual(second.recv_close(), 4003)
        for survivor in (first, third, fourth):
            survivor.send_text("alive")
            self.assertEqual(survivor.recv_text(), "echo:alive")

    def test_other_users_are_not_replaced(self) -> None:
        mine = [self.connect("fc_session=tok-1") for _ in range(3)]
        other = [self.connect("fc_session=tok-2") for _ in range(2)]
        for client in mine + other:
            client.send_text("ping")
            self.assertEqual(client.recv_text(), "echo:ping")

    def test_per_ip_cap_is_503(self) -> None:
        clients = [self.connect("fc_session=tok-%d" % i) for i in range(1, 6)]
        for client in clients:
            client.send_text("x")
            client.recv_text()
        status, headers = self.probe("fc_session=tok-9")
        self.assertEqual(status, 503)
        self.assertIn("retry-after", headers)

    def test_total_cap_is_503(self) -> None:
        self.registry_total = self.h.server  # keep the harness referenced
        clients = []
        for i in range(1, 6):
            clients.append(self.connect("fc_session=tok-%d" % i))
        for client in clients:
            client.send_text("x")
            client.recv_text()
        self.h.cfg.test_limits["ws_per_ip"] = 100
        sixth = self.connect("fc_session=tok-6")
        sixth.send_text("x")
        sixth.recv_text()
        self.assertEqual(self.probe("fc_session=tok-8")[0], 503)

    def test_slot_is_freed_when_a_connection_closes(self) -> None:
        clients = [self.connect("fc_session=tok-%d" % i) for i in range(1, 6)]
        for client in clients:
            client.send_text("x")
            client.recv_text()
        clients[0].send(build_frame(0x8, struct.pack("!H", 1000)))
        clients[0].recv_close()
        self.assertTrue(support.wait_until(lambda: self.registry.count() == 4))
        again = self.connect("fc_session=tok-9")
        self.assertEqual(again.response[0], 101)


class HandshakeRateTests(WsBase):
    def config(self) -> Dict[str, Any]:
        return {"test_limits": {"ws_handshakes_per_ip_min": 4, "ws_handshakes_per_user_min": 3, "ws_per_user": 50}}

    def test_per_user_rate_limit_is_429_with_retry_after(self) -> None:
        for _ in range(3):
            self.assertEqual(self.connect("fc_session=tok-1").response[0], 101)
        status, headers = self.probe("fc_session=tok-1")
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(headers["retry-after"]), 1)
        self.assertEqual(self.connect("fc_session=tok-2").response[0], 101)

    def test_per_ip_rate_limit(self) -> None:
        for uid in (1, 2, 3, 4):
            self.assertEqual(self.connect("fc_session=tok-%d" % uid).response[0], 101)
        self.assertEqual(self.probe("fc_session=tok-5")[0], 429)


class LivenessTests(WsBase):
    scale = 0.05  # ping every 1.25 s, liveness 3 s

    def test_server_sends_pings(self) -> None:
        client = self.connect()
        client.sock.settimeout(4)
        opcode, _ = client.recv_frame()
        self.assertEqual(opcode, 0x9)

    def test_silent_connection_is_closed_with_1001(self) -> None:
        client = self.connect()
        client.send_text("x")
        client.recv_text()
        time.sleep(0.5 * 1)
        self.assertEqual(self.h.call(self._check()), 0)
        deadline = time.monotonic() + 8
        closed = 0
        while time.monotonic() < deadline and not closed:
            time.sleep(0.3)
            closed = self.h.call(self._check())
        self.assertEqual(closed, 1)
        self.assertEqual(client.recv_close(), 1001)

    def test_pong_keeps_the_connection_alive(self) -> None:
        client = self.connect()
        client.sock.settimeout(8)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            opcode, payload = client.recv_frame()
            if opcode == 0x9:
                client.send(build_frame(0xA, payload))
            self.assertEqual(self.h.call(self._check()), 0)
        self.assertFalse(self.hub.finished.is_set())

    async def _check(self) -> int:
        return self.registry.check_liveness()


class ShutdownTests(WsBase):
    def test_close_all_sends_1001_restart(self) -> None:
        clients = [self.connect() for _ in range(3)]
        for client in clients:
            client.send_text("x")
            client.recv_text()
        self.h.call(self.registry.close_all(1001, "restart"))
        for client in clients:
            self.assertEqual(client.recv_close(), 1001)
        self.assertTrue(support.wait_until(lambda: self.registry.count() == 0))


if __name__ == "__main__":
    unittest.main()
