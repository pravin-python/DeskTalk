"""Static lint of the web client (SPEC section 12 "Static UI lint", section 9.1 safety rule, 5.4 CSP, 9.5, 9.8).

Runs over whatever exists under ``web/`` and reports exact ``file:line`` findings, so it can be used while the client is
still being written.  The JavaScript is not grepped as plain text: a small lexer (:func:`lex`) first separates code from
comments, string and template literals and regex literals, so a comment that says "never use innerHTML" or a blocklist
string cannot cause a false positive, and a sink hidden after a long line cannot hide either.

Checks (each is one test; every finding is ``path:line: rule: text``):

* all static and literal dynamic ``import`` specifiers resolve to an existing file (exact letter case, ``.js``);
  a dynamic ``import(x)`` with a non-literal specifier is itself a finding (the lint cannot follow it);
* none of the DOM sinks of section 9.1 and none of the string-to-code sinks of section 5.4 is used
  (``innerHTML``, ``outerHTML``, ``insertAdjacentHTML``, ``document.write``, ``eval``, ``new Function``,
  ``setTimeout("...")``, ``setAttribute('style'|'on...'|'srcdoc')``, ``DOMParser``, ``createContextualFragment``,
  ``srcdoc``, ``javascript:`` URLs, ``CSSStyleSheet``/``adoptedStyleSheets``/``insertRule``,
  ``createElement('style'|'script'|...)``, ``document.cookie`` ...);
* no external URL anywhere (JS strings, HTML, CSS, manifest), no web fonts, no inline ``<script>``/``<style>``/
  ``style=``/``on...=`` in ``index.html``, every local reference of ``index.html``/CSS/manifest exists, and
  ``index.html`` carries the metas and scripts of sections 9.5 and 9.8;
* the UI never touches the session cookie or stores tokens in web storage, ``icons.js`` builds SVG with
  ``createElementNS`` only;
* every ``....request('x.y'`` / ``socket.send('x')`` type is a request of ``chatd/hub.py`` (when it exists; else of the
  section 7.4 list), every ``'ev.*'`` literal is an event of section 7.3, and every section 7.3 event is handled;
* ``requestAnimationFrame`` is absent from the receipt/notification paths (section 8.2, 9.4, 9.6 rule 8);
* no regex lookbehind and no ``v``-flag regex literal (section 9.8, Safari < 16.4 cannot parse them).
"""

from __future__ import annotations

import bisect
import json
import os
import re
import unittest
from html.parser import HTMLParser
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
SPEC_PATH = os.path.join(ROOT, "docs", "SPEC.md")
HUB_PATH = os.path.join(ROOT, "chatd", "hub.py")

#: XML namespace identifiers that look like URLs but are never fetched.
NAMESPACE_URIS = (
    "www.w3.org/2000/svg",
    "www.w3.org/1999/xlink",
    "www.w3.org/1999/xhtml",
    "www.w3.org/XML/1998/namespace",
    "www.w3.org/1998/Math/MathML",
)
#: Files in which ``requestAnimationFrame`` is forbidden outright: the socket handler, the store and the notification
#: engine run synchronously (SPEC 9.6 rule 8, 9.4).
NO_RAF_FILES = frozenset(("js/core/notify.js", "js/core/sound.js", "js/core/socket.js", "js/core/store.js"))
#: Elsewhere (views) it may defer DOM patching, but not inside a function whose name says it handles receipts or alerts.
RECEIPT_FUNCTION = re.compile(r"receipt|deliver|notif|chime|badge|favicon|sound|markread|readsync", re.IGNORECASE)


class Finding(NamedTuple):
    path: str
    line: int
    rule: str
    text: str

    def __str__(self) -> str:
        return "%s:%d: %s: %s" % (self.path, self.line, self.rule, self.text)


# --------------------------------------------------------------------------------------------------------------
# A JavaScript lexer that is just good enough to tell code from comments, strings, templates and regex literals
# --------------------------------------------------------------------------------------------------------------


class Token(NamedTuple):
    start: int  # offset of the opening delimiter: a quote, a backtick, or the "}" that resumes a template
    text: str  # the raw content between the delimiters, escapes untouched


class Regex(NamedTuple):
    start: int
    body: str
    flags: str


class Lexed:
    """``code`` has the same length and line structure as the source, with comments, string/template text and regex
    bodies replaced by spaces (quotes, slashes and flags stay); ``strings`` are the string literals and template text
    chunks; ``regexes`` the regex literals."""

    def __init__(self, source: str, code: str, strings: List[Token], regexes: List[Regex]) -> None:
        self.source = source
        self.code = code
        self.strings = strings
        self.regexes = regexes
        self._line_starts = [0] + [m.end() for m in re.finditer("\n", source)]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self._line_starts, offset)

    def before(self, offset: int, width: int = 160) -> str:
        """The code in front of ``offset`` with trailing whitespace removed (to recognise ``foo(`` before a string)."""
        return self.code[max(0, offset - width) : offset].rstrip()


_REGEX_AFTER_WORDS = frozenset(
    (
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "yield",
        "await",
    )
)
_REGEX_AFTER_CHARS = frozenset("(,=:[!&|?{;+-*%<>~^")


