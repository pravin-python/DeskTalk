"""Hand-written HTTP/1.1 server on ``asyncio`` streams (SPEC 5.1 - 5.4, 5.9, 6.2).

Public API (exactly the contract of SPEC 6.2): :class:`Request`, :class:`Response`, :func:`json_response`,
:func:`error_response`, :class:`HttpError`, :class:`Router`.  Beyond that this module owns

* :func:`check_request_origin` - the one shared Host / ``Sec-Fetch-Site`` / ``Origin`` / ``X-Requested-With`` guard,
* :class:`HttpServer` - sockets, strict request parsing, timeouts, caps, keep-alive, response writing and the
  hand-over of an upgraded socket to the WebSocket layer,
* the static-file handler (positive allow-list, SPEC 5.3), the upload handler ``/api/upload`` and the download
  handler ``/files/<id>`` (registered by :func:`register_http_routes`),
* :class:`RedirectServer` - the optional plain-HTTP listener that only redirects to HTTPS.
"""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
import logging
import math
import os
import re
import socket
import ssl
import stat
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, unquote

from . import files, util
from .config import Config

log = logging.getLogger("chatd.http")

# ---- limits and timeouts (SPEC 5.1; every duration goes through util.scaled) ----------------------------------------
MAX_HEAD_BYTES = 16 * 1024
MAX_HEADERS = 100
HEADER_TOTAL_TIMEOUT_S = 15.0
IDLE_TIMEOUT_S = 30.0
BODY_GRACE_S = 10.0
BODY_MIN_RATE = 8 * 1024  # bytes per second after the grace period
BODY_MIN_TIME_S = 30.0
BODY_DEADLINE_RATE = 32 * 1024  # the overall deadline is max(30 s, Content-Length / 32 KiB/s)
JSON_BODY_MAX = 64 * 1024
WRITE_TIMEOUT_S = 30.0
CLOSE_TIMEOUT_S = 2.0
TLS_HANDSHAKE_TIMEOUT_S = 10.0
DRAIN_MAX_BYTES = 1024 * 1024
DRAIN_MAX_S = 2.0
MAX_SOCKETS = 1200
MAX_SOCKETS_PER_IP = 64
MAX_TARGET_CHARS = 8192
HEAD_READ_SIZE = 8192

_REASONS = {
    100: "Continue", 101: "Switching Protocols", 200: "OK", 201: "Created", 204: "No Content",
    206: "Partial Content", 301: "Moved Permanently", 304: "Not Modified", 400: "Bad Request", 408: "Request Timeout",
    401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    411: "Length Required", 413: "Payload Too Large", 415: "Unsupported Media Type",
    416: "Range Not Satisfiable", 421: "Misdirected Request", 429: "Too Many Requests",
    431: "Request Header Fields Too Large", 500: "Internal Server Error", 501: "Not Implemented",
    503: "Service Unavailable", 505: "HTTP Version Not Supported", 507: "Insufficient Storage",
}

_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_METHOD_RE = re.compile(r"^[A-Z]{1,16}$")
_VERSION_RE = re.compile(r"^HTTP/[0-9]\.[0-9]$")
_CONTENT_LENGTH_RE = re.compile(r"^[0-9]{1,12}$")
_HOST_RE = re.compile(r"^([a-z0-9.-]{1,253}|\[[0-9a-f:.]+\])(:\d{1,5})?$")
_ORIGIN_RE = re.compile(r"^(https?)://(([a-z0-9.-]{1,253}|\[[0-9a-f:.]+\])(:\d{1,5})?)$")
_FORBIDDEN_HEADER_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_BARE_LF = re.compile(rb"(?<!\r)\n")
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".corp")

_TEXTUAL_TYPES = ("text/", "application/json", "application/manifest+json", "application/javascript")

#: Names this machine answers to (hostname / FQDN); filled by :func:`prime_host_names` at startup.
_MACHINE_NAMES: Set[str] = set()


# --------------------------------------------------------------------------------------------------------------
# Response objects
# --------------------------------------------------------------------------------------------------------------


