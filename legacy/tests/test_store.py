import os
import tempfile
import threading
import unittest
from unittest import mock

from desktalk import store as store_mod
from desktalk.store import HistoryStore, MemoryStore, StoreError, open_store

needs_sqlite = unittest.skipIf(store_mod.sqlite3 is None, "sqlite3 is not available in this Python")


class StoreContract:
    """Behaviour every store must have."""

    def make(self):
        raise NotImplementedError

    def test_add_recent_since(self):
        st = self.make()
        ids = [st.add("a", "m{}".format(i), float(i)) for i in range(5)]
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual(st.last_id(), 5)
        self.assertEqual([m["text"] for m in st.recent(3)], ["m2", "m3", "m4"])
        self.assertEqual([m["id"] for m in st.recent(10, since_id=3)], [4, 5])
        self.assertEqual(st.recent(10, since_id=5), [])
        self.assertEqual(st.recent(0), [])
        st.close()

    def test_empty(self):
        st = self.make()
        self.assertEqual(st.last_id(), 0)
        self.assertEqual(st.recent(10), [])
        st.close()

    def test_concurrent_adds(self):
        st = self.make()

        def worker():
            for i in range(50):
                st.add("t", "x{}".format(i), 1.0)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(st.last_id(), 200)
        self.assertEqual(len({m["id"] for m in st.recent(500)}), 200)
        st.close()


class MemoryStoreTests(StoreContract, unittest.TestCase):
    def make(self):
        return MemoryStore()

    def test_prunes(self):
        with mock.patch.object(store_mod, "KEEP_MESSAGES", 3):
            st = MemoryStore()
            for i in range(10):
                st.add("a", str(i), 0.0)
            self.assertEqual([m["text"] for m in st.recent(100)], ["7", "8", "9"])


@needs_sqlite
class SqliteStoreTests(StoreContract, unittest.TestCase):
    def make(self):
        return HistoryStore(":memory:")

    def test_persists_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            st = HistoryStore(path)
            st.add("a", "hello", 1.0)
            st.close()
            st = HistoryStore(path)
            self.assertEqual(st.recent(5)[0]["text"], "hello")
            st.close()

    def test_prunes(self):
        with mock.patch.object(store_mod, "KEEP_MESSAGES", 3), mock.patch.object(store_mod, "PRUNE_EVERY", 5):
            st = HistoryStore(":memory:")
            for i in range(10):
                st.add("a", str(i), 0.0)
            self.assertEqual([m["text"] for m in st.recent(100)], ["7", "8", "9"])
            st.close()

    def test_errors_become_store_error(self):
        st = HistoryStore(":memory:")
        st.close()
        with self.assertRaises(StoreError):
            st.add("a", "x", 1.0)

    def test_unopenable_path_falls_back(self):
        logs = []
        bad = os.path.join(tempfile.gettempdir(), "definitely", "missing", "dir", "x.db")
        st = open_store(bad, log_fn=logs.append)
        self.assertIsInstance(st, MemoryStore)
        self.assertEqual(len(logs), 1)


class OpenStoreTests(unittest.TestCase):
    def test_memory_path(self):
        self.assertIsInstance(open_store(":memory:"), MemoryStore)

    def test_falls_back_without_sqlite(self):
        logs = []
        with mock.patch.object(store_mod, "sqlite3", None):
            st = open_store("whatever.db", log_fn=logs.append)
        self.assertIsInstance(st, MemoryStore)
        self.assertFalse(st.persistent)
        self.assertEqual(len(logs), 1)


if __name__ == "__main__":
    unittest.main()
