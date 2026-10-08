"""Self-tests of ``tests/wsclient.py`` and ``tests/harness.py`` (the shared infrastructure of the protocol suites).

The client is exercised through the REAL transport (``chatd.http`` + ``chatd.websocket``) against a tiny stub hub that
speaks just enough of SPEC 7 (``ev.ready`` first, events before ``res``, ``id:null`` answers to malformed frames, a kick
followed by ``4001``).  The harness is booted with the real ``app.Server`` (stub hub too); the flows that need the real
hub and REST handlers skip with a message until ``chatd/hub.py`` and ``chatd/api.py`` exist.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Set

try:  # `unittest discover -s tests -t .` imports this module as part of the `tests` package
    from . import harness, wsclient
except ImportError:  # `unittest discover -s tests` or running from inside tests/
    import harness
    import wsclient

from chatd import util, websocket
from chatd.config import Config
from chatd.http import HttpServer, Request, Response, Router, error_response, json_response

PASSWORD = "pw-1234567"
USERS = {"alice": 1, "bob": 2, "carol": 3}


class StubAuth:
    """``auth`` stand-in: cookie ``fc_session=tok-<id>`` is user ``<id>``."""

    def parse_cookie(self, header: str) -> Optional[str]:
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fc_session" and value:
                return value
        return None

    async def authenticate(self, db: Any, token: str, ip: str, user_agent: str) -> Optional[dict]:
        if not token.startswith("tok-") or not token[4:].isdigit():
            return None
        uid = int(token[4:])
        return {"user_id": uid, "token_hash": "h%d" % uid, "ip": ip, "user_agent": user_agent,
                "must_change_password": False, "reissue_cookie": False}

    def cookie_header(self, token: str, secure: bool, max_age: int) -> str:
        return "fc_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (token, max_age)

    def clear_cookie_header(self, secure: bool) -> str:
        return "fc_session=; Path=/; Max-Age=0"


class StubHub:
    """The protocol of the self-test: ``ping``, ``echo {n}``, ``fail``, ``sleep {s}``, ``kick``, ``shout {text}``,
    ``note`` (fire and forget).  Anything else with an ``id`` is ``bad_request``."""

    def __init__(self) -> None:
        self.stopping = False
        self.sockets: List[Any] = []
        self.noted: List[dict] = []
        self.finished = 0
        self.counters: Dict[str, int] = {}
        self._tasks: Set["asyncio.Task[None]"] = set()

    async def serve(self, ws: Any, session: dict) -> None:
        self.sockets.append(ws)
        uid = session["user_id"]
        self._emit(ws, "ev.ready", {"protocol": 1, "me": {"id": uid, "username": "user%d" % uid}})
        try:
            while True:
                text = await ws.recv()
                if text is None:
                    break
                await self._handle(ws, text)
        finally:
            self.finished += 1

    @staticmethod
    def _emit(ws: Any, name: str, d: dict) -> None:
        ws.send_text(json.dumps({"t": name, "d": d}))

    @staticmethod
    def _res(ws: Any, rid: Any, ok: bool = True, d: Optional[dict] = None, err: Optional[dict] = None) -> None:
        frame: Dict[str, Any] = {"t": "res", "id": rid, "ok": ok}
        frame.update({"d": d or {}} if ok else {"err": err})
        ws.send_text(json.dumps(frame))

    async def _handle(self, ws: Any, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            frame = None
        if not isinstance(frame, dict) or not isinstance(frame.get("t"), str):
            self._res(ws, None, False, err={"code": "bad_request", "msg": "malformed frame"})
            return
        kind, rid, d = frame["t"], frame.get("id"), frame.get("d") or {}
        if kind == "note":
            self.noted.append(d)
        elif kind == "ping":
            self._res(ws, rid, d={"now": time.time()})
        elif kind == "echo":
            for index in range(int(d.get("n", 1))):
                self._emit(ws, "ev.echo", {"i": index})
            self._res(ws, rid, d={"n": d.get("n", 1)})
        elif kind == "fail":
            self._res(ws, rid, False, err={"code": "forbidden", "msg": "no"})
        elif kind == "sleep":
            task = asyncio.get_running_loop().create_task(self._sleep(ws, rid, float(d.get("s", 0.1))))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "kick":
            self._emit(ws, "ev.kicked", {"reason": "revoked"})
            ws.close(4001, "kicked")
        elif kind == "shout":
            for other in self.sockets:
                self._emit(other, "ev.shout", {"text": d.get("text")})
            self._res(ws, rid)
        elif rid is not None:
            self._res(ws, rid, False, err={"code": "bad_request", "msg": "unknown type"})

    async def _sleep(self, ws: Any, rid: Any, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._res(ws, rid, d={"slept": seconds})

    # ---- the rest of the hub API that ``app.Server`` calls (SPEC 6.1) ------------------------------------------

    async def start(self) -> None:
        return None

    def sweep_typing(self) -> None:
        return None

    async def revalidate_all(self) -> None:
        return None

    async def external_change(self) -> None:
        return None

    def online_user_ids(self) -> List[int]:
        return sorted({s.session["user_id"] for s in self.sockets if not s.closed})

    async def shutdown(self) -> None:
        self.stopping = True


class StubTransport:
    """The real HTTP + WebSocket transport on 127.0.0.1:0 with the stub hub and a few stub REST routes."""

    def __init__(self, test_limits: Optional[Dict[str, Any]] = None) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dtk-wsclient-")
        self.cfg = Config(host="127.0.0.1", port=0, data_dir=self.tmp, scrypt_n=1024,
                          test_limits=test_limits or {})
        self.hub = StubHub()
        self.auth = StubAuth()
        self.router = Router(self.cfg, None, auth_module=self.auth)
        self._add_routes()
        self.http = HttpServer(self.cfg, self.router)
        self.http.ws_registry = websocket.WsRegistry(self.cfg)
        self.port = 0
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stub-transport", daemon=True)

    def _add_routes(self) -> None:
        auth, hub = self.auth, self.hub

        async def sign_in(req: Request) -> Response:
            body = await req.read_json()
            uid = USERS.get(str(body.get("username")))
            if uid is None or body.get("password") != PASSWORD:
                return error_response(401, "bad_credentials", "wrong user or password")
            resp = json_response(200 if req.path == "/api/login" else 201, {"me": {"id": uid}})
            resp.add_header("Set-Cookie", auth.cookie_header("tok-%d" % uid, False, 3600))
            return resp

        async def sign_out(req: Request) -> Response:
            resp = Response(204, [])
            resp.add_header("Set-Cookie", auth.clear_cookie_header(False))
            return resp

        async def echo(req: Request) -> Response:
            size = 0
            async for chunk in req.iter_body():
                size += len(chunk)
            return json_response(200, {"headers": dict(req.headers), "body_bytes": size, "method": req.method})

        self.router.add("POST", "/api/login", sign_in)
        self.router.add("POST", "/api/register", sign_in)
        self.router.add("POST", "/api/logout", sign_out, auth="cookie")
        self.router.add("POST", "/api/echo", echo)
        self.router.add("POST", "/api/upload", echo)
        self.router.add("GET", "/ws", lambda req: websocket.upgrade_response(req, hub), auth="cookie")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            loop.close()

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        self.port = await self.http.start()
        self._ready.set()
        await self._stop.wait()
        self.http.close_listeners()
        await self.http.ws_registry.close_all(1001, "restart", 2.0)
        self.http.abort_connections()
        await self.http.wait_connections(3)
        await self.http.wait_closed(2)

    def start(self) -> "StubTransport":
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("the stub transport did not start")
        return self

    def stop(self) -> None:
        if self.loop is not None and self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, func: Any, timeout: float = 5.0) -> Any:
        """Run a plain function on the server loop and return its result."""
        assert self.loop is not None
        done: "asyncio.Future[Any]" = asyncio.run_coroutine_threadsafe(_wrap(func), self.loop)  # type: ignore[assignment]
        return done.result(timeout)


async def _wrap(func: Any) -> Any:
    return func()


class TransportCase(unittest.TestCase):
    """One stub transport per test; sessions made with :meth:`session` are aborted afterwards."""

    test_limits: Dict[str, Any] = {}

    def setUp(self) -> None:
        self.transport = StubTransport(self.test_limits).start()
        self.addCleanup(self.transport.stop)
        self.addCleanup(util.set_test_scale, 1.0)
        self._sessions: List[wsclient.ChatSession] = []
        self.addCleanup(self._abort_all)

    def _abort_all(self) -> None:
        for session in self._sessions:
            session.abort()

    def session(self, user: str = "alice", connect: bool = True) -> wsclient.ChatSession:
        s = wsclient.ChatSession(self.transport.port, name=user)
        self._sessions.append(s)
        response = s.login(user, PASSWORD)
        self.assertEqual(response.status, 200, response)
        return s.connect() if connect else s

    @property
    def hub(self) -> StubHub:
        return self.transport.hub


class FrameEncodingTests(unittest.TestCase):
    """``encode_frame`` and friends need no server."""

    def test_masking_round_trips_and_is_symmetric(self) -> None:
        key = b"\x01\x02\x03\x04"
        for size in (0, 1, 3, 4, 5, 125, 1000):
            data = bytes(range(256)) * 4
            data = data[:size]
            self.assertEqual(wsclient.mask_bytes(wsclient.mask_bytes(data, key), key), data)
        self.assertEqual(wsclient.mask_bytes(b"\x00\x00\x00\x00\x00", key), b"\x01\x02\x03\x04\x01")

    def test_length_forms_and_mask_bit(self) -> None:
        small = wsclient.encode_frame(wsclient.OP_TEXT, b"hi", key=b"abcd")
        self.assertEqual(small[:2], bytes((0x81, 0x80 | 2)))
        self.assertEqual(small[2:6], b"abcd")
        medium = wsclient.encode_frame(wsclient.OP_BINARY, b"x" * 300, key=b"abcd")
        self.assertEqual(medium[:4], bytes((0x82, 0x80 | 126, 300 >> 8, 300 & 255)))
        large = wsclient.encode_frame(wsclient.OP_BINARY, b"x" * 70000, key=b"abcd")
        self.assertEqual(large[1], 0x80 | 127)
        self.assertEqual(int.from_bytes(large[2:10], "big"), 70000)
        plain = wsclient.encode_frame(wsclient.OP_TEXT, b"hi", masked=False)
        self.assertEqual(plain, bytes((0x81, 2)) + b"hi")

    def test_misbehaviours(self) -> None:
        self.assertEqual(wsclient.encode_frame(1, b"", rsv=4, masked=False)[0], 0x80 | 0x40 | 1)
        non_minimal = wsclient.encode_frame(1, b"hi", masked=False, length_form="16")
        self.assertEqual(non_minimal, bytes((0x81, 126, 0, 2)) + b"hi")
        lie = wsclient.encode_frame(1, b"", masked=False, length_form="64", declared_length=1 << 63)
        self.assertEqual(int.from_bytes(lie[2:10], "big"), 1 << 63)
        with self.assertRaises(ValueError):
            wsclient.encode_frame(1, b"x" * 200, length_form="7")
        with self.assertRaises(ValueError):
            wsclient.encode_frame(1, b"", length_form="9")

    def test_fragments(self) -> None:
        frames = wsclient.fragment_frames(wsclient.OP_TEXT, b"abcdefghij", [3, 4], masked=False)
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0], bytes((0x01, 3)) + b"abc")  # TEXT, FIN=0
        self.assertEqual(frames[1], bytes((0x00, 4)) + b"defg")  # CONT, FIN=0
        self.assertEqual(frames[2], bytes((0x80, 3)) + b"hij")  # CONT, FIN=1
        self.assertEqual(wsclient.fragment_frames(wsclient.OP_TEXT, b"x", [], masked=False), [bytes((0x81, 1)) + b"x"])


class HttpSideTests(TransportCase):
    def test_login_keeps_the_cookie_and_sends_csrf_header(self) -> None:
        s = wsclient.ChatSession(self.transport.port)
        bad = s.login("alice", "wrong")
        self.assertEqual((bad.status, bad.code), (401, "bad_credentials"))
        self.assertIsNone(s.token)
        ok = s.login("alice", PASSWORD)
        self.assertEqual(ok.status, 200)
        self.assertEqual(s.token, "tok-1")
        self.assertEqual(s.me, {"id": 1})
        self.assertEqual((s.username, s.password), ("alice", PASSWORD))
        echoed = s.post("/api/echo", {"x": 1}).json
        self.assertEqual(echoed["headers"]["x-requested-with"], "desktalk")
        self.assertEqual(echoed["headers"]["cookie"], "fc_session=tok-1")
        self.assertEqual(echoed["headers"]["content-type"], "application/json")
        self.assertEqual(echoed["body_bytes"], len('{"x": 1}'))

    def test_header_overrides_and_removal(self) -> None:
        s = self.session("bob", connect=False)
        echoed = s.post("/api/echo", {}, headers={"X-Extra": "1", "Host": "localhost:9"})
        self.assertEqual(echoed.status, 200, echoed)
        self.assertEqual(echoed.json["headers"]["x-extra"], "1")
        self.assertEqual(echoed.json["headers"]["host"], "localhost:9")
        missing = s.post("/api/echo", {}, headers={"X-Requested-With": None})
        self.assertEqual((missing.status, missing.code), (403, "forbidden"))
        self.assertEqual(s.post("/api/echo", {}, headers={"Host": None}).status, 400)

    def test_logout_clears_the_cookie(self) -> None:
        s = self.session("carol", connect=False)
        self.assertEqual(s.logout().status, 204)
        self.assertIsNone(s.token)

    def test_upload_headers(self) -> None:
        s = self.session("alice", connect=False)
        response = s.upload("résumé 100%.txt", b"hello", meta={"width": 3})
        self.assertEqual(response.status, 200, response)
        headers = response.json["headers"]
        self.assertEqual(headers["x-file-name"], "r%C3%A9sum%C3%A9%20100%25.txt")
        self.assertEqual(json.loads(headers["x-meta"]), {"width": 3})
        self.assertEqual(response.json["body_bytes"], 5)
        self.assertEqual(headers["content-type"], "application/octet-stream")

    def test_clone_shares_the_identity(self) -> None:
        s = self.session("alice", connect=False)
        other = s.clone("tab 2")
        self.assertEqual((other.token, other.username), ("tok-1", "alice"))
        other.connect()
        self._sessions.append(other)
        self.assertEqual(other.user_id, 1)


class ProtocolTests(TransportCase):
    def test_ev_ready_is_first_and_seq_counts_from_zero(self) -> None:
        s = self.session("alice")
        first_seq, first = s.events[0]
        self.assertEqual((first_seq, first["t"]), (0, "ev.ready"))
        self.assertEqual(first.seq, 0)
        self.assertEqual(s.ready["me"]["username"], "user1")
        self.assertEqual(s.user_id, 1)
        self.assertEqual(s.mark(), 1)

    def test_request_returns_res_and_never_raises_on_error(self) -> None:
        s = self.session()
        ok = s.request("ping", {})
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["t"], "res")
        failed = s.request("fail")
        self.assertEqual((failed["ok"], failed["err"]["code"]), (False, "forbidden"))
        unknown = s.request("nonsense", {"a": 1})
        self.assertEqual(unknown["err"]["code"], "bad_request")

    def test_events_precede_res_and_are_ordered_by_seq(self) -> None:
        s = self.session()
        since = s.mark()
        res = s.request("echo", {"n": 3})
        echoes = s.events_matching("echo", since=since)
        self.assertEqual([f["d"]["i"] for f in echoes], [0, 1, 2])
        self.assertTrue(all(f.seq < res.seq for f in echoes), "events must precede res (SPEC 7.6(4))")
        self.assertEqual(res.seq, echoes[-1].seq + 1)
        self.assertEqual([seq for seq, _ in s.events if seq >= since], [f.seq for f in echoes])

    def test_wait_event_with_predicate_since_and_no_consumption(self) -> None:
        s = self.session()
        s.request("echo", {"n": 4})
        two = s.wait_event("ev.echo", lambda f: f["d"]["i"] == 2)
        self.assertEqual(two["d"], {"i": 2})
        self.assertIs(s.wait_event("echo", lambda f: f["d"]["i"] == 2), two)  # still there: nothing is consumed
        later = s.mark()
        s.request("echo", {"n": 1})
        fresh = s.wait_event("ev.echo", since=later)
        self.assertGreaterEqual(fresh.seq, later)
        with self.assertRaises(wsclient.ClientTimeout) as ctx:
            s.wait_event("ev.never", timeout=0.2)
        self.assertIn("ev.never", str(ctx.exception))

    def test_expect_none_is_a_barrier(self) -> None:
        s, other = self.session("alice"), self.session("bob")
        since = s.mark()
        other.request("shout", {"text": "hi"})
        with self.assertRaises(AssertionError):
            s.expect_none("ev.shout", since=since)  # the shout reached alice: the assertion must fail
        s.expect_none("ev.shout", since=s.mark())  # nothing since now
        s.expect_none("ev.shout", lambda f: f["d"]["text"] == "other text", since=since)  # predicate filters

    def test_fire_and_forget_has_no_res(self) -> None:
        s = self.session()
        since = s.mark()
        s.send("note", {"a": 1})
        s.request("ping")
        self.assertEqual(self.hub.noted, [{"a": 1}])
        self.assertEqual([f for f in s.responses if f.seq >= since and f["id"] is None], [])
        self.assertEqual(len([f for f in s.responses if f.seq >= since]), 1)  # only the ping answer

    def test_malformed_frames_get_an_id_null_res(self) -> None:
        s = self.session()
        s.raw("this is not json")
        res = s.wait_res(None)
        self.assertEqual((res["ok"], res["id"], res["err"]["code"]), (False, None, "bad_request"))
        s.raw('["array"]')
        s.raw('{"no_t": 1}')
        s.request("ping")  # barrier: the stub answers in order
        self.assertEqual(len([f for f in s.responses if f["id"] is None]), 3)

    def test_request_nowait_and_wait_res_allow_concurrency(self) -> None:
        s = self.session()
        slow = s.request_nowait("sleep", {"s": 0.3})
        fast = s.request("ping")
        self.assertTrue(fast["ok"])
        self.assertLess(fast.seq, s.mark())
        self.assertNotIn(slow, [f["id"] for f in s.responses])
        self.assertEqual(s.wait_res(slow)["d"]["slept"], 0.3)

    def test_fragmented_request_with_interleaved_ping(self) -> None:
        s = self.session()
        text = json.dumps({"t": "ping", "id": "frag-1", "d": {}})
        assert s.ws is not None
        s.ws.send_fragments(text, [4, 6, 3], ping_between=True)
        self.assertTrue(s.wait_res("frag-1")["ok"])
        self.assertEqual(s.ws.pongs, [b"between"] * 3)

    def test_ping_pong_and_server_frame_rules(self) -> None:
        s = self.session()
        assert s.ws is not None
        s.ws.ping(b"hello")
        self.assertEqual(s.ws.wait_pong(b"hello"), b"hello")
        s.request("echo", {"n": 2})
        self.assertEqual(s.ws.violations, [], "the server must send unmasked, minimal, unreserved frames")

    def test_two_sessions_see_each_others_broadcast(self) -> None:
        alice, bob, carol = self.session("alice"), self.session("bob"), self.session("carol")
        since = carol.mark()
        alice.request("shout", {"text": "hello"})
        self.assertEqual(bob.wait_event("shout")["d"]["text"], "hello")
        self.assertEqual(carol.wait_event("shout", since=since)["d"]["text"], "hello")


class FramingMisbehaviourTests(TransportCase):
    """The negative-test knobs really reach the server (its answers are the transport suite's business)."""

    def _expect_close(self, send: Any, code: int) -> None:
        s = self.session()
        assert s.ws is not None
        send(s)
        self.assertEqual(s.wait_closed(), code)
        self.assertEqual(s.close_code, code)

    def test_unmasked_frame_closes_1002(self) -> None:
        self._expect_close(lambda s: s.ws.send_text('{"t":"ping"}', masked=False), 1002)

    def test_reserved_bits_close_1002(self) -> None:
        self._expect_close(lambda s: s.ws.send_text('{"t":"ping"}', rsv=4), 1002)

    def test_non_minimal_length_closes_1002(self) -> None:
        self._expect_close(lambda s: s.ws.send_text('{"t":"ping"}', length_form="64"), 1002)

    def test_bad_utf8_closes_1007(self) -> None:
        self._expect_close(lambda s: s.ws.send_frame(wsclient.OP_TEXT, b'{"t":"\xff\xfe"}'), 1007)

    def test_binary_closes_1003(self) -> None:
        self._expect_close(lambda s: s.ws.send_binary(b"\x00\x01"), 1003)

    def test_oversize_message_closes_1009(self) -> None:
        self._expect_close(lambda s: s.raw("x" * (256 * 1024 + 1), ignore_errors=True), 1009)

    def test_lying_length_closes_1009_before_any_payload(self) -> None:
        self._expect_close(
            lambda s: s.raw(wsclient.encode_frame(wsclient.OP_TEXT, b"", length_form="64", declared_length=1 << 63),
                            ignore_errors=True),
            1009,
        )

    def test_raw_bytes_can_be_anything(self) -> None:
        s = self.session()
        s.raw(wsclient.encode_frame(wsclient.OP_TEXT, b'{"t":"ping","id":"raw-1"}'))
        self.assertTrue(s.wait_res("raw-1")["ok"])


class CloseAndHandshakeTests(TransportCase):
    def test_graceful_close_handshake(self) -> None:
        s = self.session()
        s.close()
        self.assertEqual(s.close_code, 1000)
        self.assertTrue(s.ws is not None and s.ws.closed)
        deadline = time.monotonic() + 3
        while self.hub.finished < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.hub.finished, 1)

    def test_kicked_precedes_its_close_code(self) -> None:
        s = self.session()
        s.send("kick")
        self.assertEqual(s.wait_closed(), 4001)
        kicked = s.wait_event("ev.kicked")
        self.assertEqual(kicked["d"]["reason"], "revoked")
        self.assertIsNotNone(s.close_seq)
        self.assertLess(kicked.seq, s.close_seq)
        self.assertEqual(s.ws.close_reason, "kicked")
        with self.assertRaises(wsclient.ConnectionClosedError):
            s.request("ping", timeout=1)

    def test_abort_without_close_frame_looks_like_1006(self) -> None:
        s = self.session()
        s.abort()
        self.assertEqual(s.wait_closed(), 1006)
        deadline = time.monotonic() + 3
        while self.hub.finished < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.hub.finished, 1)

    def test_handshake_refusals_carry_status_and_code(self) -> None:
        anonymous = wsclient.ChatSession(self.transport.port)
        with self.assertRaises(wsclient.HandshakeError) as ctx:
            anonymous.connect()
        self.assertEqual(ctx.exception.status, 401)
        alice = self.session(connect=False)
        with self.assertRaises(wsclient.HandshakeError) as ctx:
            alice.connect(origin="http://evil.example")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(wsclient.HandshakeError) as ctx:
            alice.connect(headers={"Sec-WebSocket-Version": "12"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(wsclient.HandshakeError) as ctx:
            alice.connect(headers={"Upgrade": None})
        self.assertEqual(ctx.exception.status, 400)
        alice.connect(origin="http://127.0.0.1:%d" % self.transport.port)  # a matching Origin is fine
        self.assertEqual(alice.ready["me"]["id"], 1)

    def test_reconnect_keeps_counting(self) -> None:
        s = self.session()
        s.abort()
        s.wait_closed()
        mark = s.mark()
        s.connect()
        ready = s.wait_event("ev.ready", since=mark)
        self.assertGreaterEqual(ready.seq, mark)
        self.assertEqual(len(s.events_matching("ev.ready")), 2)
        self.assertEqual(s.ready, ready["d"])


class LivenessSwitchTests(TransportCase):
    """``auto_pong`` is the switch of the SPEC 6 liveness tests (ping every 25 s, silent for 60 s => 1001)."""

    def test_auto_pong_keeps_an_idle_connection_alive(self) -> None:
        s = self.session(connect=False)
        util.set_test_scale(0.02)  # ping every 0.5 s, liveness limit 1.2 s
        s.connect()
        time.sleep(2.0)
        self.assertEqual(self.transport.call(self.transport.http.ws_registry.check_liveness), 0)
        self.assertTrue(s.request("ping")["ok"])
        assert s.ws is not None
        self.assertGreaterEqual(len(s.ws.pings), 2)

    def test_without_auto_pong_the_server_closes_1001(self) -> None:
        s = self.session(connect=False)
        s.auto_pong = False
        util.set_test_scale(0.02)
        s.connect()
        assert s.ws is not None
        time.sleep(2.0)
        self.assertGreaterEqual(len(s.ws.pings), 2)
        self.assertEqual(s.ws.pongs, [])
        self.assertEqual(self.transport.call(self.transport.http.ws_registry.check_liveness), 1)
        self.assertEqual(s.wait_closed(), 1001)
        s.auto_pong = True  # the setter works on a closed session too
        self.assertTrue(s.ws.auto_pong)


class HarnessBootTests(unittest.TestCase):
    """The harness with a stub hub: boot, config recipe, restart and cleanup (needs ``sqlite3``)."""

    def setUp(self) -> None:
        reason = harness.stack_problem(custom_hub=True)
        if reason is not None:
            self.skipTest(reason)

    @staticmethod
    def _routes(router: Router, hub: Any, database: Any, cfg: Config) -> None:
        async def info(req: Request) -> Response:
            return json_response(200, {"name": cfg.workspace_name})

        router.add("GET", "/api/info", info)

    def make(self, **kwargs: Any) -> harness.ServerHarness:
        h = harness.ServerHarness(hub_factory=lambda db, cfg: StubHub(), routes_factory=self._routes, **kwargs)
        self.addCleanup(h.stop)
        return h

    def test_config_recipe_and_default_limits(self) -> None:
        data = os.path.join(tempfile.mkdtemp(prefix="dtk-cfg-"), "data")
        self.addCleanup(shutil.rmtree, os.path.dirname(data), True)
        cfg = harness.build_config(
            data, {"msg.send": [3, 1.0], "login_a": None}, test_scale=0.1, settings={"max_users": 7}
        )
        self.assertEqual((cfg.test_scale, cfg.max_users, cfg.host, cfg.scrypt_n), (0.1, 7, "127.0.0.1", 1024))
        self.assertEqual(cfg.test_limits["msg.send"], [3, 1.0])
        self.assertEqual(cfg.test_limits["*"], [100000, 1])
        self.assertNotIn("login_a", cfg.test_limits)  # None restores the production default of that key
        self.assertEqual(cfg.test_limits["ws_handshakes_per_ip_min"], 100000)
        self.assertEqual(cfg.warnings, [])

    def test_boot_serve_and_clean_stop(self) -> None:
        h = self.make()
        h.start()
        data = h.data_dir
        self.assertTrue(os.path.isdir(data))
        self.assertGreater(h.port, 0)
        anon = h.anonymous()
        self.assertEqual(anon.get("/healthz").text, "ok")
        self.assertEqual(anon.get("/api/info").json, {"name": "DeskTalk Test"})
        port = h.port
        h.stop()
        h.stop()  # idempotent
        self.assertFalse(os.path.exists(os.path.dirname(data)))
        self.assertEqual(h.leaked_threads, [])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_scale_is_reset_after_stop(self) -> None:
        h = self.make(test_scale=0.05)
        h.start()
        self.assertAlmostEqual(util.scaled(1.0), 0.05)
        h.stop()
        self.assertEqual(util.scaled(1.0), 1.0)

    def test_restart_keeps_the_data_dir(self) -> None:
        h = self.make()
        h.start()
        marker = os.path.join(h.data_dir, "marker.txt")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("kept")
        h.restart()
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(h.anonymous().get("/healthz").status, 200)
        self.assertGreater(h.port, 0)

    def test_stop_without_start_and_skip_reason(self) -> None:
        harness.ServerHarness().stop()  # never started: nothing to do, nothing raised
        self.assertIsNone(harness.stack_problem(custom_hub=True))


class SetupFlowTests(harness.ServerTestCase):
    """The first admin through the real setup-code flow (skipped until ``chatd/hub.py`` and ``chatd/api.py`` exist)."""

    def test_first_admin_through_the_setup_code_flow(self) -> None:
        h = self.harness
        self.assertTrue(h.anonymous().get("/api/info").json["needs_setup"])
        admin = h.create_admin()
        self.assertEqual(admin.ready["me"]["role"], "admin")
        self.assertEqual(admin.events[0][1]["t"], "ev.ready")
        self.assertFalse(h.anonymous().get("/api/info").json["needs_setup"])
        self.assertFalse(os.path.exists(os.path.join(h.data_dir, "setup_code.txt")))


class RealHubFlowTests(harness.ServerTestCase):
    """The harness and the client against the real hub: users made by an admin can talk to each other."""

    admin: wsclient.ChatSession

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.admin = cls.harness.create_admin()

    def test_created_users_are_activated_and_can_talk(self) -> None:
        h, admin = self.harness, self.admin
        bob = h.create_user(admin, h.unique("bob"))
        self.assertEqual(bob.ready["me"]["role"], "member")
        since = admin.mark()
        direct = bob.request("chat.open_direct", {"user_id": admin.user_id})
        self.assertTrue(direct["ok"], direct)
        sent = bob.request(
            "msg.send", {"chat_id": direct["d"]["chat"]["id"], "client_id": "selftest-0001", "body": "hello"}
        )
        self.assertTrue(sent["ok"], sent)
        got = admin.wait_event("ev.message", lambda f: f["d"]["message"]["body"] == "hello", since=since)
        self.assertEqual(got["d"]["message"]["sender_id"], bob.user_id)
        eve = h.create_user(admin, h.unique("eve"))
        eve.expect_none("ev.message", lambda f: f["d"]["message"]["body"] == "hello")

    def test_never_activated_user_has_no_session(self) -> None:
        h = self.harness
        carol = h.create_user(self.admin, h.unique("carol"), activate=False)
        self.assertIsNone(carol.token)
        self.assertEqual(carol.password, harness.TEMP_PASSWORD)
        update = self.admin.wait_event("ev.user_update", lambda f: f["d"]["user"]["username"] == carol.username)
        self.assertFalse(update["d"]["user"]["activated"])


if __name__ == "__main__":
    unittest.main()
