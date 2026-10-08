"""``doctor.health_probe`` against real local servers, and drift guards against the installer's copies.

``service/install_service.py`` imports nothing from ``chatd`` and carries its own copy of the health probe, host
mapping and SDDL reading (SPEC 6.2). These tests run both copies on the same fixtures and fail when they diverge.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple

from chatd import doctor, tlsutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INFO = {"name": "DeskTalk", "registration_open": False, "needs_setup": True, "tls": False}
Routes = Dict[str, Tuple[int, bytes]]


def load_installer() -> Any:
    """Import service/install_service.py (a script, not a package) once per test process."""
    name = "desktalk_install_service"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "service", "install_service.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


installer = load_installer()


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: Routes = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        status, body = self.routes.get(self.path, (404, b"not found"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return None


def healthy() -> Routes:
    return {"/healthz": (200, b"ok"), "/api/info": (200, json.dumps(INFO).encode())}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ProbeCase(unittest.TestCase):
    def serve(self, routes: Routes, tls: Optional[Tuple[str, str]] = None) -> int:
        handler = type("Handler", (_Handler,), {"routes": routes})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return int(server.server_address[1])


class HealthProbeTests(ProbeCase):
    def test_healthy_server_returns_the_info_dict(self) -> None:
        port = self.serve(healthy())
        self.assertEqual(doctor.health_probe("127.0.0.1", port, False, 3.0), INFO)

    def test_wildcard_binds_are_probed_on_loopback(self) -> None:
        port = self.serve(healthy())
        for host in ("0.0.0.0", "::", "localhost", "127.0.0.1", "127.8.8.8"):
            self.assertEqual(doctor.health_probe(host, port, False, 3.0), INFO, host)

    def test_anything_unhealthy_is_none(self) -> None:
        good = healthy()
        variants = {
            "healthz 500": {**good, "/healthz": (500, b"ok")},
            "healthz body": {**good, "/healthz": (200, b"fine")},
            "info missing": {"/healthz": (200, b"ok")},
            "info not json": {**good, "/api/info": (200, b"<html>")},
            "info list": {**good, "/api/info": (200, b"[1]")},
            "info 503": {**good, "/api/info": (503, b"{}")},
            "info bad utf8": {**good, "/api/info": (200, b"\xff\xfe")},
        }
        for label, routes in variants.items():
            port = self.serve(routes)
            self.assertIsNone(doctor.health_probe("127.0.0.1", port, False, 3.0), label)

    def test_trailing_newline_after_ok_is_accepted(self) -> None:
        port = self.serve({**healthy(), "/healthz": (200, b"ok\n")})
        self.assertEqual(doctor.health_probe("127.0.0.1", port, False, 3.0), INFO)

    def test_closed_port_and_wrong_scheme(self) -> None:
        self.assertIsNone(doctor.health_probe("127.0.0.1", free_port(), False, 1.0))
        port = self.serve(healthy())
        self.assertIsNone(doctor.health_probe("127.0.0.1", port, True, 1.0))  # https against a plain-http server

    def test_https_with_a_self_signed_certificate(self) -> None:
        folder = tempfile.mkdtemp(prefix="dt-probe-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        if not tlsutil.ensure_cert(folder, hostname="localhost", lan_ips=[]):
            self.skipTest("no openssl: cannot create a certificate")
        pair = (os.path.join(folder, "tls", "cert.pem"), os.path.join(folder, "tls", "key.pem"))
        port = self.serve(healthy(), tls=pair)
        self.assertEqual(doctor.health_probe("127.0.0.1", port, True, 5.0), INFO)  # unverified context accepts it
        self.assertIsNone(doctor.health_probe("127.0.0.1", port, False, 1.0))  # plain http to a TLS socket


class InstallerParityTests(ProbeCase):
    """The installer's copy and the doctor's must answer identically."""

    def fixtures(self) -> List[Tuple[str, int, bool]]:
        good = healthy()
        cases = {
            "healthy": good,
            "healthz 500": {**good, "/healthz": (500, b"ok")},
            "healthz body": {**good, "/healthz": (200, b"nope")},
            "info missing": {"/healthz": (200, b"ok")},
            "info junk": {**good, "/api/info": (200, b"{not json")},
            "info list": {**good, "/api/info": (200, b"[]")},
            "info 404": {**good, "/api/info": (404, b"")},
        }
        found = [(label, self.serve(routes), False) for label, routes in cases.items()]
        found.append(("closed port", free_port(), False))
        found.append(("tls to plain", self.serve(good), True))
        return found

    def test_health_probe_gives_identical_results(self) -> None:
        for label, port, tls in self.fixtures():
            for host in ("127.0.0.1", "0.0.0.0", "localhost"):
                self.assertEqual(
                    doctor.health_probe(host, port, tls, 1.0),
                    installer.health_probe(host, port, tls, 1.0),
                    "%s via %s" % (label, host),
                )

    def test_probe_host_mapping_is_identical(self) -> None:
        hosts = [
            "",
            "0.0.0.0",
            "::",
            "localhost",
            "127.0.0.1",
            "127.1.2.3",
            "192.168.1.9",
            "::1",
            "chat.corp",
            " 0.0.0.0 ",
        ]
        for host in hosts:
            self.assertEqual(doctor.probe_host(host), installer.probe_host(host), repr(host))

    def test_sddl_reading_is_identical(self) -> None:
        dumps = {
            "python313": (
                "Python313\r\nD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)S:PAINO_ACCESS_CONTROL\r\n"
            ),
            "e_root": (
                "\r\nD:(A;;FA;;;BA)(A;OICIIO;GA;;;BA)(A;;FA;;;SY)(A;OICIIO;GA;;;SY)(A;;0x1301bf;;;AU)"
                "(A;OICIIO;SDGXGWGR;;;AU)(A;;0x1200a9;;;BU)(A;OICIIO;GXGR;;;BU)\r\n"
            ),
            "program_data": "ProgramData\r\nD:PAI(A;OICI;FA;;;SY)(A;OICIIO;GA;;;CO)(A;CI;DCLCRPCR;;;BU)\r\n",
            "everyone": "x\r\nD:(A;;FW;;;WD)(A;;GW;;;IU)(D;;FA;;;BU)(A;;0x2;;;S-1-5-4)\r\n",
            "conditional": 'x\r\nD:(XA;;FW;;;WD;(@User.Title=="PM"))(A;;FA;;;BA)\r\n',
        }
        self.assertEqual(doctor.WRITE_MASK, installer.WRITE_MASK)
        self.assertEqual(doctor.BROAD_SIDS, installer.BROAD_SIDS)
        for label, text in dumps.items():
            for raw in (text.encode("utf-16-le"), text.encode("utf-16"), text.encode("utf-8")):
                sddl = doctor.extract_sddl(raw)
                self.assertEqual(sddl, installer.extract_sddl(raw), label)
                mine = sorted((name, rights) for name, rights in doctor.broad_access(str(sddl), doctor.WRITE_MASK))
                theirs = sorted((f.name, f.mask) for f in installer.unsafe_aces(str(sddl)))
                self.assertEqual(mine, theirs, label)

    def test_health_probe_signature_matches_the_spec(self) -> None:
        import inspect

        for function in (doctor.health_probe, installer.health_probe):
            names = list(inspect.signature(function).parameters)
            self.assertEqual(names, ["host", "port", "tls", "timeout"])


if __name__ == "__main__":
    unittest.main()
