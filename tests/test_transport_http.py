"""HTTP layer: strict framing, timeouts, caps, origin guard, static allow-list, headers (SPEC 5.1 - 5.4, 5.9)."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict

try:  # `unittest discover -s tests -t .` imports this module as part of the `tests` package
    from . import test_transport_support as support
except ImportError:  # `unittest discover -s tests` or running from inside tests/
    import test_transport_support as support

from chatd import util
from chatd.http import Request, Response, Router, error_response, json_response, make_static_handler


def _routes(web_root: str) -> Callable[[Router, support.Harness], None]:
    def register(router: Router, harness: support.Harness) -> None:
        async def healthz(req: Request) -> Response:
            return Response(200, [("Content-Type", "text/plain")], b"ok")

        async def echo_json(req: Request) -> Response:
            return json_response(200, {"got": await req.read_json()})

        async def whoami(req: Request) -> Response:
            return json_response(200, {"user": (req.session or {}).get("user_id"), "host": req.host})

        async def boom(req: Request) -> Response:
            raise RuntimeError("secret detail")

        async def err(req: Request) -> Response:
            return error_response(429, "rate_limited", "slow down", retry_after=2.2)

        async def noop(req: Request) -> Response:
            return Response(204)

        router.add("GET", "/healthz", healthz)
        router.add("POST", "/api/echo", echo_json)
        router.add("GET", "/api/whoami", whoami, auth="cookie")
        router.add("GET", "/api/me", whoami, auth="cookie", pw_exempt=True)
        router.add("GET", "/boom", boom)
        router.add("GET", "/api/err", err)
        router.add("POST", "/api/noop", noop)
        router.add("GET", "/", make_static_handler(harness.cfg, Path(web_root)), prefix=True)

    return register


class HttpBase(unittest.TestCase):
    scale = 1.0

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="dt-http-")
        cls.web = os.path.join(cls.tmp, "web")
        files = {
            "index.html": b"<!doctype html><title>x</title>",
            "css/base.css": b"body{}",
            "js/app.js": b"export const a = 1;",
            "img/icon.png": b"\x89PNG\r\n\x1a\n",
            "README.md": b"# no",
            "js/app.js.map": b"{}",
            "manifest.webmanifest": b"{}",
            ".git/config": b"[core]",
        }
        for rel, data in files.items():
            path = os.path.join(cls.web, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def config(self) -> Dict[str, Any]:
        return {}

    def setUp(self) -> None:
        util.set_test_scale(self.scale)
        self.addCleanup(support.reset_scale)
        self.auth = support.FakeAuth(must_change={7}, reissue={2})
        self.cfg = support.make_config(os.path.join(self.tmp, "data"), **self.config())
        self.h = support.Harness(self.cfg, _routes(self.web), self.auth).start()
        self.addCleanup(self.h.stop)

    def req(self, raw: bytes) -> Any:
        return support.exchange(self.h.port, raw)


class FramingTests(HttpBase):
    def test_healthz_and_default_headers(self) -> None:
        status, headers, body = support.get(self.h.port, "/healthz")
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")
        self.assertIn("microphone=(self)", headers["permissions-policy"])
        self.assertNotIn("server", headers)
        self.assertIn("date", headers)
        self.assertFalse([h for h in headers if h.startswith("access-control")])

    def test_duplicate_and_odd_content_length(self) -> None:
        base = b"POST /api/noop HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
        for cl in (
            b"Content-Length: 1_0\r\n",
            b"Content-Length: +5\r\n",
            b"Content-Length: 5 5\r\n",
            b"Content-Length: -1\r\n",
            b"Content-Length: 1234567890123\r\n",
            b"Content-Length: \r\n",
            b"Content-Length: 0x5\r\n",
        ):
            result = self.req(base + cl + b"\r\n12345")
            self.assertEqual(result[0], 400, cl)
        result = self.req(base + b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
        self.assertEqual(result[0], 400)

    def test_transfer_encoding_is_501_with_code(self) -> None:
        raw = (
            b"POST /api/noop HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        )
        status, headers, body = self.req(raw)
        self.assertEqual(status, 501)
        self.assertIn(b"not_implemented", body)
        self.assertEqual(headers["connection"], "close")

    def test_malformed_heads_are_400(self) -> None:
        cases = {
            "obs-fold": b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-A: b\r\n c\r\n\r\n",
            "name trailing space": b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-A : b\r\n\r\n",
            "bare LF": b"GET /healthz HTTP/1.1\nHost: 127.0.0.1\n\n",
            "NUL in header": b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-A: b\x00c\r\n\r\n",
            "bare CR in header": b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-A: b\rc\r\n\r\n",
            "absolute form": b"GET http://127.0.0.1/healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "asterisk": b"GET * HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "h2 preface": b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n",
            "lower-case method": b"get /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "two spaces": b"GET  /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "no colon": b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nGarbage\r\n\r\n",
            "bad percent": b"GET /%ff HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "encoded NUL": b"GET /a%00b HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "no host": b"GET /healthz HTTP/1.1\r\n\r\n",
            "two hosts": b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nHost: 127.0.0.1\r\n\r\n",
            "malformed host": b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:99999x\r\n\r\n",
            "host port too big": b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:70000\r\n\r\n",
        }
        for name, raw in cases.items():
            result = self.req(raw)
            self.assertIsNotNone(result, name)
            self.assertEqual(result[0], 400, name)

    def test_http_version_505(self) -> None:
        for version in (b"HTTP/2.0", b"HTTP/0.9", b"HTTP/1.2", b"HTTP/3.0"):
            status, _, body = self.req(b"GET /healthz " + version + b"\r\nHost: 127.0.0.1\r\n\r\n")
            self.assertEqual(status, 505, version)
            self.assertIn(b"version_not_supported", body)

    def test_header_limits_431(self) -> None:
        big = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Big: " + b"a" * 17000 + b"\r\n\r\n"
        self.assertEqual(self.req(big)[0], 431)
        many = (
            b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n" + b"".join(b"X-%d: v\r\n" % i for i in range(120)) + b"\r\n"
        )
        self.assertEqual(self.req(many)[0], 431)
        ok = (
            b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
            + b"".join(b"X-%d: v\r\n" % i for i in range(90))
            + b"\r\n"
        )
        self.assertEqual(self.req(ok)[0], 200)

    def test_methods(self) -> None:
        for method in (b"OPTIONS", b"PUT", b"DELETE", b"TRACE", b"PATCH"):
            raw = method + b" /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n\r\n"
            status, headers, body = self.req(raw)
            self.assertEqual(status, 405, method)
            self.assertIn(b"method_not_allowed", body)
            self.assertNotIn("access-control-allow-methods", headers)
        raw = b"POST /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\nContent-Length: 0\r\n\r\n"
        self.assertEqual(self.req(raw)[0], 405)

    def test_head_has_length_but_no_body(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(
            b"HEAD /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        first, second = data.split(b"HTTP/1.1 200 OK")[1:]
        self.assertIn(b"Content-Length: 2", first)
        self.assertFalse(first.rstrip().endswith(b"ok"))
        self.assertTrue(second.endswith(b"ok"))

    def test_keep_alive_serves_several_requests(self) -> None:
        sock = support.connect(self.h.port)
        for _ in range(3):
            sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            status, headers, body = support.read_response(sock)
            self.assertEqual((status, body), (200, b"ok"))
            self.assertEqual(headers["connection"], "keep-alive")
        sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        self.assertEqual(support.read_response(sock)[1]["connection"], "close")
        self.assertTrue(support.closed_by_peer(sock))
        sock.close()

    def test_pipelined_requests_are_answered_in_order(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(
            b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\nGET /api/err HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        )
        self.assertEqual(support.read_response(sock)[0], 200)
        self.assertEqual(support.read_response(sock)[0], 429)
        sock.close()

    def test_http10_closes(self) -> None:
        status, headers, _ = self.req(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertEqual((status, headers["connection"]), (200, "close"))

    def test_error_body_forms(self) -> None:
        status, headers, body = support.get(self.h.port, "/api/err")
        self.assertEqual(status, 429)
        self.assertEqual(headers["retry-after"], "3")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn('"code":"rate_limited"', body.decode())
        self.assertIn('"retry_after":2.2', body.decode())
        status, headers, body = support.get(self.h.port, "/nonexistent.css")
        self.assertEqual(status, 404)
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        status, headers, body = support.get(self.h.port, "/api/unknown")
        self.assertEqual(status, 404)
        self.assertTrue(headers["content-type"].startswith("application/json"))

    def test_handler_exception_is_generic_500(self) -> None:
        status, _, body = support.get(self.h.port, "/boom")
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)
        self.assertIn(b"server_error", body)
        self.assertEqual(support.get(self.h.port, "/healthz")[0], 200)


class BodyTests(HttpBase):
    def post(self, body: bytes, ctype: bytes = b"application/json") -> Any:
        raw = (
            b"POST /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\nContent-Type: "
            + ctype
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        return self.req(raw)

    def test_json_roundtrip_and_errors(self) -> None:
        status, _, body = self.post(b'{"a":1}')
        self.assertEqual((status, body), (200, b'{"got":{"a":1}}'))
        self.assertEqual(self.post(b'{"a":1}', b"text/plain")[0], 415)
        self.assertEqual(self.post(b'{"a":1}', b"application/json; charset=utf-8")[0], 200)
        for bad in (b"[]", b"nope", b'{"a":NaN}', b"\xff\xfe", b'{"a":1,"a":2}', b"123"):
            self.assertEqual(self.post(bad)[0], 400, bad)
        self.assertEqual(self.post(b"")[0], 400)

    def test_lone_surrogate_in_json_is_invalid_text(self) -> None:
        status, _, body = self.post(b'{"a":"\\ud800"}')
        self.assertEqual(status, 400)
        self.assertIn(b'"reason":"invalid_text"', body)
        self.assertEqual(self.post(b'{"a":"\\ud83d\\ude00"}')[0], 200)  # a valid pair is fine

    def test_oversize_json_is_413_before_reading_and_closes(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(
            b"POST /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
            b"Content-Type: application/json\r\nContent-Length: 65537\r\n\r\n"
        )
        status, headers, body = support.read_response(sock)
        self.assertEqual(status, 413)
        self.assertEqual(headers["connection"], "close")
        self.assertIn(b"too_large", body)
        self.assertTrue(support.closed_by_peer(sock))
        sock.close()

    def test_413_comes_before_authentication(self) -> None:
        raw = (
            b"POST /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
            b"Content-Length: 999999\r\n\r\n"
        )
        self.assertEqual(self.req(raw)[0], 413)

    def test_unread_body_forces_close(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(
            b"POST /api/noop HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
            b"Content-Length: 5\r\n\r\nabcde"
        )
        status, headers, _ = support.read_response(sock)
        self.assertEqual(status, 204)
        self.assertEqual(headers["connection"], "close")
        self.assertTrue(support.closed_by_peer(sock))
        sock.close()

    def test_expect_continue(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(
            b"POST /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
            b"Content-Type: application/json\r\nExpect: 100-continue\r\nContent-Length: 2\r\n\r\n"
        )
        self.assertTrue(sock.recv(64).startswith(b"HTTP/1.1 100 Continue"))
        sock.sendall(b"{}")
        self.assertEqual(support.read_response(sock)[0], 200)
        sock.close()
        raw = (
            b"POST /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
            b"Expect: nope\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertEqual(self.req(raw)[0], 400)


class OriginTests(HttpBase):
    def test_host_allow_list(self) -> None:
        cases = (
            (b"127.0.0.1:8765", 200),
            (b"localhost", 200),
            (b"[::1]:8765", 200),
            (b"172.31.4.5", 200),
            (b"pc050607", 200),
            (b"chat.local", 200),
            (b"chat.lan:80", 200),
            (b"x.internal", 200),
            (b"x.home.arpa", 200),
            (b"x.corp", 200),
            (b"LOCALHOST", 200),
            (b"evil.example.com", 421),
            (b"attacker.com:8765", 421),
            (b"999.1.1.1", 421),
        )
        for host, expected in cases:
            status, _, body = support.get(self.h.port, "/healthz", host=b"Host: " + host + b"\r\n")
            self.assertEqual(status, expected, host)
            if expected == 421:
                self.assertIn(b"host_not_allowed", body)

    def test_allowed_hosts_option_extends_list(self) -> None:
        self.assertEqual(support.get(self.h.port, "/healthz", host=b"Host: chat.example.com\r\n")[0], 421)
        self.h.cfg.allowed_hosts.append("chat.example.com")
        self.assertEqual(support.get(self.h.port, "/healthz", host=b"Host: chat.example.com\r\n")[0], 200)

    def test_sec_fetch_and_origin_matrix(self) -> None:
        def api(extra: bytes) -> int:
            raw = (
                b"GET /api/whoami HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nCookie: fc_session=tok-1\r\n"
                b"Connection: close\r\n" + extra + b"\r\n"
            )
            return self.req(raw)[0]

        self.assertEqual(api(b""), 200)
        self.assertEqual(api(b"Sec-Fetch-Site: same-origin\r\n"), 200)
        self.assertEqual(api(b"Sec-Fetch-Site: none\r\n"), 200)
        self.assertEqual(api(b"Sec-Fetch-Site: same-site\r\n"), 403)
        self.assertEqual(api(b"Sec-Fetch-Site: cross-site\r\n"), 403)
        self.assertEqual(api(b"Origin: http://127.0.0.1:8765\r\n"), 200)
        self.assertEqual(api(b"Origin: http://127.0.0.1:8766\r\n"), 403)
        self.assertEqual(api(b"Origin: https://127.0.0.1:8765\r\n"), 403)
        self.assertEqual(api(b"Origin: null\r\n"), 403)
        self.assertEqual(api(b"Origin: http://evil.com\r\n"), 403)
        self.assertEqual(api(b"Origin: HTTP://127.0.0.1:8765\r\n"), 200)
        self.assertEqual(api(b"Sec-Fetch-Site: same-origin\r\nOrigin: http://evil.com\r\n"), 200)

    def test_default_ports_are_elided_in_origin_comparison(self) -> None:
        def api(host: bytes, origin: bytes) -> int:
            raw = (
                b"GET /api/whoami HTTP/1.1\r\nHost: "
                + host
                + b"\r\nCookie: fc_session=tok-1\r\nOrigin: "
                + origin
                + b"\r\nConnection: close\r\n\r\n"
            )
            return self.req(raw)[0]

        self.assertEqual(api(b"localhost", b"http://localhost:80"), 200)
        self.assertEqual(api(b"localhost:80", b"http://localhost"), 200)
        self.assertEqual(api(b"localhost", b"http://localhost:8080"), 403)

    def test_static_navigation_is_never_blocked_by_fetch_metadata(self) -> None:
        for extra in (b"Sec-Fetch-Site: cross-site\r\n", b"Origin: http://evil.com\r\n", b"Sec-Fetch-Site: none\r\n"):
            self.assertEqual(support.get(self.h.port, "/css/base.css", extra=extra)[0], 200, extra)
        extra = b"Sec-Fetch-Site: cross-site\r\nCookie: fc_session=tok-1\r\n"
        self.assertEqual(support.get(self.h.port, "/api/whoami", extra=extra)[0], 403)

    def test_x_requested_with_required_for_post(self) -> None:
        raw = b"POST /api/noop HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n"
        self.assertEqual(self.req(raw)[0], 403)
        raw = b"POST /api/noop HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: other\r\nContent-Length: 0\r\n\r\n"
        self.assertEqual(self.req(raw)[0], 403)
        raw = b"POST /api/noop HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\nContent-Length: 0\r\n\r\n"
        self.assertEqual(self.req(raw)[0], 204)


class SessionTests(HttpBase):
    def test_cookie_routes(self) -> None:
        self.assertEqual(support.get(self.h.port, "/api/whoami")[0], 401)
        self.assertEqual(support.get(self.h.port, "/api/whoami", extra=b"Cookie: fc_session=tok-bad\r\n")[0], 401)
        status, _, body = support.get(self.h.port, "/api/whoami", extra=b"Cookie: fc_session=tok-1\r\n")
        self.assertEqual((status, body), (200, b'{"user":1,"host":"127.0.0.1"}'))

    def test_forced_password_change(self) -> None:
        status, _, body = support.get(self.h.port, "/api/whoami", extra=b"Cookie: fc_session=tok-7\r\n")
        self.assertEqual(status, 403)
        self.assertIn(b"password_change_required", body)
        self.assertEqual(support.get(self.h.port, "/api/me", extra=b"Cookie: fc_session=tok-7\r\n")[0], 200)
        self.assertEqual(support.get(self.h.port, "/healthz", extra=b"Cookie: fc_session=tok-7\r\n")[0], 200)

    def test_cookie_reissue(self) -> None:
        _, headers, _ = support.get(self.h.port, "/api/whoami", extra=b"Cookie: fc_session=tok-2\r\n")
        self.assertIn("fc_session=tok-2", headers["set-cookie"])
        self.assertIn("Max-Age=%d" % (30 * 86400), headers["set-cookie"])
        _, headers, _ = support.get(self.h.port, "/api/whoami", extra=b"Cookie: fc_session=tok-1\r\n")
        self.assertNotIn("set-cookie", headers)


class StaticTests(HttpBase):
    def test_serves_allowed_files_with_types(self) -> None:
        cases = {
            "/": "text/html",
            "/index.html": "text/html",
            "/css/base.css": "text/css",
            "/js/app.js": "text/javascript",
            "/img/icon.png": "image/png",
            "/manifest.webmanifest": "application/manifest+json",
        }
        for path, ctype in cases.items():
            status, headers, body = support.get(self.h.port, path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["content-type"].startswith(ctype), (path, headers["content-type"]))
            self.assertEqual(headers["cache-control"], "no-cache")
            self.assertTrue(body)
        self.assertIn("charset=utf-8", support.get(self.h.port, "/js/app.js")[1]["content-type"])

    def test_html_has_csp_built_from_validated_host(self) -> None:
        _, headers, _ = support.get(self.h.port, "/", host=b"Host: 172.31.1.5:8765\r\n")
        csp = headers["content-security-policy"]
        self.assertIn("connect-src 'self' ws://172.31.1.5:8765 wss://172.31.1.5:8765;", csp)
        self.assertIn("require-trusted-types-for 'script'", csp)
        self.assertIn("script-src 'self'", csp)
        _, headers, _ = support.get(self.h.port, "/js/app.js")
        self.assertNotIn("content-security-policy", headers)

    def test_traversal_and_aliases_are_404(self) -> None:
        paths = (
            "/css/base.css.",
            "/css/base.css::$DATA",
            "/css/CON",
            "/css/con.css",
            "/css/NUL.css",
            "/js/%5c..%5cchatd%5cdb.py",
            "/.git/config",
            "/README.md",
            "/js/app.js.map",
            "/css/",
            "/css",
            "/css/../js/app.js",
            "/%2e%2e/chatd/db.py",
            "/js/..%2fchatd%2fdb.py",
            "/css//base.css",
            "//css/base.css",
            "/css/base.css%20",
            "/css/base.css~",
            "/css/.hidden.css",
            "/img/icon.png.",
            "/css/COM1.css",
            "/css/lpt9",
            "/index.html.",
            "/%43ON",
            "/css/base.css/",
            "/css/base.css/extra",
            "/..",
            "/./index.html",
            "/css/base.css::$DATA.css",
        )
        for path in paths:
            self.assertEqual(support.get(self.h.port, path)[0], 404, path)

    def test_etag_and_304(self) -> None:
        _, headers, _ = support.get(self.h.port, "/css/base.css")
        etag = headers["etag"]
        status, headers, body = support.get(
            self.h.port, "/css/base.css", extra=b"If-None-Match: " + etag.encode() + b"\r\n"
        )
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(headers["etag"], etag)
        self.assertEqual(support.get(self.h.port, "/css/base.css", extra=b'If-None-Match: "other"\r\n')[0], 200)

    @unittest.skipIf(os.name == "nt", "creating symlinks needs privileges on Windows")
    def test_symlink_is_not_followed(self) -> None:
        outside = os.path.join(self.tmp, "secret.css")
        with open(outside, "wb") as fh:
            fh.write(b"secret")
        link = os.path.join(self.web, "css", "link.css")
        os.symlink(outside, link)
        self.addCleanup(os.remove, link)
        self.assertEqual(support.get(self.h.port, "/css/link.css")[0], 404)


class CapTests(HttpBase):
    def config(self) -> Dict[str, Any]:
        return {"test_limits": {"sockets_per_ip": 3, "sockets_total": 10}}

    def test_per_ip_socket_cap_503(self) -> None:
        socks = [support.connect(self.h.port) for _ in range(3)]
        try:
            for s in socks:
                s.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                self.assertEqual(support.read_response(s)[0], 200)
            extra = support.connect(self.h.port)
            status, headers, body = support.read_response(extra)
            self.assertEqual(status, 503)
            self.assertIn(b"unavailable", body)
            self.assertEqual(headers["retry-after"], "1")
            self.assertTrue(support.closed_by_peer(extra))
            extra.close()
        finally:
            for s in socks:
                s.close()
        self.assertTrue(support.wait_until(lambda: self.h.server.connection_count == 0))
        self.assertEqual(support.get(self.h.port, "/healthz")[0], 200)


class TimeoutTests(HttpBase):
    scale = 0.05  # header total 0.75 s, idle 1.5 s, body grace 0.5 s, minimum body time 1.5 s

    def test_idle_keep_alive_is_closed_silently(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertEqual(support.read_response(sock)[0], 200)
        start = time.monotonic()
        self.assertTrue(support.closed_by_peer(sock, 5))
        self.assertLess(time.monotonic() - start, 4)
        sock.close()

    def test_stalled_header_block_is_answered_408_and_closed(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        start = time.monotonic()
        status, headers, body = support.read_response(sock)
        self.assertEqual((status, headers["connection"]), (408, "close"))
        self.assertIn(b"request_timeout", body)
        self.assertLess(time.monotonic() - start, 4)
        self.assertTrue(support.closed_by_peer(sock, 3))
        sock.close()

    def test_dripping_header_block_is_cut_off(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        for _ in range(8):  # one byte every 0.2 s never completes the head
            time.sleep(0.2)
            try:
                sock.sendall(b"X")
            except OSError:
                break
        result = support.read_response(sock)
        self.assertTrue(result is None or result[0] == 408)
        self.assertTrue(support.closed_by_peer(sock, 3))
        sock.close()

    def test_slow_body_is_answered_408_and_closed(self) -> None:
        sock = support.connect(self.h.port)
        sock.sendall(
            b"POST /api/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
            b"Content-Type: application/json\r\nContent-Length: 60000\r\n\r\n{"
        )
        start = time.monotonic()
        status, headers, body = support.read_response(sock)
        self.assertEqual((status, headers["connection"]), (408, "close"))
        self.assertIn(b"request_timeout", body)
        self.assertLess(time.monotonic() - start, 4)
        sock.close()


if __name__ == "__main__":
    unittest.main()
