import socket
import time
import unittest

from desktalk.protocol import MAX_TEXT_LEN, encode
from tests.helpers import ServerCase, of_type


class SlowClientTests(ServerCase):
    def test_slow_client_does_not_block_others_and_is_kicked(self):
        srv = self.start_server()
        fast, ef = self.join(srv, "fast")
        sender, es = self.join(srv, "sender")
        ef.wait(of_type("welcome"))
        es.wait(of_type("welcome"))

        slow = socket.create_connection(("127.0.0.1", srv.port))
        self.addCleanup(slow.close)
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        slow.sendall(encode({"type": "join", "user": "slow"}))  # joins, then never reads
        deadline = time.time() + 5
        while "slow" not in srv.online() and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("slow", srv.online())

        big = "x" * (MAX_TEXT_LEN - 10)
        total = 2500
        for i in range(total):
            sender.say("{}:{}".format(i, big))
            if i % 20 == 19:  # keep the fast reader in step: this tests the slow client, not a flood
                ef.wait(lambda e, i=i: e.get("type") == "msg" and e["text"].startswith("{}:".format(i)),
                        timeout=30)

        deadline = time.time() + 15
        while "slow" in srv.online() and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(srv.online(), ["fast", "sender"])  # the slow client was kicked


if __name__ == "__main__":
    unittest.main()
