import socket
import threading
import time
import unittest
from unittest import mock

from desktalk import client as client_mod
from desktalk.client import ChatClient
from desktalk.protocol import MAX_LINE, encode
from desktalk.server import hub as hub_mod
from tests.helpers import Events, ServerCase, msg_with, of_type


class ReconnectTests(ServerCase):
    def test_auto_reconnect_and_no_duplicate_history(self):
        srv = self.start_server()
        port = srv.port
        a, ea = self.join(srv, "alice")
        ea.wait(of_type("welcome", rejoined=False))
        a.say("before")
        ea.wait(msg_with("before"))
        before_id = a.last_id

        srv.stop()
        self.assertTrue(ea.wait(of_type("disconnected"))["reconnecting"])
        self.assertFalse(a.say("lost"))  # sending while reconnecting reports failure

        # server comes back on the same port + db; one more message was stored meanwhile
        self.store.add("bob", "while-away", time.time())
        srv2 = self.start_server(port=port)
        ea.wait(of_type("welcome", rejoined=True), timeout=15)
        ea.wait(msg_with("while-away"))

        time.sleep(0.3)
        ea.drain()
        self.assertEqual(len([e for e in ea.seen if msg_with("before")(e)]), 1)  # not replayed again
        self.assertGreater(a.last_id, before_id)
        self.assertEqual(srv2.online(), ["alice"])

    def test_no_reconnect_when_disabled(self):
        srv = self.start_server()
        a, ea = self.join(srv, "alice", auto_reconnect=False)
        ea.wait(of_type("welcome"))
        srv.stop()
        self.assertFalse(ea.wait(of_type("disconnected"))["reconnecting"])

    def test_close_stops_thread_quietly(self):
        srv = self.start_server()
        a, ea = self.join(srv, "alice")
        ea.wait(of_type("welcome"))
        a.close()
        a.close()  # idempotent
        deadline = time.time() + 5
        while srv.online() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(srv.online(), [])
        a.thread.join(5)
        self.assertFalse(a.thread.is_alive())

    def test_name_taken_during_reconnect_is_retried_not_fatal(self):
        c = ChatClient("127.0.0.1", 1, "alice", lambda ev: None)
        c._welcomed = True
        forwarded = c._handle({"type": "error", "code": "name_taken", "text": "x"})
        self.assertFalse(forwarded)
        self.assertIsNone(c._fatal)


class HeartbeatTests(ServerCase):
    def test_idle_client_dropped_but_pinging_client_stays(self):
        with mock.patch.object(hub_mod, "IDLE_TIMEOUT", 1.0), \
                mock.patch.object(client_mod, "PING_INTERVAL", 0.2), \
                mock.patch.object(client_mod, "READ_TICK", 0.1):
            srv = self.start_server()
            a, ea = self.join(srv, "alice")
            ea.wait(of_type("welcome"))

            idle = socket.create_connection(("127.0.0.1", srv.port))
            idle.settimeout(5)
            idle.sendall(encode({"type": "join", "user": "idler"}))
            self.assertIn(b"welcome", idle.makefile("rb").readline())

            time.sleep(2.5)
            self.assertEqual(srv.online(), ["alice"])
            self.assertTrue(a.connected)
            idle.close()


class FakeServer:
    """A TCP listener whose behaviour we script, to test how the client copes with a bad server."""

    def __init__(self, behaviour):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.behaviour = behaviour
        self.conns = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.conns.append(conn)
            try:
                self.behaviour(conn)
            except OSError:
                pass

    def close(self):
        self.listener.close()
        for c in self.conns:
            c.close()


class MisbehavingServerTests(unittest.TestCase):
    def setUp(self):
        self.to_close = []

    def tearDown(self):
        for item in self.to_close:
            item.close()

    def client_for(self, behaviour, **kw):
        fake = FakeServer(behaviour)
        ev = Events()
        c = ChatClient("127.0.0.1", fake.port, "alice", ev, **kw)
        self.to_close += [fake, c]
        c.connect()
        return c, ev

    def test_silent_server_is_detected_as_dead(self):
        with mock.patch.object(client_mod, "DEAD_AFTER", 0.6), mock.patch.object(client_mod, "READ_TICK", 0.1):
            c, ev = self.client_for(lambda conn: None)
            self.assertTrue(ev.wait(of_type("disconnected"), timeout=5)["reconnecting"])
            ev.wait(of_type("reconnecting"))

    def test_endless_line_drops_connection(self):
        def flood(conn):
            conn.sendall(b"x" * (MAX_LINE * 3))  # no newline, ever
        c, ev = self.client_for(flood)
        self.assertTrue(ev.wait(of_type("disconnected"), timeout=5)["reconnecting"])

    def test_garbage_and_odd_events_do_not_crash_client(self):
        def garbage(conn):
            conn.sendall(b'not json\n[1,2]\n\n{"type":"error","code":["x"],"text":5}\n'
                         b'{"type":"msg","id":"nan","user":1,"text":null}\n'
                         + encode({"type": "system", "text": "alive"}))
            time.sleep(1)
        c, ev = self.client_for(garbage)
        ev.wait(of_type("system", text="alive"))
        self.assertIsNone(c._fatal)

    def test_callback_exception_does_not_kill_reader(self):
        seen = []

        def boom_then_ok(ev):
            seen.append(ev["type"])
            if ev["type"] == "system" and ev["text"] == "first":
                raise RuntimeError("UI bug")

        def two(conn):
            conn.sendall(encode({"type": "system", "text": "first"}) + encode({"type": "system", "text": "second"}))
            time.sleep(1)

        fake = FakeServer(two)
        c = ChatClient("127.0.0.1", fake.port, "alice", boom_then_ok)
        self.to_close += [fake, c]
        c.connect()
        deadline = time.time() + 5
        while seen.count("system") < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(seen.count("system"), 2)

    def test_invalid_host_raises_oserror_not_unicode_error(self):
        c = ChatClient("a..b", 9, "alice", lambda ev: None)
        with self.assertRaises(OSError):
            c.connect(timeout=1)

    def test_refused_connection_raises_oserror(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OSError):
            ChatClient("127.0.0.1", port, "alice", lambda ev: None).connect(timeout=1)

    def test_bad_port_rejected_early(self):
        with self.assertRaises(ValueError):
            ChatClient("127.0.0.1", "abc", "alice", lambda ev: None)

    def test_send_without_connection_is_false(self):
        c = ChatClient("127.0.0.1", 9, "alice", lambda ev: None)
        self.assertFalse(c.say("x"))
        self.assertFalse(c.send({"x": object()}))  # unencodable -> False, no exception


if __name__ == "__main__":
    unittest.main()
