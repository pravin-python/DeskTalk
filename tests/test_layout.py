"""Repository layout vs SPEC section 1 (the ownership table).

The listing is PARSED from ``docs/SPEC.md`` at test time, so the spec stays the single source of truth:

* ``chatd/`` may contain only the ``.py`` files listed in section 1 (the four ``db_*.py`` modules, ``doctor.py`` ...);
* the repository root may hold only the entries section 1 lists plus its "allowed extras" sentence;
* ``web/``, ``docs/`` and ``service/`` may hold only listed files (every file has exactly one owner);
* every listed file must exist - reported as a SEPARATE test that is skipped (with the list of what is still missing)
  until ``DESKTALK_REQUIRE_COMPLETE=1`` switches it on, which is done once every owner has delivered.
"""

from __future__ import annotations

import itertools
import os
import re
import unittest
from typing import List, Optional, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(ROOT, "docs", "SPEC.md")

#: Tool caches that linters recreate in the repository root on every run (they are git-ignored state, not files).
TOLERATED_ROOT = frozenset((".ruff_cache", ".pytest_cache", ".mypy_cache"))
#: Directories that never count (bytecode).
IGNORED_DIRS = frozenset(("__pycache__",))
REQUIRE_COMPLETE = os.environ.get("DESKTALK_REQUIRE_COMPLETE") == "1"

_BRACES = re.compile(r"\{([^{}]*)\}")
_PATH_TOKEN = re.compile(r"^[A-Za-z0-9_./{},-]+$")


def expand_braces(pattern: str) -> List[str]:
    """``js/core/{a,b}.js`` -> ``['js/core/a.js', 'js/core/b.js']`` (nested-free shell brace expansion)."""
    match = _BRACES.search(pattern)
    if match is None:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    return list(itertools.chain.from_iterable(expand_braces(head + part + tail) for part in match.group(1).split(",")))


class Layout:
    """What section 1 of the spec declares."""

    def __init__(self, files: Set[str], top_level: Set[str], allowed_extras: Set[str]) -> None:
        self.files = files  # repo-relative, '/'-separated
        self.top_level = top_level  # names that appear directly under the repository root in the tree
        self.allowed_extras = allowed_extras

    def listed_in(self, directory: str) -> Set[str]:
        prefix = directory + "/"
        return {f[len(prefix) :] for f in self.files if f.startswith(prefix)}


def parse_layout(text: str) -> Layout:
    """Read the tree block and the "allowed extras" sentence of section ``## 1.`` out of the spec text."""
    start = text.index("## 1. Repository layout")
    section = text[start : text.index("\n## 2.", start)]
    block = section.split("```", 2)[1]
    files: Set[str] = set()
    top_level: Set[str] = set()
    current: Optional[str] = None
    for line in block.splitlines()[1:]:  # the first line is the "desktalk/" root
        indent = len(line) - len(line.lstrip(" "))
        tokens = line.split()
        if not tokens or indent not in (2, 4):
            continue  # blank lines and wrapped description lines
        if indent == 2:
            first = tokens[0]
            top_level.add(first.split("/", 1)[0])
            current = first[:-1] if first.endswith("/") else None
            if current is None and _PATH_TOKEN.match(first):
                files.add(first)
        elif current in ("chatd", "web"):
            names = [tokens[0].rstrip(",")]
            if current == "web":  # several paths may share one line: "css/auth.css css/sidebar.css css/panels.css"
                names += [
                    t.rstrip(",")
                    for t in itertools.takewhile(lambda t: "." in t and _PATH_TOKEN.match(t.rstrip(",")), tokens[1:])
                ]
            for name in names:
                if _PATH_TOKEN.match(name):
                    files.update("%s/%s" % (current, path) for path in expand_braces(name))
    sentence = re.search(r"allowed extras: ([^)]*)\)", section)
    extras = set(re.findall(r"`([^`]+)`", sentence.group(1))) if sentence else set()
    return Layout(files, top_level, extras)


def _walk(directory: str) -> List[str]:
    """Every file below ``directory`` as repo-relative '/'-separated paths (bytecode directories skipped)."""
    found: List[str] = []
    for current, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in names:
            if not name.endswith((".pyc", ".pyo")):
                found.append(os.path.relpath(os.path.join(current, name), ROOT).replace(os.sep, "/"))
    return sorted(found)


class LayoutTests(unittest.TestCase):
    layout: Layout

    @classmethod
    def setUpClass(cls) -> None:
        with open(SPEC_PATH, "r", encoding="utf-8") as handle:
            cls.layout = parse_layout(handle.read())

    def test_the_spec_listing_parses(self) -> None:
        listed = self.layout.listed_in("chatd")
        for name in (
            "__init__.py",
            "__main__.py",
            "hub.py",
            "api.py",
            "doctor.py",
            "db.py",
            "db_users.py",
            "db_chats.py",
            "db_messages.py",
            "db_receipts.py",
            "maintenance.py",
            "tlsutil.py",
        ):
            self.assertIn(name, listed, "the parser lost chatd/%s from section 1" % name)
        self.assertGreaterEqual(len(listed), 19)
        self.assertIn("web/js/core/store.js", self.layout.files)
        self.assertIn("web/img/icon-512.png", self.layout.files)
        self.assertIn("web/css/composer.css", self.layout.files)
        self.assertIn("service/install_service.py", self.layout.files)
        self.assertIn("docs/DB_API_chat.md", self.layout.files)
        self.assertTrue({".git", "legacy", "data", "__pycache__"} <= self.layout.allowed_extras)

    def test_chatd_has_no_python_file_missing_from_section_1(self) -> None:
        listed = self.layout.listed_in("chatd")
        unlisted = [
            f for f in _walk(os.path.join(ROOT, "chatd")) if f.endswith(".py") and f[len("chatd/") :] not in listed
        ]
        self.assertEqual(
            unlisted,
            [],
            "chatd/ holds .py files that SPEC section 1 does not list (db modules are only "
            "db.py and db_users/db_chats/db_messages/db_receipts.py)",
        )

    def test_repository_root_holds_only_what_section_1_lists(self) -> None:
        allowed = self.layout.top_level | self.layout.allowed_extras | TOLERATED_ROOT
        extra = sorted(name for name in os.listdir(ROOT) if name not in allowed)
        self.assertEqual(
            extra, [], "top-level entries that SPEC section 1 does not list (move them to legacy/ or extend section 1)"
        )

    def test_owned_directories_hold_only_listed_files(self) -> None:
        problems: List[str] = []
        for directory in ("web", "docs", "service"):
            path = os.path.join(ROOT, directory)
            if os.path.isdir(path):
                problems += [f for f in _walk(path) if f not in self.layout.files]
        self.assertEqual(problems, [], "files that SPEC section 1 assigns to nobody")

    def test_every_listed_file_exists(self) -> None:
        missing = sorted(f for f in self.layout.files if not os.path.isfile(os.path.join(ROOT, *f.split("/"))))
        if missing and not REQUIRE_COMPLETE:
            self.skipTest(
                "%d listed file(s) not written yet (set DESKTALK_REQUIRE_COMPLETE=1 to enforce): %s"
                % (len(missing), ", ".join(missing))
            )
        self.assertEqual(missing, [], "SPEC section 1 lists files that do not exist")


if __name__ == "__main__":
    unittest.main()
