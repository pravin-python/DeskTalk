"""The Python 3.8 / SQLite 3.24 floor of SPEC section 0 (items 1 and 4), enforced statically.

* every ``chatd`` module and ``service/install_service.py`` parses with ``ast.parse(src, feature_version=(3, 8))``
  (that alone rejects ``match``, parenthesised ``with`` and ``except*``) and starts with
  ``from __future__ import annotations`` (``chatd/__init__.py`` excepted);
* an AST walk (so comments, docstrings and strings never cause false positives) looks for every forbidden name of
  section 0: ``str.removeprefix``, ``asyncio.to_thread/timeout/TaskGroup/Runner/Barrier`` (``threading.Barrier`` is
  fine), ``zoneinfo``, ``tomllib``, ``functools.cache`` ..., runtime ``X | Y`` and ``dict | dict``, runtime
  ``list[int]``-style generics, ``zip(strict=)``, ``dataclass(slots=/kw_only=)``, ``asyncio.get_event_loop()`` outside a
  coroutine, ``os.fork``, an unguarded ``add_signal_handler``, ``asyncio`` primitives created at import time, and a
  ``create_task()`` result that is dropped;
* every non-docstring string literal of ``db.py``, ``db_*.py`` and ``maintenance.py`` that looks like SQL is grepped for
  the SQL tokens that need SQLite newer than 3.24 (word-boundary, case-insensitive);
* text files are opened with an explicit ``encoding`` (section 0.3).

Findings are reported as ``path:line: rule: text``.  A line that carries the comment ``# floor-ok: <reason>`` is exempt
(for a use that is guarded in a way the walk cannot see).  Guarded imports (``try: import tomllib except ImportError``,
``if sys.version_info >= ...``) are allowed.  The checkers are themselves tested against small snippets below.
"""

from __future__ import annotations

import ast
import glob
import io
import os
import re
import sys
import tokenize
import unittest
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEATURE_VERSION = (3, 8)

#: Classes whose ``__init__`` may create asyncio primitives because they are only ever constructed inside the running
#: loop (SPEC 6.2: ``Hub`` is built by ``app.py`` inside the loop, ``WebSocket`` by the frame loop, ``_Slot`` by the
#: limiter on first use).  Everything else must create them lazily inside a coroutine (SPEC 0.1).
LOOP_BOUND_CLASSES = frozenset(
    (("chatd/hub.py", "Hub"), ("chatd/websocket.py", "WebSocket"), ("chatd/files.py", "_Slot"))
)

FORBIDDEN_DOTTED = frozenset(
    (
        "asyncio.to_thread",
        "asyncio.timeout",
        "asyncio.TaskGroup",
        "asyncio.Runner",
        "asyncio.Barrier",
        "asyncio.WindowsSelectorEventLoopPolicy",
        "functools.cache",
        "math.lcm",
        "itertools.pairwise",
        "contextlib.aclosing",
        "datetime.UTC",
        "argparse.BooleanOptionalAction",
        "hashlib.file_digest",
        "random.randbytes",
        "typing.TypeAlias",
        "typing.ParamSpec",
        "typing.Annotated",
        "typing.Self",
        "typing.TypeGuard",
        "typing.Concatenate",
        "typing.Never",
        "typing.LiteralString",
        "typing.Required",
        "typing.NotRequired",
        "typing.Unpack",
        "typing.TypeVarTuple",
        "typing.override",
        "os.fork",
        "zoneinfo",
        "tomllib",
    )
)
FORBIDDEN_PREFIXES = ("zoneinfo.", "tomllib.")
#: Method names that exist only on newer Pythons, whatever object they are called on.
FORBIDDEN_METHODS = frozenset(("removeprefix", "removesuffix", "is_relative_to", "with_stem", "bit_count"))
#: Subscripting these at runtime needs Python 3.9 (``list[int]``, ``collections.deque[int]``, ``asyncio.Task[None]``).
RUNTIME_GENERICS = frozenset(
    (
        "list",
        "dict",
        "tuple",
        "set",
        "frozenset",
        "type",
        "collections.deque",
        "collections.defaultdict",
        "collections.OrderedDict",
        "collections.Counter",
        "collections.ChainMap",
        "asyncio.Task",
        "asyncio.Future",
        "asyncio.Queue",
        "re.Pattern",
        "re.Match",
        "queue.Queue",
        "os.PathLike",
        "functools.partial",
    )
)
RUNTIME_GENERIC_PREFIXES = ("collections.abc.",)
BUILTIN_TYPE_NAMES = frozenset(
    (
        "int",
        "str",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "complex",
        "list",
        "dict",
        "tuple",
        "set",
        "frozenset",
        "type",
        "object",
    )
)
ASYNCIO_PRIMITIVES = frozenset(
    ("Lock", "Queue", "Event", "Semaphore", "BoundedSemaphore", "Condition", "PriorityQueue", "LifoQueue")
)
IMPORT_GUARD_EXCEPTIONS = frozenset(("ImportError", "ModuleNotFoundError", "Exception", "BaseException"))

