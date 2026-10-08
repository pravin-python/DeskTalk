"""Uploads and downloads: sniffing, names, quotas, access rule, Range/ETag/304, caps (SPEC 4.2, 5.1, 5.5, 5.6)."""

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
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock
from urllib.parse import quote

try:  # `unittest discover -s tests -t .` imports this module as part of the `tests` package
    from . import test_transport_support as support
except ImportError:  # `unittest discover -s tests` or running from inside tests/
    import test_transport_support as support

from chatd import files, util
from chatd.db import Database
from chatd.http import Router, register_http_routes

ONE_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7V\xbd\xfa\x00\x00\x00\x00IEND\xaeB`\x82"
)


def ftyp(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00" + brand + b"isom"


class SniffTests(unittest.TestCase):
    def test_signature_table(self) -> None:
        cases = [
            (ONE_PIXEL_PNG, "image/png", "image"),
            (b"\xff\xd8\xff\xe0" + b"\0" * 20, "image/jpeg", "image"),
            (b"GIF87a" + b"\0" * 10, "image/gif", "image"),
            (b"GIF89a" + b"\0" * 10, "image/gif", "image"),
            (b"RIFF\x24\0\0\0WEBPVP8 ", "image/webp", "image"),
            (b"RIFF\x24\0\0\0WAVEfmt ", "audio/wav", "audio"),
            (
                b"BM" + b"\0\0\0\0" + b"\0\0\0\0" + b"\x36\0\0\0" + (40).to_bytes(4, "little") + b"\0" * 20,
                "image/bmp",
                "image",
            ),
            (b"OggS\0\x02" + b"\0" * 20, "audio/ogg", "audio"),
            (b"fLaC\0\0\0\x22", "audio/flac", "audio"),
            (b"ID3\x03\0\0\0\0\0\0", "audio/mpeg", "audio"),
            (b"\xff\xfb\x90\x00" + b"\0" * 10, "audio/mpeg", "audio"),
            (b"\x1a\x45\xdf\xa3\x93\x42\x82\x88webm", "video/webm", "video"),
            (ftyp(b"avif"), "image/avif", "image"),
            (ftyp(b"avis"), "image/avif", "image"),
            (ftyp(b"qt  "), "video/quicktime", "video"),
            (ftyp(b"M4A "), "audio/mp4", "audio"),
            (ftyp(b"M4B "), "audio/mp4", "audio"),
            (ftyp(b"isom"), "video/mp4", "video"),
            (ftyp(b"mp42"), "video/mp4", "video"),
            (ftyp(b"M4V "), "video/mp4", "video"),
            (ftyp(b"3gp4"), "video/mp4", "video"),
            (ftyp(b"heic"), "application/octet-stream", "file"),
            (ftyp(b"mif1"), "application/octet-stream", "file"),
            (ftyp(b"heix"), "application/octet-stream", "file"),
            (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "application/octet-stream", "file"),
            (b"<!doctype html><script>alert(1)</script>", "application/octet-stream", "file"),
            (b"MZ\x90\x00", "application/octet-stream", "file"),
            (b"BM is just text", "application/octet-stream", "file"),
            (b"\xff\xf1\x50\x80", "application/octet-stream", "file"),  # ADTS AAC is not MPEG audio
            (b"", "application/octet-stream", "file"),
        ]
        for head, mime, kind in cases:
            self.assertEqual(files.sniff(head), (mime, kind), head[:16])

    def test_audio_only_hint_only_narrows(self) -> None:
        webm = b"\x1a\x45\xdf\xa3\x93\x42\x82\x88webm"
        self.assertEqual(files.sniff(webm, True), ("audio/webm", "audio"))
        self.assertEqual(files.sniff(ftyp(b"isom"), True), ("audio/mp4", "audio"))
        self.assertEqual(files.sniff(ftyp(b"mp42"), True), ("audio/mp4", "audio"))
        self.assertEqual(files.sniff(ftyp(b"heic"), True), ("application/octet-stream", "file"))
        self.assertEqual(files.sniff(ONE_PIXEL_PNG, True), ("image/png", "image"))
        self.assertEqual(files.sniff(b"MZ\x90\x00", True), ("application/octet-stream", "file"))

    def test_classify_uses_hints_only_when_consistent(self) -> None:
        webm = b"\x1a\x45\xdf\xa3\x93\x42\x82\x88webm"
        self.assertEqual(files.classify(webm, {"audio_only": True, "duration": 3.5})[:2], ("audio/webm", "audio"))
        self.assertEqual(
            files.classify(webm, {"audio_only": True, "width": 640, "height": 480})[:2], ("video/webm", "video")
        )
        self.assertEqual(files.classify(webm, {})[:2], ("video/webm", "video"))
        self.assertEqual(files.classify(ftyp(b"heic"), {"audio_only": True})[:2], ("application/octet-stream", "file"))
        self.assertEqual(
            files.classify(ONE_PIXEL_PNG, {"width": 10, "height": 20}), ("image/png", "image", 10, 20, None)
        )
        self.assertEqual(
            files.classify(b"MZ", {"width": 10, "duration": 3.0}),
            ("application/octet-stream", "file", None, None, None),
        )
        self.assertEqual(files.classify(b"OggS\0\x02", {"width": 10, "duration": 3.0})[2:], (None, None, 3.0))

    def test_parse_meta_drops_bad_values(self) -> None:
        self.assertEqual(
            files.parse_meta('{"width":10,"height":20,"duration":1.5,"audio_only":true}'),
            {"width": 10, "height": 20, "duration": 1.5, "audio_only": True},
        )
        self.assertEqual(files.parse_meta('{"width":0,"height":16385,"duration":-1}'), {})
        self.assertEqual(files.parse_meta('{"width":true,"height":"7","duration":86401,"audio_only":false}'), {})
        self.assertEqual(files.parse_meta('{"width":1.5}'), {})
        self.assertEqual(files.parse_meta('{"audio_only":"yes"}'), {})
        self.assertEqual(files.parse_meta('{"duration":1e999}'), {})
        self.assertEqual(files.parse_meta('{"duration":NaN}'), {})
        for junk in (None, "", "nope", "[]", "1", '{"width":' + "9" * 2000 + "}"):
            self.assertEqual(files.parse_meta(junk), {})


class NameTests(unittest.TestCase):
    def test_sanitising(self) -> None:
        cases = {
            "photo.png": "photo.png",
            "../../etc/passwd": "passwd",
            "C:\\Users\\bob\\evil.txt": "evil.txt",
            'a<b>c:d"e|f?g*h.txt': "a_b_c_d_e_f_g_h.txt",
            "report.pdf.": "report.pdf",
            "name with trailing space.txt ": "name with trailing space.txt",
            "": "file",
            "...": "file",
            "   ": "file",
            "/": "file",
            "invoice\u202egpj.exe": "invoicegpj.exe",
            "zero\u200bwidth.txt": "zerowidth.txt",
            "line\nbreak\r.txt": "linebreak.txt",
            "nul\x00byte.txt": "nulbyte.txt",
            "caf\u0065\u0301.txt": "caf\u00e9.txt",
            "CON": "_CON",
            "con.txt": "_con.txt",
            "NUL.tar.gz": "_NUL.tar.gz",
            "com1": "_com1",
            "LPT9.log": "_LPT9.log",
            "COM\u00b2": "_COM\u00b2",
            "console.txt": "console.txt",
            "aux ": "_aux",
            ".htaccess": ".htaccess",
        }
        for raw, expected in cases.items():
            self.assertEqual(files.sanitize_name(raw), expected, repr(raw))

    def test_length_limits_keep_the_extension(self) -> None:
        name = files.sanitize_name("a" * 500 + ".docx")
        self.assertEqual(len(name), 200)
        self.assertTrue(name.endswith(".docx"))
        wide = files.sanitize_name("\u4e2d" * 300 + ".txt")
        self.assertLessEqual(len(wide), 200)
        self.assertLessEqual(len(wide.encode("utf-8")), 255)
        self.assertTrue(wide.endswith(".txt"))
        self.assertEqual(files.sanitize_name("x" * 300), "x" * 200)
        long_ext = files.sanitize_name("b." + "e" * 300)
        self.assertLessEqual(len(long_ext), 200)

    def test_decode_header_name(self) -> None:
        self.assertEqual(files.decode_header_name(quote("r\u00e9sum\u00e9 \u4e2d.pdf")), "r\u00e9sum\u00e9 \u4e2d.pdf")
        self.assertEqual(files.decode_header_name(None), "")

    def test_decode_header_name_is_strict(self) -> None:
        for raw in ("%ff%fe", "bad%ff.txt", "%ed%a0%80.txt", "x%c0%afy", "%e4%b8"):
            with self.assertRaises(ValueError, msg=raw):
                files.decode_header_name(raw)

    def test_blocked_extensions(self) -> None:
        blocked = {"exe", "bat", "js"}
        self.assertTrue(files.is_blocked("setup.EXE", blocked))
        self.assertTrue(files.is_blocked("a.tar.bat", blocked))
        self.assertTrue(files.is_blocked(".exe", blocked))
        self.assertFalse(files.is_blocked("exe", blocked))
        self.assertFalse(files.is_blocked("notes.txt", blocked))
        self.assertFalse(files.is_blocked("a.exe.txt", blocked))
        self.assertFalse(files.is_blocked("a.exe", set()))
        self.assertEqual(files.sanitize_name("evil.exe."), "evil.exe")
        self.assertTrue(files.is_blocked(files.sanitize_name("evil.exe. "), blocked))

    def test_content_disposition(self) -> None:
        value = files.content_disposition('r\u00e9sum\u00e9 "x".pdf', False)
        self.assertTrue(value.startswith("attachment; filename=\"r_sum___x_.pdf\"; filename*=UTF-8''"))
        self.assertIn("r%C3%A9sum%C3%A9%20%22x%22.pdf", value)
        self.assertNotIn("\r", value)
        self.assertTrue(files.content_disposition("a.png", True).startswith("inline;"))
        self.assertEqual(files.content_disposition("\u4e2d\u6587", False).split(";")[1].strip(), 'filename="__"')
        self.assertNotIn('"', files.content_disposition('a"b', False).split("filename*")[1])


class RangeTests(unittest.TestCase):
    def test_range_parsing(self) -> None:
        cases = [
            ("bytes=0-4", 100, (0, 4)),
            ("bytes=10-", 100, (10, 99)),
            ("bytes=-5", 100, (95, 99)),
            ("bytes=0-999", 100, (0, 99)),
            ("bytes=-500", 100, (0, 99)),
            ("bytes=99-99", 100, (99, 99)),
            (None, 100, None),
            ("", 100, None),
            ("items=0-1", 100, None),
            ("bytes=0-1,5-6", 100, None),
            ("bytes=5-2", 100, None),
            ("bytes=-", 100, None),
            ("bytes=a-b", 100, None),
        ]
        for header, size, expected in cases:
            self.assertEqual(files.parse_range(header, size), expected, header)
        for header, size in (("bytes=100-", 100), ("bytes=500-600", 100), ("bytes=-0", 100), ("bytes=0-1", 0)):
            with self.assertRaises(files.RangeNotSatisfiable):
                files.parse_range(header, size)

    def test_etag_matching(self) -> None:
        self.assertTrue(files.etag_matches('"a"', '"a"'))
        self.assertTrue(files.etag_matches('W/"a"', '"a"'))
        self.assertTrue(files.etag_matches('"x", "a"', '"a"'))
        self.assertTrue(files.etag_matches("*", '"a"'))
        self.assertFalse(files.etag_matches('"b"', '"a"'))
        self.assertFalse(files.etag_matches(None, '"a"'))

    def test_served_type(self) -> None:
        self.assertEqual(files.served_type("image/png", False), ("image/png", True))
        self.assertEqual(files.served_type("image/png", True), ("image/png", False))
        self.assertEqual(files.served_type("audio/webm", False), ("audio/webm", True))
        for mime in (
            "image/svg+xml",
            "text/html",
            "application/pdf",
            "application/xml",
            "text/javascript",
            "weird/type",
        ):
            self.assertEqual(files.served_type(mime, False), ("application/octet-stream", False))


def upload_request(
    body: bytes,
    name: Optional[str] = "pic.png",
    cookie: str = "fc_session=tok-1",
    meta: Optional[Dict[str, Any]] = None,
    ctype: str = "text/html",
    extra: bytes = b"",
    length: Optional[int] = None,
    with_length: bool = True,
) -> bytes:
    head = b"POST /api/upload HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Requested-With: desktalk\r\n"
    if cookie:
        head += b"Cookie: " + cookie.encode() + b"\r\n"
    head += b"Content-Type: " + ctype.encode() + b"\r\n"
    if name is not None:
        head += b"X-File-Name: " + quote(name).encode() + b"\r\n"
    if meta is not None:
        head += b"X-Meta: " + json.dumps(meta).encode() + b"\r\n"
    if with_length:
        head += b"Content-Length: " + str(len(body) if length is None else length).encode() + b"\r\n"
    return head + extra + b"\r\n" + body


class FilesBase(unittest.TestCase):
    max_upload_mb = 1

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="dt-files-")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.data = tempfile.mkdtemp(dir=self.tmp)
        self.cfg = support.make_config(self.data, max_upload_mb=self.max_upload_mb)
        os.makedirs(self.cfg.tmp_dir, exist_ok=True)
        self.database = Database(os.path.join(self.data, "chat.db"))
        self.database.open()

        def routes(router: Router, harness: support.Harness) -> None:
            register_http_routes(router, self.database, self.cfg, web_root=None)

        self.auth = support.FakeAuth()
        self.h = support.Harness(self.cfg, routes, self.auth, db=self.database).start()
        self.addCleanup(self._cleanup)
        for uid in (1, 2, 3):
            self.sql(
                "INSERT INTO users(id, username, display_name, display_key, pw_hash, created_at) VALUES(?,?,?,?,?,?)",
                uid,
                "user%d" % uid,
                "User %d" % uid,
                "user %d" % uid,
                "x",
                1.0,
            )

    def _cleanup(self) -> None:
        self.h.stop()
        asyncio_run(self.database.close())

    def sql(self, statement: str, *params: Any) -> None:
        def write(conn: Any) -> None:
            conn.execute(statement, params)

        self.h.call(self.database.run(write))

    def query(self, statement: str, *params: Any) -> List[Tuple[Any, ...]]:
        def read(conn: Any) -> List[Tuple[Any, ...]]:
            return conn.execute(statement, params).fetchall()

        return self.h.call(self.database.run_read(read))

    def upload(self, body: bytes, **kw: Any) -> Tuple[int, Dict[str, str], bytes]:
        return support.exchange(self.h.port, upload_request(body, **kw), timeout=10)

    def upload_ok(self, body: bytes, **kw: Any) -> Dict[str, Any]:
        status, _, resp = self.upload(body, **kw)
        self.assertEqual(status, 201, resp)
        return json.loads(resp)["attachment"]

    def download(
        self, att_id: str, cookie: str = "fc_session=tok-1", extra: bytes = b"", method: bytes = b"GET", query: str = ""
    ):
        raw = (
            method
            + b" /files/"
            + att_id.encode()
            + query.encode()
            + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: "
            + cookie.encode()
            + b"\r\n"
            + extra
            + b"Connection: close\r\n\r\n"
        )
        return support.exchange(self.h.port, raw, timeout=10, head_only=method == b"HEAD")

    def share_in_chat(self, att_id: str, members: List[int], history_from: Optional[Dict[int, int]] = None) -> int:
        """Create a group with ``members`` and one message carrying the attachment; returns the message id."""
        history_from = history_from or {}
        self.sql("INSERT INTO chats(kind, title, created_at, last_activity_at) VALUES('group','g',1,1)")
        chat_id = self.query("SELECT MAX(id) FROM chats")[0][0]
        for uid in members:
            self.sql(
                "INSERT INTO chat_members(chat_id, user_id, joined_at, history_from_id) VALUES(?,?,1,?)",
                chat_id,
                uid,
                history_from.get(uid, 0),
            )
        self.sql(
            "INSERT INTO messages(chat_id, sender_id, kind, attachment_id, created_at) VALUES(?,?,?,?,1)",
            chat_id,
            members[0],
            "image",
            att_id,
        )
        return self.query("SELECT MAX(id) FROM messages")[0][0]


def asyncio_run(coro: Any) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


class UploadTests(FilesBase):
    def test_upload_roundtrip_sniffs_and_ignores_client_type(self) -> None:
        att = self.upload_ok(ONE_PIXEL_PNG, name="pic.png", ctype="text/html", meta={"width": 1, "height": 1})
        self.assertEqual(set(att), {"id", "name", "mime", "size", "kind", "url", "width", "height", "duration"})
        self.assertEqual(
            (att["mime"], att["kind"], att["size"], att["name"]), ("image/png", "image", len(ONE_PIXEL_PNG), "pic.png")
        )
        self.assertEqual((att["width"], att["height"], att["duration"]), (1, 1, None))
        self.assertEqual(att["url"], "/files/" + att["id"])
        self.assertRegex(att["id"], r"^[0-9a-f]{32}$")
        stored = os.path.join(self.cfg.uploads_dir, att["id"][:2], att["id"])
        with open(stored, "rb") as fh:
            self.assertEqual(fh.read(), ONE_PIXEL_PNG)
        self.assertEqual(os.listdir(self.cfg.tmp_dir), [])
        row = self.query("SELECT uploader_id, path, mime FROM attachments WHERE id=?", att["id"])[0]
        self.assertEqual(row, (1, att["id"][:2] + "/" + att["id"], "image/png"))

    def test_html_disguised_as_png_stays_a_download(self) -> None:
        att = self.upload_ok(b"<html><script>alert(1)</script>", name="x.png", ctype="image/png")
        self.assertEqual((att["mime"], att["kind"]), ("application/octet-stream", "file"))
        _, headers, body = self.download(att["id"])
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertTrue(headers["content-disposition"].startswith("attachment"))

    def test_voice_note_hint_and_names(self) -> None:
        webm = b"\x1a\x45\xdf\xa3\x93\x42\x82\x88webm" + b"\0" * 32
        att = self.upload_ok(webm, name="voice.webm", meta={"audio_only": True, "duration": 4.2})
        self.assertEqual((att["mime"], att["kind"], att["duration"]), ("audio/webm", "audio", 4.2))
        att = self.upload_ok(webm, name="clip.webm", meta={"width": 640, "height": 360})
        self.assertEqual((att["mime"], att["kind"]), ("video/webm", "video"))
        att = self.upload_ok(ftyp(b"heic") + b"\0" * 16, name="IMG.HEIC")
        self.assertEqual((att["mime"], att["kind"]), ("application/octet-stream", "file"))

    def test_bidi_and_traversal_names_are_sanitised(self) -> None:
        att = self.upload_ok(b"data", name="invoice\u202egpj.pdf")
        self.assertEqual(att["name"], "invoicegpj.pdf")
        att = self.upload_ok(b"data", name="..\\..\\windows\\system32\\evil.txt")
        self.assertEqual(att["name"], "evil.txt")
        att = self.upload_ok(b"data", name=None)
        self.assertEqual(att["name"], "file")
        att = self.upload_ok(b"data", name="CON.txt")
        self.assertEqual(att["name"], "_CON.txt")

    def test_empty_file_is_accepted(self) -> None:
        att = self.upload_ok(b"", name="empty.txt")
        self.assertEqual(att["size"], 0)
        status, headers, body = self.download(att["id"])
        self.assertEqual((status, body, headers["content-length"]), (200, b"", "0"))

    def test_framing_and_auth_errors(self) -> None:
        status, _, body = self.upload(b"abc", with_length=False)
        self.assertEqual(status, 411)
        self.assertIn(b"length_required", body)
        self.assertEqual(self.upload(b"abc", cookie="")[0], 401)
        self.assertEqual(self.upload(b"abc", cookie="fc_session=tok-bad")[0], 401)
        raw = upload_request(b"abc").replace(b"X-Requested-With: desktalk\r\n", b"")
        self.assertEqual(support.exchange(self.h.port, raw)[0], 403)
        raw = upload_request(b"abc").replace(b"POST", b"GET", 1)
        self.assertEqual(support.exchange(self.h.port, raw)[0], 405)

    def test_oversize_is_413_before_reading_and_status_survives_the_unread_body(self) -> None:
        big = b"\0" * (self.cfg.max_upload_bytes + 1)
        sock = support.connect(self.h.port, 10)
        sock.sendall(upload_request(b"", length=len(big)))
        sock.sendall(big[:300000])  # the client keeps sending; the server drains and still answers
        status, headers, body = support.read_response(sock)
        self.assertEqual(status, 413)
        self.assertIn(b"too_large", body)
        self.assertEqual(headers["connection"], "close")
        sock.close()
        self.assertEqual(os.listdir(self.cfg.tmp_dir), [])

    def test_early_401_drains_the_body(self) -> None:
        payload = b"\1" * 600000
        sock = support.connect(self.h.port, 10)
        sock.sendall(upload_request(payload, cookie=""))
        status, headers, _ = support.read_response(sock)
        self.assertEqual((status, headers["connection"]), (401, "close"))
        sock.close()

    def test_exact_limit_is_accepted(self) -> None:
        att = self.upload_ok(b"\0" * self.cfg.max_upload_bytes, name="max.bin")
        self.assertEqual(att["size"], self.cfg.max_upload_bytes)

    def test_file_name_must_be_valid_utf8_text(self) -> None:
        for raw in ("bad%ff.txt", "%ed%a0%80.txt", "x%c0%afy"):
            request = upload_request(b"data", name=None).replace(
                b"Content-Length", b"X-File-Name: " + raw.encode() + b"\r\nContent-Length", 1
            )
            status, _, body = support.exchange(self.h.port, request, timeout=10)
            self.assertEqual(status, 400, raw)
            self.assertIn(b'"reason":"invalid_text"', body)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attachments"), [(0,)])

    def test_blocked_extensions(self) -> None:
        for name in ("setup.exe", "a.BAT", "x.tar.cmd", "evil.exe.", "evil.exe ", "pay.scr", "x.jar", ".lnk"):
            status, _, body = self.upload(b"MZ", name=name)
            self.assertEqual(status, 400, name)
            self.assertIn(b"blocked_type", body)
        self.assertEqual(self.upload(b"MZ", name="notes.exe.txt")[0], 201)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attachments")[0][0], 1)

    def test_disk_reserve_is_507(self) -> None:
        with mock.patch.object(files, "disk_has_room", return_value=False):
            status, _, body = self.upload(b"abc")
        self.assertEqual(status, 507)
        self.assertIn(b"insufficient_storage", body)

    def test_disk_check_formula(self) -> None:
        usage = shutil.disk_usage(self.data)
        self.assertTrue(files.disk_has_room(self.cfg.data_dir, 1))
        self.assertFalse(files.disk_has_room(self.cfg.data_dir, usage.free))

    def test_unattached_quota_is_413(self) -> None:
        with mock.patch.object(files, "MAX_UNATTACHED_BYTES", 20):
            self.upload_ok(b"a" * 12, name="one.bin")
            status, _, body = self.upload(b"b" * 12, name="two.bin")
        self.assertEqual(status, 413)
        self.assertIn(b"quota_exceeded", body)
        self.assertEqual(self.query("SELECT COUNT(*) FROM attachments")[0][0], 1)
        self.assertEqual(len(os.listdir(self.cfg.tmp_dir)), 0)

    def test_attached_uploads_do_not_count_as_unattached(self) -> None:
        with mock.patch.object(files, "MAX_UNATTACHED_BYTES", 20):
            first = self.upload_ok(b"a" * 12, name="one.bin")
            self.share_in_chat(first["id"], [1])
            self.assertEqual(self.upload(b"b" * 12, name="two.bin")[0], 201)

    def test_daily_quota_is_413(self) -> None:
        with mock.patch.object(files, "MAX_DAILY_BYTES", 20):
            first = self.upload_ok(b"a" * 12, name="one.bin")
            self.share_in_chat(first["id"], [1])
            self.assertEqual(self.upload(b"b" * 12, name="two.bin")[0], 413)
            self.assertEqual(self.upload(b"b" * 12, name="two.bin", cookie="fc_session=tok-2")[0], 201)

    def test_disconnect_mid_upload_removes_the_temp_file(self) -> None:
        sock = support.connect(self.h.port, 10)
        sock.sendall(upload_request(b"x" * 1000, length=500000))
        self.assertTrue(support.wait_until(lambda: bool(os.listdir(self.cfg.tmp_dir)), 5))
        sock.close()
        self.assertTrue(support.wait_until(lambda: os.listdir(self.cfg.tmp_dir) == [], 5))
        self.assertEqual(self.query("SELECT COUNT(*) FROM attachments")[0][0], 0)

    def test_concurrent_upload_cap_per_user(self) -> None:
        socks = []
        try:
            for _ in range(3):
                s = support.connect(self.h.port, 10)
                s.sendall(upload_request(b"x", length=100000))
                socks.append(s)
            self.assertTrue(support.wait_until(lambda: len(os.listdir(self.cfg.tmp_dir)) == 3, 5))
            status, headers, body = self.upload(b"y", name="fourth.bin")
            self.assertEqual(status, 429)
            self.assertIn(b"rate_limited", body)
            self.assertIn("retry-after", headers)
            self.assertEqual(self.upload(b"y", name="other.bin", cookie="fc_session=tok-2")[0], 201)
        finally:
            for s in socks:
                s.close()
        self.assertTrue(support.wait_until(lambda: os.listdir(self.cfg.tmp_dir) == [], 5))
        self.assertEqual(self.upload(b"y", name="after.bin")[0], 201)

    def test_per_ip_upload_cap(self) -> None:
        socks = []
        try:
            for uid in (1, 1, 1, 2, 2, 2):
                s = support.connect(self.h.port, 10)
                s.sendall(upload_request(b"x", length=100000, cookie="fc_session=tok-%d" % uid))
                socks.append(s)
            self.assertTrue(support.wait_until(lambda: len(os.listdir(self.cfg.tmp_dir)) == 6, 5))
            self.assertEqual(self.upload(b"y", cookie="fc_session=tok-3")[0], 429)
        finally:
            for s in socks:
                s.close()

    def test_slow_upload_is_cut_off(self) -> None:
        util.set_test_scale(0.05)
        self.addCleanup(support.reset_scale)
        sock = support.connect(self.h.port, 10)
        sock.sendall(upload_request(b"x", length=200000))
        start = time.monotonic()
        result = support.read_response(sock)
        self.assertTrue(result is None or result[0] == 408)
        self.assertLess(time.monotonic() - start, 6)
        sock.close()
        self.assertTrue(support.wait_until(lambda: os.listdir(self.cfg.tmp_dir) == [], 5))


class DownloadTests(FilesBase):
    def setUp(self) -> None:
        super().setUp()
        self.png = self.upload_ok(ONE_PIXEL_PNG, name="pic.png")
        self.pdf = self.upload_ok(b"%PDF-1.4 hello", name="Q3 r\u00e9sum\u00e9.pdf")

    def test_uploader_can_download_with_security_headers(self) -> None:
        status, headers, body = self.download(self.png["id"])
        self.assertEqual((status, body), (200, ONE_PIXEL_PNG))
        self.assertEqual(headers["content-type"], "image/png")
        self.assertTrue(headers["content-disposition"].startswith("inline;"))
        self.assertEqual(headers["etag"], '"%s"' % self.png["id"])
        self.assertEqual(headers["cache-control"], "private, no-cache")
        self.assertEqual(headers["vary"], "Cookie")
        self.assertEqual(headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(headers["content-security-policy"], "sandbox; default-src 'none'")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(headers["content-length"], str(len(ONE_PIXEL_PNG)))

    def test_pdf_and_dl_flag_force_download(self) -> None:
        status, headers, body = self.download(self.pdf["id"])
        self.assertEqual((status, body), (200, b"%PDF-1.4 hello"))
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertTrue(headers["content-disposition"].startswith("attachment;"))
        self.assertIn("filename*=UTF-8''Q3%20r%C3%A9sum%C3%A9.pdf", headers["content-disposition"])
        self.assertIn('filename="Q3_r_sum_.pdf"', headers["content-disposition"])
        _, headers, _ = self.download(self.png["id"], query="?dl=1")
        self.assertTrue(headers["content-disposition"].startswith("attachment;"))

    def test_access_rule_uploader_member_stranger(self) -> None:
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-2")[0], 404)
        message_id = self.share_in_chat(self.png["id"], [1, 2])
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-2")[0], 200)
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-3")[0], 404)
        self.sql("UPDATE messages SET deleted_at=2 WHERE id=?", message_id)
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-2")[0], 404)
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-1")[0], 200)

    def test_late_joiner_cannot_read_pre_join_attachments(self) -> None:
        message_id = self.share_in_chat(self.png["id"], [1, 2, 3], history_from={3: 10**6})
        self.assertGreater(message_id, 0)
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-2")[0], 200)
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-3")[0], 404)

    def test_unlisted_and_hidden_members_get_404(self) -> None:
        message_id = self.share_in_chat(self.png["id"], [1, 2])
        self.sql("INSERT INTO hidden_messages(user_id, message_id) VALUES(2, ?)", message_id)
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-2")[0], 404)

    def test_bad_ids_are_404_without_touching_the_database(self) -> None:
        for bad in ("../x", "A" * 32, "g" * 32, "a" * 31, "a" * 33, "%2e%2e%2f", "0" * 32, ""):
            self.assertEqual(self.download(bad)[0], 404, bad)

    def test_unauthenticated_is_401(self) -> None:
        raw = b"GET /files/" + self.png["id"].encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        self.assertEqual(support.exchange(self.h.port, raw)[0], 401)

    def test_304_is_decided_after_the_access_check(self) -> None:
        etag = ('If-None-Match: "%s"\r\n' % self.png["id"]).encode()
        status, headers, body = self.download(self.png["id"], extra=etag)
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(headers["etag"], '"%s"' % self.png["id"])
        self.assertEqual(headers["cache-control"], "private, no-cache")
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-2", extra=etag)[0], 404)
        self.assertEqual(self.download(self.png["id"], "fc_session=tok-1", extra=b'If-None-Match: "zzz"\r\n')[0], 200)

    def test_range_requests(self) -> None:
        data = os.urandom(5000)
        att = self.upload_ok(data, name="blob.bin")

        def rng(value: str):
            return self.download(att["id"], extra=("Range: %s\r\n" % value).encode())

        status, headers, body = rng("bytes=0-99")
        self.assertEqual((status, body), (206, data[:100]))
        self.assertEqual(headers["content-range"], "bytes 0-99/5000")
        self.assertEqual(headers["content-length"], "100")
        status, headers, body = rng("bytes=4900-")
        self.assertEqual((status, body, headers["content-range"]), (206, data[4900:], "bytes 4900-4999/5000"))
        status, headers, body = rng("bytes=-10")
        self.assertEqual((status, body), (206, data[-10:]))
        status, headers, body = rng("bytes=10-99999")
        self.assertEqual((status, body, headers["content-range"]), (206, data[10:], "bytes 10-4999/5000"))
        status, headers, body = rng("bytes=5000-")
        self.assertEqual(status, 416)
        self.assertEqual(headers["content-range"], "bytes */5000")
        self.assertEqual(rng("bytes=0-1,5-6")[0:3:2], (200, data))
        self.assertEqual(rng("garbage")[2], data)
        status, _, body = self.download(att["id"], extra=b'Range: bytes=0-9\r\nIf-Range: "other"\r\n')
        self.assertEqual((status, body), (200, data))
        status, _, body = self.download(
            att["id"], extra=('Range: bytes=0-9\r\nIf-Range: "%s"\r\n' % att["id"]).encode()
        )
        self.assertEqual((status, body), (206, data[:10]))

    def test_head_has_headers_and_no_body(self) -> None:
        status, headers, body = self.download(self.png["id"], method=b"HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["content-length"], str(len(ONE_PIXEL_PNG)))
        self.assertEqual(headers["content-type"], "image/png")

    def test_missing_file_is_404(self) -> None:
        stored = os.path.join(self.cfg.uploads_dir, self.png["id"][:2], self.png["id"])
        os.remove(stored)
        self.assertEqual(self.download(self.png["id"])[0], 404)

    def test_corrupt_stored_path_is_404(self) -> None:
        self.sql("UPDATE attachments SET path='../../chat.db' WHERE id=?", self.png["id"])
        self.assertEqual(self.download(self.png["id"])[0], 404)


class LargeDownloadTests(FilesBase):
    max_upload_mb = 4

    def test_twenty_parallel_large_downloads_all_succeed_and_caps_release(self) -> None:
        body = b"\x00" * (2 * 1024 * 1024)
        att = self.upload_ok(body, name="big.bin")
        results: List[Any] = []
        lock = threading.Lock()

        def fetch() -> None:
            result = self.download(att["id"])
            with lock:
                results.append((result[0], len(result[2])))

        threads = [threading.Thread(target=fetch) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(results, [(200, len(body))] * 20)

    def test_slow_reader_holds_a_slot_and_disconnect_frees_it(self) -> None:
        body = b"\x00" * (3 * 1024 * 1024)
        att = self.upload_ok(body, name="big.bin")
        socks = []
        try:
            for _ in range(8):
                s = support.connect(self.h.port, 10)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                s.sendall(
                    b"GET /files/"
                    + att["id"].encode()
                    + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: fc_session=tok-1\r\n\r\n"
                )
                socks.append(s)
            time.sleep(0.5)
            # a ninth request by the same user has to wait for a slot: it must not complete while all 8 are stuck
            ninth = support.connect(self.h.port, 10)
            ninth.sendall(
                b"GET /files/"
                + att["id"].encode()
                + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: fc_session=tok-1\r\n\r\n"
            )
            ninth.settimeout(1.5)
            with self.assertRaises(socket.timeout):
                ninth.recv(1)
            # another user is not affected (it cannot see the file: 404, but it is answered immediately)
            self.assertEqual(self.download(att["id"], "fc_session=tok-2")[0], 404)
            for s in socks[:2]:
                s.close()
            ninth.settimeout(10)
            self.assertTrue(ninth.recv(16).startswith(b"HTTP/1.1 200"))
            ninth.close()
        finally:
            for s in socks:
                s.close()


class KeyedLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_wait_timeout_and_cleanup(self) -> None:
        limiter = files.KeyedLimiter(2)
        self.assertTrue(await limiter.acquire("u", 1))
        self.assertTrue(await limiter.acquire("u", 1))
        self.assertFalse(await limiter.acquire("u", 0.05))
        self.assertTrue(await limiter.acquire("v", 1))
        limiter.release("u")
        self.assertTrue(await limiter.acquire("u", 0.5))
        for key in ("u", "u", "v"):
            limiter.release(key)
        self.assertEqual(limiter._slots, {})

    async def test_upload_slots(self) -> None:
        slots = files.UploadSlots(2, 3)
        self.assertTrue(slots.acquire(1, "a"))
        self.assertTrue(slots.acquire(1, "a"))
        self.assertFalse(slots.acquire(1, "b"))
        self.assertTrue(slots.acquire(2, "a"))
        self.assertFalse(slots.acquire(3, "a"))
        slots.release(1, "a")
        self.assertTrue(slots.acquire(3, "a"))


if __name__ == "__main__":
    unittest.main()