class _Lexer:
    def __init__(self, source: str) -> None:
        self.src = source
        self.n = len(source)
        self.out = list(source)
        self.strings: List[Token] = []
        self.regexes: List[Regex] = []
        self.stack: List[str] = []  # "blk" for "{", "tpl" for "${" inside a template literal

    def blank(self, start: int, end: int) -> None:
        for k in range(start, min(end, self.n)):
            if self.out[k] not in "\r\n":
                self.out[k] = " "

    def string_end(self, i: int) -> int:
        quote, j = self.src[i], i + 1
        while j < self.n:
            ch = self.src[j]
            if ch == "\\":
                j += 2
            elif ch == quote:
                return j + 1
            elif ch == "\n":
                return j
            else:
                j += 1
        return self.n

    def regex_end(self, i: int) -> Optional[Tuple[int, int]]:
        """``(index of the closing slash, index after the flags)`` of a regex literal starting at ``i``."""
        j, in_class = i + 1, False
        while j < self.n:
            ch = self.src[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "\n":
                return None
            if in_class:
                in_class = ch != "]"
            elif ch == "[":
                in_class = True
            elif ch == "/":
                break
            j += 1
        else:
            return None
        k = j + 1
        while k < self.n and self.src[k].isalpha():
            k += 1
        return j, k

    def template(self, start: int) -> Tuple[int, bool]:
        """Scan template text from ``start``.

        Returns ``(index after the closing backtick, False)`` or ``(index after a "${", True)``.
        """
        i = start
        while i < self.n:
            ch = self.src[i]
            if ch == "\\":
                i += 2
            elif ch == "`":
                self.strings.append(Token(start - 1, self.src[start:i]))
                self.blank(start, i)
                return i + 1, False
            elif ch == "$" and i + 1 < self.n and self.src[i + 1] == "{":
                self.strings.append(Token(start - 1, self.src[start:i]))
                self.blank(start, i)
                self.stack.append("tpl")
                return i + 2, True
            else:
                i += 1
        return self.n, False

    def run(self) -> Lexed:
        src, n = self.src, self.n
        prev_char, prev_word = "", ""
        i = 0
        while i < n:
            c = src[i]
            if c in " \t\r\n":
                i += 1
            elif c == "/" and src.startswith("//", i):
                end = src.find("\n", i)
                end = n if end < 0 else end
                self.blank(i, end)
                i = end
            elif c == "/" and src.startswith("/*", i):
                end = src.find("*/", i + 2)
                end = n if end < 0 else end + 2
                self.blank(i, end)
                i = end
            elif c == "/" and (
                prev_char == ""
                or prev_char in _REGEX_AFTER_CHARS
                or (prev_char == "w" and prev_word in _REGEX_AFTER_WORDS)
            ):
                found = self.regex_end(i)
                if found is None:
                    prev_char, prev_word, i = "/", "", i + 1
                    continue
                close, end = found
                self.regexes.append(Regex(i, src[i + 1 : close], src[close + 1 : end]))
                self.blank(i + 1, close)
                prev_char, prev_word, i = "x", "", end
            elif c in "'\"":
                end = self.string_end(i)
                self.strings.append(Token(i, src[i + 1 : end - 1]))
                self.blank(i + 1, end - 1)
                prev_char, prev_word, i = c, "", end
            elif c == "`":
                i, opened = self.template(i + 1)
                prev_char, prev_word = "{" if opened else "x", ""
            elif c == "{":
                self.stack.append("blk")
                prev_char, prev_word, i = "{", "", i + 1
            elif c == "}":
                if self.stack and self.stack.pop() == "tpl":
                    i, opened = self.template(i + 1)
                    prev_char, prev_word = "{" if opened else "x", ""
                else:
                    prev_char, prev_word, i = "}", "", i + 1
            elif c.isalnum() or c in "_$":
                j = i
                while j < n and (src[j].isalnum() or src[j] in "_$"):
                    j += 1
                prev_char, prev_word, i = "w", src[i:j], j
            else:
                prev_char, prev_word, i = c, "", i + 1
        return Lexed(src, "".join(self.out), self.strings, self.regexes)


def lex(source: str) -> Lexed:
    return _Lexer(source).run()


def match_braces(code: str) -> Dict[int, int]:
    """``{offset of "{": offset of its "}"}`` over the code view."""
    pairs: Dict[int, int] = {}
    stack: List[int] = []
    for index, ch in enumerate(code):
        if ch == "{":
            stack.append(index)
        elif ch == "}" and stack:
            pairs[stack.pop()] = index
    return pairs


_FUNCTION_HEADER = re.compile(
    r"\bfunction\s*\*?\s*(?P<f>[A-Za-z_$][\w$]*)?\s*\("
    r"|^[ \t]*(?:static\s+|async\s+|get\s+|set\s+)*(?P<m>[A-Za-z_$#][\w$]*)\s*\(",
    re.MULTILINE,
)
_NOT_FUNCTIONS = frozenset(("if", "for", "while", "switch", "catch", "return", "with", "function", "await", "typeof"))


def function_spans(code: str) -> List[Tuple[int, int, str]]:
    """``(body start, body end, name)`` of the named ``function`` declarations and class/object methods."""
    pairs = match_braces(code)
    spans: List[Tuple[int, int, str]] = []
    for match in _FUNCTION_HEADER.finditer(code):
        name = match.group("f") or match.group("m")
        if not name or name in _NOT_FUNCTIONS:
            continue
        depth, j = 1, match.end()
        while j < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[j], 0)
            j += 1
        while j < len(code) and code[j] in " \t\r\n":
            j += 1
        if j < len(code) and code[j] == "{" and j in pairs:
            spans.append((j, pairs[j], name))
    return spans


# --------------------------------------------------------------------------------------------------------------
# The spec tables
# --------------------------------------------------------------------------------------------------------------


def _spec_section(text: str, heading: str, end_heading: str) -> str:
    start = text.index(heading)
    return text[start : text.index(end_heading, start + len(heading))]


def spec_events(text: str) -> Set[str]:
    """The ``ev.*`` names of the table in section 7.3."""
    section = _spec_section(text, "### 7.3 Server", "**Recipient matrix.**")
    return set(re.findall(r"^\| `(ev\.[a-z_]+)`", section, re.MULTILINE))


