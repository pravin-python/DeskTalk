"""Wiring and lifecycle of the server (SPEC 2.3, 2.4, 2.6, 5.8, 6.2): :class:`Server`.

``Server(cfg).run()`` blocks on the calling thread (installing signal handlers when that is the main thread);
``Server(cfg).start()`` runs ``run()`` on a daemon thread and returns once the sockets are bound (tests); ``.port`` is
the bound port, ``.stop()`` is thread-safe and idempotent, ``.join()`` waits for the graceful shutdown to finish.

``hub.py`` and ``api.py`` are imported by name when the server starts (``chatd.hub.Hub(db, cfg)`` and
``chatd.api.register_routes(router, hub, db, cfg)``); ``hub_factory`` / ``routes_factory`` replace them (tests).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import platform
import shutil
import signal
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from . import __version__, auth, maintenance, tlsutil, util, websocket
from . import db as dbmod
from . import http as httpmod
from .config import Config
from .http import HttpServer, RedirectServer, Router

log = logging.getLogger("chatd.app")

# ---- background task intervals in seconds (SPEC 2.4; all pass through util.scaled) -------------------------------
TICK_S = 1.0
CONTROL_POLL_S = 2.0
REVALIDATE_S = 300.0
LIVENESS_S = 10.0
HEARTBEAT_S = 60.0
ORPHAN_SWEEP_S = 900.0
ORPHAN_FIRST_S = 120.0
HOURLY_S = 3600.0
BACKUP_TICK_S = 900.0
BACKUP_FIRST_S = 120.0
BACKUP_EVERY_S = 20 * 3600.0
BACKUP_KEEP = 7
SHUTDOWN_HUB_S = 8.0
SHUTDOWN_HTTP_S = 3.0
FILE_LIMIT = 8192

WIN_SYSTEM_SID = "S-1-5-18"
WIN_LOCAL_SERVICE_SID = "S-1-5-19"
WIN_ADMINS_SID = "S-1-5-32-544"
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

HubFactory = Callable[[Any, Config], Any]
RoutesFactory = Callable[[Router, Any, Any, Config], None]


# --------------------------------------------------------------------------------------------------------------
# Data directory, process limits, keep-awake
# --------------------------------------------------------------------------------------------------------------


def _current_windows_sid() -> Optional[str]:
    """SID of the account this process runs as (``whoami /user``), or ``None`` when it cannot be determined."""
    try:
        out = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=15, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    fields = [f.strip().strip('"') for f in out.strip().split(",")]
    return fields[-1] if fields and fields[-1].startswith("S-1-") else None


def _restrict_windows_acl(path: Path) -> None:
    """SPEC 10.2 data-dir ACL: SYSTEM and Administrators full, LocalService modify, no inheritance.

    The account running the server is granted full control as well (unless it is SYSTEM or LocalService): an
    interactive development run would otherwise lock itself out of the directory it just created.
    """
    grants = [
        "*%s:(OI)(CI)F" % WIN_SYSTEM_SID,
        "*%s:(OI)(CI)F" % WIN_ADMINS_SID,
        "*%s:(OI)(CI)M" % WIN_LOCAL_SERVICE_SID,
    ]
    sid = _current_windows_sid()
    if sid is not None and sid not in (WIN_SYSTEM_SID, WIN_LOCAL_SERVICE_SID):
        grants.append("*%s:(OI)(CI)F" % sid)
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r"] + grants, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=60, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not restrict the data directory ACL: %s", type(exc).__name__)
        return
    if result.returncode != 0:
        log.warning("icacls refused to restrict the data directory (exit %d); run `python -m chatd doctor`",
                    result.returncode)


def prepare_data_dir(cfg: Config) -> None:
    """Create the data directory tree (SPEC 2.1): ``uploads/.tmp``, ``logs``, ``backups``, ``control``.

    POSIX: ``umask 077`` and mode ``0700``.  Windows: the ACL of SPEC 10.2 when the directory is created here.
    Raises ``util.FatalError`` (exit 78) when the directory cannot be created or written.
    """
    if os.name == "posix":
        os.umask(0o077)
    created = not cfg.data_dir.exists()
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created and os.name == "nt":
            _restrict_windows_acl(cfg.data_dir)
        for sub in (cfg.tmp_dir, cfg.logs_dir, cfg.control_dir, cfg.backup_dir):
            sub.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = cfg.control_dir / ("write-test-%d" % os.getpid())
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise util.FatalError(
            "the data directory %s is not usable (%s): create it, make it writable for this account or choose another "
            "--data-dir; `python -m chatd doctor` explains the details" % (cfg.data_dir, type(exc).__name__)
        )


def raise_file_limit() -> None:
    """Raise the soft ``RLIMIT_NOFILE`` to ``min(hard, 8192)`` on POSIX (SPEC 5.1)."""
    try:
        import resource
    except ImportError:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = FILE_LIMIT if hard == resource.RLIM_INFINITY else min(hard, FILE_LIMIT)
        if soft == resource.RLIM_INFINITY or soft >= target:
            return
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError) as exc:
        log.warning("could not raise the open-file limit: %s", type(exc).__name__)


class _KeepAwake:
    """Holds ``ES_CONTINUOUS | ES_SYSTEM_REQUIRED`` on the calling thread (Windows; SPEC 2.2); a no-op elsewhere."""

    def __init__(self) -> None:
        self._held = False

    def acquire(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            if ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):  # type: ignore[attr-defined]
                self._held = True
        except (AttributeError, OSError) as exc:
            log.warning("could not request keep-awake: %s", type(exc).__name__)

    def release(self) -> None:
        if not self._held:
            return
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            log.debug("could not release keep-awake")
        self._held = False


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
    """SPEC 2.6: peers that vanish are routine on Windows and only logged at DEBUG."""
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)) or (
        isinstance(exc, OSError) and getattr(exc, "winerror", None) in (64, 995)
    ):
        log.debug("connection error ignored: %s", type(exc).__name__)
        return
    loop.default_exception_handler(context)


# --------------------------------------------------------------------------------------------------------------
# Startup banner
# --------------------------------------------------------------------------------------------------------------


def format_urls(cfg: Config, port: int, primary: Optional[str], others: List[str]) -> List[str]:
    """The lines of the "Share this link" block (SPEC 5.8)."""
    scheme = cfg.scheme
    suffix = "" if (scheme, port) in (("http", 80), ("https", 443)) else ":%d" % port
    if cfg.host not in ("0.0.0.0", "::", ""):
        return ["Share this link:", "    %s://%s%s/" % (scheme, cfg.host, suffix)]
    lines = []
    if primary is None and not others:
        lines.append("No network address yet - connect this PC to the network, then open http://localhost%s/" % suffix)
    else:
        lines.append("Share this link (open it in a browser on any device on this network):")
        lines.append("    %s://%s%s/" % (scheme, primary or others[0], suffix))
        remaining = others if primary else others[1:]
        if remaining:
            lines.append("Other adapters (may not be reachable by phones):")
            lines.extend("    %s://%s%s/" % (scheme, address, suffix) for address in remaining)
    lines.append("On this PC: %s://localhost%s/" % (scheme, suffix))
    return lines


def _emit(lines: List[str]) -> None:
    """Print the banner to stdout (when there is one) and log it."""
    for line in lines:
        log.info("%s", line)
    if sys.stdout is not None:
        try:
            print("\n".join(lines), flush=True)
        except (OSError, ValueError):
            log.debug("stdout is not writable")


# --------------------------------------------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------------------------------------------


def _default_hub_factory(database: Any, cfg: Config) -> Any:
    return importlib.import_module(__package__ + ".hub").Hub(database, cfg)


def _default_routes_factory(router: Router, hub: Any, database: Any, cfg: Config) -> None:
    importlib.import_module(__package__ + ".api").register_routes(router, hub, database, cfg)


class Server:
    """The whole application: database, hub, HTTP/WebSocket transport and the background tasks of SPEC 2.4."""

    def __init__(
        self, cfg: Config, hub_factory: Optional[HubFactory] = None, routes_factory: Optional[RoutesFactory] = None
    ) -> None:
        self.cfg = cfg
        self._hub_factory = hub_factory or _default_hub_factory
        self._routes_factory = routes_factory or _default_routes_factory
        self._port = cfg.port
        self.db: Any = None
        self.hub: Any = None
        self.http: Optional[HttpServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._stop_requested = False
        self._ready = threading.Event()
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._startup_error: Optional[BaseException] = None
        self._lock: Optional[util.InstanceLock] = None
        self._redirect: Optional[RedirectServer] = None
        self._tasks: Set["asyncio.Task[None]"] = set()
        self._keep_awake = _KeepAwake()
        self._primary_address: Optional[str] = None
        self._control_state: Dict[str, Any] = {}
        self._signals_installed: List[Any] = []

    # ---- public API (SPEC 6.2) ---------------------------------------------------------------------------------

    @property
    def port(self) -> int:
        """The bound port (``--port 0`` binds a free one); the configured port before the sockets are bound."""
        return self._port

    def run(self) -> None:
        """Serve until a signal, ``control/stop.request`` or :meth:`stop`; returns after the graceful shutdown."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc  # recorded before start() wakes up
            raise
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                shutdown_executor = getattr(loop, "shutdown_default_executor", None)
                if shutdown_executor is not None:
                    loop.run_until_complete(shutdown_executor())
            finally:
                self._loop = None
                loop.close()
                self._ready.set()
                self._done.set()

    def start(self, timeout: float = 120.0) -> None:
        """Run :meth:`run` on a daemon thread; returns once the sockets are bound or raises the startup failure."""

        def target() -> None:
            try:
                self.run()
            except BaseException as exc:  # noqa: BLE001 - start() re-raises startup failures; later ones are logged
                if self._startup_error is None:
                    self._startup_error = exc
                    if not isinstance(exc, util.FatalError):
                        log.exception("the server stopped with an error")

        self._thread = threading.Thread(target=target, name="desktalk-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("the server did not start within %.0f s" % timeout)
        if self._startup_error is not None:
            raise self._startup_error

    def stop(self) -> None:
        """Begin the graceful shutdown (thread-safe, idempotent, safe before the server is up or after it ended)."""
        self._stop_requested = True
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                log.debug("stop() after the event loop closed")

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait until :meth:`run` has finished; ``False`` when ``timeout`` expired first."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return self._done.wait(timeout)

    # ---- startup -----------------------------------------------------------------------------------------------

    async def _main(self) -> None:
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stop_requested:
            self._stop_event.set()
        loop.set_exception_handler(_loop_exception_handler)
        self._install_signals(loop)
        try:
            await self._start_services(loop)
            self._ready.set()
            await self._stop_event.wait()
            log.info("shutting down")
        finally:
            try:
                await self._shutdown(loop)
            finally:
                self._restore_signals()
                util.stop_logging()

    async def _start_services(self, loop: asyncio.AbstractEventLoop) -> None:
        cfg = self.cfg
        problem = dbmod.sqlite_problem()
        if problem is not None:
            raise util.FatalError(problem)
        util.set_test_scale(cfg.test_scale)
        prepare_data_dir(cfg)
        data_dir = str(cfg.data_dir)
        self._lock = util.instance_lock(data_dir, {"port": cfg.port, "tls": cfg.tls, "version": __version__})
        util.setup_logging(cfg.log_level, cfg.data_dir, attach_file=True)
        for warning in cfg.warnings:
            log.warning("%s", warning)
        raise_file_limit()
        await self._startup_recovery(loop)
        self.db = dbmod.Database(str(cfg.db_path), cfg=cfg, readers=3)
        await loop.run_in_executor(None, self.db.open)
        auth.configure(
            scrypt_n=cfg.scrypt_n, session_days=cfg.session_days, min_password_len=cfg.min_password_len,
            test_limits=cfg.test_limits,
        )
        await self._warn_about_data_dir(loop)
        setup_code = await auth.ensure_setup_code(self.db, data_dir)

        context = await self._tls_context(loop)
        hub = self.hub = self._hub_factory(self.db, cfg)
        await hub.start()
        router = Router(cfg, self.db)
        httpmod.register_http_routes(router, self.db, cfg, stopping=lambda: bool(getattr(hub, "stopping", False)))
        self._routes_factory(router, hub, self.db, cfg)
        router.add("GET", "/ws", lambda req: websocket.upgrade_response(req, hub), auth="cookie")
        self.http = HttpServer(cfg, router, context)
        self.http.ws_registry = websocket.WsRegistry(cfg)
        try:
            await asyncio.wait_for(loop.run_in_executor(None, httpmod.prime_host_names), 5)
        except asyncio.TimeoutError:
            log.warning("looking up this machine's host name took too long; only its short name is accepted")
        try:
            self._port = await self.http.start()
        except OSError as exc:
            raise util.FatalError(
                "cannot listen on %s:%d (%s); is another program using the port? `python -m chatd doctor` can tell"
                % (cfg.host, cfg.port, type(exc).__name__), 1
            )
        self._lock.update({"port": self._port, "tls": cfg.tls})
        await self._start_redirect()
        self._keep_awake_start()
        await self._control_baseline()
        await self._refresh_storage_bytes()
        self._spawn_background()
        await self._announce(loop, setup_code)

    async def _startup_recovery(self, loop: asyncio.AbstractEventLoop) -> None:
        """SPEC 2.4: a stale ``stop.request`` must never stop the new process; temp uploads are garbage."""
        stop_request = self.cfg.control_dir / "stop.request"
        await loop.run_in_executor(None, util.retry_file_op, os.remove, str(stop_request))
        util.pending_delete_load(self.cfg.data_dir)
        await loop.run_in_executor(None, self._clear_tmp_uploads)

    def _clear_tmp_uploads(self) -> None:
        try:
            entries = list(os.scandir(str(self.cfg.tmp_dir)))
        except OSError:
            return
        for entry in entries:
            util.retry_file_op(os.remove, entry.path)

    async def _warn_about_data_dir(self, loop: asyncio.AbstractEventLoop) -> None:
        """Log what differs between the configuration and the database (SPEC 2.1, 2.4)."""
        cfg = self.cfg

        def read(conn: Any) -> Dict[str, Any]:
            return {
                "users": dbmod.user_count(conn),
                "name": dbmod.get_meta(conn, "workspace_name"),
                "registration": dbmod.get_meta(conn, "registration_open"),
            }

        facts = await self.db.run_read(read)
        if facts["users"] == 0:
            def populated() -> bool:
                for directory in (cfg.uploads_dir, cfg.backup_dir):
                    try:
                        if any(e.name != ".tmp" for e in os.scandir(str(directory))):
                            return True
                    except OSError:
                        continue
                return False

            if await loop.run_in_executor(None, populated):
                log.warning("the database has no users but uploads/ or backups/ contain data: is --data-dir wrong?")
        name_source = cfg.source("workspace_name")
        if facts["name"] is not None and name_source != "default" and facts["name"] != cfg.workspace_name:
            log.warning("workspace_name from %s differs from the database value: DB overrides", name_source)
        reg_source = cfg.source("registration_open")
        if facts["registration"] is not None and reg_source != "default" and (
            (facts["registration"] == "1") != cfg.registration_open
        ):
            log.warning("registration_open from %s differs from the database value: DB overrides", reg_source)

    async def _tls_context(self, loop: asyncio.AbstractEventLoop) -> Optional[ssl.SSLContext]:
        """``serve --tls``: (re)generate the certificate when needed and build the context; ``None`` = plain HTTP."""
        cfg = self.cfg
        if not cfg.tls:
            return None
        context: Optional[ssl.SSLContext] = None
        if await loop.run_in_executor(None, tlsutil.ensure_cert, cfg.data_dir):
            try:
                context = tlsutil.build_context(cfg.tls_dir / "cert.pem", cfg.tls_dir / "key.pem")
            except (OSError, ssl.SSLError) as exc:
                log.warning("the TLS certificate cannot be loaded (%s)", type(exc).__name__)
        if context is None:
            log.warning("TLS could not be enabled; serving plain HTTP")
            cfg.tls = False
        return context

    async def _start_redirect(self) -> None:
        cfg = self.cfg
        if not (cfg.tls and cfg.redirect_port):
            return
        redirect = RedirectServer(cfg.host, cfg.redirect_port, self._port)
        try:
            await redirect.start()
        except OSError as exc:
            log.warning("the redirect listener on port %d could not start: %s", cfg.redirect_port, type(exc).__name__)
            return
        self._redirect = redirect

    def _keep_awake_start(self) -> None:
        if not self.cfg.allow_sleep:
            self._keep_awake.acquire()

    async def _announce(self, loop: asyncio.AbstractEventLoop, setup_code: Optional[str]) -> None:
        cfg = self.cfg
        primary, others = await loop.run_in_executor(None, util.lan_addresses)
        self._primary_address = primary
        lines = [
            "DeskTalk %s  (Python %s, %s)" % (__version__, platform.python_version(), platform.system()),
            "Data dir: %s" % cfg.data_dir,
        ]
        lines.extend(format_urls(cfg, self._port, primary, others))
        if setup_code is not None:
            lines.append("SETUP CODE for the first admin account: %s" % setup_code)
            lines.append("Open %s://127.0.0.1:%d/ and create the admin account." % (cfg.scheme, self._port))
        if cfg.tls:
            lines.append("TLS is on (self-signed certificate: browsers show a one-time warning).")
        else:
            lines.append("Plain HTTP: anyone on this network can read passwords and messages. Consider --tls.")
        _emit(lines)

    # ---- signals -----------------------------------------------------------------------------------------------

    def _install_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        """SIGINT/SIGTERM/SIGBREAK -> graceful stop; only on the main thread (the tests run the server elsewhere)."""
        if threading.current_thread() is not threading.main_thread():
            return
        stop = self._stop_event
        assert stop is not None
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, stop.set)
                self._signals_installed.append((sig, None))
            except (NotImplementedError, ValueError, RuntimeError):
                try:
                    previous = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
                except (ValueError, OSError):
                    log.debug("cannot install a handler for %s", name)
                    continue
                self._signals_installed.append((sig, previous if previous is not None else signal.SIG_DFL))

    def _restore_signals(self) -> None:
        loop = self._loop
        for sig, previous in self._signals_installed:
            try:
                if previous is None:
                    if loop is not None:
                        loop.remove_signal_handler(sig)
                else:
                    signal.signal(sig, previous)
            except (ValueError, OSError, RuntimeError):
                log.debug("could not restore a signal handler")
        self._signals_installed = []

    # ---- background tasks (SPEC 2.4) ---------------------------------------------------------------------------

    def _spawn_background(self) -> None:
        loop = asyncio.get_running_loop()
        jobs = [
            ("tick", TICK_S, TICK_S, self._tick),
            ("control-poll", CONTROL_POLL_S, CONTROL_POLL_S, self._control_poll),
            ("revalidate", REVALIDATE_S, REVALIDATE_S, self._revalidate),
            ("liveness", LIVENESS_S, LIVENESS_S, self._liveness),
            ("heartbeat", HEARTBEAT_S, HEARTBEAT_S, self._heartbeat),
            ("lan-addresses", HEARTBEAT_S, HEARTBEAT_S, self._check_addresses),
            ("orphan-sweep", ORPHAN_SWEEP_S, ORPHAN_FIRST_S, self._orphan_sweep),
            ("hourly", HOURLY_S, HOURLY_S, self._hourly),
            ("backup", BACKUP_TICK_S, BACKUP_FIRST_S, self._backup_tick),
        ]
        for name, interval, first, func in jobs:
            task = loop.create_task(self._periodic(name, interval, first, func))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _periodic(self, name: str, interval: float, first: float, func: Callable[[], Awaitable[None]]) -> None:
        delay = first
        while True:
            await asyncio.sleep(util.scaled(delay))
            delay = interval
            try:
                await func()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failing job is logged with its traceback and retried next interval
                log.exception("background job %s failed", name)

    async def _tick(self) -> None:
        """Every second: ``control/stop.request`` and ``hub.sweep_typing()`` (SPEC 2.4, 6.1)."""
        stop_request = self.cfg.control_dir / "stop.request"
        if stop_request.exists():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, util.retry_file_op, os.remove, str(stop_request))
            log.info("stop requested through control/stop.request")
            self.stop()
            return
        self.hub.sweep_typing()

    async def _control_baseline(self) -> None:
        version = await self.db.run_raw(dbmod.data_version)
        self._control_state = {"reload": self._reload_mtime(), "data_version": version}

    def _reload_mtime(self) -> Optional[int]:
        try:
            return (self.cfg.control_dir / "reload").stat().st_mtime_ns
        except OSError:
            return None

    async def _control_poll(self) -> None:
        """Every 2 s: another process (the CLI, an own-connection job) changed the database (SPEC 2.4)."""
        reload_mtime = self._reload_mtime()
        version = await self.db.run_raw(dbmod.data_version)
        known = self._control_state
        changed = reload_mtime != known.get("reload") or version != known.get("data_version")
        self._control_state = {"reload": reload_mtime, "data_version": version}
        if changed:
            await self.hub.external_change()
        await self._clear_setup_code_if_done()

    async def _clear_setup_code_if_done(self) -> None:
        """The setup code is deleted as soon as the first admin exists (SPEC 4.3(1)), however the account was made."""
        if not (self.cfg.data_dir / "setup_code.txt").exists():
            return
        if not await self.db.run_read(dbmod.needs_setup):
            await asyncio.get_running_loop().run_in_executor(None, auth.clear_setup_code, str(self.cfg.data_dir))
            log.info("the first account exists: setup code removed")

    async def _revalidate(self) -> None:
        await self.hub.revalidate_all()

    async def _liveness(self) -> None:
        registry = self.http.ws_registry if self.http is not None else None
        if registry is not None:
            registry.check_liveness()

    async def _heartbeat(self) -> None:
        """Every 60 s: crash-safe ``users.last_seen_at`` of everyone online (SPEC 8.4)."""
        ids = list(self.hub.online_user_ids())
        if ids:
            await self.db.run(dbmod.set_last_seen, ids, util.now())

    async def _check_addresses(self) -> None:
        loop = asyncio.get_running_loop()
        primary, others = await loop.run_in_executor(None, util.lan_addresses)
        if primary != self._primary_address:
            self._primary_address = primary
            for line in format_urls(self.cfg, self._port, primary, others):
                log.info("%s", line)

    async def _orphan_sweep(self) -> None:
        counts = await asyncio.get_running_loop().run_in_executor(None, self._sweep_orphans_sync)
        if any(counts.values()):
            log.info("orphan sweep: %s", ", ".join("%s=%d" % kv for kv in sorted(counts.items()) if kv[1]))

    def _sweep_orphans_sync(self) -> Dict[str, int]:
        conn = self.db.connect_extra()
        try:
            counts = dbmod.sweep_orphans(conn, str(self.cfg.uploads_dir))
        finally:
            conn.close()
        try:
            usage = shutil.disk_usage(str(self.cfg.data_dir))
            if usage.free < usage.total * 0.10:
                log.warning("free disk space is below 10 %% (%d MiB left)", usage.free // (1024 * 1024))
        except OSError:
            log.debug("disk usage unavailable")
        return counts

    async def _hourly(self) -> None:
        """Purge expired sessions, refresh the cached storage size, retry deferred deletions."""
        purged = await self.db.run(dbmod.purge_expired_sessions)
        if purged:
            log.info("purged %d expired session(s)", purged)
        await self._refresh_storage_bytes()
        remaining = await asyncio.get_running_loop().run_in_executor(None, util.pending_delete_sweep)
        if remaining:
            log.warning("%d file(s) still cannot be deleted", remaining)

    async def _refresh_storage_bytes(self) -> None:
        """``hub.storage_bytes``: total attachment size, refreshed hourly (``admin.stats.storage_bytes``)."""
        self.hub.storage_bytes = await self.db.run_read(dbmod.attachment_storage_bytes)

    async def _backup_tick(self) -> None:
        """Elapsed-time based automatic backup (SPEC 2.4): when the last one is older than 20 h."""
        last = await self.db.run_read(dbmod.get_meta, "last_backup_at")
        try:
            age = util.now() - float(last) if last is not None else None
        except ValueError:
            age = None
        if age is not None and age <= BACKUP_EVERY_S:
            return
        loop = asyncio.get_running_loop()
        try:
            path = await loop.run_in_executor(
                None, maintenance.backup, str(self.cfg.db_path), str(self.cfg.backup_dir), False, "auto"
            )
        except (maintenance.MaintenanceError, OSError) as exc:
            log.error("automatic backup failed (%s); retrying in 15 minutes", type(exc).__name__)
            return
        await self.db.run(dbmod.set_meta, "last_backup_at", util.now())
        await loop.run_in_executor(None, maintenance.prune_backups, str(self.cfg.backup_dir), "auto", BACKUP_KEEP)
        log.info("automatic backup written: %s", os.path.basename(path))

    # ---- shutdown ----------------------------------------------------------------------------------------------

    async def _shutdown(self, loop: asyncio.AbstractEventLoop) -> None:
        """The graceful sequence of SPEC 2.4; every step tolerates a partially started server.

        The listening sockets close first: the installer's ``stop`` waits (up to 10 s) for the port to refuse
        connections before it ends the task.
        """
        http = self.http
        if http is not None:
            http.stopping = True
            http.close_listeners()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        online: List[int] = []
        if self.hub is not None:
            try:
                online = list(self.hub.online_user_ids())
            except Exception:  # noqa: BLE001 - shutdown must continue whatever the hub says
                log.exception("could not read the online users")
        if self.hub is not None:
            try:
                await asyncio.wait_for(self.hub.shutdown(), util.scaled(SHUTDOWN_HUB_S))
            except asyncio.TimeoutError:
                log.warning("the hub did not finish its in-flight work in time")
            except Exception:  # noqa: BLE001 - shutdown must continue whatever the hub says
                log.exception("hub shutdown failed")
        if http is not None:
            if http.ws_registry is not None:
                await http.ws_registry.close_all(1001, "restart", util.scaled(2.0))
            http.abort_connections()
            await http.wait_connections(util.scaled(SHUTDOWN_HTTP_S))
            await http.wait_closed(util.scaled(SHUTDOWN_HTTP_S))
        if self._redirect is not None:
            await self._redirect.close()
        if self.db is not None:
            if online:
                try:
                    await self.db.run(dbmod.set_last_seen, online, util.now())
                except Exception:  # noqa: BLE001 - last_seen is best effort at shutdown
                    log.exception("could not store last_seen")
            await loop.run_in_executor(None, self.db.close)
        auth.hasher.shutdown()
        self._keep_awake.release()
        if self._lock is not None:
            self._lock.release()
        log.info("stopped")
