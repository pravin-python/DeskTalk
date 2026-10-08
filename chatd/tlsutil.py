"""Optional self-signed TLS certificate through the ``openssl`` command line (SPEC 5.7).

The server never depends on TLS being available: :func:`ensure_cert` returns ``True`` or ``False`` (after logging
why); the caller then serves HTTPS (:func:`build_context` turns the files into an ``ssl.SSLContext``) or plain HTTP.
Files live in ``<data>/tls/``: ``cert.pem``, ``key.pem`` and ``meta.json`` (``{san:[...], not_after, created}``); a
regenerated leaf keeps the previous files as ``cert.pem.old`` / ``key.pem.old`` and reuses the key.  Callers:
``serve --tls`` (``app.py``), ``tls-init`` (``__main__.py``) and ``doctor.py``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from . import util

log = logging.getLogger("chatd.tls")

CERT_DAYS = 825
RENEW_BEFORE_S = 30 * 86400
OPENSSL_TIMEOUT_S = 60

_OPENSSL_FALLBACKS = (
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    "/usr/bin/openssl",
    "/opt/homebrew/bin/openssl",
    "/usr/local/bin/openssl",
)


def find_openssl() -> Optional[str]:
    """Locate the ``openssl`` executable: ``PATH`` first, then the well-known install locations."""
    found = shutil.which("openssl")
    if found:
        return found
    for candidate in _OPENSSL_FALLBACKS:
        if os.path.isfile(candidate):
            return candidate
    return None


def default_hostname() -> str:
    """This machine's host name reduced to characters that are safe in ``openssl.cnf`` and a certificate CN."""
    raw = socket.gethostname() or ""
    name = re.sub(r"[^A-Za-z0-9.-]", "-", raw).strip(".-")[:63]
    return name or "desktalk"


def usable_ips(addresses: Iterable[str]) -> List[str]:
    """IPv4 addresses that belong in the SAN list: no loopback, no link-local (169.254/16), no duplicates."""
    out: List[str] = []
    for text in addresses:
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local or str(ip) in out:
            continue
        out.append(str(ip))
    return out


def wanted_san(hostname: str, lan_ips: Iterable[str]) -> List[str]:
    """The names a certificate must cover: ``[hostname, "localhost", "127.0.0.1", <lan ips>]``.

    This is also what ``meta.json`` stores under ``san`` - the very layout ``service/install_service.py`` writes when
    it generates the certificate at install time, so either side can judge the other's certificate.
    """
    entries = [hostname]
    if hostname.lower() != "localhost":
        entries.append("localhost")
    entries.append("127.0.0.1")
    entries.extend(usable_ips(lan_ips))
    return entries


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def render_openssl_cnf(hostname: str, lan_ips: Iterable[str]) -> str:
    """The ``openssl.cnf`` of SPEC 5.7 (CA:FALSE, serverAuth EKU and SAN; works on OpenSSL 1.1.1/3.x and LibreSSL)."""
    lines = [
        "[req]", "distinguished_name=dn", "x509_extensions=v3", "prompt=no",
        "[dn]", "CN=" + hostname,
        "[v3]", "basicConstraints=critical,CA:FALSE", "keyUsage=critical,digitalSignature,keyEncipherment",
        "extendedKeyUsage=serverAuth", "subjectAltName=@alt",
        "[alt]",
    ]
    dns = 0
    ip_n = 0
    for name in wanted_san(hostname, lan_ips):
        if _is_ip(name):
            ip_n += 1
            lines.append("IP.%d=%s" % (ip_n, name))
        else:
            dns += 1
            lines.append("DNS.%d=%s" % (dns, name))
    return "\n".join(lines) + "\n"