def spec_requests(text: str) -> Set[str]:
    """The request types of section 7.4 (one ``* `type {...}` `` bullet each)."""
    section = _spec_section(text, "### 7.4 Client", "### 7.5 Limits")
    return set(re.findall(r"^\* `([a-z_]+(?:\.[a-z_]+)?)(?=[ `])", section, re.MULTILINE))


# --------------------------------------------------------------------------------------------------------------
# Reading the web tree
# --------------------------------------------------------------------------------------------------------------


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def web_files(subdir: str, suffix: str) -> List[str]:
    """Absolute paths of ``web/<subdir>/**/*<suffix>`` (sorted)."""
    found: List[str] = []
    for current, _dirs, names in os.walk(os.path.join(WEB, subdir)):
        found += [os.path.join(current, n) for n in names if n.endswith(suffix)]
    return sorted(found)


def rel(path: str) -> str:
    """Path relative to ``web/`` with forward slashes (``js/core/store.js``)."""
    return os.path.relpath(path, WEB).replace(os.sep, "/")


def exists_exact(path: str) -> bool:
    """``path`` is a file and every component has exactly this letter case (Windows would accept ``Store.js``)."""
    current = os.path.abspath(path)
    if not os.path.isfile(current):
        return False
    parts: List[str] = []
    while True:
        parent, name = os.path.split(current)
        if parent == current:
            break
        parts.append(name)
        current = parent
        if os.path.normcase(current) == os.path.normcase(WEB):
            break
    walk = WEB
    for name in reversed(parts):
        if name not in os.listdir(walk):
            return False
        walk = os.path.join(walk, name)
    return True


# --------------------------------------------------------------------------------------------------------------
# JavaScript checks
# --------------------------------------------------------------------------------------------------------------

#: identifier-level sinks searched in the code view: (name, pattern, why, rule)
CODE_SINKS: Tuple[Tuple[str, str, str, str], ...] = (
    ("innerHTML", r"\binnerHTML\b", "HTML injection sink", "dom-sink"),
    ("outerHTML", r"\bouterHTML\b", "HTML injection sink", "dom-sink"),
    ("insertAdjacentHTML", r"\binsertAdjacentHTML\b", "HTML injection sink", "dom-sink"),
    ("document.write", r"\bdocument\s*\.\s*write(?:ln)?\b", "HTML injection sink", "dom-sink"),
    ("eval", r"(?<![\w$.])eval\s*\(", "string-to-code sink", "dom-sink"),
    ("new Function", r"\bnew\s+Function\b|(?<![\w$.])Function\s*\(", "string-to-code sink", "dom-sink"),
    ("execScript", r"\bexecScript\b", "string-to-code sink", "dom-sink"),
    ("importScripts", r"\bimportScripts\b", "string-to-code sink", "dom-sink"),
    ("DOMParser", r"\bDOMParser\b", "HTML parsing sink", "dom-sink"),
    ("createContextualFragment", r"\bcreateContextualFragment\b", "HTML parsing sink", "dom-sink"),
    ("setHTML", r"\b(?:setHTML|setHTMLUnsafe|parseHTMLUnsafe)\b", "HTML parsing sink", "dom-sink"),
    ("srcdoc", r"\bsrcdoc\b", "HTML injection sink", "dom-sink"),
    ("CSSStyleSheet", r"\bCSSStyleSheet\b", "forbidden by the CSP (style-src 'self')", "dom-sink"),
    ("adoptedStyleSheets", r"\badoptedStyleSheets\b", "forbidden by the CSP (style-src 'self')", "dom-sink"),
    ("insertRule", r"\binsertRule\b", "forbidden by the CSP (style-src 'self')", "dom-sink"),
    ("serviceWorker", r"\bserviceWorker\b", "no service worker (SPEC 0)", "dom-sink"),
    ("document.cookie", r"\bdocument\s*\.\s*cookie\b", "the UI never touches cookies (SPEC 4.1)", "cookie"),
)
#: property names that must not even be reached through ``el['...']``
BRACKET_SINKS = frozenset(
    name.lower() for name in ("innerHTML", "outerHTML", "insertAdjacentHTML", "srcdoc", "createContextualFragment")
)
LOOPBACK_HOSTS = frozenset(("localhost", "127.0.0.1", "[::1]", "::1"))
#: ``createElement('<tag>')`` tags that are forbidden
FORBIDDEN_TAGS = frozenset(("script", "style", "iframe", "frame", "object", "embed", "base"))
_URL = re.compile(
    r"\b(?:https?|wss?|ftp)://(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9][A-Za-z0-9.-]*)|^\s*//([A-Za-z0-9.-]+\.[a-z]{2,})(?:[/:?#]|$)"
)
_REQUEST_TYPE = re.compile(r"^[a-z_]+(?:\.[a-z_]+)?$")


_NAMESPACE = re.compile(r"(?:[a-z]+:)?//(?:%s)" % "|".join(re.escape(u) for u in NAMESPACE_URIS), re.IGNORECASE)


def external_url_spans(text: str) -> List[Tuple[int, str]]:
    """``(offset, host)`` of the URLs in ``text`` that are neither loopback nor an XML namespace identifier."""
    found: List[Tuple[int, str]] = []
    for match in _URL.finditer(text):
        host = (match.group(1) or match.group(2)).lower()
        if host in LOOPBACK_HOSTS or _NAMESPACE.match(text[match.start() :].lstrip()):
            continue
        found.append((match.start(), host))
    return found


def external_urls(text: str) -> List[str]:
    return [host for _, host in external_url_spans(text)]