class Response:
    """An HTTP response.  Exactly one of ``body`` / ``stream`` / ``upgrade`` normally applies (SPEC 6.2).

    ``stream`` is an async iterator of ``bytes`` (the handler sets ``Content-Length`` itself; without it the
    connection is closed after the body).  The HTTP layer calls ``aclose()`` on the stream when it has one.
    ``upgrade(reader, writer)`` receives the raw socket after the ``101`` head was written (``/ws`` only).
    """

    def __init__(
        self,
        status: int,
        headers: Optional[List[Tuple[str, str]]] = None,
        body: Optional[bytes] = None,
        stream: Optional[AsyncIterator[bytes]] = None,
        upgrade: Optional[Callable[[Any, Any], Awaitable[None]]] = None,
    ) -> None:
        self.status = status
        self.headers: List[Tuple[str, str]] = list(headers or [])
        self.body = body
        self.stream = stream
        self.upgrade = upgrade
        self.error: Optional[Tuple[str, str]] = None  # (code, msg) when built by error_response()
        self.close = False  # force Connection: close

    def header(self, name: str) -> Optional[str]:
        """The first header called ``name`` (case-insensitive) or ``None``."""
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None

    def set_header(self, name: str, value: str) -> None:
        """Replace every header called ``name`` by one with ``value``."""
        self.remove_header(name)
        self.headers.append((name, value))

    def add_header(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def remove_header(self, name: str) -> None:
        lowered = name.lower()
        self.headers = [(k, v) for k, v in self.headers if k.lower() != lowered]


def json_response(status: int, obj: Any, headers: Optional[List[Tuple[str, str]]] = None) -> Response:
    """A JSON response with a compact, ASCII-only body."""
    resp = Response(status, [("Content-Type", "application/json; charset=utf-8")], util.json_dumps(obj).encode("ascii"))
    for name, value in headers or []:
        resp.add_header(name, value)
    return resp


def error_response(
    status: int, code: str, msg: str, retry_after: Optional[float] = None, reason: Optional[str] = None
) -> Response:
    """The error object of SPEC 4.2 plus ``Retry-After`` when ``retry_after`` is given."""
    err: Dict[str, Any] = {"code": code, "msg": msg}
    if retry_after is not None:
        err["retry_after"] = retry_after
    if reason is not None:
        err["reason"] = reason
    resp = json_response(status, {"error": err})
    if retry_after is not None:
        resp.add_header("Retry-After", str(max(1, int(math.ceil(retry_after)))))
    resp.error = (code, msg)
    return resp


class HttpError(Exception):
    """Raised by handlers (and by :meth:`Request.read_json`) to answer with ``response``."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status)
        self.response = response


class ClientGone(Exception):
    """The peer closed the connection (or timed out) while the server was still reading its request."""


# --------------------------------------------------------------------------------------------------------------
# Connection and request body
# --------------------------------------------------------------------------------------------------------------


class Connection:
    """One client socket.  Also the ``reader`` handed to an upgrade callable (it serves bytes read past the head).

    ``read`` / ``readexactly`` follow the ``asyncio.StreamReader`` contract (``IncompleteReadError`` on EOF).
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, remote_addr: str, scheme: str
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.remote_addr = remote_addr
        self.scheme = scheme
        self.buf = bytearray()
        self.is_ws = False

    async def read(self, n: int = 65536) -> bytes:
        if self.buf:
            data = bytes(self.buf[:n])
            del self.buf[:n]
            return data
        return await self.reader.read(n)

    async def readexactly(self, n: int) -> bytes:
        if len(self.buf) >= n:
            data = bytes(self.buf[:n])
            del self.buf[:n]
            return data
        head = bytes(self.buf)
        self.buf.clear()
        try:
            return head + await self.reader.readexactly(n - len(head))
        except asyncio.IncompleteReadError as exc:
            raise asyncio.IncompleteReadError(head + exc.partial, n)


class _Body:
    """The request body of one request, read with the timeouts of SPEC 5.1."""

    def __init__(self, conn: Connection, length: int, expect_continue: bool) -> None:
        self._conn = conn
        self.length = length
        self.remaining = length
        self._expect = expect_continue
        self._t0: Optional[float] = None
        self._deadline = 0.0
        self._mark: Optional[Tuple[float, int]] = None
        self._received = 0

    async def read(self, n: int) -> bytes:
        """Up to ``n`` bytes of the body, ``b""`` once it is complete.  Raises HttpError (slow) / ClientGone."""
        if self.remaining <= 0:
            return b""
        loop = asyncio.get_running_loop()
        if self._t0 is None:
            self._t0 = loop.time()
            self._deadline = self._t0 + util.scaled(max(BODY_MIN_TIME_S, self.length / BODY_DEADLINE_RATE))
            if self._expect:
                self._expect = False
                self._conn.writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        while True:
            now = loop.time()
            self._check_rate(now)
            if now >= self._deadline:
                raise _too_slow()
            try:
                data = await asyncio.wait_for(
                    self._conn.read(min(n, self.remaining)), min(self._deadline - now, util.scaled(1.0))
                )
            except asyncio.TimeoutError:
                continue
            except (ConnectionError, ssl.SSLError, OSError):
                raise ClientGone
            if not data:
                raise ClientGone
            self.remaining -= len(data)
            self._received += len(data)
            return data

    def _check_rate(self, now: float) -> None:
        """Abort when less than 8 KiB/s arrived since the first 10 s (SPEC 5.1)."""
        assert self._t0 is not None
        if now - self._t0 < util.scaled(BODY_GRACE_S):
            return
        if self._mark is None:
            self._mark = (now, self._received)
            return
        mark_t, mark_bytes = self._mark
        elapsed = now - mark_t
        if elapsed >= util.scaled(1.0):
            rate = (self._received - mark_bytes) / (elapsed / util.scaled(1.0))
            if rate < BODY_MIN_RATE:
                raise _too_slow()

    async def read_exact(self, n: int) -> bytes:
        parts: List[bytes] = []
        left = n
        while left > 0:
            chunk = await self.read(min(left, 65536))
            parts.append(chunk)
            left -= len(chunk)
        return b"".join(parts)


def _too_slow() -> HttpError:
    resp = error_response(408, "request_timeout", "the request body arrived too slowly")
    resp.close = True
    return HttpError(resp)


class _BodyIter:
    """``async for chunk in request.iter_body()``: the body in chunks, with the SPEC 5.1 timeouts."""

    def __init__(self, body: Optional[_Body], chunk_size: int) -> None:
        self._body = body
        self._size = chunk_size

    def __aiter__(self) -> "_BodyIter":
        return self

    async def __anext__(self) -> bytes:
        if self._body is None:
            raise StopAsyncIteration
        data = await self._body.read(self._size)
        if not data:
            raise StopAsyncIteration
        return data


class Request:
    """A parsed request (SPEC 6.2).  Header names are lower-case; ``path`` is percent-decoded exactly once.

    Beyond the contract: ``raw_headers`` (every header line, for duplicate detection), ``content_length``,
    ``version``, ``server`` (the :class:`HttpServer`) and :meth:`iter_body` for streaming uploads.
    """

    def __init__(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        headers: Dict[str, str],
        raw_headers: List[Tuple[str, str]],
        remote_addr: str,
        scheme: str,
        version: str,
        content_length: Optional[int],
        body: Optional[_Body],
        server: Optional["HttpServer"] = None,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.raw_headers = raw_headers
        self.remote_addr = remote_addr
        self.scheme = scheme
        self.version = version
        self.content_length = content_length
        self.host = ""
        self.session: Optional[dict] = None
        self.cookie_token: Optional[str] = None
        self.server = server
        self.drain_on_error = False  # set for routes with a large body: unread data is drained before an error reply
        self._body = body

    @property
    def body_unread(self) -> int:
        """Bytes of the body that were not consumed (> 0 forces ``Connection: close``)."""
        return self._body.remaining if self._body is not None else 0

    def iter_body(self, chunk_size: int = 65536) -> "_BodyIter":
        """Async iterator over the raw body (used by the upload handler)."""
        return _BodyIter(self._body, chunk_size)

    async def read_json(self, max_bytes: int = JSON_BODY_MAX) -> dict:
        """Read and parse a JSON object body.

        Raises :class:`HttpError`: ``415 unsupported_media_type`` (no ``application/json``), ``413 too_large``
        (declared length over ``max_bytes``), ``400 bad_request`` (empty, not UTF-8, not a JSON object, NaN, ...;
        ``reason:"invalid_text"`` when a string holds a lone surrogate, SPEC 7.1), ``408 request_timeout`` (slow body).
        """
        ctype = self.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(error_response(415, "unsupported_media_type", "Content-Type must be application/json"))
        if self.content_length is not None and self.content_length > max_bytes:
            raise HttpError(error_response(413, "too_large", "request body too large"))
        if not self.content_length or self._body is None:
            raise HttpError(error_response(400, "bad_request", "a JSON body is required"))
        raw = await self._body.read_exact(self.content_length)
        try:
            obj = util.json_loads_strict(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(error_response(400, "bad_request", "invalid JSON"))
        if not isinstance(obj, dict):
            raise HttpError(error_response(400, "bad_request", "a JSON object is required"))
        if util.has_lone_surrogate(obj):
            raise HttpError(error_response(400, "bad_request", "text is not valid", reason="invalid_text"))
        return obj


# --------------------------------------------------------------------------------------------------------------
# Origin / Host guard (SPEC 5.9)
# --------------------------------------------------------------------------------------------------------------


def prime_host_names() -> None:
    """Collect this machine's host name and FQDN (``getfqdn`` can be slow: call it once, off the event loop)."""
    names: Set[str] = set()
    for getter in (socket.gethostname, socket.getfqdn):
        try:
            name = getter().lower().rstrip(".")
        except OSError:
            continue
        if name:
            names.add(name)
    _MACHINE_NAMES.update(names)


def _split_authority(authority: str) -> Tuple[str, str]:
    """``"host:port"`` -> ``("host", "port")`` (port ``""`` when absent; IPv6 literals keep their brackets)."""
    if authority.startswith("["):
        end = authority.index("]")
        return authority[: end + 1], authority[end + 2:]
    host, _, port = authority.partition(":")
    return host, port


def _normalise_authority(authority: str, scheme: str) -> str:
    """Lower-case authority with the scheme's default port elided (80 for http, 443 for https)."""
    host, port = _split_authority(authority.lower())
    if port == ("443" if scheme == "https" else "80"):
        port = ""
    return host + (":" + port if port else "")


def host_allowed(name: str, extra: Any = ()) -> bool:
    """The default Host allow-list of SPEC 5.9(b) plus ``extra`` (the configured ``allowed_hosts``)."""
    name = name.rstrip(".")
    if name.startswith("["):
        try:
            ipaddress.IPv6Address(name[1:-1])
        except ValueError:
            return False
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return True
    return (
        name == "localhost"
        or "." not in name
        or name in extra
        or name in _MACHINE_NAMES
        or name.endswith(_LOCAL_SUFFIXES)
    )


def check_request_origin(req: Request, cfg: Config) -> Optional[Response]:
    """SPEC 5.9: returns the refusal :class:`Response` or ``None``; sets ``req.host`` to the validated Host.

    (a)/(b) Host: exactly one header, well formed, on the allow-list (400 / 421) - for EVERY request.
    (c)/(d) ``Sec-Fetch-Site`` or ``Origin`` - for non-GET requests, ``/ws``, and GET ``/api/*`` and ``/files/*``.
    (e) ``X-Requested-With: desktalk`` - for non-GET requests.  The server never emits CORS headers.
    """
    hosts = [value for name, value in req.raw_headers if name == "host"]
    if len(hosts) != 1:
        return error_response(400, "bad_request", "exactly one Host header is required")
    host = hosts[0].strip().lower()
    if not _HOST_RE.match(host):
        return error_response(400, "bad_request", "malformed Host header")
    name, port = _split_authority(host)
    if port and not 0 < int(port) <= 65535:
        return error_response(400, "bad_request", "malformed Host header")
    if not host_allowed(name, cfg.allowed_hosts):
        return error_response(421, "host_not_allowed", "this host name is not accepted by the server")
    req.host = host

    is_get = req.method in ("GET", "HEAD")
    path = req.path
    if not is_get or path == "/ws" or path.startswith(("/api/", "/files/")):
        fetch_site = req.headers.get("sec-fetch-site")
        if fetch_site is not None:
            site = fetch_site.strip().lower()
            if site != "same-origin" and not (site == "none" and is_get):
                return error_response(403, "forbidden", "cross-site request refused")
        else:
            origin = req.headers.get("origin")
            if origin is not None:
                match = _ORIGIN_RE.match(origin.strip().lower())
                if (
                    not match
                    or match.group(1) != req.scheme
                    or _normalise_authority(match.group(2), req.scheme) != _normalise_authority(host, req.scheme)
                ):
                    return error_response(403, "forbidden", "cross-origin request refused")
    if not is_get and req.headers.get("x-requested-with", "").strip().lower() != "desktalk":
        return error_response(403, "forbidden", "missing X-Requested-With header")
    return None


# --------------------------------------------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------------------------------------------

Handler = Callable[[Request], Awaitable[Response]]


class _Route:
    __slots__ = ("method", "path", "handler", "auth", "prefix", "pw_exempt", "max_body")

    def __init__(
        self, method: str, path: str, handler: Handler, auth: str, prefix: bool, pw_exempt: bool, max_body: int
    ) -> None:
        self.method, self.path, self.handler = method, path, handler
        self.auth, self.prefix, self.pw_exempt, self.max_body = auth, prefix, pw_exempt, max_body


_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), geolocation=(), payment=(), usb=(), microphone=(self)"),
)


def html_csp(host: str) -> str:
    """The ``Content-Security-Policy`` of SPEC 5.4; ``host`` must be the validated Host header."""
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
        "media-src 'self' blob:; connect-src 'self' ws://%s wss://%s; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'; require-trusted-types-for 'script'" % (host, host)
    )


