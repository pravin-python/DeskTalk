import json
import socket
import unittest
from unittest import mock

from desktalk import discovery
from desktalk.discovery import ServerInfo, build_probe, build_reply, is_probe, parse_reply
from desktalk.protocol import DISCOVERY_REPLY


def reply(**over):
    base = {"magic": DISCOVERY_REPLY, "room": "R", "port": 9009, "users": 2, "locked": True}
    base.update(over)
    return json.dumps(base).encode()


class ParseReplyTests(unittest.TestCase):
    def test_roundtrip(self):
        info = parse_reply(build_reply("Room", 9009, 3, True), "10.0.0.5")
        self.assertEqual(info, ServerInfo("10.0.0.5", 9009, "Room", 3, True))

    def test_probe(self):
        self.assertTrue(is_probe(build_probe()))
        self.assertTrue(is_probe(build_probe() + b"\n"))
        self.assertFalse(is_probe(b"hello"))
        self.assertFalse(is_probe(b"\xff\xfe"))

    def test_rejects_malformed(self):
        bad = [
            b"garbage", b"\xff\xfe", b"[1]", b"null", b"[" * 60000,
            reply(magic="OTHER"),
            reply(port="9009"), reply(port=0), reply(port=70000), reply(port=True), reply(port=None),
        ]
        for data in bad:
            self.assertIsNone(parse_reply(data, "1.1.1.1"), msg=repr(data[:40]))

    def test_sanitises_optional_fields(self):
        info = parse_reply(reply(users="many", locked="yes", room="x" * 500), "1.1.1.1")
        self.assertEqual(info.users, 0)
        self.assertFalse(info.locked)
        self.assertEqual(len(info.room), discovery.MAX_ROOM_LEN)

    def test_missing_optional_fields(self):
        data = json.dumps({"magic": DISCOVERY_REPLY, "port": 9009}).encode()
        info = parse_reply(data, "1.1.1.1")
        self.assertEqual((info.room, info.users, info.locked), ("", 0, False))


class FakeSocket:
    def __init__(self, results):
        self.results = list(results)
        self.closed = False

    def setsockopt(self, *a):
        pass

    def settimeout(self, *a):
        pass

    def sendto(self, *a):
        pass

    def recvfrom(self, n):
        if not self.results:
            raise socket.timeout()
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


class ScanTests(unittest.TestCase):
    def scan_with(self, results, **kw):
        fake = FakeSocket(results)
        with mock.patch.object(discovery.socket, "socket", return_value=fake):
            found = discovery.scan(timeout=kw.get("timeout", 0.3))
        self.assertTrue(fake.closed, "socket must always be closed")
        return found

    def test_collects_valid_and_ignores_garbage(self):
        found = self.scan_with([
            (b"garbage", ("9.9.9.9", 9010)),
            (reply(port=9100), ("1.1.1.2", 9010)),
            (reply(port="bad"), ("1.1.1.3", 9010)),
            (reply(), ("1.1.1.1", 9010)),
        ])
        self.assertEqual([s.host for s in found], ["1.1.1.1", "1.1.1.2"])

    def test_windows_connection_reset_does_not_abort_scan(self):
        found = self.scan_with([ConnectionResetError(10054, "reset"), (reply(), ("1.1.1.1", 9010))])
        self.assertEqual(len(found), 1)

    def test_other_oserror_stops_cleanly(self):
        self.assertEqual(self.scan_with([OSError("boom")]), [])

    def test_socket_creation_failure_returns_empty(self):
        with mock.patch.object(discovery.socket, "socket", side_effect=OSError("no network")):
            self.assertEqual(discovery.scan(timeout=0.1), [])

    def test_deduplicates_by_host(self):
        found = self.scan_with([(reply(room="a"), ("1.1.1.1", 9010)), (reply(room="b"), ("1.1.1.1", 9010))])
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