def _line_findings(path: str, lexed: Lexed) -> List[Finding]:
    findings: List[Finding] = []

    def add(offset: int, rule: str, text: str) -> None:
        findings.append(Finding(path, lexed.line(offset), rule, text))

    for name, pattern, why, rule in CODE_SINKS:
        for match in re.finditer(pattern, lexed.code):
            add(match.start(), rule, "%s: %s" % (name, why))
    for token in lexed.strings:
        value = token.text
        before = lexed.before(token.start)
        lowered = value.strip().lower()
        if re.search(r"\.\s*setAttribute\s*\($", before) and (
            lowered in ("style", "srcdoc") or re.match(r"on[a-z]+$", lowered)
        ):
            add(token.start, "dom-sink", "setAttribute('%s'): inline style/handler/srcdoc attribute" % value)
        if re.search(r"\.\s*createElement\s*\($", before) and lowered in FORBIDDEN_TAGS:
            add(token.start, "dom-sink", "createElement('%s') is forbidden" % value)
        if re.search(r"\bexecCommand\s*\($", before) and lowered == "inserthtml":
            add(token.start, "dom-sink", "execCommand('insertHTML')")
        if re.search(r"\b(?:setTimeout|setInterval)\s*\($", before):
            add(token.start, "dom-sink", "setTimeout/setInterval with a string is a string-to-code sink")
        if re.match(r"(?:javascript|vbscript):", lowered) or lowered.startswith("data:text/html"):
            add(token.start, "dom-sink", "script-capable URL %r" % value[:30])
        if lowered in BRACKET_SINKS and before.endswith("["):
            add(token.start, "dom-sink", "bracket access to the %s sink" % value)
        if re.search(r"\bdocument\s*\[$", before) and lowered == "cookie":
            add(token.start, "cookie", "document['cookie']")
        if "fc_session" in value:
            add(token.start, "cookie", "the UI must never name the session cookie")
        if re.search(r"\b(?:local|session)Storage\s*\.\s*(?:set|get|remove)Item\s*\($", before) and re.search(
            r"token|password|secret|cookie|session", lowered
        ):
            add(token.start, "cookie", "tokens and secrets are never stored in web storage: %r" % value)
        if external_urls(value):
            add(token.start, "external-url", "%r" % value[:60])
        if "(?<=" in value or "(?<!" in value:
            add(token.start, "regex", "lookbehind in a RegExp string (Safari < 16.4 fails to parse it)")
    for regex in lexed.regexes:
        if re.search(r"\(\?<[=!]", regex.body):
            add(regex.start, "regex", "lookbehind in a regex literal (Safari < 16.4 fails to parse the file)")
        if "v" in regex.flags:
            add(regex.start, "regex", "regex literal with the v flag (Safari < 17 fails to parse it)")
    for match in re.finditer(r"\bimport\s*\(\s*([^\s'\"`)])", lexed.code):
        add(match.start(), "import", "dynamic import() with a non-literal specifier cannot be followed by the lint")
    return findings


def _import_findings(path: str, absolute: str, lexed: Lexed) -> List[Finding]:
    findings: List[Finding] = []
    for token in lexed.strings:
        before = lexed.before(token.start)
        if not re.search(r"(?:\bfrom|\bimport|\bimport\s*\()$", before):
            continue
        spec, line = token.text, lexed.line(token.start)
        if spec.startswith("/"):
            target = os.path.join(WEB, *spec.lstrip("/").split("/"))
        elif spec.startswith(("./", "../")):
            target = os.path.normpath(os.path.join(os.path.dirname(absolute), *spec.split("/")))
        else:
            findings.append(
                Finding(
                    path,
                    line,
                    "import",
                    "specifier %r is neither relative nor absolute (no bundler resolves it)" % spec,
                )
            )
            continue
        if not spec.endswith(".js"):
            findings.append(
                Finding(path, line, "import", "specifier %r has no .js extension (browsers need the exact file)" % spec)
            )
        elif not exists_exact(target):
            findings.append(
                Finding(
                    path, line, "import", "specifier %r does not resolve to a file (or differs in letter case)" % spec
                )
            )
    return findings


#: namespaces of the dotted request types of SPEC 7.4; a literal of one of them that is the first argument of ANY call
#: (``ask('admin.users')`` through a local wrapper) is treated like ``socket.request('admin.users')``
REQUEST_NAMESPACES = ("admin", "chat", "msg", "receipt", "profile")


def request_literals(lexed: Lexed) -> List[Tuple[int, str]]:
    """``(line, type)`` of every request type the code names with a literal: ``....request('type'``,
    ``socket.send('type'`` and ``anyFunction('ns.type'`` for the namespaces of SPEC 7.4."""
    found: List[Tuple[int, str]] = []
    for token in lexed.strings:
        before = lexed.before(token.start)
        if not _REQUEST_TYPE.match(token.text):
            continue
        direct = re.search(r"\.\s*request\s*\($", before) or re.search(r"[sS]ocket\s*\.\s*send\s*\($", before)
        wrapped = (
            token.text.split(".")[0] in REQUEST_NAMESPACES and "." in token.text and re.search(r"[\w$]\s*\($", before)
        )
        if direct or wrapped:
            found.append((lexed.line(token.start), token.text))
    return found


def event_literals(lexed: Lexed) -> List[Tuple[int, str]]:
    """``(line, name)`` of every string literal that is exactly ``ev.<name>``."""
    return [(lexed.line(t.start), t.text) for t in lexed.strings if re.match(r"^ev\.[A-Za-z_]+$", t.text)]


# --------------------------------------------------------------------------------------------------------------
# HTML and CSS checks
# --------------------------------------------------------------------------------------------------------------


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: List[Tuple[str, Dict[str, Optional[str]], int]] = []
        self.noscript = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.tags.append((tag, dict(attrs), self.getpos()[0]))
        if tag == "noscript":
            self.noscript = True


