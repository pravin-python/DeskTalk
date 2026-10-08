"""SQL / syntax floor of the db-core modules (SPEC section 0, constraint 4): SQLite 3.24 and Python 3.8.

The orchestrator's ``tests/test_syntax_floor.py`` greps the same modules; this suite pins db-core's own sources so a
regression is caught inside the owner's tests too.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import unittest
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SQL_MODULES = ("chatd/db.py", "chatd/db_users.py", "chatd/maintenance.py")
ALL_MODULES = ("chatd/util.py", "chatd/db.py", "chatd/db_users.py", "chatd/auth.py", "chatd/maintenance.py")

#: Forbidden SQL (SPEC section 0.4): needs a SQLite newer than 3.24.
FORBIDDEN_SQL = (
    r"\bRETURNING\b",
    r"->",
    r"\bunixepoch\b",
    r"\bSTRICT\b",
    r"\b(?:RIGHT|FULL)\s+(?:OUTER\s+)?JOIN\b",
    r"\bjson\w*\s*\(",
    r"\b(?:acos|acosh|asin|asinh|atan|atan2|atanh|ceil|ceiling|cos|cosh|degrees|exp|floor|ln|log|log10|log2|mod"
    r"|pi|pow|power|radians|sin|sinh|sqrt|tan|tanh|trunc)\s*\(",
    r"\bOVER\s*\(",
    r"\bROW_NUMBER\b",
    r"\bFILTER\s*\(",
    r"\bNULLS\s+(?:FIRST|LAST)\b",
    r"\bGENERATED\s+ALWAYS\b",
    r"\bIIF\s*\(",
    r"\bUPDATE\b[^;]*?\bFROM\b",
    r"\bRENAME\s+COLUMN\b",
    r"\bDROP\s+COLUMN\b",
)

#: Forbidden Python (SPEC section 0.1): needs a Python newer than 3.8.
FORBIDDEN_PYTHON = (
    r"\bremoveprefix\b",
    r"\bremovesuffix\b",
    r"\bto_thread\b",
    r"asyncio\.timeout\b",
    r"\bTaskGroup\b",
    r"\bzoneinfo\b",
    r"\bis_relative_to\b",
    r"\bwith_stem\b",
    r"\breadlink\b",
    r"\bkw_only\s*=",
    r"zip\([^)]*strict\s*=",
    r"\bbit_count\b",
    r"\bpairwise\b",
    r"\baiter\b",
    r"\banext\b",
    r"\baclosing\b",
    r"\bTypeAlias\b",
    r"\bParamSpec\b",
    r"asyncio\.Runner\b",
    r"asyncio\.Bar" r"rier\b",  # split: a plain grep for the name must not trip over this list
    r"datetime\.UTC\b",
    r"\btomllib\b",
    r"\bfile_digest\b",
    r"\bexcept\s*\*",
    r"\brandbytes\b",
    r"functools\.cache\b",
    r"\bmath\.lcm\b",
    r"basicConfig\([^)]*encoding",
    r"BooleanOptional" r"Action",
    r"asyncio\.get_event_loop\(",
)


def read(relative: str) -> str:
    with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


def string_literals(source: str) -> List[Tuple[int, str]]:
    tree = ast.parse(source, feature_version=(3, 8))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


class SqlFloorTest(unittest.TestCase):
    def test_no_forbidden_sql_token_in_any_string_literal(self) -> None:
        for relative in SQL_MODULES:
            for line, text in string_literals(read(relative)):
                for pattern in FORBIDDEN_SQL:
                    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                    self.assertIsNone(match, "%s:%d matches %s" % (relative, line, pattern))

    def test_the_scan_itself_catches_the_listed_tokens(self) -> None:
        samples = (
            "SELECT 1 RETURNING id",
            "SELECT a -> b",
            "SELECT unixepoch()",
            "CREATE TABLE t(a) STRICT",
            "SELECT 1 FROM a RIGHT JOIN b",
            "SELECT json_extract(a, 1)",
            "SELECT floor(1.5)",
            "SELECT row_number() OVER (ORDER BY a)",
            "SELECT count(*) FILTER (WHERE a)",
            "SELECT a ORDER BY a NULLS FIRST",
            "CREATE TABLE t(a INT GENERATED ALWAYS AS (1))",
            "SELECT iif(a, 1, 2)",
            "UPDATE t SET a = b.a FROM b WHERE b.id = t.id",
            "ALTER TABLE t RENAME COLUMN a TO b",
            "ALTER TABLE t DROP COLUMN a",
        )
        for sample in samples:
            self.assertTrue(
                any(re.search(pattern, sample, re.IGNORECASE | re.DOTALL) for pattern in FORBIDDEN_SQL), sample
            )
        for fine in (
            "DELETE FROM sessions WHERE user_id = ?",
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            "SELECT COALESCE(SUM(size), 0) FROM attachments WHERE uploader_id = ?",
        ):
            self.assertFalse(
                any(re.search(pattern, fine, re.IGNORECASE | re.DOTALL) for pattern in FORBIDDEN_SQL), fine
            )

    def test_executed_statements_never_use_an_update_with_a_subquery_from(self) -> None:
        for relative in SQL_MODULES:
            for line, text in string_literals(read(relative)):
                if re.match(r"\s*UPDATE\b", text, re.IGNORECASE):
                    self.assertNotRegex(text, r"(?i)\bFROM\b", "%s:%d" % (relative, line))


class PythonFloorTest(unittest.TestCase):
    def test_every_module_parses_as_python_3_8_and_avoids_newer_names(self) -> None:
        for relative in ALL_MODULES:
            source = read(relative)
            ast.parse(source, feature_version=(3, 8))
            for pattern in FORBIDDEN_PYTHON:
                self.assertIsNone(re.search(pattern, source, re.MULTILINE), "%s matches %s" % (relative, pattern))

    def test_every_module_starts_with_the_future_import(self) -> None:
        for relative in ALL_MODULES:
            self.assertIn("from __future__ import annotations", read(relative), relative)

    def test_modules_do_not_import_sqlite3_at_the_top_unguarded_except_via_try(self) -> None:
        for relative in ("chatd/util.py", "chatd/auth.py", "chatd/maintenance.py"):
            tree = ast.parse(read(relative))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    self.assertNotIn("sqlite3", [alias.name for alias in node.names], relative)
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "sqlite3", relative)

    def test_maintenance_imports_db_modules_only_inside_functions(self) -> None:
        tree = ast.parse(read("chatd/maintenance.py"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level:
                names = {alias.name for alias in node.names}
                self.assertFalse(names & {"db", "db_users", "db_chats", "db_messages", "auth"}, names)


if __name__ == "__main__":
    unittest.main()
