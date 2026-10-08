"""``EXPLAIN QUERY PLAN`` of every statement the db-chat functions execute (SPEC 2.3: index-backed, no table scans)."""

from __future__ import annotations

import re
import unittest
from typing import Any, Callable, List, Set, Tuple

try:
    from tests import test_dbchat_support as support
except ImportError:  # run as `unittest discover -s tests` without the repo root importable as a package
    import test_dbchat_support as support  # type: ignore[no-redef]

from chatd import db_chats, db_messages, db_receipts

#: ``msg.search``'s LIKE is the only statement allowed to read a table without an index (SPEC 2.3, time-boxed).
_SEARCH_MARK = "LIKE"
_SCAN = re.compile(r"^SCAN (\w+)")


class QueryPlanTest(support.DbChatCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.user("alice")
        self.b = self.user("bob")
        self.c = self.user("carol")
        self.g = self.group(self.a, self.b, self.c)
        self.dormant = self.dm(self.a, self.c)

    def statements(self, fn: Callable[..., Any], *args: Any, reader: bool = False) -> List[str]:
        collected: List[str] = []

        def traced(conn: Any) -> Any:
            conn.set_trace_callback(collected.append)
            try:
                return fn(conn, *args)
            finally:
                conn.set_trace_callback(None)

        if reader:
            self.read(traced)
        else:
            self.write(traced)
        return collected

    def plans(self, statements: List[str]) -> List[Tuple[str, List[str]]]:
        def explain(conn: Any) -> List[Tuple[str, List[str]]]:
            result = []
            for sql in dict.fromkeys(statements):
                if not sql.lstrip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE", "WITH")):
                    continue
                rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
                result.append((sql, [row[3] for row in rows]))
            return result

        return self.write(explain)

    def assert_indexed(self, statements: List[str], allow_scan: Set[str] = frozenset()) -> None:
        offenders = []
        for sql, details in self.plans(statements):
            if _SEARCH_MARK in sql and "fold(" in sql:
                continue
            for detail in details:
                found = _SCAN.match(detail)
                if found and found.group(1) not in allow_scan and not detail.startswith("SCAN (subquery"):
                    offenders.append((detail, sql[:240]))
        self.assertEqual(offenders, [])

    def test_send_paths(self) -> None:
        attachment = self.add_attachment(self.a)
        first = self.mid(self.send(self.b, self.g, "to reply to"))
        self.assert_indexed(
            self.statements(db_messages.msg_send, self.a, self.g, "plan-send-1", "hello @bob", attachment, first, first)
        )
        self.assert_indexed(self.statements(db_messages.msg_send, self.a, self.dormant, "plan-send-2", "first in dm"))
        self.assert_indexed(self.statements(db_messages.msg_send, self.a, self.g, "plan-send-1", "again"))  # dedupe

    def test_history_cursors(self) -> None:
        ids = [self.mid(self.send(self.a, self.g, "m%d" % i)) for i in range(6)]
        for kwargs in ({}, {"before_id": ids[3]}, {"after_id": ids[2]}, {"after_id": 0}, {"around_id": ids[3]}):
            self.assert_indexed(
                self.statements(
                    db_messages.chat_history,
                    self.b,
                    self.g,
                    kwargs.get("before_id"),
                    kwargs.get("after_id"),
                    kwargs.get("around_id"),
                    3,
                    reader=True,
                )
            )

    def test_unread_counts_and_chat_builders(self) -> None:
        self.send(self.a, self.g, "@bob one")
        self.assert_indexed(self.statements(db_receipts.counters, self.b, self.g, reader=True))
        self.assert_indexed(self.statements(db_receipts.counters_for_viewer, self.b, reader=True))
        self.assert_indexed(self.statements(db_chats.chat_get, self.b, self.g, reader=True))
        self.assert_indexed(self.statements(db_chats.build_chats, [(self.b, self.g), (self.c, self.g)], reader=True))
        self.assert_indexed(self.statements(db_chats.build_ready, self.b, reader=True), allow_scan={"users"})

    def test_message_mutations(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "to mutate"))
        other = self.mid(self.send(self.b, self.g, "reply source"))
        for fn, args in (
            (db_messages.msg_edit, (self.a, mid, "edited @bob")),
            (db_messages.msg_react, (self.b, mid, "\U0001f44d")),
            (db_messages.msg_star, (self.b, mid, True)),
            (db_messages.msg_pin, (self.a, self.g, mid, True)),
            (db_messages.msg_pin, (self.a, self.g, mid, False)),
            (db_messages.msg_forward, (self.a, [mid, other], [self.g, self.dormant], "plan-forward-1")),
            (db_messages.msg_delete, (self.b, other, "me")),
            (db_messages.msg_delete, (self.a, mid, "everyone")),
        ):
            self.assert_indexed(self.statements(fn, *args))

    def test_receipts(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "to read"))
        self.assert_indexed(
            self.statements(db_receipts.receipt_delivered, self.b, [{"chat_id": self.g, "up_to_id": mid}])
        )
        self.assert_indexed(self.statements(db_receipts.receipt_read, self.c, self.g, mid))
        self.assert_indexed(self.statements(db_chats.chat_clear, self.c, self.g))

    def test_reads(self) -> None:
        mid = self.mid(self.send(self.a, self.g, "see http://example.org needle"))
        self.write(db_messages.msg_star, self.b, mid, True)
        self.assert_indexed(self.statements(db_messages.msg_info, self.a, mid, reader=True))
        self.assert_indexed(self.statements(db_messages.msg_starred, self.b, None, None, 5, reader=True))
        self.assert_indexed(self.statements(db_messages.msg_starred, self.b, self.g, None, 5, reader=True))
        for kind in ("media", "files", "links"):
            self.assert_indexed(self.statements(db_messages.msg_shared, self.b, self.g, kind, None, 5, reader=True))
        self.assert_indexed(self.statements(db_messages.msg_search, self.b, "needle", self.g, None, 5, reader=True))

    def test_group_administration(self) -> None:
        d = self.user("dave")
        for fn, args in (
            (db_chats.chat_update, (self.a, self.g, "Renamed", "d", True)),
            (db_chats.chat_add_members, (self.a, self.g, [d])),
            (db_chats.chat_set_admin, (self.a, self.g, d, True)),
            (db_chats.chat_prefs, (self.a, self.g, 4102444800, True)),
            (db_chats.chat_remove_member, (self.a, self.g, self.c)),
            (db_chats.chat_leave, (d, self.g)),
            (db_chats.chat_open_direct, (self.b, self.c)),
            (db_chats.chat_create_group, (self.a, "Another", [self.b, self.c])),
        ):
            self.assert_indexed(self.statements(fn, *args))

    def test_the_membership_index_loader_is_a_constant_number_of_statements(self) -> None:
        selects = [
            sql for sql in self.statements(db_chats.load_membership_index, reader=True) if sql.startswith("SELECT")
        ]
        self.assertEqual(len(selects), 2)

    def test_ready_has_no_per_chat_queries(self) -> None:
        def populate(viewer: int, count: int) -> None:
            for i in range(count):
                chat = self.group(self.a, viewer, title="n%d" % i)
                first = self.mid(self.send(self.a, chat, "first"))
                self.send(self.a, chat, "reply", reply_to_id=first)
                self.write(db_messages.msg_pin, self.a, chat, first, True)

        few, many = self.user("few"), self.user("many")
        populate(few, 3)
        populate(many, 30)
        small = self.statements(db_chats.build_ready, few, reader=True)
        large = self.statements(db_chats.build_ready, many, reader=True)
        self.assertEqual(len(small), len(large))
        self.assertLess(len(large), 25)
        self.assertEqual(len(self.read(db_chats.build_ready, many)["chats"]), 31)


if __name__ == "__main__":
    unittest.main()