def _local_target(value: str, base_dir: str) -> Optional[str]:
    """The file a local reference points to, or ``None`` for ``data:``/``#``/``mailto:`` and the like."""
    value = value.strip()
    if not value or value.startswith(("#", "data:", "mailto:", "tel:", "blob:")):
        return None
    value = re.split(r"[?#]", value, 1)[0]
    if value in ("", "/"):
        return None
    if value.startswith("/"):
        return os.path.join(WEB, *value.lstrip("/").split("/"))
    return os.path.normpath(os.path.join(base_dir, *value.split("/")))


def html_findings(path: str, html: str) -> List[Finding]:
    page = _Page()
    page.feed(html)
    findings: List[Finding] = []

    def add(line: int, rule: str, text: str) -> None:
        findings.append(Finding(path, line, rule, text))

    for tag, attrs, line in page.tags:
        if tag == "style":
            add(line, "inline-style", "<style> element (the CSP is style-src 'self')")
        if tag == "script" and "src" not in attrs:
            add(line, "inline-script", "<script> without src (the CSP is script-src 'self')")
        for name, value in attrs.items():
            lowered = (value or "").strip().lower()
            if name == "style":
                add(line, "inline-style", "inline style= attribute")
            elif name.startswith("on"):
                add(line, "inline-handler", "inline event attribute %s=" % name)
            if name in ("href", "src", "action", "poster", "data", "formaction"):
                if lowered.startswith(("javascript:", "vbscript:")):
                    add(line, "dom-sink", "%s=%r" % (name, lowered[:30]))
                elif external_urls(lowered):
                    add(line, "external-url", "%s=%r" % (name, lowered[:60]))
                elif tag in ("link", "script", "img", "source", "audio", "video"):
                    target = _local_target(value or "", WEB)
                    if target is not None and not exists_exact(target):
                        add(line, "missing-file", "<%s %s=%r> does not exist" % (tag, name, value))
    return findings