class Router:
    """Routing plus the cross-cutting request pipeline (SPEC 5.2 / 6.2).

    ``dispatch`` runs, in this order: ``check_request_origin`` (every request), method / route resolution
    (404 / 405), the body-size cap (413, before authentication), the cookie session check (401, 403 for a forced
    password change) and the handler; the response then gets ``Set-Cookie`` when the session asks for a re-issue
    and the headers of SPEC 5.4.

    ``auth_module`` supplies ``authenticate``, ``parse_cookie``, ``cookie_header`` (SPEC 6.2 ``auth.py`` row); it
    defaults to ``chatd.auth`` imported on first use.
    """

    def __init__(self, cfg: Config, db: Any = None, auth_module: Any = None) -> None:
        self.cfg = cfg
        self.db = db
        self._auth = auth_module
        self._exact: Dict[Tuple[str, str], _Route] = {}
        self._exact_paths: Set[str] = set()
        self._prefixes: List[_Route] = []

    def add(
        self,
        method: str,
        path: str,
        handler: Handler,
        auth: str = "none",
        prefix: bool = False,
        pw_exempt: bool = False,
        max_body: int = JSON_BODY_MAX,
    ) -> None:
        """Register a route.  ``max_body`` (an optional extension) is the largest ``Content-Length`` accepted."""
        if auth not in ("none", "cookie"):
            raise ValueError("auth must be 'none' or 'cookie'")
        route = _Route(method.upper(), path, handler, auth, prefix, pw_exempt, max_body)
        if prefix:
            self._prefixes.append(route)
            self._prefixes.sort(key=lambda r: len(r.path), reverse=True)
        else:
            self._exact[(route.method, path)] = route
            self._exact_paths.add(path)

    def _auth_module(self) -> Any:
        if self._auth is None:
            self._auth = importlib.import_module(__package__ + ".auth")
        return self._auth

    def _lookup(self, method: str, path: str) -> Optional[_Route]:
        route = self._exact.get((method, path))
        if route is None and path not in self._exact_paths:  # an exact route of another method means 405
            for candidate in self._prefixes:
                if candidate.method == method and path.startswith(candidate.path):
                    return candidate
        return route

    def _resolve(self, method: str, path: str) -> Optional[_Route]:
        route = self._lookup(method, path)
        if route is None and method == "HEAD":
            route = self._lookup("GET", path)
        return route

    def _has_other_method(self, path: str) -> bool:
        if path in self._exact_paths:
            return True
        return any(r.path != "/" and path.startswith(r.path) for r in self._prefixes)

    async def dispatch(self, req: Request) -> Response:
        """Run the pipeline for ``req`` and return the finished response (never raises ``HttpError``)."""
        try:
            resp = await self._pipeline(req)
        except HttpError as exc:
            resp = exc.response
        return self.finalize(req, resp)

    async def _pipeline(self, req: Request) -> Response:
        denied = check_request_origin(req, self.cfg)
        if denied is not None:
            return denied
        if req.method not in ("GET", "HEAD", "POST"):
            resp = error_response(405, "method_not_allowed", "method not allowed")
            resp.add_header("Allow", "GET, HEAD, POST")
            return resp
        route = self._resolve(req.method, req.path)
        if route is None:
            if self._has_other_method(req.path):
                resp = error_response(405, "method_not_allowed", "method not allowed")
                resp.add_header("Allow", "GET, HEAD, POST")
                return resp
            return error_response(404, "not_found", "not found")
        req.drain_on_error = route.max_body > JSON_BODY_MAX
        if req.content_length is not None and req.content_length > route.max_body:
            resp = error_response(413, "too_large", "request body too large")
            resp.close = True
            return resp
        if route.auth == "cookie":
            denied = await self._authenticate(req, route)
            if denied is not None:
                return denied
        return await route.handler(req)

    async def _authenticate(self, req: Request, route: _Route) -> Optional[Response]:
        auth = self._auth_module()
        token = auth.parse_cookie(req.headers.get("cookie", ""))
        session = None
        if token:
            session = await auth.authenticate(self.db, token, req.remote_addr, req.headers.get("user-agent", "")[:200])
        if session is None:
            return error_response(401, "unauthorized", "sign in required")
        req.session = session
        req.cookie_token = token
        if session.get("must_change_password") and not route.pw_exempt:
            return error_response(403, "password_change_required", "change your password first")
        return None

    def finalize(self, req: Optional[Request], resp: Response) -> Response:
        """Add ``Set-Cookie`` (session re-issue), the security headers, CSP for HTML and the error text form."""
        path = req.path if req is not None else ""
        if resp.error is not None and req is not None and not path.startswith("/api/"):
            code, msg = resp.error
            resp.body = ("%s: %s\n" % (code, msg)).encode("utf-8")
            resp.set_header("Content-Type", "text/plain; charset=utf-8")
        if req is not None and req.session and req.session.get("reissue_cookie") and req.cookie_token:
            cookie = self._auth_module().cookie_header(
                req.cookie_token, self.cfg.tls, self.cfg.session_days * 86400
            )
            resp.add_header("Set-Cookie", cookie)
        for name, value in _SECURITY_HEADERS:
            if resp.header(name) is None:
                resp.add_header(name, value)
        ctype = resp.header("Content-Type")
        if ctype is not None:
            lowered = ctype.lower()
            if lowered.startswith(_TEXTUAL_TYPES) and "charset" not in lowered:
                resp.set_header("Content-Type", ctype + "; charset=utf-8")
            if lowered.startswith("text/html") and req is not None and req.host and resp.header(
                "Content-Security-Policy"
            ) is None:
                resp.add_header("Content-Security-Policy", html_csp(req.host))
        if path.startswith("/api/") and resp.header("Cache-Control") is None:
            resp.add_header("Cache-Control", "no-store")
        return resp