#: SQL features newer than SQLite 3.24 (SPEC 0.4), matched case-insensitively with word boundaries inside SQL strings.
SQL_TOKENS: Dict[str, "re.Pattern[str]"] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in (
        ("RETURNING", r"\bRETURNING\b"),
        ("-> / ->> operators", r"->>?"),
        ("JSON1 function", r"\bjson(?:_[a-z_]+)?\s*\("),
        ("unixepoch()", r"\bunixepoch\s*\("),
        ("STRICT table", r"\bSTRICT\b"),
        ("RIGHT/FULL JOIN", r"\b(?:RIGHT|FULL)\s+(?:OUTER\s+)?JOIN\b"),
        (
            "SQL math function",
            r"\b(?:acos|acosh|asin|asinh|atan|atan2|atanh|ceil|ceiling|cos|cosh|degrees|exp|floor|ln|"
            r"log|log10|log2|mod|pi|pow|power|radians|sin|sinh|sqrt|tan|tanh|trunc)\s*\(",
        ),
        ("window function OVER", r"\bOVER\s*\("),
        (
            "window function",
            r"\b(?:ROW_NUMBER|DENSE_RANK|PERCENT_RANK|CUME_DIST|NTILE|LAG|LEAD|FIRST_VALUE|LAST_VALUE|"
            r"NTH_VALUE|RANK)\s*\(",
        ),
        ("FILTER (WHERE", r"\bFILTER\s*\(\s*WHERE\b"),
        ("NULLS FIRST/LAST", r"\bNULLS\s+(?:FIRST|LAST)\b"),
        ("GENERATED ALWAYS", r"\bGENERATED\s+ALWAYS\b"),
        ("IIF(", r"\bIIF\s*\("),
        ("RENAME COLUMN", r"\bRENAME\s+COLUMN\b"),
        ("DROP COLUMN", r"\bDROP\s+COLUMN\b"),
        ("MATERIALIZED CTE", r"\bMATERIALIZED\b"),
        ("VACUUM INTO", r"\bVACUUM\s+INTO\b"),
    )
}
_SQL_LIKE = re.compile(
    r"\bSELECT\b.+\bFROM\b|\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+\w+|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\s+\w+|"
    r"\bCREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\b|\bALTER\s+TABLE\b|\bDROP\s+(?:TABLE|INDEX)\b|"
    r"\bPRAGMA\s+\w+|\bWITH\s+\w+\s+AS\b|\bEXPLAIN\s+QUERY\b|\bVACUUM\b|\bREPLACE\s+INTO\b",
    re.IGNORECASE | re.DOTALL,
)


class Finding(NamedTuple):
    path: str
    line: int
    rule: str
    text: str

    def __str__(self) -> str:
        return "%s:%d: %s: %s" % (self.path, self.line, self.rule, self.text)


# --------------------------------------------------------------------------------------------------------------
# SQL token grep
# --------------------------------------------------------------------------------------------------------------


def _top_level_keyword(statement: str, keyword: str) -> bool:
    """True when ``keyword`` occurs outside parentheses and quotes (``UPDATE ... SET ... FROM`` has it, a correlated
    ``(SELECT ... FROM ...)`` subquery does not)."""
    depth, quote, word = 0, "", ""
    for char in statement + " ":
        if quote:
            quote = "" if char == quote else quote
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char.isalnum() or char == "_":
            word += char
            continue
        if depth == 0 and word.upper() == keyword:
            return True
        word = ""
    return False


def sql_problems(text: str) -> List[str]:
    """The forbidden SQLite features in ``text`` (empty for a string that is not SQL at all)."""
    if not _SQL_LIKE.search(text):
        return []
    problems = [name for name, pattern in SQL_TOKENS.items() if pattern.search(text)]
    for statement in text.split(";"):
        if re.match(r"\s*UPDATE\b", statement, re.IGNORECASE) and _top_level_keyword(statement, "FROM"):
            problems.append("UPDATE ... FROM")
    return problems


# --------------------------------------------------------------------------------------------------------------
# The AST walk
# --------------------------------------------------------------------------------------------------------------