def css_findings(path: str, css: str, absolute: str) -> List[Finding]:
    stripped = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.DOTALL)
    findings: List[Finding] = []

    def line_of(offset: int) -> int:
        return stripped.count("\n", 0, offset) + 1

    for match in re.finditer(r"@font-face", stripped):
        findings.append(
            Finding(
                path, line_of(match.start()), "web-font", "@font-face: web fonts are forbidden (system font stack only)"
            )
        )
    for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)|@import\s+(['\"])(.*?)\3", stripped, re.DOTALL):
        value = (match.group(2) if match.group(2) is not None else match.group(4)) or ""
        target = None if external_urls(value) else _local_target(value, os.path.dirname(absolute))
        if target is not None and not exists_exact(target):
            findings.append(Finding(path, line_of(match.start()), "missing-file", "%r does not exist" % value))
    for offset, host in external_url_spans(stripped):
        findings.append(Finding(path, line_of(offset), "external-url", "external host %s in a stylesheet" % host))
    return findings


def index_contract(path: str, html: str) -> List[Finding]:
    """The tags SPEC 9.5 and 9.8 require in ``index.html``."""
    page = _Page()
    page.feed(html)
    findings: List[Finding] = []

    def need(condition: bool, text: str) -> None:
        if not condition:
            findings.append(Finding(path, 1, "index", text))

    metas = {a.get("name"): (a.get("content") or "") for t, a, _ in page.tags if t == "meta" and a.get("name")}
    links = [a for t, a, _ in page.tags if t == "link"]
    scripts = [a for t, a, _ in page.tags if t == "script"]
    viewport = metas.get("viewport", "")
    need(
        "width=device-width" in viewport and "viewport-fit=cover" in viewport,
        "viewport meta lacks width=device-width / viewport-fit=cover",
    )
    need("interactive-widget=resizes-content" in viewport, "viewport meta lacks interactive-widget=resizes-content")
    need(
        "maximum-scale" not in viewport and "user-scalable" not in viewport,
        "the viewport must not disable zoom (maximum-scale/user-scalable)",
    )
    for name in (
        "color-scheme",
        "theme-color",
        "apple-mobile-web-app-capable",
        "mobile-web-app-capable",
        "apple-mobile-web-app-title",
    ):
        need(name in metas, "missing <meta name=%s>" % name)
    need(
        any(a.get("rel") == "manifest" and a.get("href") == "/manifest.webmanifest" for a in links),
        'missing <link rel="manifest" href="/manifest.webmanifest">',
    )
    need(any(a.get("rel") == "apple-touch-icon" for a in links), "missing apple-touch-icon link")
    need(
        any(a.get("rel") == "icon" and (a.get("href") or "").startswith("data:") for a in links),
        "the favicon must be a data: URL",
    )
    need(
        sum(1 for a in scripts if a.get("type") == "module" and a.get("src") == "/js/main.js") == 1,
        'exactly one <script type="module" src="/js/main.js">',
    )
    need(
        any("nomodule" in a and a.get("src") == "/js/unsupported.js" for a in scripts),
        '<script nomodule src="/js/unsupported.js"> missing',
    )
    need(page.noscript, "missing <noscript> message")
    need(any(t == "meta" and "charset" in a for t, a, _ in page.tags), "missing <meta charset>")
    return findings


# --------------------------------------------------------------------------------------------------------------
# The tests
# --------------------------------------------------------------------------------------------------------------


class JsFile(NamedTuple):
    path: str  # relative to web/
    absolute: str
    lexed: Lexed


def load_js() -> List[JsFile]:
    files = []
    for absolute in web_files("js", ".js"):
        files.append(JsFile(rel(absolute), absolute, lex(_read(absolute))))
    return files


class LexerTests(unittest.TestCase):
    def test_comments_strings_templates_and_regexes_are_separated(self) -> None:
        source = (
            "// el.innerHTML = 1\n"
            "/* eval('x') */\n"
            'const a = \'innerHTML\', b = "x\\"y", c = `t ${ d + `n${e}` } u`;\n'
            "const r = /a(?<=b)\\/[/]/gi, q = x / y / z;\n"
            "el.textContent = a; // trailing\n"
        )
        lexed = lex(source)
        self.assertEqual(len(lexed.code), len(source))
        self.assertEqual(lexed.code.count("\n"), source.count("\n"))
        self.assertNotIn("innerHTML", lexed.code)
        self.assertNotIn("eval", lexed.code)
        self.assertIn("textContent", lexed.code)
        self.assertEqual([t.text for t in lexed.strings], ["innerHTML", 'x\\"y', "t ", "n", "", " u"])
        self.assertEqual([(r.body, r.flags) for r in lexed.regexes], [("a(?<=b)\\/[/]", "gi")])
        self.assertEqual(lexed.line(source.index("el.textContent")), 5)

    def test_division_is_not_a_regex(self) -> None:
        lexed = lex("const x = a / b; const y = (a + b) / 2; const z = arr[0] / 3 / 4;\n")
        self.assertEqual(lexed.regexes, [])
        self.assertTrue(lex("return /x/.test(s);\n").regexes)
        self.assertTrue(lex("const f = (s) => /x/.test(s);\n").regexes)

    def test_braces_balance_across_templates(self) -> None:
        code = lex("const a = `x ${ {a: 1}.a } {` + '}' + \"{\"; function f() { return `}${1}{`; }\n").code
        self.assertEqual(code.count("{"), code.count("}"))

    def test_function_spans(self) -> None:
        code = lex(
            "class A {\n  _onReceipt(d) {\n    go();\n  }\n  static async load(x, y) { return 1; }\n}\n"
            "function top(a) { if (a) { b(); } }\n"
        ).code
        names = sorted(name for _, _, name in function_spans(code))
        self.assertEqual(names, ["_onReceipt", "load", "top"])


class SpecTableTests(unittest.TestCase):
    def test_tables_parse(self) -> None:
        text = _read(SPEC_PATH)
        events, requests = spec_events(text), spec_requests(text)
        self.assertEqual(len(events), 15, sorted(events))
        self.assertTrue({"ev.ready", "ev.receipt", "ev.kicked", "ev.chat_members"} <= events)
        self.assertTrue(
            {"ping", "typing", "msg.send", "receipt.read", "chat.history", "admin.audit"} <= requests, sorted(requests)
        )
        self.assertGreaterEqual(len(requests), 34, sorted(requests))


class CheckerTests(unittest.TestCase):
    """The per-file checks must flag the bad and pass the good (they are what makes a clean run meaningful)."""

    @staticmethod
    def rules(source: str) -> List[str]:
        return sorted(f.rule for f in _line_findings("t.js", lex(source)))

    def test_sinks_are_found_in_code_only(self) -> None:
        bad = [
            "el.innerHTML = x;",
            "el.outerHTML = x;",
            "el.insertAdjacentHTML('beforeend', x);",
            "document.write(x);",
            "eval(x);",
            "new Function('return 1');",
            "setTimeout('go()', 5);",
            "setInterval(`go()`, 5);",
            "el.setAttribute('style', 'a:b');",
            "el.setAttribute('onclick', 'go()');",
            "el.setAttribute('srcdoc', x);",
            "new DOMParser();",
            "range.createContextualFragment(x);",
            "iframe.srcdoc = x;",
            "location.href = 'javascript:go()';",
            "new CSSStyleSheet();",
            "document.adoptedStyleSheets = [];",
            "sheet.insertRule('a{}');",
            "document.createElement('style');",
            "document.createElement('script');",
            "x = document.cookie;",
            "x = document['cookie'];",
            "el['innerHTML'] = x;",
            "navigator.serviceWorker.register('/sw.js');",
            "document.execCommand('insertHTML', false, x);",
        ]
        for snippet in bad:
            with self.subTest(snippet):
                self.assertTrue({"dom-sink", "cookie"} & set(self.rules(snippet)), snippet)
        good = [
            "// el.innerHTML = x\nel.textContent = x;",
            "const s = 'innerHTML';",
            "setTimeout(() => go(), 5);",
            "el.setAttribute('class', 'a');",
            "el.setAttribute('data-x', 'x');",
            "document.createElement('div');",
            "document.execCommand('copy');",
            "x.evaluate(y);",
            "const f = x instanceof Function;",
            "el.style.color = 'red'; el.style.setProperty('a', 'b'); el.style.cssText = 'a:b';",
            "document.createElementNS(NS, 'svg');",
            "const url = '/api/me';",
            "const ns = 'http://www.w3.org/2000/svg';",
        ]
        for snippet in good:
            with self.subTest(snippet):
                self.assertEqual(self.rules(snippet), [], snippet)

    def test_urls_cookies_storage_and_regexes(self) -> None:
        self.assertEqual(self.rules("const u = 'https://cdn.example.com/x.js';"), ["external-url"])
        self.assertEqual(self.rules("const u = `wss://example.org/ws`;"), ["external-url"])
        self.assertEqual(self.rules("const u = '//cdn.example.com/x.js';"), ["external-url"])
        self.assertEqual(self.rules("const p = 'https://', q = 'http://' + host; const r = /https?:\\/\\//;"), [])
        self.assertEqual(
            self.rules("const t = 'Open http://localhost:8765/ or http://127.0.0.1:8765/ on this PC';"), []
        )
        self.assertEqual(self.rules("const t = 'Set http://<server-ip>:8765 in the policy';"), [])
        self.assertEqual(self.rules("const c = 'fc_session=1';"), ["cookie"])
        self.assertEqual(self.rules("localStorage.setItem('auth_token', t);"), ["cookie"])
        self.assertEqual(self.rules("localStorage.setItem('fc:v1:outbox', t);"), [])
        self.assertEqual(self.rules("const r = /(?<=a)b/;"), ["regex"])
        self.assertEqual(self.rules("const r = /(?<!a)b/u;"), ["regex"])
        self.assertEqual(self.rules("const r = new RegExp('(?<=a)b');"), ["regex"])
        self.assertEqual(self.rules("const r = /\\p{L}/v;"), ["regex"])
        self.assertEqual(self.rules("const r = /(?<name>a)\\k<name>/u;"), [])

    def test_imports(self) -> None:
        self.assertEqual(self.rules("const m = await import(name);"), ["import"])
        self.assertEqual(self.rules("const m = await import('./x.js');"), [])
        lexed = lex(
            "import { a } from './util.js';\nimport './dom.js';\n"
            "export * from '../core/store.js';\nconst m = import('./a.js');\n"
        )
        specs = [
            t.text for t in lexed.strings if re.search(r"(?:\bfrom|\bimport|\bimport\s*\()$", lexed.before(t.start))
        ]
        self.assertEqual(specs, ["./util.js", "./dom.js", "../core/store.js", "./a.js"])
        self.assertEqual(
            request_literals(
                lex(
                    "this._socket.request('msg.send', d); socket.send('typing', d);"
                    " api.request('GET', '/x'); this.request(type); ask('admin.users', {}); x = {'msg.send': 1};"
                )
            ),
            [(1, "msg.send"), (1, "typing"), (1, "admin.users")],
        )
        self.assertEqual(
            event_literals(lex("on('ev.ready', f); x = 'ev.'; y = 'ev.message_update';")),
            [(1, "ev.ready"), (1, "ev.message_update")],
        )

    def test_html_and_css(self) -> None:
        bad_html = (
            '<html><head><style>a{}</style><script>go()</script><script src="https://cdn.example.com/a.js"></script>'
            '<link rel="stylesheet" href="//cdn.example.com/a.css"></head>'
            '<body onload="go()"><div style="color:red"></div><a href="javascript:go()">x</a></body></html>'
        )
        rules = sorted({f.rule for f in html_findings("index.html", bad_html)})
        self.assertEqual(rules, ["dom-sink", "external-url", "inline-handler", "inline-script", "inline-style"])
        css = (
            "@font-face{src:url(/fonts/a.woff2)}\n"
            ".a{background:url(https://x.example/a.png)}\n"
            ".b{background:url('data:image/svg+xml,<svg xmlns=\"http://www.w3.org/2000/svg\"/>')}\n"
        )
        found = css_findings("css/x.css", css, os.path.join(WEB, "css", "x.css"))
        self.assertEqual(sorted({f.rule for f in found}), ["external-url", "missing-file", "web-font"])
        self.assertEqual([f.line for f in found if f.rule == "external-url"], [2])


class WebClientLintTests(unittest.TestCase):
    js: List[JsFile]
    spec: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = load_js()
        cls.spec = _read(SPEC_PATH)

    def assert_clean(self, findings: List[Finding], what: str) -> None:
        self.assertEqual(findings, [], "%s:\n  %s" % (what, "\n  ".join(str(f) for f in findings)))

    # ---- sanity ------------------------------------------------------------------------------------------------

    def test_there_is_a_client_to_lint(self) -> None:
        self.assertTrue(os.path.isfile(os.path.join(WEB, "index.html")), "web/index.html is missing")
        names = {f.path for f in self.js}
        self.assertTrue({"js/main.js", "js/core/store.js", "js/core/socket.js"} <= names, sorted(names))

    def test_the_lexer_agrees_with_every_source_file(self) -> None:
        problems = []
        for f in self.js:
            code = f.lexed.code
            if (
                code.count("{") != code.count("}")
                or code.count("(") != code.count(")")
                or code.count("[") != code.count("]")
            ):
                problems.append(
                    Finding(
                        f.path,
                        1,
                        "lexer",
                        "unbalanced brackets after removing comments/strings (lexer bug or syntax error)",
                    )
                )
        self.assert_clean(problems, "the lint cannot trust these files")

    # ---- imports -----------------------------------------------------------------------------------------------

    def test_all_relative_imports_resolve(self) -> None:
        findings: List[Finding] = []
        for f in self.js:
            findings += _import_findings(f.path, f.absolute, f.lexed)
            findings += [x for x in _line_findings(f.path, f.lexed) if x.rule == "import"]
        self.assert_clean(findings, "imports that do not resolve (static, `export ... from` and literal `import()`)")

    # ---- sinks, urls, cookies, regexes -------------------------------------------------------------------------

    def _by_rule(self, rule: str) -> List[Finding]:
        found: List[Finding] = []
        for f in self.js:
            found += [x for x in _line_findings(f.path, f.lexed) if x.rule == rule]
        return found

    def test_no_forbidden_dom_sinks(self) -> None:
        self.assert_clean(self._by_rule("dom-sink"), "DOM and string-to-code sinks of SPEC 9.1 / 5.4")

    def test_no_external_urls_in_scripts(self) -> None:
        self.assert_clean(
            self._by_rule("external-url"), "external URLs in JavaScript strings (the LAN has no internet)"
        )

    def test_ui_never_touches_cookies_or_stores_tokens(self) -> None:
        self.assert_clean(
            self._by_rule("cookie"), "cookie/token handling in the UI (document.cookie is forbidden, SPEC 4.1)"
        )

    def test_no_regex_lookbehind_or_v_flag(self) -> None:
        self.assert_clean(self._by_rule("regex"), "regex syntax Safari < 16.4 cannot parse (SPEC 9.8)")

    def test_icons_use_createElementNS_only(self) -> None:
        icons = [f for f in self.js if f.path == "js/core/icons.js"]
        if not icons:
            self.skipTest("web/js/core/icons.js does not exist yet")
        lexed = icons[0].lexed
        findings = [
            Finding(
                icons[0].path,
                lexed.line(m.start()),
                "icons",
                "%s: icons.js builds SVG with createElementNS only" % m.group(0),
            )
            for m in re.finditer(r"\bcreateElement\s*\(|\binnerHTML\b|\bDOMParser\b|\binsertAdjacentHTML\b", lexed.code)
        ]
        self.assert_clean(findings, "icons.js")
        self.assertIn("createElementNS", lexed.code, "icons.js never calls createElementNS")

    # ---- protocol names ----------------------------------------------------------------------------------------

    def _known_requests(self) -> Tuple[Set[str], str]:
        spec = spec_requests(self.spec)
        if not os.path.isfile(HUB_PATH):
            return spec, "SPEC 7.4 (chatd/hub.py does not exist yet)"
        hub = _read(HUB_PATH)

        def in_hub(name: str) -> bool:
            if ('"%s"' % name) in hub or ("'%s'" % name) in hub:
                return True
            return "." in name and re.search(r"\b\w*%s\b" % name.replace(".", "_"), hub) is not None

        return {name for name in spec | {n for _, n in self._used_requests()} if in_hub(name)}, "chatd/hub.py"

    def _used_requests(self) -> List[Tuple[str, str]]:
        used: List[Tuple[str, str]] = []
        for f in self.js:
            used += [("%s:%d" % (f.path, line), name) for line, name in request_literals(f.lexed)]
        return used

    def test_request_types_exist(self) -> None:
        used = self._used_requests()
        self.assertTrue(used, "the extractor found no request('...') call at all (lint is vacuous)")
        known, source = self._known_requests()
        findings = []
        for where, name in used:
            if name not in known:
                path, _, line = where.rpartition(":")
                findings.append(
                    Finding(path, int(line), "request-type", "%r is not a request type of %s" % (name, source))
                )
        self.assert_clean(findings, "request types the UI sends")

    def test_event_literals_are_in_the_spec(self) -> None:
        events = spec_events(self.spec)
        findings = []
        for f in self.js:
            findings += [
                Finding(f.path, line, "event", "%r is not an event of SPEC 7.3" % name)
                for line, name in event_literals(f.lexed)
                if name not in events
            ]
        self.assert_clean(findings, "ev.* names handled by the client")

    def test_every_spec_event_is_handled_somewhere(self) -> None:
        handled = {name for f in self.js for _, name in event_literals(f.lexed)}
        missing = sorted(spec_events(self.spec) - handled)
        self.assertEqual(missing, [], "SPEC 7.3 events that no module under web/js handles")

    # ---- requestAnimationFrame ---------------------------------------------------------------------------------

    def test_no_request_animation_frame_in_receipt_and_notification_paths(self) -> None:
        findings: List[Finding] = []
        for f in self.js:
            spans = function_spans(f.lexed.code)
            for match in re.finditer(r"\brequestAnimationFrame\b", f.lexed.code):
                line = f.lexed.line(match.start())
                if f.path in NO_RAF_FILES:
                    findings.append(
                        Finding(
                            f.path,
                            line,
                            "raf",
                            "requestAnimationFrame in a module that runs inside the socket handler (SPEC 9.6 rule 8)",
                        )
                    )
                    continue
                for start, end, name in spans:
                    if start < match.start() < end and RECEIPT_FUNCTION.search(name):
                        findings.append(
                            Finding(
                                f.path,
                                line,
                                "raf",
                                "requestAnimationFrame inside %s(): receipts and alerts use setTimeout (SPEC 8.2)"
                                % name,
                            )
                        )
                        break
        self.assert_clean(
            findings, "rAF does not fire in hidden tabs, exactly when delivery receipts and chimes are needed"
        )

    # ---- html / css / manifest ---------------------------------------------------------------------------------

    def test_index_html_has_no_inline_code_or_external_urls(self) -> None:
        path = os.path.join(WEB, "index.html")
        self.assert_clean(html_findings("index.html", _read(path)), "index.html")

    def test_index_html_contract(self) -> None:
        path = os.path.join(WEB, "index.html")
        self.assert_clean(index_contract("index.html", _read(path)), "index.html (SPEC 9.5 / 9.8)")

    def test_stylesheets_are_local_and_use_system_fonts(self) -> None:
        findings: List[Finding] = []
        for absolute in web_files("css", ".css"):
            findings += css_findings(rel(absolute), _read(absolute), absolute)
        self.assert_clean(findings, "stylesheets")

    def test_manifest_is_valid_and_local(self) -> None:
        path = os.path.join(WEB, "manifest.webmanifest")
        if not os.path.isfile(path):
            self.skipTest("web/manifest.webmanifest does not exist yet")
        manifest = json.loads(_read(path))
        findings = []
        for icon in manifest.get("icons", []):
            src = icon.get("src", "")
            target = _local_target(src, WEB)
            if external_urls(src) or (target is not None and not exists_exact(target)):
                findings.append(Finding("manifest.webmanifest", 1, "manifest", "icon %r is external or missing" % src))
        start_url = manifest.get("start_url", "/")
        if external_urls(start_url):
            findings.append(Finding("manifest.webmanifest", 1, "manifest", "start_url %r is external" % start_url))
        self.assert_clean(findings, "manifest")
        self.assertTrue(manifest.get("icons"), "the manifest lists no icons")

    def test_home_screen_icons_exist_and_are_png(self) -> None:
        for size in (180, 192, 512):
            path = os.path.join(WEB, "img", "icon-%d.png" % size)
            self.assertTrue(os.path.isfile(path), "web/img/icon-%d.png is missing (SPEC 9.5)" % size)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n", "icon-%d.png is not a PNG" % size)


if __name__ == "__main__":
    unittest.main()