# --------------------------------------------------------------------------------------------------------------
# Static files (SPEC 5.3)
# --------------------------------------------------------------------------------------------------------------

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RESERVED_DEVICE_RE = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$", re.IGNORECASE)
_MIME_TABLE = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}
_REPARSE_POINT = 0x400


def static_segments(path: str) -> Optional[List[str]]:
    """Validate a decoded path against the allow-list: its segments (``index.html`` for ``/``) or ``None``."""
    if path == "/":
        return ["index.html"]
    parts = path.split("/")
    if parts[0] != "":
        return None
    segments = parts[1:]
    for seg in segments:
        if not _SEGMENT_RE.match(seg) or seg.endswith("."):
            return None
        if _RESERVED_DEVICE_RE.match(seg.split(".", 1)[0]):
            return None
    suffix = os.path.splitext(segments[-1])[1].lower()
    if suffix not in _MIME_TABLE:
        return None
    return segments


def _static_stat(root: Path, segments: List[str]) -> Optional[Tuple[Path, os.stat_result]]:
    """``lstat`` the target and require a regular, non-symlink, non-reparse file inside ``root`` (blocking)."""
    target = root.joinpath(*segments)
    try:
        st = os.lstat(str(target))
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
        return None
    if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        return None
    try:
        resolved_root = os.path.realpath(str(root))
        resolved = os.path.realpath(str(target))
        if os.path.commonpath([resolved_root, resolved]) != resolved_root:
            return None
    except (OSError, ValueError):
        return None
    return target, st


def make_static_handler(cfg: Config, web_root: Optional[Path] = None) -> Handler:
    """The ``GET /`` prefix handler serving ``<app>/web`` (or ``web_root``) through the allow-list of SPEC 5.3."""
    root = web_root if web_root is not None else cfg.web_dir

    async def static_handler(req: Request) -> Response:
        segments = static_segments(req.path)
        if segments is None:
            return error_response(404, "not_found", "not found")
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, _static_stat, root, segments)
        if found is None:
            return error_response(404, "not_found", "not found")
        target, st = found
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        headers = [("Cache-Control", "no-cache"), ("ETag", etag)]
        if files.etag_matches(req.headers.get("if-none-match"), etag):
            return Response(304, headers)
        opened = await loop.run_in_executor(None, files.open_for_read, target)
        if opened is None:
            return error_response(404, "not_found", "not found")
        fh, fst = opened
        if not stat.S_ISREG(fst.st_mode) or fst.st_size != st.st_size:
            fh.close()
            return error_response(404, "not_found", "not found")
        ctype = _MIME_TABLE[os.path.splitext(segments[-1])[1].lower()]
        headers += [("Content-Type", ctype), ("Content-Length", str(st.st_size))]
        return Response(200, headers, stream=files.FileStream(fh, 0, st.st_size))

    return static_handler




