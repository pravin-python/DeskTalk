import json
import socket
import unittest
from unittest import mock

from desktalk.errors import StoreError
from desktalk.protocol import DISCOVERY_MAGIC, MAX_LINE, MAX_TEXT_LEN, decode, encode
from desktalk.server import app as app_mod
from desktalk.server.responder import DiscoveryResponder
from tests.helpers import ServerCase, msg_with, of_type


class RawPeer:
    """A bare socket speaking the wire protocol, for sending things a real client never would."""

    def __init__(self, port, join=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.f = self.sock.makefile("rb")
        if join:
            self.send({"type": "join", "user": join})
            assert self.read()["type"] == "welcome"

    def send(self, obj):
        self.sock.sendall(encode(obj))

    def send_raw(self, data):
        self.sock.sendall(data)

    def read(self):
        try:
            line = self.f.readline()
        except OSError:  # the server may reset the connection after sending its last message
            return None
        return decode(line) if line else None

    def read_until(self, pred, limit=50):
        for _ in range(limit):
            ev = self.read()
            if ev is None or pred(ev):
                return ev
        raise AssertionError("expected message not received")

    def close(self):
        try:
            self.f.close()
            self.sock.close()
        except OSError:
            pass


class ChatTests(ServerCase):
    def peer(self, srv, join=None):
        p = RawPeer(srv.port, join)
        self.addCleanup(p.close)
        return p

    def test_broadcast_and_history_replay(self):
        srv = self.start_server()
        a, ea = self.join(srv, "alice")
        ea.wait(of_type("welcome"))
        a.say("pehla")
        a.say("doosra")
        ea.wait(msg_with("doosra"))

        b, eb = self.join(srv, "bob")
        eb.wait(of_type("welcome"))
        got = eb.wait(msg_with("doosra"))
        self.assertEqual(got["user"], "alice")
        self.assertIsInstance(got["id"], int)

        b.say("hi alice")
        self.assertEqual(ea.wait(lambda e: e.get("type") == "msg" and e["user"] == "bob")["text"], "hi alice")

    def test_dm(self):
        srv = self.start_server()
        a, ea = self.join(srv, "alice")
        b, eb = self.join(srv, "bob")
        eb.wait(of_type("welcome"))
        ea.wait(lambda e: e.get("type") == "users" and e["users"] == ["alice", "bob"])
        a.dm("bob", "secret")
        self.assertEqual(eb.wait(of_type("dm"))["text"], "secret")
        a.dm("nobody", "x")
        ea.wait(of_type("error"))

    # ---- join / auth ----

    def test_wrong_password_is_fatal(self):
        srv = self.start_server(password="s3cret")
        c, ev = self.join(srv, "mallory", password="nope")
        ev.wait(of_type("error", code="auth"))  # the machine-readable code is the contract, not the wording
        self.assertFalse(ev.wait(of_type("disconnected"))["reconnecting"])
        self.assertEqual(srv.online(), [])

    def test_missing_password_is_fatal(self):
        srv = self.start_server(password="s3cret")
        c, ev = self.join(srv, "mallory")
        ev.wait(of_type("error", code="auth"))
        self.assertFalse(ev.wait(of_type("disconnected"))["reconnecting"])

    def test_correct_password(self):
        srv = self.start_server(password="s3cret")
        c, ev = self.join(srv, "alice", password="s3cret")
        ev.wait(of_type("welcome"))
        self.assertEqual(srv.online(), ["alice"])

    def test_non_string_password_is_rejected_not_crashing(self):
        srv = self.start_server(password="s3cret")
        p = self.peer(srv)
        p.send({"type": "join", "user": "x", "password": {"a": 1}})
        self.assertEqual(p.read()["code"], "auth")

    def test_duplicate_name_is_fatal(self):
        srv = self.start_server()
        a, ea = self.join(srv, "alice")
        ea.wait(of_type("welcome"))
        b, eb = self.join(srv, "ALICE")
        eb.wait(of_type("error", code="name_taken"))
        self.assertFalse(eb.wait(of_type("disconnected"))["reconnecting"])
        self.assertEqual(srv.online(), ["alice"])

    def test_bad_names_rejected(self):
        srv = self.start_server()
        for bad in ("", "has space", 123, None, ["x"]):
            p = self.peer(srv)
            p.send({"type": "join", "user": bad})
            self.assertEqual(p.read()["code"], "bad_name", msg=repr(bad))
        self.assertEqual(srv.online(), [])

    def test_hostile_since_id_is_ignored(self):
        srv = self.start_server()
        self.store.add("x", "old", 1.0)
        for i, bad in enumerate(("abc", -5, 10 ** 30, 1.5, None, [1], True)):
            p = self.peer(srv)
            p.send({"type": "join", "user": "u{}".format(i), "since_id": bad})
            self.assertEqual(p.read()["type"], "welcome", msg=repr(bad))

    # ---- input hardening ----

    def test_message_before_join_is_refused(self):
        srv = self.start_server()
        p = self.peer(srv)
        p.send({"type": "msg", "text": "hi"})
        self.assertIn("join", p.read()["text"].lower())

    def test_unknown_type_gets_error_and_connection_survives(self):
        srv = self.start_server()
        p = self.peer(srv, join="eve")
        p.send({"type": "nonsense"})
        p.send({"type": ["unhashable"]})
        p.send({"no": "type"})
        for _ in range(3):
            self.assertEqual(p.read_until(lambda e: e["type"] == "error")["type"], "error")
        p.send({"type": "ping"})
        self.assertEqual(p.read_until(lambda e: e["type"] == "pong")["type"], "pong")

    def test_bad_json_and_deep_nesting_do_not_kill_connection(self):
        srv = self.start_server()
        p = self.peer(srv, join="eve")
        p.send_raw(b"not json\n")
        p.send_raw(b"[" * 60000 + b"\n")
        p.send_raw(b"[1,2]\n")
        for _ in range(3):
            self.assertEqual(p.read_until(lambda e: e["type"] == "error")["type"], "error")
        p.send({"type": "ping"})
        p.read_until(lambda e: e["type"] == "pong")

    def test_oversize_line_closes_connection(self):
        srv = self.start_server()
        p = self.peer(srv)
        p.send_raw(b"x" * (MAX_LINE + 10) + b"\n")
        self.assertEqual(p.read()["type"], "error")
        self.assertIsNone(p.read())  # server closed the connection

    def test_text_too_long_and_wrong_types(self):
        srv = self.start_server()
        p = self.peer(srv, join="eve")
        p.send({"type": "msg", "text": "x" * (MAX_TEXT_LEN + 1)})
        self.assertIn(str(MAX_TEXT_LEN), p.read_until(lambda e: e["type"] == "error")["text"])
        p.send({"type": "msg", "text": {"a": 1}})
        p.send({"type": "dm", "to": 5, "text": "x"})
        p.send({"type": "dm", "to": "eve", "text": ["x"]})
        for _ in range(3):
            p.read_until(lambda e: e["type"] == "error")
        self.assertEqual(srv.online(), ["eve"])

    def test_lone_surrogate_cannot_break_other_clients(self):
        srv = self.start_server()
        victim, ev = self.join(srv, "victim")
        ev.wait(of_type("welcome"))
        p = self.peer(srv, join="eve")
        p.send_raw(b'{"type": "msg", "text": "boom \\ud800 end"}\n')
        got = ev.wait(lambda e: e.get("type") == "msg" and e["user"] == "eve")
        self.assertIn("boom", got["text"])
        self.assertTrue(victim.connected)
        victim.say("still here")
        ev.wait(msg_with("still here"))

    # ---- fault isolation ----

    def test_handler_exception_does_not_drop_client(self):
        srv = self.start_server()
        a, ea = self.join(srv, "alice")
        ea.wait(of_type("welcome"))
        with mock.patch.object(self.store, "add", side_effect=RuntimeError("db exploded")):
            a.say("one")
            err = ea.wait(of_type("error"))
        self.assertIn("internal", err["text"].lower())
        a.say("two")
        ea.wait(msg_with("two"))
        self.assertTrue(a.connected)

    def test_store_failure_still_delivers_message(self):
        srv = self.start_server()
        a, ea = self.join(srv, "alice")
        ea.wait(of_type("welcome"))
        with mock.patch.object(self.store, "add", side_effect=StoreError("disk full")):
            a.say("no id")
            got = ea.wait(msg_with("no id"))
        self.assertNotIn("id", got)

    def test_history_read_failure_does_not_block_join(self):
        srv = self.start_server()
        with mock.patch.object(self.store, "recent", side_effect=StoreError("corrupt")):
            c, ev = self.join(srv, "alice")
            ev.wait(of_type("welcome"))

    def test_shutdown_notifies_and_closes_clients(self):
        srv = self.start_server()
        a, ea = self.join(srv, "alice")
        ea.wait(of_type("welcome"))
        srv.call(srv.chat.shutdown("bye now"))
        self.assertEqual(ea.wait(of_type("system", text="bye now"))["text"], "bye now")
        self.assertTrue(ea.wait(of_type("disconnected"))["reconnecting"])  # client will retry


class DiscoveryResponderTests(ServerCase):
    def responder(self, **kw):
        srv = self.start_server(**kw)
        sent = []
        proto = DiscoveryResponder(srv.chat)
        proto.connection_made(mock.Mock(sendto=lambda data, addr: sent.append((data, addr))))
        return proto, sent

    def test_replies_with_lock_state(self):
        proto, sent = self.responder(password="x")
        proto.datagram_received(DISCOVERY_MAGIC.encode(), ("1.2.3.4", 1))
        self.assertTrue(json.loads(sent[0][0].decode())["locked"])

    def test_ignores_other_packets_and_never_raises(self):
        proto, sent = self.responder()
        for junk in (b"", b"hello", b"\xff\xfe", b"{" * 10):
            proto.datagram_received(junk, ("1.2.3.4", 1))
        self.assertEqual(sent, [])
        proto.transport = mock.Mock(sendto=mock.Mock(side_effect=OSError("net down")))
        proto.datagram_received(DISCOVERY_MAGIC.encode(), ("1.2.3.4", 1))  # must not raise


class AppTests(unittest.TestCase):
    def test_parse_args_defaults_and_env_password(self):
        with mock.patch.dict("os.environ", {"DESKTALK_PASSWORD": "envpw"}):
            cfg = app_mod.parse_args(["--port", "9100", "--no-discovery"])
        self.assertEqual((cfg.port, cfg.password, cfg.discovery), (9100, "envpw", False))

    def test_cli_password_beats_env(self):
        with mock.patch.dict("os.environ", {"DESKTALK_PASSWORD": "envpw"}):
            self.assertEqual(app_mod.parse_args(["--password", "cli"]).password, "cli")

    def test_invalid_port_is_rejected(self):
        for bad in ("0", "70000", "abc", "-1"):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                app_mod.parse_args(["--port", bad])

    def test_port_in_use_returns_error_code(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        try:
            port = busy.getsockname()[1]
            code = app_mod.main(["--host", "127.0.0.1", "--port", str(port), "--no-discovery", "--db", ":memory:"])
        finally:
            busy.close()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
