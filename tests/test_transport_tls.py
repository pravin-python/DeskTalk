"""Optional TLS certificate generation through the openssl command line (SPEC 5.7)."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Iterable, Optional
from unittest import mock

from chatd import tlsutil

OPENSSL = tlsutil.find_openssl()
DAY = 86400


def prepare(
    tls_dir: Path, ips: Iterable[str], hostname: Optional[str] = None, now: Optional[float] = None
) -> Optional[ssl.SSLContext]:
    """``tlsutil.ensure_cert`` for ``<data>/tls`` = ``tls_dir`` plus its server context (``None`` without one)."""
    if not tlsutil.ensure_cert(tls_dir.parent, hostname, list(ips), now=now):
        return None
    return tlsutil.build_context(tls_dir / "cert.pem", tls_dir / "key.pem")


class PureFunctionTests(unittest.TestCase):
    def test_usable_ips_drop_loopback_linklocal_ipv6_and_duplicates(self) -> None:
        self.assertEqual(
            tlsutil.usable_ips(
                ["172.31.1.5", "127.0.0.1", "169.254.7.7", "::1", "fe80::1", "garbage", "172.31.1.5", "10.0.0.2"]
            ),
            ["172.31.1.5", "10.0.0.2"],
        )

    def test_san_list(self) -> None:
        self.assertEqual(
            tlsutil.wanted_san("PC-1", ["10.0.0.2", "169.254.1.1"]),
            ["PC-1", "localhost", "127.0.0.1", "10.0.0.2"],
        )
        self.assertEqual(tlsutil.wanted_san("localhost", []), ["localhost", "127.0.0.1"])

    def test_openssl_cnf_matches_the_spec(self) -> None:
        text = tlsutil.render_openssl_cnf("pc-1", ["10.0.0.2", "10.0.0.3"])
        for line in (
            "[req]",
            "distinguished_name=dn",
            "x509_extensions=v3",
            "prompt=no",
            "[dn]",
            "CN=pc-1",
            "[v3]",
            "basicConstraints=critical,CA:FALSE",
            "keyUsage=critical,digitalSignature,keyEncipherment",
            "extendedKeyUsage=serverAuth",
            "subjectAltName=@alt",
            "[alt]",
            "DNS.1=pc-1",
            "DNS.2=localhost",
            "IP.1=127.0.0.1",
            "IP.2=10.0.0.2",
            "IP.3=10.0.0.3",
        ):
            self.assertIn(line, text.splitlines(), line)
        self.assertNotIn("addext", text)

    def test_hostname_is_made_safe(self) -> None:
        with mock.patch("socket.gethostname", return_value="my pc;rm -rf=1\n"):
            name = tlsutil.default_hostname()
        self.assertRegex(name, r"^[A-Za-z0-9.-]+$")
        with mock.patch("socket.gethostname", return_value="  "):
            self.assertEqual(tlsutil.default_hostname(), "desktalk")

    def test_regeneration_reasons(self) -> None:
        now = 1_000_000_000.0
        wanted = ["pc", "localhost", "127.0.0.1", "10.0.0.2"]
        good = {"san": list(wanted), "not_after": now + 100 * DAY, "created": now}
        self.assertIsNone(tlsutil.regeneration_reason(good, wanted, now))
        self.assertIsNone(tlsutil.regeneration_reason(dict(good, san=wanted + ["9.9.9.9"]), wanted, now))
        self.assertIsNone(tlsutil.regeneration_reason(dict(good, san=[s.upper() for s in wanted]), wanted, now))
        prefixed = ["DNS:pc", "DNS:localhost", "IP:127.0.0.1", "IP:10.0.0.2"]  # an older / foreign spelling
        self.assertIsNone(tlsutil.regeneration_reason(dict(good, san=prefixed), wanted, now))
        self.assertIn("10.0.0.9", tlsutil.regeneration_reason(good, wanted + ["10.0.0.9"], now))
        self.assertIn("pc2", tlsutil.regeneration_reason(good, wanted + ["pc2"], now))
        self.assertIn("30 days", tlsutil.regeneration_reason(dict(good, not_after=now + 29 * DAY), wanted, now))
        self.assertIsNone(tlsutil.regeneration_reason(dict(good, not_after=now + 31 * DAY), wanted, now))
        self.assertIsNotNone(tlsutil.regeneration_reason(None, wanted, now))
        self.assertIsNotNone(tlsutil.regeneration_reason({"san": "x"}, wanted, now))
        self.assertIsNotNone(tlsutil.regeneration_reason({"san": wanted, "not_after": "soon"}, wanted, now))


@unittest.skipIf(OPENSSL is None, "openssl is not available")
class GenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dt-tls-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = Path(self.tmp) / "tls"

    def cert_text(self) -> str:
        out = subprocess.run(
            [OPENSSL, "x509", "-in", str(self.dir / "cert.pem"), "-noout", "-text"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return out.stdout.decode("utf-8", "replace")

    def test_generation_produces_a_leaf_certificate_with_the_right_extensions(self) -> None:
        ctx = prepare(self.dir, ["172.31.1.5", "169.254.9.9"], hostname="testhost")
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["cert.pem", "key.pem", "meta.json"])
        text = self.cert_text()
        self.assertIn("CA:FALSE", text)
        self.assertIn("TLS Web Server Authentication", text)
        self.assertIn("Digital Signature", text)
        self.assertIn("DNS:testhost", text)
        self.assertIn("DNS:localhost", text)
        self.assertIn("IP Address:127.0.0.1", text)
        self.assertIn("IP Address:172.31.1.5", text)
        self.assertNotIn("169.254", text)
        self.assertRegex(text, r"Public-Key: \(2048 bit\)")
        self.assertRegex(text, r"CN\s*=\s*testhost")
        meta = tlsutil.read_meta(self.dir)
        self.assertEqual(meta["san"], ["testhost", "localhost", "127.0.0.1", "172.31.1.5"])
        self.assertGreater(meta["not_after"] - meta["created"], 800 * DAY)
        if os.name == "posix":
            self.assertEqual(os.stat(self.dir / "key.pem").st_mode & 0o777, 0o600)

    def test_a_current_certificate_is_reused(self) -> None:
        prepare(self.dir, ["10.1.1.1"], hostname="testhost")
        before = (self.dir / "cert.pem").read_bytes()
        stamp = (self.dir / "cert.pem").stat().st_mtime_ns
        prepare(self.dir, ["10.1.1.1"], hostname="testhost")
        self.assertEqual((self.dir / "cert.pem").read_bytes(), before)
        self.assertEqual((self.dir / "cert.pem").stat().st_mtime_ns, stamp)
        self.assertFalse((self.dir / "cert.pem.old").exists())

    def test_a_certificate_made_by_the_installer_is_recognised(self) -> None:
        prepare(self.dir, ["10.1.1.1"], hostname="TestHost")
        installer_meta = {
            "san": ["TESTHOST", "localhost", "127.0.0.1", "10.1.1.1"],
            "not_after": time.time() + 700 * DAY,
            "created": time.time(),
        }
        (self.dir / "meta.json").write_text(json.dumps(installer_meta), encoding="utf-8")
        before = (self.dir / "cert.pem").read_bytes()
        self.assertIsNotNone(prepare(self.dir, ["10.1.1.1"], hostname="testhost"))
        self.assertEqual((self.dir / "cert.pem").read_bytes(), before)
        self.assertFalse((self.dir / "cert.pem.old").exists())

    def test_new_address_regenerates_keeps_old_files_and_reuses_the_key(self) -> None:
        prepare(self.dir, ["10.1.1.1"], hostname="testhost")
        old_cert = (self.dir / "cert.pem").read_bytes()
        old_key = (self.dir / "key.pem").read_bytes()
        prepare(self.dir, ["10.1.1.1", "10.2.2.2"], hostname="testhost")
        self.assertNotEqual((self.dir / "cert.pem").read_bytes(), old_cert)
        self.assertEqual((self.dir / "cert.pem.old").read_bytes(), old_cert)
        self.assertEqual((self.dir / "key.pem.old").read_bytes(), old_key)
        self.assertEqual((self.dir / "key.pem").read_bytes(), old_key)
        self.assertIn("IP Address:10.2.2.2", self.cert_text())
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["cert.pem", "cert.pem.old", "key.pem", "key.pem.old", "meta.json"],
        )

    def test_expiring_or_unknown_certificates_are_regenerated(self) -> None:
        prepare(self.dir, [], hostname="testhost")
        first = (self.dir / "cert.pem").read_bytes()
        future = time.time() + 800 * DAY
        prepare(self.dir, [], hostname="testhost", now=future)
        self.assertNotEqual((self.dir / "cert.pem").read_bytes(), first)
        (self.dir / "meta.json").unlink()
        second = (self.dir / "cert.pem").read_bytes()
        prepare(self.dir, [], hostname="testhost")
        self.assertNotEqual((self.dir / "cert.pem").read_bytes(), second)

    def test_stray_openssl_conf_in_the_environment_does_not_matter(self) -> None:
        with mock.patch.dict(os.environ, {"OPENSSL_CONF": os.path.join(self.tmp, "does-not-exist.cnf")}):
            self.assertIsNotNone(prepare(self.dir, [], hostname="testhost"))
        self.assertFalse((self.dir / "openssl.cnf").exists())

    def test_clients_can_verify_the_certificate_by_ip_and_name(self) -> None:
        ctx = prepare(self.dir, [], hostname="testhost")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def serve() -> None:
            conn, _ = listener.accept()
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        client = ssl.create_default_context(cafile=str(self.dir / "cert.pem"))
        raw = socket.create_connection(listener.getsockname(), timeout=10)
        with client.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
            self.assertIn(tls.version(), ("TLSv1.2", "TLSv1.3"))
            self.assertIn(("IP Address", "127.0.0.1"), tls.getpeercert()["subjectAltName"])
            tls.sendall(b"x")
        thread.join(5)

    def test_fingerprint_matches_openssl(self) -> None:
        prepare(self.dir, [], hostname="testhost")
        out = subprocess.run(
            [OPENSSL, "x509", "-in", str(self.dir / "cert.pem"), "-noout", "-fingerprint", "-sha256"],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.decode()
        expected = out.strip().split("=", 1)[1].upper()
        self.assertEqual(tlsutil.fingerprint(self.dir / "cert.pem"), expected)
        self.assertRegex(expected, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertIsNone(tlsutil.fingerprint(self.dir / "missing.pem"))


@unittest.skipIf(OPENSSL is None, "openssl is not available")
class EnsureCertContractTests(unittest.TestCase):
    """The names and results of SPEC 6.2: ``ensure_cert(data_dir, hostname, lan_ips, force) -> bool``."""

    def setUp(self) -> None:
        self.data = Path(tempfile.mkdtemp(prefix="dt-tls-contract-"))
        self.addCleanup(shutil.rmtree, str(self.data), ignore_errors=True)

    def test_returns_true_and_writes_the_three_files(self) -> None:
        self.assertIs(tlsutil.ensure_cert(self.data, "testhost", ["10.1.1.1"]), True)
        self.assertEqual(sorted(p.name for p in (self.data / "tls").iterdir()), ["cert.pem", "key.pem", "meta.json"])
        fingerprint = tlsutil.cert_fingerprint(self.data)
        self.assertRegex(fingerprint, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertIsNone(tlsutil.cert_fingerprint(self.data / "nowhere"))

    def test_none_arguments_are_discovered(self) -> None:
        with mock.patch("socket.gethostname", return_value="DiscoveredBox"), mock.patch(
            "chatd.util.lan_addresses", return_value=("10.7.7.7", ["10.8.8.8"])
        ):
            self.assertTrue(tlsutil.ensure_cert(self.data))
        meta = tlsutil.read_meta(self.data / "tls")
        self.assertEqual(meta["san"], ["DiscoveredBox", "localhost", "127.0.0.1", "10.7.7.7", "10.8.8.8"])

    def test_force_regenerates_a_valid_certificate_and_reuses_the_key(self) -> None:
        tlsutil.ensure_cert(self.data, "testhost", [])
        cert = (self.data / "tls" / "cert.pem").read_bytes()
        key = (self.data / "tls" / "key.pem").read_bytes()
        self.assertTrue(tlsutil.ensure_cert(self.data, "testhost", []))
        self.assertEqual((self.data / "tls" / "cert.pem").read_bytes(), cert)
        self.assertTrue(tlsutil.ensure_cert(self.data, "testhost", [], force=True))
        self.assertNotEqual((self.data / "tls" / "cert.pem").read_bytes(), cert)
        self.assertEqual((self.data / "tls" / "key.pem").read_bytes(), key)
        self.assertEqual((self.data / "tls" / "cert.pem.old").read_bytes(), cert)

    def test_false_means_no_certificate_and_no_files(self) -> None:
        with mock.patch.object(tlsutil, "find_openssl", return_value=None):
            self.assertIs(tlsutil.ensure_cert(self.data, "testhost", []), False)
        self.assertFalse((self.data / "tls" / "cert.pem").exists())


class FallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dt-tls-fb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = Path(self.tmp) / "tls"

    def test_missing_openssl_means_no_tls_and_no_crash(self) -> None:
        with mock.patch.object(tlsutil, "find_openssl", return_value=None):
            self.assertIsNone(prepare(self.dir, ["10.0.0.1"], hostname="h"))
        self.assertFalse((self.dir / "cert.pem").exists())

    def test_openssl_failure_means_no_tls_and_no_leftovers(self) -> None:
        with mock.patch.object(tlsutil, "find_openssl", return_value="/nonexistent/openssl"):
            self.assertIsNone(prepare(self.dir, [], hostname="h"))
        self.assertEqual([p.name for p in self.dir.iterdir()], [])

    def test_garbage_certificate_without_openssl_is_removed(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "cert.pem").write_text("not a certificate", encoding="ascii")
        (self.dir / "key.pem").write_text("not a key", encoding="ascii")
        with mock.patch.object(tlsutil, "find_openssl", return_value=None):
            self.assertIsNone(prepare(self.dir, [], hostname="h"))
        self.assertFalse((self.dir / "cert.pem").exists())
        self.assertFalse((self.dir / "key.pem").exists())

    @unittest.skipIf(OPENSSL is None, "openssl is not available")
    def test_failed_renewal_keeps_a_still_valid_certificate(self) -> None:
        self.assertIsNotNone(prepare(self.dir, [], hostname="h"))
        before = (self.dir / "cert.pem").read_bytes()
        with mock.patch.object(tlsutil, "_run_openssl", return_value=False):
            ctx = prepare(self.dir, ["10.9.9.9"], hostname="h")
        self.assertIsNotNone(ctx)
        self.assertEqual((self.dir / "cert.pem").read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith(".new")], [])

    def test_unwritable_directory_means_no_tls(self) -> None:
        blocker = Path(self.tmp) / "file"
        blocker.write_text("x", encoding="utf-8")
        self.assertIsNone(prepare(blocker / "tls", [], hostname="h"))

    def test_fingerprint_of_garbage_is_none(self) -> None:
        path = Path(self.tmp) / "bad.pem"
        path.write_text("garbage", encoding="ascii")
        self.assertIsNone(tlsutil.fingerprint(path))
        self.assertTrue(re.match(r"^[A-Za-z0-9.-]+$", tlsutil.default_hostname()))


if __name__ == "__main__":
    unittest.main()