# --------------------------------------------------------------------------------------------------------------
# Uploads and downloads (SPEC 4.2, 5.1, 5.5, 5.6)
# --------------------------------------------------------------------------------------------------------------

_UNLIMITED_BODY = 1 << 62


def make_upload_handler(cfg: Config, database: Any) -> Handler:
    """``POST /api/upload``: raw body = file bytes; answers ``201 {attachment}`` (SPEC 4.2).

    Everything that can be refused is checked before the first body byte is read (SPEC 5.1): length, blocked
    extension, free disk, per-user quotas, concurrency caps.  The body is streamed to ``uploads/.tmp`` and moved
    into place; the row is inserted by ``db.insert_attachment`` which re-checks the quotas inside its transaction.
    """
    from . import db as dbmod  # imported here so that this module stays importable without sqlite3

    slots = files.UploadSlots()
    blocked = {str(e).lower().lstrip(".") for e in cfg.blocked_extensions}

    async def upload(req: Request) -> Response:
        assert req.session is not None
        user_id = req.session["user_id"]
        length = req.content_length
        if length is None:
            return error_response(411, "length_required", "Content-Length is required")
        if length > cfg.max_upload_bytes:
            return error_response(413, "too_large", "file is larger than %d MB" % cfg.max_upload_mb)
        try:
            name = files.sanitize_name(files.decode_header_name(req.headers.get("x-file-name")))
        except ValueError:
            return error_response(400, "bad_request", "the file name is not valid text", reason="invalid_text")
        if files.is_blocked(name, blocked):
            return error_response(400, "blocked_type", "this file type is not allowed")
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, files.disk_has_room, cfg.data_dir, length):
            return error_response(507, "insufficient_storage", "the server is out of disk space")
        usage = await database.run_read(dbmod.upload_usage, user_id)
        if (
            usage["unattached_bytes"] + length > files.MAX_UNATTACHED_BYTES
            or usage["recent_bytes"] + length > files.MAX_DAILY_BYTES
        ):
            return error_response(413, "quota_exceeded", "upload quota exceeded")
        if not slots.acquire(user_id, req.remote_addr):
            return error_response(429, "rate_limited", "too many uploads in progress", retry_after=1)
        stored: Optional[str] = None
        try:
            try:
                saved = await files.save_upload(req.iter_body(), cfg.tmp_dir, cfg.max_upload_bytes)
            except files.UploadTooLarge:
                resp = error_response(413, "too_large", "file is larger than %d MB" % cfg.max_upload_mb)
                resp.close = True
                return resp
            meta = files.parse_meta(req.headers.get("x-meta"))
            mime, kind, width, height, duration = files.classify(saved.head, meta)
            attachment_id = files.new_attachment_id()
            stored = await files.finalize_upload(saved, cfg.uploads_dir, attachment_id)
            row = {
                "id": attachment_id, "name": name, "mime": mime, "kind": kind, "size": saved.size, "path": stored,
                "width": width, "height": height, "duration": duration,
            }
            try:
                attachment = await database.run(
                    dbmod.insert_attachment, user_id, row, files.MAX_UNATTACHED_BYTES, files.MAX_DAILY_BYTES
                )
            except dbmod.RequestError as exc:
                await files.remove_stored(cfg.uploads_dir, stored)
                stored = None
                if exc.code == "quota_exceeded":
                    return error_response(413, "quota_exceeded", "upload quota exceeded")
                if exc.code == "unauthorized":
                    return error_response(401, "unauthorized", "sign in required")
                log.warning("upload rejected by the database: %s", exc.code)
                return error_response(400, "bad_request", "upload rejected")
            stored = None
            return json_response(201, {"attachment": attachment})
        finally:
            slots.release(user_id, req.remote_addr)
            if stored is not None:
                await asyncio.shield(files.remove_stored(cfg.uploads_dir, stored))

    return upload


