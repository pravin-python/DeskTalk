import unittest

from desktalk.errors import ProtocolError
from desktalk.protocol import (
    MAX_NAME_LEN,
    MAX_TEXT_LEN,
    clean_name,
    clean_text,
    decode,
    encode,
    fmt_time,
    scrub,
)


class EncodeDecodeTests(unittest.TestCase):
    def test_roundtrip_unicode(self):
        obj = {"type": "msg", "text": "नमस्ते 👋 é"}
        line = encode(obj)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(decode(line), obj)

    def test_decode_rejects_garbage(self):
        for bad in (b"not json", b"[1, 2]", b'"str"', b"42", b"\xff\xfe", b"", b"{"):
            with self.assertRaises(ProtocolError, msg=repr(bad)):
                decode(bad)

    def test_decode_survives_deep_nesting(self):
        with self.assertRaises(ProtocolError):
            decode(b"[" * 60000)

    def test_protocol_error_is_value_error(self):
        self.assertTrue(issubclass(ProtocolError, ValueError))

    def test_encode_lone_surrogate_does_not_raise(self):
        line = encode({"text": "a\ud800b"})
        self.assertIn(b"a", line)
        decode(line)  # still valid UTF-8 JSON

    def test_encode_unserialisable_raises_protocol_error(self):
        with self.assertRaises(ProtocolError):
            encode({"x": object()})

    def test_scrub(self):
        self.assertEqual(scrub("ok"), "ok")
        self.assertNotIn("\ud800", scrub("a\ud800"))


class CleanNameTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(clean_name("  pravin "), "pravin")

    def test_truncates(self):
        self.assertEqual(len(clean_name("x" * 100)), MAX_NAME_LEN)

    def test_rejects_bad(self):
        for bad in ("", "   ", "a b", "tab\there", "ctl\x00", "zero\u200bwidth", 5, ["a"], {"a": 1}):
            with self.assertRaises(ProtocolError, msg=repr(bad)):
                clean_name(bad)

    def test_none_is_empty(self):
        with self.assertRaises(ProtocolError):
            clean_name(None)


class CleanTextTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(clean_text("  hi  "), "hi")
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text("   "), "")

    def test_too_long(self):
        self.assertEqual(len(clean_text("x" * MAX_TEXT_LEN)), MAX_TEXT_LEN)
        with self.assertRaises(ProtocolError):
            clean_text("x" * (MAX_TEXT_LEN + 1))

    def test_non_string(self):
        for bad in (5, {"a": 1}, ["x"], True):
            with self.assertRaises(ProtocolError):
                clean_text(bad)

    def test_surrogates_scrubbed(self):
        clean_text("a\ud800").encode("utf-8")  # must not raise


class FmtTimeTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertRegex(fmt_time(0), r"^\d\d:\d\d:\d\d$")
        for bad in (None, "x", float("nan"), 10 ** 30, [1]):
            self.assertEqual(fmt_time(bad), "")


if __name__ == "__main__":
    unittest.main()