def dotted(node: ast.AST) -> Optional[str]:
    """``a.b.c`` for a ``Name``/``Attribute`` chain, else ``None``."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _annotation_ids(tree: ast.AST) -> Set[int]:
    """ids of every node inside an annotation (never evaluated with ``from __future__ import annotations``)."""
    ids: Set[int] = set()

    def mark(node: Optional[ast.AST]) -> None:
        if node is not None:
            ids.update(id(n) for n in ast.walk(node))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            for arg in (
                arguments.posonlyargs + arguments.args + arguments.kwonlyargs + [arguments.vararg, arguments.kwarg]
            ):
                if arg is not None:
                    mark(arg.annotation)
            mark(node.returns)
        elif isinstance(node, ast.AnnAssign):
            mark(node.annotation)
    return ids


def _docstring_ids(tree: ast.AST) -> Set[int]:
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


class _Walker(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.AST, lines: List[str]) -> None:
        self.path = path
        self.lines = lines
        self.findings: List[Finding] = []
        self.ancestors: List[ast.AST] = []
        self.aliases: Dict[str, str] = {}
        self.annotation_ids = _annotation_ids(tree)
        self.has_future = any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
            for n in ast.walk(tree)
        )
        for node in ast.walk(tree):  # aliases are collected first: a use may precede the import textually (rare)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.aliases[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    self.aliases[alias.asname or alias.name] = "%s.%s" % (node.module, alias.name)

    # ---- helpers -----------------------------------------------------------------------------------------------

    def report(self, node: ast.AST, rule: str, text: str) -> None:
        line = getattr(node, "lineno", 0)
        if 0 < line <= len(self.lines) and "floor-ok" in self.lines[line - 1]:
            return
        self.findings.append(Finding(self.path, line, rule, text))

    def resolve(self, node: ast.AST) -> Optional[str]:
        """The fully qualified dotted name of a ``Name``/``Attribute`` chain, following ``import ... as`` aliases."""
        name = dotted(node)
        if name is None:
            return None
        head, _, rest = name.partition(".")
        target = self.aliases.get(head, head)
        return target + ("." + rest if rest else "")

    def in_annotation(self, node: ast.AST) -> bool:
        return id(node) in self.annotation_ids

    def nearest(self, *kinds: type) -> Optional[ast.AST]:
        for ancestor in reversed(self.ancestors[:-1]):
            if isinstance(ancestor, kinds):
                return ancestor
        return None

    def guarded_by(self, node: ast.AST, catching: Set[str]) -> bool:
        """``node`` is in the ``try`` body of a ``try`` whose handlers catch one of ``catching`` (or in a
        ``sys.version_info`` branch)."""
        line = getattr(node, "lineno", 0)
        for ancestor in self.ancestors[:-1]:
            if (
                isinstance(ancestor, ast.Try)
                and ancestor.body
                and ancestor.body[0].lineno <= line <= (ancestor.body[-1].end_lineno or line)
            ):
                for handler in ancestor.handlers:
                    names = (
                        {
                            dotted(n)
                            for n in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type])
                        }
                        if handler.type is not None
                        else {"BaseException"}
                    )
                    if names & catching:
                        return True
            if isinstance(ancestor, ast.If) and any(
                isinstance(n, ast.Attribute) and n.attr == "version_info" for n in ast.walk(ancestor.test)
            ):
                return True
        return False

    # ---- traversal ---------------------------------------------------------------------------------------------

    def visit(self, node: ast.AST) -> None:
        self.ancestors.append(node)
        super().visit(node)
        self.ancestors.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if self._forbidden_dotted(alias.name) and not self.guarded_by(node, IMPORT_GUARD_EXCEPTIONS):
                self.report(node, "forbidden-import", "import %s needs a newer Python than 3.8" % alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module or node.module == "__future__":
            return
        for alias in node.names:
            full = "%s.%s" % (node.module, alias.name)
            if (self._forbidden_dotted(full) or self._forbidden_dotted(node.module)) and not self.guarded_by(
                node, IMPORT_GUARD_EXCEPTIONS
            ):
                self.report(
                    node,
                    "forbidden-import",
                    "from %s import %s needs a newer Python than 3.8" % (node.module, alias.name),
                )

    @staticmethod
    def _forbidden_dotted(name: str) -> bool:
        return name in FORBIDDEN_DOTTED or name.startswith(FORBIDDEN_PREFIXES)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = self.resolve(node)
        if name is not None and self._forbidden_dotted(name):
            self.report(node, "forbidden-api", "%s needs a newer Python than 3.8" % name)
        elif node.attr in FORBIDDEN_METHODS:
            self.report(node, "forbidden-api", ".%s() needs a newer Python than 3.8" % node.attr)
        elif node.attr == "readlink" and name is not None and not name.startswith("os."):
            self.report(node, "forbidden-api", "Path.readlink() needs Python 3.9 (os.readlink is fine)")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func_name = self.resolve(node.func) or ""
        short = func_name.rsplit(".", 1)[-1]
        keywords = {k.arg for k in node.keywords if k.arg}
        if short in ("aiter", "anext") and isinstance(node.func, ast.Name):
            self.report(node, "forbidden-api", "%s() needs Python 3.10" % short)
        elif short == "zip" and "strict" in keywords:
            self.report(node, "forbidden-api", "zip(strict=) needs Python 3.10")
        elif short == "dataclass" and keywords & {"slots", "kw_only"}:
            self.report(node, "forbidden-api", "dataclass(slots=/kw_only=) needs Python 3.10")
        elif short == "basicConfig" and "encoding" in keywords:
            self.report(node, "forbidden-api", "logging.basicConfig(encoding=) needs Python 3.9")
        elif short == "shutdown" and "cancel_futures" in keywords:
            self.report(node, "forbidden-api", "Executor.shutdown(cancel_futures=) needs Python 3.9")
        elif short == "get_event_loop" and not isinstance(
            self.nearest(ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda), ast.AsyncFunctionDef
        ):
            self.report(node, "get-event-loop", "get_event_loop() outside a coroutine; use asyncio.get_running_loop()")
        elif short == "add_signal_handler" and not self.guarded_by(node, {"NotImplementedError"}):
            self.report(
                node,
                "signal-handler",
                "loop.add_signal_handler raises NotImplementedError on Windows: wrap it in "
                "try/except and fall back to signal.signal",
            )
        elif func_name.startswith("asyncio.") and short in ASYNCIO_PRIMITIVES:
            self._check_primitive(node, func_name)
        elif short == "open" and isinstance(node.func, ast.Name):
            self._check_open(node, keywords)
        elif short in ("read_text", "write_text") and isinstance(node.func, ast.Attribute):
            encoding_at = 0 if short == "read_text" else 1
            if "encoding" not in keywords and len(node.args) <= encoding_at:
                self.report(
                    node, "encoding", "%s() without encoding= (the default is the locale code page on Windows)" % short
                )
        self.generic_visit(node)

    def _check_primitive(self, node: ast.Call, name: str) -> None:
        scope = self.nearest(ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        if scope is None or isinstance(scope, ast.ClassDef):
            self.report(
                node, "loop-bound-primitive", "%s() created at import time binds to the wrong loop on 3.8/3.9" % name
            )
            return
        if isinstance(scope, ast.FunctionDef) and scope.name == "__init__":
            owner = self.nearest(ast.ClassDef)
            if owner is None or (self.path, owner.name) not in LOOP_BOUND_CLASSES:  # type: ignore[attr-defined]
                self.report(
                    node,
                    "loop-bound-primitive",
                    "%s() in __init__: create it lazily inside the running loop (or list the class in "
                    "LOOP_BOUND_CLASSES when it is only constructed inside the loop)" % name,
                )

    def _check_open(self, node: ast.Call, keywords: Set[Optional[str]]) -> None:
        mode: Optional[ast.AST] = (
            node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
        )
        binary = isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value
        if not binary and "encoding" not in keywords and len(node.args) < 4:
            self.report(node, "encoding", "open() in text mode without encoding=")

    def visit_Expr(self, node: ast.Expr) -> None:
        value = node.value
        if isinstance(value, ast.Call) and (self.resolve(value.func) or "").rsplit(".", 1)[-1] in (
            "create_task",
            "ensure_future",
        ):
            self.report(
                node,
                "discarded-task",
                "the result of create_task()/ensure_future() is dropped: keep it in a set "
                "until done (the loop holds only a weak reference)",
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if (
            isinstance(node.value, ast.Call)
            and (self.resolve(node.value.func) or "").rsplit(".", 1)[-1] in ("create_task", "ensure_future")
            and all(isinstance(t, ast.Name) and t.id == "_" for t in node.targets)
        ):
            self.report(node, "discarded-task", "create_task() assigned to _ drops the task")
        self.generic_visit(node)

    @staticmethod
    def _is_dictish(node: ast.AST) -> bool:
        return isinstance(node, (ast.Dict, ast.DictComp)) or (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"
        )

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.BitOr):
            if self._is_dictish(node.left) or self._is_dictish(node.right):
                self.report(node, "dict-merge", "dict | dict needs Python 3.9; use {**a, **b}")
            elif self.in_annotation(node):
                if not self.has_future:
                    self.report(node, "runtime-union", "X | Y annotation without `from __future__ import annotations`")
            elif any(
                (isinstance(n, ast.Constant) and n.value is None)
                or (isinstance(n, ast.Name) and n.id in BUILTIN_TYPE_NAMES)
                for n in (node.left, node.right)
            ):
                self.report(node, "runtime-union", "runtime `X | Y` of types needs Python 3.10 (use Optional/Union)")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.op, ast.BitOr) and self._is_dictish(node.value):
            self.report(node, "dict-merge", "dict |= dict needs Python 3.9; use .update()")
        self.generic_visit(node)

    def _visit_definition(self, node: ast.AST) -> None:
        """Decorators: Python 3.8 accepts only ``dotted.name`` or ``dotted.name(args)`` (PEP 614 is 3.9)."""
        for decorator in node.decorator_list:  # type: ignore[attr-defined]
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if dotted(target) is None:
                self.report(decorator, "relaxed-decorator", "this decorator expression needs Python 3.9 (PEP 614)")
        self.generic_visit(node)

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_definition

    def _next_char(self, lineno: int, col: int) -> str:
        """The first character after ``(lineno, col)`` (a UTF-8 byte offset) that is not blank, a comma or a comment."""
        while lineno <= len(self.lines):
            text = self.lines[lineno - 1].encode("utf-8")[col:].decode("utf-8", "ignore")
            for char in text:
                if char == "#":
                    break
                if char not in " \t,\\":
                    return char
            lineno, col = lineno + 1, 0
        return ""

    def visit_With(self, node: ast.With) -> None:
        """``with (a as x, b as y):`` is PEP 617/Python 3.9; the parser only refuses it on some versions."""
        last = node.items[-1]
        if len(node.items) > 1 or last.optional_vars is not None:
            tail = last.optional_vars if last.optional_vars is not None else last.context_expr
            if self._next_char(tail.end_lineno or 0, tail.end_col_offset or 0) == ")":
                self.report(
                    node, "parenthesized-with", "`with (a as x, b as y):` needs Python 3.9; use a backslash or nest"
                )
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_Subscript(self, node: ast.Subscript) -> None:
        subscript = node.slice
        if isinstance(subscript, ast.Tuple) and any(isinstance(e, ast.Starred) for e in subscript.elts):
            first_line = self.lines[subscript.lineno - 1].encode("utf-8")
            if first_line[subscript.col_offset : subscript.col_offset + 1] != b"(":  # a[(*b,)] is fine on 3.8
                self.report(node, "star-index", "a[*b] needs Python 3.11")
        name = self.resolve(node.value)
        if (
            name is not None
            and not self.in_annotation(node)
            and (name in RUNTIME_GENERICS or name.startswith(RUNTIME_GENERIC_PREFIXES))
        ):
            self.report(
                node, "runtime-generic", "%s[...] evaluated at runtime needs Python 3.9 (quote it or use typing)" % name
            )
        self.generic_visit(node)


def fstring_problems(source: str) -> List[Tuple[int, str]]:
    """What PEP 701 (3.12) allows inside an f-string field and 3.8-3.11 reject: the enclosing quote character,
    backslashes and comments.  Python 3.12+ ``ast.parse(feature_version=(3, 8))`` accepts all of it; on older
    interpreters the parse itself fails, so there is nothing to find here."""
    if sys.version_info < (3, 12):
        return []
    problems: List[Tuple[int, str]] = []
    quotes: List[str] = []  # quote character of every open f-string
    depth: List[int] = []  # open braces of the current replacement field of every open f-string (0 = literal text)
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            line = token.start[0]
            if token.type == tokenize.FSTRING_START:  # type: ignore[attr-defined]
                quote = token.string.lstrip("fFrR")[0]
                if depth and depth[-1] > 0 and quote == quotes[-1]:
                    problems.append((line, "an f-string field reuses the enclosing quote character (3.12 only)"))
                quotes.append(quote)
                depth.append(0)
            elif token.type == tokenize.FSTRING_END:  # type: ignore[attr-defined]
                quotes.pop()
                depth.pop()
            elif depth and token.type == tokenize.OP and token.string in "{}":
                depth[-1] += 1 if token.string == "{" else -1
            elif depth and depth[-1] > 0:
                if token.type == tokenize.STRING and token.string.lstrip("bBrRuU")[0] == quotes[-1]:
                    problems.append((line, "an f-string field reuses the enclosing quote character (3.12 only)"))
                elif token.type == tokenize.STRING and "\\" in token.string:
                    problems.append((line, "a backslash inside an f-string field (3.12 only)"))
                elif token.type == tokenize.COMMENT:
                    problems.append((line, "a comment inside an f-string field (3.12 only)"))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return problems
    return problems


def check_source(path: str, source: str) -> List[Finding]:
    """Every floor finding of one Python source (``path`` is only used in the messages and the allow-lists)."""
    try:
        tree = ast.parse(source, filename=path, feature_version=FEATURE_VERSION)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "syntax", "not valid Python 3.8: %s" % exc.msg)]
    walker = _Walker(path, tree, source.splitlines())
    walker.findings += [Finding(path, line, "fstring-nesting", text) for line, text in fstring_problems(source)]
    if not walker.has_future and os.path.basename(path) != "__init__.py":
        walker.findings.append(
            Finding(path, 1, "future-import", "the module does not start with `from __future__ import annotations`")
        )
    walker.visit(tree)
    return walker.findings


def check_sql(path: str, source: str) -> List[Finding]:
    """Forbidden SQL tokens in the (non-docstring) string literals of one module."""
    tree = ast.parse(source, filename=path, feature_version=FEATURE_VERSION)
    skip = _docstring_ids(tree)
    findings: List[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            for problem in sql_problems(node.value):
                findings.append(
                    Finding(
                        path,
                        node.lineno,
                        "sql-token",
                        "%s is newer than SQLite 3.24: %r" % (problem, node.value.strip()[:70]),
                    )
                )
    return findings


# --------------------------------------------------------------------------------------------------------------
# The repository scan
# --------------------------------------------------------------------------------------------------------------


def _relative(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def server_modules() -> List[str]:
    """Absolute paths of every ``chatd/*.py`` plus ``service/install_service.py``."""
    paths = sorted(glob.glob(os.path.join(ROOT, "chatd", "*.py")))
    installer = os.path.join(ROOT, "service", "install_service.py")
    return paths + ([installer] if os.path.exists(installer) else [])


def sql_modules() -> List[str]:
    """``db.py``, ``db_*.py`` and ``maintenance.py`` (SPEC 0.4)."""
    names = sorted(glob.glob(os.path.join(ROOT, "chatd", "db*.py")))
    return names + [p for p in (os.path.join(ROOT, "chatd", "maintenance.py"),) if os.path.exists(p)]


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class RepositoryFloorTests(unittest.TestCase):
    def _assert_clean(self, findings: List[Finding], what: str) -> None:
        self.assertEqual(findings, [], "%s:\n  %s" % (what, "\n  ".join(str(f) for f in findings)))

    def test_there_are_modules_to_check(self) -> None:
        names = {os.path.basename(p) for p in server_modules()}
        self.assertTrue({"app.py", "http.py", "websocket.py", "db.py"} <= names, names)

    def test_every_module_is_python38_and_has_no_forbidden_name(self) -> None:
        findings: List[Finding] = []
        for path in server_modules():
            findings += check_source(_relative(path), _read(path))
        self._assert_clean(findings, "constructs newer than Python 3.8 (SPEC 0.1)")

    def test_sql_strings_stay_within_sqlite_3_24(self) -> None:
        findings: List[Finding] = []
        for path in sql_modules():
            findings += check_sql(_relative(path), _read(path))
        self._assert_clean(findings, "SQL newer than SQLite 3.24 (SPEC 0.4)")

    def test_test_files_parse_as_python38(self) -> None:
        findings: List[Finding] = []
        for path in sorted(glob.glob(os.path.join(ROOT, "tests", "*.py"))):
            try:
                ast.parse(_read(path), filename=path, feature_version=FEATURE_VERSION)
            except SyntaxError as exc:
                findings.append(
                    Finding(_relative(path), exc.lineno or 0, "syntax", "not valid Python 3.8: %s" % exc.msg)
                )
        self._assert_clean(
            findings, "test modules must stay 3.8-parseable (they run on 3.11-3.13 but share the code style)"
        )


# --------------------------------------------------------------------------------------------------------------
# Tests of the checkers themselves
# --------------------------------------------------------------------------------------------------------------

HEADER = "from __future__ import annotations\nimport asyncio\nimport threading\n"


def rules(source: str, path: str = "chatd/x.py", header: bool = True) -> List[str]:
    return sorted(f.rule for f in check_source(path, (HEADER if header else "") + source))


class CheckerBehaviourTests(unittest.TestCase):
    def test_clean_code_passes(self) -> None:
        clean = (
            "import typing\n"
            "async def f(lock: asyncio.Lock | None = None) -> list[int]:\n"
            "    loop = asyncio.get_running_loop()\n"
            "    barrier = threading.Barrier(2)\n"
            "    ev = asyncio.Event()\n"
            "    tasks = set()\n"
            "    t = loop.create_task(g())\n"
            "    tasks.add(t)\n"
            "    x: dict[str, int] = {}\n"
            "    return [1, 2]\n"
            "def g():\n"
            "    s = 'text'.strip()\n"
            "    with open('f', 'rb') as a, open('g', 'w', encoding='utf-8') as b:\n"
            "        pass\n"
            "    return {**{}, **{}}, (1 | 2), s\n"
        )
        self.assertEqual(rules(clean), [])

    def test_syntax_that_needs_a_newer_python(self) -> None:
        self.assertEqual(rules("match x:\n    case 1:\n        pass\n"), ["syntax"])
        self.assertEqual(rules("try:\n    pass\nexcept* ValueError:\n    pass\n"), ["syntax"])
        # the parser refuses these only on some interpreters (3.12 the first, 3.12+ never the f-string): the walker
        # catches them on every interpreter the suites run on
        parenthesised = (
            "with (a as x, b as y):\n    pass\n",
            "with (\n    a as x,\n    b as y,\n):\n    pass\n",
            "with (a, b):\n    pass\n",
            "with (a as x):\n    pass\n",
        )
        for source in parenthesised:
            with self.subTest(source):
                self.assertTrue({"syntax", "parenthesized-with"} & set(rules(source)), source)
        for source in (
            "with (a) as x:\n    pass\n",
            "with a as x, b as y:\n    pass\n",
            "with foo(a, (b)) as x, bar() as y:  # (comment)\n    pass\n",
        ):
            with self.subTest(source):
                self.assertEqual(rules(source), [], source)
        self.assertEqual(rules("@a[0]\ndef f():\n    pass\n"), ["relaxed-decorator"])
        self.assertEqual(rules("@(a or b)\nclass C:\n    pass\n"), ["relaxed-decorator"])
        self.assertEqual(rules("@a.b.c(1, x=2)\n@d\ndef f():\n    pass\n"), [])
        self.assertEqual(rules("x = a[*b]\n"), ["star-index"])
        self.assertEqual(rules("x = a[1, *b]\ny = a[1:2, 3]\nz = a[(*b,)]\n"), ["star-index"])
        for source in ('x = f"{a["b"]}"\n', "x = f\"{'\\n'.join(a)}\"\n", 'x = f"{a  # note\n}"\n'):
            with self.subTest(source):
                self.assertTrue({"syntax", "fstring-nesting"} & set(rules(source)), source)
        for source in ("x = f\"{a['b']} {{literal}} {c=} {d:>{w}}\"\n", "x = f'{a[\"b\"]}'\n", "x = f\"{f'{a}'}\"\n"):
            with self.subTest(source):
                self.assertEqual(rules(source), [], source)

    def test_missing_future_import(self) -> None:
        self.assertEqual(rules("x = 1\n", header=False), ["future-import"])
        self.assertEqual(rules("x = 1\n", path="chatd/__init__.py", header=False), [])

    def test_forbidden_api_names(self) -> None:
        cases = {
            "'abc'.removeprefix('a')": "forbidden-api",
            "x.removesuffix('a')": "forbidden-api",
            "asyncio.to_thread(f)": "forbidden-api",
            "asyncio.timeout(3)": "forbidden-api",
            "asyncio.TaskGroup()": "forbidden-api",
            "asyncio.Runner()": "forbidden-api",
            "asyncio.Barrier(2)": "forbidden-api",
            "p.is_relative_to(q)": "forbidden-api",
            "p.with_stem('a')": "forbidden-api",
            "p.readlink()": "forbidden-api",
            "(5).bit_count()": "forbidden-api",
            "aiter(x)": "forbidden-api",
            "anext(x)": "forbidden-api",
            "zip(a, b, strict=True)": "forbidden-api",
            "logging.basicConfig(encoding='utf-8')": "forbidden-api",
            "os.fork()": "forbidden-api",
            "asyncio.WindowsSelectorEventLoopPolicy()": "forbidden-api",
        }
        for snippet, rule in cases.items():
            with self.subTest(snippet):
                self.assertIn(rule, rules("import logging\nimport os\n" + snippet + "\n"), snippet)
        for allowed in (
            "os.readlink(p)",
            "threading.Barrier(2)",
            "asyncio.get_running_loop()",
            "asyncio.wait_for(f, 1)",
        ):
            with self.subTest(allowed):
                self.assertEqual(rules("import os\n" + allowed + "\n"), [])

    def test_forbidden_imports_and_aliases(self) -> None:
        for source in (
            "import zoneinfo",
            "import tomllib",
            "from asyncio import timeout",
            "from typing import TypeAlias",
            "from functools import cache",
            "from math import lcm",
            "from itertools import pairwise",
            "from contextlib import aclosing",
            "from datetime import UTC",
            "from hashlib import file_digest",
            "from random import randbytes",
            "from argparse import BooleanOptionalAction",
            "from zoneinfo import ZoneInfo",
            "import asyncio as aio\naio.timeout(1)",
        ):
            with self.subTest(source):
                self.assertIn(
                    "forbidden-import"
                    if "import" in source.split("\n")[0] and "aio" not in source
                    else "forbidden-api",
                    rules(source + "\n"),
                )
        guarded = "try:\n    import tomllib\nexcept ImportError:\n    tomllib = None\n"
        self.assertEqual(rules(guarded), [])
        self.assertEqual(rules("import sys\nif sys.version_info >= (3, 11):\n    import tomllib\n"), [])
        self.assertEqual(rules("import zoneinfo  # floor-ok: optional dependency probed elsewhere\n"), [])

    def test_runtime_unions_generics_and_dict_merge(self) -> None:
        self.assertEqual(rules("x = isinstance(1, int | None)\n"), ["runtime-union"])
        self.assertEqual(rules("x = {} | {'a': 1}\n"), ["dict-merge"])
        self.assertEqual(rules("a = {}\na |= {'b': 1}\n"), ["dict-merge"])
        self.assertEqual(rules("x = list[int]()\n"), ["runtime-generic"])
        self.assertEqual(rules("x = dict[str, int]\n"), ["runtime-generic"])
        self.assertEqual(
            rules("import collections.abc\nx = collections.abc.Callable[[int], str]\n"), ["runtime-generic"]
        )
        self.assertEqual(rules("def f(a: int | None, b: list[int]) -> dict[str, int | None]:\n    return {}\n"), [])
        self.assertEqual(rules("def f(a: int | None):\n    pass\n", header=False), ["future-import", "runtime-union"])
        self.assertEqual(rules("x = [1 | 2, 3 | 4]\nflags = a | b\n"), [])

    def test_event_loop_rules(self) -> None:
        self.assertEqual(rules("def f():\n    return asyncio.get_event_loop()\n"), ["get-event-loop"])
        self.assertEqual(rules("async def f():\n    return asyncio.get_event_loop()\n"), [])
        self.assertEqual(rules("lock = asyncio.Lock()\n"), ["loop-bound-primitive"])
        self.assertEqual(rules("class A:\n    q = asyncio.Queue()\n"), ["loop-bound-primitive"])
        self.assertEqual(
            rules("class A:\n    def __init__(self):\n        self.e = asyncio.Event()\n"), ["loop-bound-primitive"]
        )
        self.assertEqual(
            rules(
                "class WebSocket:\n    def __init__(self):\n        self.e = asyncio.Event()\n",
                path="chatd/websocket.py",
            ),
            [],
        )
        self.assertEqual(rules("class A:\n    async def start(self):\n        self.e = asyncio.Event()\n"), [])
        self.assertEqual(rules("class A:\n    def __init__(self):\n        self.e = threading.Event()\n"), [])
        self.assertEqual(rules("def f(loop):\n    loop.add_signal_handler(2, g)\n"), ["signal-handler"])
        guarded = (
            "def f(loop):\n    try:\n        loop.add_signal_handler(2, g)\n"
            "    except (NotImplementedError, ValueError):\n        pass\n"
        )
        self.assertEqual(rules(guarded), [])

    def test_dropped_tasks(self) -> None:
        self.assertEqual(rules("def f(loop):\n    loop.create_task(g())\n"), ["discarded-task"])
        self.assertEqual(rules("def f():\n    asyncio.ensure_future(g())\n"), ["discarded-task"])
        self.assertEqual(rules("def f(loop):\n    _ = loop.create_task(g())\n"), ["discarded-task"])
        self.assertEqual(rules("def f(loop, s):\n    t = loop.create_task(g())\n    s.add(t)\n"), [])
        self.assertEqual(rules("def f(loop):\n    return loop.create_task(g())\n"), [])

    def test_dataclass_options_and_encoding(self) -> None:
        self.assertEqual(
            rules("import dataclasses\n@dataclasses.dataclass(slots=True)\nclass A:\n    x: int = 1\n"),
            ["forbidden-api"],
        )
        self.assertEqual(
            rules("from dataclasses import dataclass\n@dataclass(frozen=True)\nclass A:\n    x: int = 1\n"), []
        )
        self.assertEqual(rules("f = open('x')\n"), ["encoding"])
        self.assertEqual(rules("f = open('x', 'w')\n"), ["encoding"])
        self.assertEqual(rules("f = open('x', 'rb')\ng = open('x', encoding='utf-8')\nh = open('x', mode='ab')\n"), [])
        self.assertEqual(rules("p.read_text()\n"), ["encoding"])
        self.assertEqual(rules("p.write_text('x')\n"), ["encoding"])
        self.assertEqual(rules("p.read_text(encoding='utf-8')\np.write_text('x', encoding='utf-8')\n"), [])

    def test_strings_and_comments_never_trigger_the_name_checks(self) -> None:
        source = (
            '"""asyncio.timeout and removeprefix and match."""\n'
            "# asyncio.to_thread zoneinfo\n"
            'x = "asyncio.TaskGroup list[int]"\n'
        )
        self.assertEqual(rules(source), [])

    def test_sql_token_grep(self) -> None:
        bad = {
            "INSERT INTO t(a) VALUES (1) RETURNING id": "RETURNING",
            "SELECT data->>'a' FROM t": "-> / ->> operators",
            "SELECT json_extract(body, '$.a') FROM t": "JSON1 function",
            "SELECT unixepoch() FROM t": "unixepoch()",
            "CREATE TABLE t(a INTEGER) STRICT": "STRICT table",
            "SELECT * FROM a RIGHT JOIN b ON 1": "RIGHT/FULL JOIN",
            "select * from a full outer join b on 1": "RIGHT/FULL JOIN",
            "SELECT floor(x) FROM t": "SQL math function",
            "SELECT ROW_NUMBER() OVER (ORDER BY id) FROM t": "window function",
            "SELECT count(*) FILTER (WHERE a) FROM t": "FILTER (WHERE",
            "SELECT a FROM t ORDER BY a NULLS LAST": "NULLS FIRST/LAST",
            "CREATE TABLE t(a INTEGER GENERATED ALWAYS AS (1))": "GENERATED ALWAYS",
            "SELECT IIF(a, 1, 2) FROM t": "IIF(",
            "UPDATE t SET a = b.a FROM b WHERE t.id = b.id": "UPDATE ... FROM",
            "ALTER TABLE t RENAME COLUMN a TO b": "RENAME COLUMN",
            "ALTER TABLE t DROP COLUMN a": "DROP COLUMN",
            "VACUUM INTO 'x.db'": "VACUUM INTO",
            "WITH c AS MATERIALIZED (SELECT 1) SELECT * FROM c": "MATERIALIZED CTE",
        }
        for sql, expected in bad.items():
            with self.subTest(sql):
                self.assertIn(expected, sql_problems(sql), sql)
        good = (
            "UPDATE t SET a = (SELECT b.a FROM b WHERE b.id = t.id) WHERE id IN (SELECT id FROM c)",
            "INSERT INTO t(a) VALUES (?) ON CONFLICT(a) DO UPDATE SET b = excluded.b",
            "SELECT a FROM t WHERE fold(body) LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 5",
            "CREATE TABLE audit_log(id INTEGER PRIMARY KEY, ts REAL)",
            "PRAGMA wal_checkpoint(TRUNCATE)",
            "SELECT COUNT(*) FROM (SELECT 1 FROM t LIMIT 1000)",
            "the log(arguments) of a message -> are plain prose",
            "RETURNING is mentioned in a plain sentence",
        )
        for sql in good:
            with self.subTest(sql):
                self.assertEqual(sql_problems(sql), [], sql)

    def test_sql_check_skips_docstrings(self) -> None:
        source = (
            '"""SELECT RETURNING FROM docs."""\n'
            "def f():\n"
            '    """INSERT INTO t VALUES (1) RETURNING id (explained)"""\n'
            '    return "SELECT a FROM t WHERE a = ?"\n'
            "def g():\n"
            '    return "DELETE FROM t WHERE id = 1 RETURNING id"\n'
        )
        findings = check_sql("chatd/db_x.py", source)
        self.assertEqual([(f.line, f.rule) for f in findings], [(6, "sql-token")])


if __name__ == "__main__":
    unittest.main()