def read_meta(tls_dir: Path) -> Optional[Dict[str, object]]:
    """``meta.json`` of the current certificate, or ``None`` when absent or unreadable."""
    try:
        with open(tls_dir / "meta.json", "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _bare(entry: str) -> str:
    """A SAN entry without an optional ``DNS:`` / ``IP:`` prefix, lower-cased (both spellings are accepted)."""
    lowered = entry.strip().lower()
    for prefix in ("dns:", "ip:"):
        if lowered.startswith(prefix):
            return lowered[len(prefix):]
    return lowered


def regeneration_reason(meta: Optional[Dict[str, object]], wanted: List[str], now: float) -> Optional[str]:
    """Why the existing certificate must be replaced, or ``None`` when it is still good."""
    if meta is None:
        return "metadata missing"
    san = meta.get("san")
    if not isinstance(san, list):
        return "metadata invalid"
    covered = {_bare(str(s)) for s in san}
    missing = [w for w in wanted if _bare(w) not in covered]
    if missing:
        return "address or host name not covered: " + ", ".join(missing)
    not_after = meta.get("not_after")
    if not isinstance(not_after, (int, float)) or not_after - now < RENEW_BEFORE_S:
        return "expires within 30 days"
    return None


def build_context(cert: Path, key: Path) -> ssl.SSLContext:
    """Server-side context (TLS 1.2+) for the given files; raises ``ssl.SSLError``/``OSError`` when unusable."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def fingerprint(cert: Path) -> Optional[str]:
    """SHA-256 fingerprint of a PEM certificate as colon-separated upper-case hex (for ``doctor``)."""
    try:
        der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _run_openssl(exe: str, args: List[str], cwd: Path) -> bool:
    env = {k: v for k, v in os.environ.items() if k != "OPENSSL_CONF"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [exe] + args, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=OPENSSL_TIMEOUT_S, creationflags=flags, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("openssl could not be run: %s", type(exc).__name__)
        return False
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        log.warning("openssl failed (exit %d): %s", proc.returncode, ascii(tail))
        return False
    return True


def _write_meta(tls_dir: Path, san: List[str], created: float) -> None:
    meta = {"san": san, "not_after": created + CERT_DAYS * 86400, "created": created}
    tmp = tls_dir / "meta.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    os.replace(str(tmp), str(tls_dir / "meta.json"))


def _generate(tls_dir: Path, exe: str, hostname: str, lan_ips: List[str], now: float) -> bool:
    """Create ``cert.pem``/``key.pem``/``meta.json``; the key is reused when one already exists.

    The new pair is produced under ``*.new`` names, validated with ``load_cert_chain`` and only then moved into
    place (the previous pair stays as ``*.old``).  Returns ``False`` and leaves the old files untouched on failure.
    """
    cert, key = tls_dir / "cert.pem", tls_dir / "key.pem"
    new_cert, new_key = tls_dir / "cert.pem.new", tls_dir / "key.pem.new"
    cnf = tls_dir / "openssl.cnf"
    try:
        cnf.write_text(render_openssl_cnf(hostname, lan_ips), encoding="utf-8", newline="\n")
        common = ["req", "-x509", "-sha256", "-days", str(CERT_DAYS), "-config", str(cnf), "-out", str(new_cert)]
        if key.exists():
            shutil.copyfile(str(key), str(new_key))
            args = common + ["-new", "-key", str(new_key)]
        else:
            args = common + ["-newkey", "rsa:2048", "-nodes", "-keyout", str(new_key)]
        if not _run_openssl(exe, args, tls_dir):
            return False
        build_context(new_cert, new_key)
        if os.name == "posix":
            os.chmod(str(new_key), 0o600)
        for current in (cert, key):
            if current.exists():
                shutil.copyfile(str(current), str(current) + ".old")
        os.replace(str(new_key), str(key))
        os.replace(str(new_cert), str(cert))
        _write_meta(tls_dir, wanted_san(hostname, lan_ips), now)
        return True
    except (OSError, ssl.SSLError) as exc:
        log.warning("certificate generation failed: %s", type(exc).__name__)
        return False
    finally:
        for leftover in (cnf, new_cert, new_key):
            try:
                os.remove(str(leftover))
            except FileNotFoundError:
                pass
            except OSError:
                log.debug("could not remove %s", leftover.name)


def ensure_cert(
    data_dir: Union[str, "os.PathLike[str]"],
    hostname: Optional[str] = None,
    lan_ips: Optional[Iterable[str]] = None,
    force: bool = False,
    now: Optional[float] = None,
) -> bool:
    """Make sure ``<data_dir>/tls/{cert.pem,key.pem,meta.json}`` hold a valid certificate for this machine (SPEC 5.7).

    The certificate is (re)generated - the key is reused, the previous files stay as ``*.old`` - when it is missing,
    when the host name or a current LAN address is not in ``san``, when fewer than 30 days remain, or when ``force``
    is true.  ``hostname`` / ``lan_ips`` default to ``socket.gethostname()`` / ``util.lan_addresses()``; ``now`` is a
    test hook.  Returns ``True`` when a certificate that ``ssl`` accepts is in place.  ``False`` means "no
    certificate": a warning was logged, unusable files were removed and the caller continues without TLS.  A stale
    but valid certificate survives a failed regeneration (and still counts as ``True``).
    """
    tls_dir = Path(data_dir) / "tls"
    host = hostname or default_hostname()
    if lan_ips is None:
        primary, others = util.lan_addresses()
        lan_ips = ([primary] if primary else []) + others
    ips = usable_ips(lan_ips)
    stamp = time.time() if now is None else now
    cert, key = tls_dir / "cert.pem", tls_dir / "key.pem"
    try:
        tls_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("TLS disabled: cannot create %s (%s)", tls_dir.name, type(exc).__name__)
        return False
    if force:
        reason: Optional[str] = "regeneration requested"
    elif not (cert.exists() and key.exists()):
        reason = "certificate or key missing"
    else:
        reason = regeneration_reason(read_meta(tls_dir), wanted_san(host, ips), stamp)
    if reason is not None:
        exe = find_openssl()
        if exe is None:
            log.warning("TLS needs a certificate (%s) but the openssl binary was not found", reason)
        else:
            log.info("generating the TLS certificate: %s", reason)
            if _generate(tls_dir, exe, host, ips, stamp):
                reason = None
    try:
        build_context(cert, key)
    except (OSError, ssl.SSLError) as exc:
        log.warning("TLS disabled: no usable certificate (%s); serving plain HTTP", type(exc).__name__)
        for stale in (cert, key):
            try:
                os.remove(str(stale))
            except OSError:
                log.debug("could not remove %s", stale.name)
        return False
    if reason is not None:
        log.warning("keeping the existing certificate although it should be replaced (%s)", reason)
    return True


def cert_fingerprint(data_dir: Union[str, "os.PathLike[str]"]) -> Optional[str]:
    """SHA-256 of the DER of ``<data_dir>/tls/cert.pem`` as ``AA:BB:...``; ``None`` when absent or unreadable."""
    return fingerprint(Path(data_dir) / "tls" / "cert.pem")