def _file_headers(etag: str) -> List[Tuple[str, str]]:
    return [
        ("Cache-Control", "private, no-cache"),
        ("ETag", etag),
        ("Vary", "Cookie"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("Content-Security-Policy", files.FILE_CSP),
        ("X-Content-Type-Options", "nosniff"),
        ("Accept-Ranges", "bytes"),
    ]


def make_files_handler(cfg: Config, database: Any) -> Handler:
    """``GET /files/<id>``: access check, ``ETag``/304, ``Range``, forced-download rules and download caps."""
    from . import db as dbmod

    per_user = files.KeyedLimiter(files.DOWNLOAD_CAP_USER)
    per_ip = files.KeyedLimiter(files.DOWNLOAD_CAP_IP)

    async def acquire_slots(user_id: int, ip: str) -> bool:
        wait = util.scaled(files.DOWNLOAD_WAIT_S)
        started = time.monotonic()
        if not await per_user.acquire(user_id, wait):
            return False
        left = max(0.0, wait - (time.monotonic() - started))
        if not await per_ip.acquire(ip, left):
            per_user.release(user_id)
            return False
        return True

    async def serve_file(req: Request) -> Response:
        assert req.session is not None
        user_id = req.session["user_id"]
        file_id = req.path[len("/files/"):]
        if not files.ATTACHMENT_ID_RE.match(file_id):
            return error_response(404, "not_found", "not found")
        row = await database.run_read(dbmod.attachment_access, user_id, file_id)
        if row is None:
            return error_response(404, "not_found", "not found")
        etag = '"%s"' % file_id
        headers = _file_headers(etag)
        if files.etag_matches(req.headers.get("if-none-match"), etag):
            return Response(304, headers)
        path = files.stored_path(cfg.uploads_dir, row["path"])
        loop = asyncio.get_running_loop()
        opened = await loop.run_in_executor(None, files.open_for_read, path) if path is not None else None
        if opened is None:
            return error_response(404, "not_found", "not found")
        fh, st = opened
        size = st.st_size
        try:
            if_range = req.headers.get("if-range")
            wanted = req.headers.get("range") if if_range is None or if_range.strip() == etag else None
            try:
                span = files.parse_range(wanted, size)
            except files.RangeNotSatisfiable:
                fh.close()
                resp = error_response(416, "range_not_satisfiable", "range not satisfiable")
                resp.add_header("Content-Range", "bytes */%d" % size)
                return resp
            ctype, inline = files.served_type(row["mime"], req.query.get("dl") == "1")
            headers += [
                ("Content-Type", ctype),
                ("Content-Disposition", files.content_disposition(row["name"], inline)),
            ]
            start, end = span if span is not None else (0, size - 1)
            length = max(0, end - start + 1) if size else 0
            if span is not None:
                headers.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
            headers.append(("Content-Length", str(length)))
            capped = req.method == "GET" and length > files.DOWNLOAD_CAP_MIN_BYTES
            if capped and not await acquire_slots(user_id, req.remote_addr):
                fh.close()
                return error_response(429, "rate_limited", "too many downloads in progress", retry_after=1)
        except BaseException:
            fh.close()
            raise

        def release() -> None:
            per_user.release(user_id)
            per_ip.release(req.remote_addr)

        stream = files.FileStream(fh, start, length, release if capped else None)
        return Response(206 if span is not None else 200, headers, stream=stream)

    return serve_file


def register_http_routes(
    router: Router,
    database: Any,
    cfg: Config,
    stopping: Optional[Callable[[], bool]] = None,
    web_root: Optional[Path] = None,
) -> None:
    """Register ``GET``/``HEAD /healthz``, ``/api/upload``, ``/files/<id>`` and the static files (SPEC 4.2, 6.2).

    ``/ws`` and the REST handlers of ``api.py`` are registered by ``app.py`` and ``api.register_routes``.
    ``stopping`` (given by ``app.py``) reports that the shutdown began (``hub.stopping``): ``/healthz`` then answers
    ``503 unavailable`` with ``Retry-After: 5``.  ``/healthz`` never touches the database.
    """

    async def healthz(req: Request) -> Response:
        if (stopping is not None and stopping()) or (req.server is not None and req.server.stopping):
            return error_response(503, "unavailable", "the server is shutting down", retry_after=5)
        return Response(200, [("Content-Type", "text/plain")], b"ok")

    router.add("GET", "/healthz", healthz)
    router.add("POST", "/api/upload", make_upload_handler(cfg, database), auth="cookie", max_body=_UNLIMITED_BODY)
    router.add("GET", "/files/", make_files_handler(cfg, database), auth="cookie", prefix=True)
    router.add("GET", "/", make_static_handler(cfg, web_root), prefix=True)


# --------------------------------------------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------------------------------------------


class _HeadError(Exception):
    """A request head that must be answered with ``response`` (the request, if parseable, is attached)."""

    def __init__(self, response: Response, req: Optional[Request] = None) -> None:
        super().__init__(response.status)
        self.response = response
        self.req = req


def _peer_ip(peername: Any) -> str:
    """Remote IP as text: IPv4-mapped IPv6 addresses are unmapped and scope ids dropped."""
    if not peername:
        return "?"
    ip = str(peername[0]).split("%", 1)[0]
    return ip[7:] if ip.lower().startswith("::ffff:") and "." in ip else ip


def _date_header() -> str:
    return formatdate(time.time(), usegmt=True)


def _head_fail(status: int, code: str, msg: str) -> _HeadError:
    resp = error_response(status, code, msg)
    resp.close = True
    return _HeadError(resp)


class HttpServer:
    """The listening HTTP(S) server: accepts sockets, parses requests and writes responses.

    ``ws_registry`` is attached by the WebSocket layer on first use.  ``close_listeners`` stops accepting;
    ``abort_connections`` tears down every tracked socket (graceful shutdown, SPEC 2.4).
    """

    def __init__(self, cfg: Config, router: Router, ssl_context: Optional[ssl.SSLContext] = None) -> None:
        self.cfg = cfg
        self.router = router
        self.ssl_context = ssl_context
        self.scheme = "https" if ssl_context is not None else "http"
        self.ws_registry: Any = None
        self.stopping = False
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: Set[Connection] = set()
        self._tasks: Set["asyncio.Task[None]"] = set()
        self._per_ip: Dict[str, int] = {}

    # ---- lifecycle ---------------------------------------------------------------------------------------------

    async def start(self) -> int:
        """Bind and start accepting; returns the bound port (``port 0`` picks a free one)."""
        self._server = await asyncio.start_server(
            self._on_connect,
            host=self.cfg.host,
            port=self.cfg.port,
            backlog=1024,
            ssl=self.ssl_context,
            ssl_handshake_timeout=util.scaled(TLS_HANDSHAKE_TIMEOUT_S) if self.ssl_context is not None else None,
        )
        socks = self._server.sockets or []
        return socks[0].getsockname()[1] if socks else self.cfg.port

    def close_listeners(self) -> None:
        if self._server is not None:
            self._server.close()

    async def wait_closed(self, timeout: float = 3.0) -> None:
        """Wait for the listening sockets to finish closing - bounded, never an unguarded ``wait_closed``."""
        if self._server is None:
            return
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout)
        except asyncio.TimeoutError:
            log.warning("listener did not finish closing within %.0f s", timeout)

    @property
    def connection_count(self) -> int:
        return len(self._conns)

    async def wait_connections(self, timeout: float = 3.0) -> None:
        """Wait (bounded) until every connection handler task has finished; cancel the stragglers."""
        tasks = list(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def abort_connections(self, include_ws: bool = True) -> None:
        """Abort every tracked socket immediately (``include_ws=False`` leaves upgraded sockets to their owner)."""
        for conn in list(self._conns):
            if include_ws or not conn.is_ws:
                transport = conn.writer.transport
                if transport is not None:
                    transport.abort()

    # ---- connection handling -----------------------------------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        ip = _peer_ip(writer.get_extra_info("peername"))
        total_cap = self.cfg.limit("sockets_total", MAX_SOCKETS)
        ip_cap = self.cfg.limit("sockets_per_ip", MAX_SOCKETS_PER_IP)
        conn = Connection(reader, writer, ip, self.scheme)
        try:
            if len(self._conns) >= total_cap or self._per_ip.get(ip, 0) >= ip_cap:
                await self._refuse(conn)
                return
            self._conns.add(conn)
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            try:
                await self._serve(conn)
            finally:
                self._conns.discard(conn)
                left = self._per_ip.get(ip, 1) - 1
                if left > 0:
                    self._per_ip[ip] = left
                else:
                    self._per_ip.pop(ip, None)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, ClientGone, asyncio.IncompleteReadError, ssl.SSLError, OSError) as exc:
            log.debug("connection from %s ended: %s", ip, type(exc).__name__)
        except Exception:  # noqa: BLE001 - last line of defence for one connection; logged with traceback
            log.exception("unexpected error on a connection from %s", ip)
        finally:
            await self._close(conn)
            if task is not None:
                self._tasks.discard(task)

    async def _refuse(self, conn: Connection) -> None:
        """Answer a connection over the socket caps with an immediate 503 and drop it."""
        body = b'{"error":{"code":"unavailable","msg":"too many connections"}}'
        head = (
            "HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
            "Retry-After: 1\r\nConnection: close\r\nX-Content-Type-Options: nosniff\r\n\r\n" % len(body)
        )
        try:
            conn.writer.write(head.encode("ascii") + body)
            await asyncio.wait_for(conn.writer.drain(), util.scaled(CLOSE_TIMEOUT_S))
        except (ConnectionError, OSError, asyncio.TimeoutError):
            log.debug("could not send the 503 refusal")

    async def _close(self, conn: Connection) -> None:
        writer = conn.writer
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), util.scaled(CLOSE_TIMEOUT_S))
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            transport = writer.transport
            if transport is not None:
                transport.abort()

    async def _serve(self, conn: Connection) -> None:
        while not self.stopping:
            try:
                head = await self._read_head(conn)
                if head is None:
                    return
                req = self._parse(conn, head)
            except _HeadError as err:
                await self._send(conn, err.req, err.response, keep_alive=False)
                return
            resp = await self._run_handler(req)
            if not await self._respond(conn, req, resp):
                return

    # ---- reading the request head --------------------------------------------------------------------------------

    async def _read_head(self, conn: Connection) -> Optional[bytes]:
        loop = asyncio.get_running_loop()
        buf = conn.buf
        deadline: Optional[float] = None
        while True:
            while buf.startswith(b"\r\n"):
                del buf[:2]
            idx = buf.find(b"\r\n\r\n")
            if _BARE_LF.search(buf, 0, idx if idx >= 0 else len(buf)):
                raise _head_fail(400, "bad_request", "bare line feed in the request head")
            if idx >= 0:
                if idx + 4 > MAX_HEAD_BYTES:
                    raise _head_fail(431, "header_fields_too_large", "request head too large")
                head = bytes(buf[:idx])
                del buf[: idx + 4]
                return head
            if len(buf) >= MAX_HEAD_BYTES:
                raise _head_fail(431, "header_fields_too_large", "request head too large")
            if buf and deadline is None:
                deadline = loop.time() + util.scaled(HEADER_TOTAL_TIMEOUT_S)
            timeout = util.scaled(IDLE_TIMEOUT_S) if deadline is None else deadline - loop.time()
            if timeout <= 0:
                raise _head_fail(408, "request_timeout", "the request head arrived too slowly")
            try:
                chunk = await asyncio.wait_for(conn.reader.read(HEAD_READ_SIZE), timeout)
            except asyncio.TimeoutError:
                if deadline is None:
                    return None
                raise _head_fail(408, "request_timeout", "the request head arrived too slowly")
            if not chunk:
                return None
            if deadline is None and chunk.strip(b"\r\n"):
                deadline = loop.time() + util.scaled(HEADER_TOTAL_TIMEOUT_S)
            buf += chunk

    def _parse(self, conn: Connection, head: bytes) -> Request:
        """Strictly parse a request head (SPEC 5.1) into a :class:`Request`; raises :class:`_HeadError`."""
        text = head.decode("latin-1")
        lines = text.split("\r\n")
        if any(("\r" in line or "\n" in line or "\x00" in line) for line in lines):
            raise _head_fail(400, "bad_request", "invalid control character in the request head")
        request_line = lines[0]
        if request_line == "PRI * HTTP/2.0":
            raise _head_fail(400, "bad_request", "HTTP/2 is not supported")
        parts = request_line.split(" ")
        if len(parts) != 3:
            raise _head_fail(400, "bad_request", "malformed request line")
        method, target, version = parts
        if not _METHOD_RE.match(method):
            raise _head_fail(400, "bad_request", "malformed request line")
        if not _VERSION_RE.match(version):
            raise _head_fail(400, "bad_request", "malformed request line")
        if version not in ("HTTP/1.0", "HTTP/1.1"):
            raise _head_fail(505, "version_not_supported", "only HTTP/1.0 and HTTP/1.1 are supported")
        if not target.startswith("/") or len(target) > MAX_TARGET_CHARS or not target.isascii():
            raise _head_fail(400, "bad_request", "unsupported request target")
        raw_path, _, raw_query = target.partition("?")
        try:
            path = unquote(raw_path, encoding="utf-8", errors="strict")
            query = dict(reversed(parse_qsl(raw_query, keep_blank_values=True, max_num_fields=100)))
        except (UnicodeDecodeError, ValueError):
            raise _head_fail(400, "bad_request", "malformed request target")
        if _FORBIDDEN_HEADER_CHARS.search(path):
            raise _head_fail(400, "bad_request", "malformed request target")

        raw_headers: List[Tuple[str, str]] = []
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if len(raw_headers) >= MAX_HEADERS:
                raise _head_fail(431, "header_fields_too_large", "too many header fields")
            name, sep, value = line.partition(":")
            if not sep or not _TOKEN_RE.match(name):
                raise _head_fail(400, "bad_request", "malformed header field")
            value = value.strip(" \t")
            if _FORBIDDEN_HEADER_CHARS.search(value):
                raise _head_fail(400, "bad_request", "malformed header field")
            lname = name.lower()
            raw_headers.append((lname, value))
            if lname in headers:
                if lname in ("content-length", "host", "transfer-encoding"):
                    if lname == "content-length":
                        raise _head_fail(400, "bad_request", "duplicate Content-Length")
                    continue
                headers[lname] += ("; " if lname == "cookie" else ", ") + value
            else:
                headers[lname] = value

        content_length: Optional[int] = None
        if "content-length" in headers:
            if not _CONTENT_LENGTH_RE.match(headers["content-length"]):
                raise _head_fail(400, "bad_request", "invalid Content-Length")
            content_length = int(headers["content-length"])
        expect = headers.get("expect")
        if expect is not None and expect.lower() != "100-continue":
            raise _head_fail(400, "bad_request", "unsupported Expect header")
        body = _Body(conn, content_length, expect is not None) if content_length else None
        req = Request(
            method, path, query, headers, raw_headers, conn.remote_addr, self.scheme, version.split("/", 1)[1],
            content_length, body, self,
        )
        if "transfer-encoding" in headers:
            refusal = error_response(501, "not_implemented", "Transfer-Encoding is not supported")
            raise _HeadError(_closing(refusal), req)
        return req

    # ---- running a request -------------------------------------------------------------------------------------

    async def _run_handler(self, req: Request) -> Response:
        if self.stopping and req.path != "/healthz":
            resp = error_response(500, "server_error", "the server is restarting")
            resp.close = True
            return self.router.finalize(req, resp)
        try:
            return await self.router.dispatch(req)
        except (asyncio.CancelledError, ClientGone):
            raise
        except Exception:  # noqa: BLE001 - a handler bug must become a 500, never kill the connection loop
            log.exception("handler failed for %s %s", req.method, util.safe_log_value(req.path))
            return self.router.finalize(req, error_response(500, "server_error", "internal error"))

    async def _respond(self, conn: Connection, req: Request, resp: Response) -> bool:
        """Write ``resp``; ``True`` when the connection may serve another request."""
        keep_alive = (
            req.version == "1.1"
            and "close" not in req.headers.get("connection", "").lower()
            and not resp.close
            and not self.stopping
            and req.body_unread == 0
            and (resp.stream is None or resp.header("Content-Length") is not None)
        )
        if req.body_unread and req.drain_on_error and resp.status >= 400:
            await self._drain(conn, req)
        if resp.upgrade is not None:
            conn.is_ws = True
            await self._send(conn, req, resp, keep_alive=True)
            await resp.upgrade(conn, conn.writer)
            return False
        await self._send(conn, req, resp, keep_alive=keep_alive)
        return keep_alive

    async def _drain(self, conn: Connection, req: Request) -> None:
        """Swallow up to 1 MiB / 2 s of an unread upload body so the client sees the error status."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + util.scaled(DRAIN_MAX_S)
        left = min(req.body_unread, DRAIN_MAX_BYTES)
        while left > 0:
            timeout = deadline - loop.time()
            if timeout <= 0:
                return
            try:
                data = await asyncio.wait_for(conn.read(min(65536, left)), timeout)
            except (asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError):
                return
            if not data:
                return
            left -= len(data)

    async def _send(self, conn: Connection, req: Optional[Request], resp: Response, keep_alive: bool) -> None:
        """Serialise ``resp`` onto the socket (head, then body or stream) honouring HEAD and bodiless statuses."""
        if resp.header("X-Content-Type-Options") is None:
            self.router.finalize(req, resp)
        try:
            head_only = req is not None and req.method == "HEAD"
            no_body = head_only or resp.status in (204, 304) or 100 <= resp.status < 200
            lines = ["HTTP/1.1 %d %s" % (resp.status, _REASONS.get(resp.status, "Status"))]
            dropped = ("content-length", "date") + (() if resp.upgrade is not None else ("connection",))
            headers = [(k, v) for k, v in resp.headers if k.lower() not in dropped]
            if resp.body is not None:
                headers.append(("Content-Length", str(len(resp.body))))
            elif resp.stream is not None and resp.header("Content-Length") is not None:
                headers.append(("Content-Length", resp.header("Content-Length") or "0"))
            elif resp.status not in (204, 304) and resp.upgrade is None:
                headers.append(("Content-Length", "0"))
            headers.append(("Date", _date_header()))
            if resp.upgrade is None:
                headers.append(("Connection", "keep-alive" if keep_alive else "close"))
            for key, value in headers:
                lines.append("%s: %s" % (key, value))
            data = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")
            if resp.body is not None and not no_body and len(resp.body) <= 65536:
                data += resp.body
                await self._write(conn, data)
            else:
                await self._write(conn, data)
                if resp.body is not None and not no_body:
                    await self._write(conn, resp.body)
                elif resp.stream is not None and not no_body:
                    async for chunk in resp.stream:
                        await self._write(conn, chunk)
            log.debug(
                "%s %s -> %d", req.method if req else "-", util.safe_log_value(req.path if req else "-"), resp.status
            )
        finally:
            closer = getattr(resp.stream, "aclose", None)
            if closer is not None:
                await closer()

    async def _write(self, conn: Connection, data: bytes) -> None:
        conn.writer.write(data)
        try:
            await asyncio.wait_for(conn.writer.drain(), util.scaled(WRITE_TIMEOUT_S))
        except asyncio.TimeoutError:
            raise ConnectionError("write timed out")


def _closing(resp: Response) -> Response:
    resp.close = True
    return resp


# --------------------------------------------------------------------------------------------------------------
# Redirect listener (SPEC 5.7 --redirect-port)
# --------------------------------------------------------------------------------------------------------------


class RedirectServer:
    """A tiny plain-HTTP listener that answers every request with ``301 https://<host>:<tls_port>/``.

    It never serves the application, sets no cookies and reads at most one small request head per connection.
    """

    def __init__(self, host: str, port: int, tls_port: int) -> None:
        self._host, self._port, self._tls_port = host, port, tls_port
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: Set["asyncio.Task[None]"] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, host=self._host, port=self._port, backlog=128)
        socks = self._server.sockets or []
        return socks[0].getsockname()[1] if socks else self._port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 3)
            except asyncio.TimeoutError:
                log.warning("redirect listener did not finish closing")
        for task in list(self._tasks):
            task.cancel()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), util.scaled(5.0))
            writer.write(self._answer(raw))
            await asyncio.wait_for(writer.drain(), util.scaled(5.0))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            log.debug("redirect connection dropped")
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), util.scaled(CLOSE_TIMEOUT_S))
            except (OSError, asyncio.TimeoutError):
                if writer.transport is not None:
                    writer.transport.abort()
            if task is not None:
                self._tasks.discard(task)

    def _answer(self, raw: bytes) -> bytes:
        hosts = [
            line.split(b":", 1)[1].strip().decode("latin-1").lower()
            for line in raw.split(b"\r\n")[1:]
            if line[:5].lower() == b"host:"
        ]
        match = _HOST_RE.match(hosts[0]) if len(hosts) == 1 else None
        if match is None:
            body = b"bad request\n"
            return b"HTTP/1.1 400 Bad Request\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)
        name = _split_authority(hosts[0])[0]
        authority = name if self._tls_port == 443 else "%s:%d" % (name, self._tls_port)
        location = "https://%s/" % authority
        return (
            "HTTP/1.1 301 Moved Permanently\r\nLocation: %s\r\nContent-Length: 0\r\nConnection: close\r\n"
            "Cache-Control: no-store\r\n\r\n" % location
        ).encode("ascii")
