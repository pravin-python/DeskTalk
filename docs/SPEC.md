koi man maa ho to# FreeChat 2 — LAN WhatsApp/Teams-style chat (offline, stdlib-only server)

Status: **contract document**. Every module is implemented against this file. If code and spec disagree, fix the code
(or, if the spec is wrong, fix the spec first and note it in "Spec changelog" at the bottom).

---------------------------------------------------------------------------------------------------

## 0. Goals, constraints, decisions

**Product goal.** An internal "WhatsApp + Teams" for a company LAN. Works with **no internet**. One machine runs the
server (auto-starts at boot as a service); everybody else just opens `http://<server-ip>:8765` in a browser
(desktop *or* phone on the office Wi-Fi). No client install.

**Hard constraints**
1. **Server = Python standard library only.** No pip. Minimum Python **3.8** (must also run on 3.10–3.13).
   Forbidden (3.9+/3.10+/3.11+ only): `match` statements, `X | Y` in runtime-evaluated annotations (use
   `from __future__ import annotations` at top of every module), `dict | dict`, `str.removeprefix/removesuffix`,
   `asyncio.to_thread`, `asyncio.timeout`, `asyncio.TaskGroup`, `zoneinfo`, parenthesised multi-line `with`.
   Use `loop.run_in_executor`, `asyncio.wait_for`, `asyncio.get_running_loop()`.
   Also forbidden (newer than 3.8): `Path.is_relative_to/with_stem/readlink` (use `os.path.commonpath` or `Path.relative_to` in
   try/except), `dataclass(slots=…/kw_only=…)`, runtime `list[…]/dict[…]/tuple[…]` outside annotations, `zip(strict=)`, `int.bit_count`,
   `itertools.pairwise`, `aiter/anext`, `contextlib.aclosing`, `typing.TypeAlias/ParamSpec`, `asyncio.Runner/Barrier`, `datetime.UTC`, `tomllib`,
   `hashlib.file_digest`, `except*`, `random.randbytes`, `functools.cache`, `math.lcm`, `logging.basicConfig(encoding=)`, `argparse.BooleanOptionalAction`,
   `asyncio.get_event_loop()` outside a coroutine. `asyncio.Lock/Queue/Event/Semaphore` are created **only inside the running loop** (never at import
   time or in `__init__` before `asyncio.run`, they bind to the wrong loop on 3.8/3.9). Every `create_task()` result is stored in a set until done
   (the loop keeps only weak references). `tests/test_syntax_floor.py` runs `ast.parse(src, feature_version=(3,8))` over every module and greps for the names above.
2. **Client = plain HTML/CSS/JS ES modules**, no build step, no npm, **no CDN / no external URL of any kind**
   (the LAN has no internet). No web fonts (use the system font stack). No emoji images (Unicode emoji only).
3. **Cross-platform server**: Windows 10/11, Ubuntu/Debian, macOS. Use `pathlib`, `encoding="utf-8"` everywhere,
   never `os.fork`, never rely on POSIX-only signals (`loop.add_signal_handler` raises NotImplementedError on
   Windows → try/except and fall back to `signal.signal`).
4. **Persistence = SQLite** (`sqlite3` module, WAL). NOTE: the dev machine's default `python` (3.10.11) has a broken
   install without the `sqlite3` package; Python 3.11/3.12/3.13 work. The server MUST detect a missing/old
   `sqlite3` at startup and print a clear, actionable message (what is wrong + "run `python -m chatd doctor`")
   instead of a traceback.
   **Minimum SQLite 3.24** (UPSERT available; `doctor` FAILS below it; Ubuntu 18.04's python3.8 links 3.22 and is therefore unsupported). Forbidden SQL: `RETURNING`
   (3.35), `->`/`->>` (3.38), `unixepoch()`, `STRICT` tables, `RIGHT/FULL JOIN`, SQL math functions, and **all JSON1 functions** (mentions live in a table, §3).
5. **Never block the event loop**: all SQLite calls, password hashing (scrypt), file hashing and disk-heavy work run
   in executors (§2.3).
6. **Security baseline** (LAN does not mean trusted): authenticated sessions, hashed passwords, CSRF/CSWSH defences,
   no HTML injection (UI never uses `innerHTML` with user data), uploads can't execute in the browser,
   path-traversal-proof static/file serving, rate limits. Details in §11 and the threat model below.

**Decisions already taken (do not re-litigate)**
| Topic | Decision |
|---|---|
| Client type | Web app served by the server itself (replaces the Tkinter client; old code is kept in `legacy/`). |
| Transport | HTTP/1.1 + WebSocket (RFC 6455) implemented on `asyncio` streams. JSON text frames. |
| Default port | **8765** (TCP). Old legacy chat used 9009/9010 — do not use those. |
| Bind | `0.0.0.0` by default. |
| Data dir | `--data-dir`. `serve` run by hand defaults to `<app>/data` (development); **installers default to an OS location outside the app tree** (§10.1: `%ProgramData%\FreeChat\data`, `/var/lib/freechat`, `/Library/Application Support/FreeChat/data`). Contains `chat.db`, `uploads/`, `logs/`, `backups/`, `tls/`, `control/`. Never committed to git (§1). |
| Identity | username + password accounts. First account created becomes **admin** (guarded by a one-time setup code, §4.3). |
| Message order | By global integer `messages.id` (never by timestamp). |
| Receipts | Per-member watermarks (`delivered`, `read`) + per-message `receipts` rows for exact times (rows are written only in chats with ≤ 50 members, §8.1; larger chats derive state from the watermarks). |
| Search | SQL `LIKE` (no FTS). |
| Calls (audio/video), E2E encryption, web-push | **Out of scope** (documented as future work, §13). |

**Insecure-context facts the UI must respect.** Browsers treat `http://172.31.x.x:8765` as an *insecure context*
(only `localhost` and HTTPS are secure). Therefore, in the UI, **feature-detect and degrade gracefully**:
`crypto.randomUUID` (absent → use `crypto.getRandomValues`), `navigator.clipboard` (absent → `document.execCommand('copy')`
fallback), `window.Notification` (may be unavailable/denied), `navigator.mediaDevices` (absent → hide mic/voice-note
button), `navigator.serviceWorker` (don't use). The server can optionally serve HTTPS (`--tls`, §5.7) which unlocks
these; plain HTTP must remain fully usable for chatting, receipts, typing, in-page notifications and sounds. OS-level notifications and
background alerts have hard platform limits that are tabulated in §9.4 (support matrix) — they are **not** achievable on plain HTTP in general.

**Threat model.** The LAN is semi-trusted: any employee phone, guest on the Wi-Fi or compromised PC can reach the port.
* Plain HTTP gives **no confidentiality or integrity against other devices on the same network** (passive sniffing, ARP spoofing, in-flight rewriting
  of `/js/*.js`, theft of the session cookie). It is acceptable only on a wired/trusted VLAN. `--tls` (§5.7) removes the risk and is recommended;
  the installer prints a WARNING when installing without it (§10.1) and, over `http:` to a non-loopback host, the login screen and Settings show the banner
  "This connection is not encrypted. Anyone on this network can read your password and messages." (never on `localhost`, `127.0.0.1`, `[::1]`).
* Defended: unauthenticated LAN hosts (setup code, join code, throttles, §4), cross-site and DNS-rebinding pages in an employee's browser (§5.9),
  malicious members (authorisation on every call, §7.1.1, §11), resource exhaustion (§5.1, §6, §7.5).
* Not defended: an administrator or anyone with OS-level access to the server PC or its data dir (messages are not end-to-end encrypted, §13);
  passive sniffing while running plain HTTP.

---------------------------------------------------------------------------------------------------

## 1. Repository layout & file ownership

```
free-chat/
  README.md                      (docs owner)
  docs/SPEC.md                   this file
  docs/DB_API.md                 written by db owner (function table used by others)
  docs/ui-core-api.md            exported API of store/socket/outbox/api/notify/ui (one line per function)   [ui-core]
  chatd/                         ← python server package  (python -m chatd)
    __init__.py                  version string only
    __main__.py                  CLI entry: serve | create-admin | reset-password | backup | restore | doctor; boot-log wrapper (§2.2)
    config.py                    Config + CLI/env/config.json loading                      [transport]
    util.py                      small shared helpers (now(), ids, json, logging setup)    [db]
    db.py                        schema + migrations + ALL SQL (sync functions)            [db]
    auth.py                      password hashing/policy, tokens, login/registration throttling   [db]
    api.py                       REST handlers /api/info register login logout me password sessions [hub]
    websocket.py                 RFC6455 server side                                       [transport]
    http.py                      HTTP/1.1 server, router, static, uploads, Range, headers  [transport]
    files.py                     upload storage, content sniffing, kind classification, name sanitising [transport]
    tlsutil.py                   optional self-signed cert via `openssl` CLI + SAN refresh [transport]
    app.py                       wiring + background tasks (sweepers, backup, heartbeat, control-dir poll, keep-awake) [transport]
    hub.py                       WS protocol handlers, presence, typing, receipts, fan-out [hub]
  web/                           ← static client (served at /)
    index.html                                                                            [ui-core]
    css/base.css                                                                          [ui-core]
    css/auth.css css/sidebar.css css/panels.css                                           [ui-shell]
    css/conversation.css css/composer.css                                                 [ui-conv]
    js/main.js                                                                            [ui-core]
    js/core/{dom,util,api,socket,outbox,router,store,notify,sound,ui,icons,avatar}.js     [ui-core]
    js/unsupported.js            classic (non-module) script for the "please update your browser" page  [ui-core]
    manifest.webmanifest, img/icon-{180,192,512}.png     "Add to Home Screen" assets (§9.5)       [ui-core]
    js/lib/{richtext,emoji}.js                                                            [ui-conv]
    js/views/{auth,sidebar,newchat,settings,admin,searchpanel}.js                         [ui-shell]
    js/views/{conversation,messageview,composer,infodrawer,lightbox,forward}.js           [ui-conv]
  service/install_service.py     cross-platform installer (Win Task Scheduler / systemd / launchd)   [service]
  tests/                         unittest suites + tests/wsclient.py (+ test_query_plans.py, test_syntax_floor.py, §12) [tests]
  legacy/                        old Tkinter/line-JSON chat (moved there at the end by the orchestrator)
  server.py                      thin wrapper (created by the orchestrator): `sys.path.insert(0, dirname(abspath(__file__)))` then
                                 `from chatd.__main__ import main`. Services run `python -X utf8 -I server.py serve …` (isolated mode, §10.1).
  .gitignore                     the orchestrator adds NOW: `data/`, `*.db`, `*.db-wal`, `*.db-shm`, `*.pem`, `setup_code.txt`, `backups/`, `__pycache__/`
```
Owners edit **only** the files they own. Cross-owner changes go through the orchestrator.

---------------------------------------------------------------------------------------------------

## 2. Server runtime

### 2.1 Config (`chatd/config.py`)
Resolution: `data_dir` is resolved first (flag > env > default), then `<data_dir>/config.json`, then env, then flags, i.e.
**CLI flags > environment (`FREECHAT_*`) > `<data-dir>/config.json` > defaults.**
Booleans in env/config accept `1|true|yes|on` / `0|false|no|off` (case-insensitive); anything else ⇒ exit 2. Boolean flags come in pairs
(`--registration/--no-registration`, `--tls/--no-tls`) implemented as two `store_const` actions with `default=None`, so a flag can override a file/env value in
either direction (no `BooleanOptionalAction`, it is 3.9+). Unknown keys in `config.json` ⇒ a warning, not a crash.
| key | flag | env | default |
|---|---|---|---|
| host | `--host` | FREECHAT_HOST | `0.0.0.0` |
| port | `--port` | FREECHAT_PORT | `8765` |
| data_dir | `--data-dir` | FREECHAT_DATA_DIR | `<app>/data` (installers pass an explicit one, §10.1) |
| workspace_name | `--name` | FREECHAT_NAME | `FreeChat` |
| registration_open | `--registration` / `--no-registration` | FREECHAT_REGISTRATION | `false` (an admin can open it after setup, §4.3) |
| max_users | — | — | `2000` |
| max_upload_mb | `--max-upload-mb` | FREECHAT_MAX_UPLOAD_MB | `100` |
| blocked_extensions | — | FREECHAT_BLOCKED_EXT (space/comma list) | `exe scr com pif bat cmd msi msp vbs vbe wsf wsh hta lnk reg cpl dll jar` (an empty list is allowed) |
| tls | `--tls` / `--no-tls` | FREECHAT_TLS | `false` |
| redirect_port | `--redirect-port` | FREECHAT_REDIRECT_PORT | `0` (off; §5.7) |
| allowed_hosts | `--allowed-host` (repeatable) | FREECHAT_ALLOWED_HOSTS (comma list) | `[]` (extends the §5.9 default list) |
| allow_sleep | `--allow-sleep` | FREECHAT_ALLOW_SLEEP | `false` (the server keeps the PC awake, §2.2) |
| backup_dir | `--backup-dir` | FREECHAT_BACKUP_DIR | `<data>/backups` |
| edit_window_s | — | — | `900` (15 min) |
| delete_window_s | — | — | `172800` (48 h) |
| max_body_chars | — | — | `8000` |
| session_days | — | — | `30` |
| min_password_len | — | — | `8` (policy in §4.1) |
| scrypt_n | — | FREECHAT_SCRYPT_N | `65536` (**tests only** may lower it, e.g. 1024) |
| log_level | `--log-level` | FREECHAT_LOG | `INFO` |
`workspace_name`, `registration_open` and the join code (§4.3) are *runtime-editable by an admin* and then persisted in `meta` (DB wins over file/env after
the first admin edit). `serve` logs a WARNING when the effective flag/env/file value of such a key differs from the DB value ("DB overrides");
`doctor` prints every key with its value and its source (`flag|env|file|db|default`). On POSIX `serve` calls `os.umask(0o077)` first and creates the data dir
with mode `0o700`; on Windows the installer applies an ACL (§10.2).

### 2.2 CLI (`python -m chatd …`)
* `serve` (default) – run the server. **Single instance:** before binding anything it takes an exclusive non-blocking lock on `<data>/control/server.lock`
  (`msvcrt.locking(fd, LK_NBLCK, 1)` on Windows, `fcntl.flock(LOCK_EX|LOCK_NB)` on POSIX) holding JSON `{pid, port, tls, version}`; if the lock is held it prints
  "FreeChat is already running on this data dir (pid N, port P)" and exits 73. Prints the reachable URLs (§5.8), data dir, Python version and — while
  `needs_setup` — the setup code (§4.3). **Keep-awake:** unless `--allow-sleep`, `serve` holds a keep-awake request while running (Windows:
  `ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)` = `ES_CONTINUOUS|ES_SYSTEM_REQUIRED`, called from the thread that runs the loop; macOS/Linux get it from the
  service wrapper, §10.3/§10.4). It does not prevent lid-close or manual sleep (`doctor` hints, §10.1).
* `create-admin USERNAME [--password-stdin]` – create an admin account or promote an existing user; prompts via `getpass`. Rescue path for a forgotten admin
  password and the installer's first-admin step (§10.1). Reserved usernames (§4.3) are allowed here. Password policy §4.1.
* `reset-password USERNAME [--password-stdin] [--must-change]` – set a new password. Revokes the user's sessions. (CLI commands set `must_change_password` only with `--must-change`.)
* `backup [--out DIR] [--with-uploads]` – consistent snapshot via the sqlite backup API on its **own** connection (`pages=256, sleep=0.02`) into
  `<out>/chat-YYYYmmdd-HHMMSS.db` (default `<out>` = `backup_dir`); the `sessions` table is emptied in the copy; `--out` under `web/` is refused; with `--with-uploads` it creates
  `chat-YYYYmmdd-HHMMSS/` containing `chat.db` plus a copy (hard links where possible) of `uploads/`. Files get the data dir's restrictive permissions. Prints the path.
* `restore PATH [--data-dir D]` – refuses while the instance lock is held; moves `chat.db`, `chat.db-wal`, `chat.db-shm` into `<data>/backups/pre-restore-<ts>/`, copies the
  snapshot (or the `chat.db` inside a `--with-uploads` folder, restoring `uploads/` too) to `chat.db`, runs `PRAGMA integrity_check`, exits non-zero on failure.
  Never copy a snapshot over `chat.db` by hand while an old `-wal`/`-shm` exists (they would be replayed onto it): stop the service, delete both, copy, start.
* `doctor` – diagnostic table: Python version/path, `sqlite3` version (**FAIL < 3.24**), `scrypt` availability, `ssl`, `openssl` binary (§5.7), data dir exists/writable
  **and** `chat.db`/`-wal`/`-shm` writable by the current user (else print the owner and "run from an elevated shell or stop the service"), WAL-incompatible location (UNC path,
  OneDrive/Dropbox folder ⇒ FAIL), Windows ACL check (§10.2), `RLIMIT_NOFILE` (WARN < 4096), port check (§10.1), LAN IPs, firewall state (§10.2), AC standby timeout on Windows (§10.1),
  app version, schema version, effective config with sources (§2.1), the setup code while `needs_setup`. Exit code 0 only if the server can run.
* `--version` – prints the app version and the supported schema version.
* **Exit codes (all commands):** `0` ok, `1` runtime error, `2` usage, `73` already running (instance lock), `78` environment/config error (sqlite3 missing/old, data dir
  unwritable, WAL unavailable, DB schema newer than the code). The systemd unit uses `RestartPreventExitStatus=78` (§10.3).
* **Boot wrapper:** `__main__.py` wraps `main()` in `try/except BaseException`: any failure (including import-time errors) is written as a traceback to `<data>/logs/boot.log`
  (append, UTF-8; fallback `tempfile.gettempdir()` if the data dir is unusable) and printed to stderr, then exits with the code above. At the start of every command:
  `for s in (sys.stdout, sys.stderr): if s is not None: s.reconfigure(encoding="utf-8", errors="replace")` (tolerate `sys.stderr is None`).
* CLI commands that write (`create-admin`, `reset-password`) do **not** take the instance lock; they use `BEGIN IMMEDIATE` with `busy_timeout` and then touch
  `<data>/control/reload`; the running server notices within 2 s (§2.4). All CLI commands catch `sqlite3.OperationalError`/`PermissionError` on open and print the data dir's owner
  and the exact command to rerun (`install_service.py cli -- …`, §10.1).

### 2.3 Concurrency model
Single asyncio event loop; blocking work goes to executors. **All in-process durations** (typing expiry, ping timeouts, rate-limit windows, throttles, presence grace) use
`time.monotonic()` / `loop.time()`; `time.time()` is used only for persisted timestamps. A new message's `created_at = max(time.time(), chats.last_activity_at)` so a backwards
clock step never creates a message older than its predecessor in the chat.

**Database access (`chatd/db.py`).**
* *Writer*: one thread, one connection, `sqlite3.connect(path, isolation_level=None, check_same_thread=True)` (autocommit, **no implicit transactions**).
  `Database.run(fn, *a)` executes `fn(conn, *a)` on the writer inside `BEGIN IMMEDIATE … COMMIT`; on ANY exception it executes `ROLLBACK` if `conn.in_transaction` and re-raises.
  `fn` never calls commit/rollback/executescript. **RULE: one logical operation = exactly one `run()`.** All authorisation (membership, `users.disabled`, `only_admins_post`,
  edit/delete windows, attachment ownership/unattached), the `(sender_id, client_id)` dedupe lookup and every write happen inside that one `fn`; handlers never check in one await and write
  in another. `fn`s are plain synchronous code and never call back into asyncio or the hub. Replies and fan-out happen only after `COMMIT` returned. After `PRAGMA journal_mode=WAL`
  assert the returned value is `"wal"`, else log ERROR and exit 78.
* *Readers*: 3 threads (`Database.run_read(fn, *a)`), each with its own connection opened with `PRAGMA query_only=ON`, used for `ev.ready`, `chat.get`, `chat.history`, `msg.search/shared/starred/info`,
  `admin.users/stats` and the `/files` access check. A reader job is one short deferred `BEGIN … COMMIT` (consistent snapshot; never leave a read transaction open, it pins WAL checkpoints).
  A handler that must observe its own write uses the writer. Limits: `msg.search`/`msg.shared` run under a global `asyncio.Semaphore(1)`, at most one in flight per user, and
  `threading.Timer(2.0, conn.interrupt)`; an interrupted query answers `server_busy`. `ev.ready` builds run under `Semaphore(4)` (connections wait, they do not fail) and use set-based
  queries (one query for all my memberships, one `GROUP BY chat_id` for latest ids, never per-chat N+1). `admin.stats.storage_bytes` comes from a value cached hourly.
  Every connection registers `conn.create_function("fold", 1, lambda v: v.casefold() if v is not None else None, deterministic=True)` (search, §7.4).
* Backups, orphan sweeps and any `VACUUM` never run on the writer: they use their own connection.
* **Every query executed per request must show `SEARCH … USING INDEX` in `EXPLAIN QUERY PLAN`** (`tests/test_query_plans.py` asserts no `SCAN messages` for msg.send, history, the unread count,
  the `/files` access check and the orphan sweep; `msg.search`'s LIKE is the only allowed scan and is time-boxed).
* Hash executor: `ThreadPoolExecutor(2)` with at most 8 queued jobs; beyond that REST returns `429 server_busy` with `Retry-After: 1`.

**Ordering, locking, cancel-safety and fan-out** are specified in §7.6 (they are part of the protocol contract). **Authorisation state is never cached on a connection**: role, `disabled`
and chat membership are read inside the same `run()` that performs the action; fan-out recipients are computed from the committing transaction (§7.6).

### 2.4 Background tasks (`app.py`)
* every 1 s: expire typing entries (§8.3, emits `stop`); poll `<data>/control/stop.request` (§10.2).
* every 2 s: poll `<data>/control/reload` and `PRAGMA data_version` (on a reader connection). On change (another process, e.g. the CLI, wrote): re-validate every WS session
  (kick revoked/disabled users, §6.1) and re-broadcast `ev.user_update` for users whose row changed.
* every 10 s: close connections with no inbound frame/pong for > 60 s with 1001 (§6).
* every 60 s: `UPDATE users SET last_seen_at=? WHERE id IN (<online user ids>)` (crash-safe last seen, §8.4).
* every 15 min: orphan sweep — delete **unattached attachments older than 2 h** (row + file), `uploads/.tmp/*.part` older than 1 h and attachment rows whose file is missing;
  log WARNING when free disk < 10 %.
* hourly: purge expired sessions; refresh the cached `storage_bytes`; sweep the pending-delete list (§2.6).
* **Backup is elapsed-time based, not clock based** (the server PC may be switched off at night): a ticker every 15 min (and 2 min after startup) runs a backup when
  `now - meta.last_backup_at > 20 h`: own connection, `<backup_dir>/chat-YYYYMMDD-HHMM.db.tmp` then `os.replace` (retry rules §2.6), then update `meta.last_backup_at` and keep the newest 7.
  A failed backup logs ERROR and is retried in 15 min.
* **Startup recovery:** delete `uploads/.tmp/*`; treat every user as offline; if `users` is empty but `uploads/` or `backups/` contain data log a loud WARNING (possible wrong `--data-dir`);
  before applying migrations write `backups/pre-migrate-v<old>-<ts>.db`; if `meta.schema_version` is newer than the code exit 78 ("database is newer than this program").
* **Graceful shutdown** (SIGINT/SIGTERM/SIGBREAK, `control/stop.request`, thread-safe `Server.stop()`): set `hub.stopping=True` (new requests get `server_error` "restarting"; presence/typing
  broadcasts and per-user last_seen writes are suppressed); await in-flight mutating tasks ≤ 5 s; close every WS with 1001 and reason `"restart"` and abort every tracked HTTP connection;
  `server.close()` then `await asyncio.wait_for(server.wait_closed(), 3)` — **never an unguarded `wait_closed()`** (on 3.12+ it blocks while any client connection is still open);
  one `UPDATE users SET last_seen_at=?` for the online ids; then, on the writer thread, `PRAGMA wal_checkpoint(TRUNCATE)` and `conn.close()` (a sqlite3 connection can only be closed in its
  creating thread); then `executor.shutdown(wait=True)`. Signals: try `loop.add_signal_handler(sig, stop.set)`; on `(NotImplementedError, ValueError, RuntimeError)` fall back to
  `signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))`; skip signal setup when not on the main thread (the tests boot the server in a thread).
  Windows: Task Scheduler `End` and OS shutdown terminate the process without running Python, so the server must be crash-safe at any instruction (WAL, `synchronous=FULL`) and rely on the
  startup recovery above; `control/stop.request` is the supported graceful path (§10.2).

### 2.5 Logging
`logging` to stderr and `data/logs/freechat.log` (`RotatingFileHandler`, 5 MB × 5, utf-8; only `serve` attaches the file handler, CLI commands log to stderr only). Never log passwords,
tokens, cookie values, message bodies, or `str(e)` of JSON/DB errors that can embed request payloads (log `type(e).__name__` + traceback). Log: startup, connect/disconnect (user id, ip),
login failures, errors. **Log-injection rules:** login failures log the `username` only when it matches `^[a-z0-9._-]{3,32}$`, otherwise the literal `<invalid>` (users paste passwords into
the username field); every other attacker-controlled value (User-Agent, request path, file names) is logged via `ascii()`/`%r` truncated to 100 characters; the `Formatter` escapes `\r\n`;
the access log omits query strings. The stderr handler is wrapped in `QueueHandler` + `QueueListener` (§2.6).

### 2.6 Windows runtime rules
Verified traps on Windows 10 / Python 3.12; all modules obey them:
* **Retry for file operations.** Every `os.remove/os.replace/rename` of upload, backup or log files goes through a helper that retries 5 times (`50 ms · 2ⁿ`) on `PermissionError`/`OSError`
  (WinError 5/32: a download streaming the file, Defender scanning a fresh upload, Explorer preview). After the last attempt the path is appended to a pending-delete list swept hourly; failure
  never propagates into a request. Upload temp files live in `<data>/uploads/.tmp/` (same volume as the destination, so `os.replace` works).
* **Log rotation.** A `RotatingFileHandler` subclass whose `rotate()` swallows `OSError` and retries after another 1 MiB (rollover fails when another process holds the file open).
* **Closing connections.** Always `try: w.close(); await asyncio.wait_for(w.wait_closed(), 2) except (OSError, asyncio.TimeoutError): pass`. Install `loop.set_exception_handler` that logs
  `ConnectionResetError/ConnectionAbortedError/BrokenPipeError/WinError 64, 995` at DEBUG. **Never** set `WindowsSelectorEventLoopPolicy` (its `select()` caps at 512 sockets).
* **Console.** The stderr handler runs on a `QueueListener` thread so a stalled console (QuickEdit selection mode) never blocks the event loop; guard `sys.stderr is None`; always `python.exe`,
  never `pythonw.exe`.

---------------------------------------------------------------------------------------------------

## 3. Data model (SQLite)

Pragmas on every connection: `journal_mode=WAL` (the returned value is asserted, §2.3), `foreign_keys=ON`, `busy_timeout=5000`; **`synchronous=FULL` on the writer connection**
(WAL+NORMAL can lose *acknowledged* commits on power loss and then reuse message ids; replies and fan-out happen only after `COMMIT` returned); reader connections add `query_only=ON`.
Schema version stored in `meta('schema_version')`; `db.py` has an ordered list of migrations (v1 = this DDL); migrations are preceded by a backup and a newer-than-code database is
refused (§2.4). All timestamps are **float seconds since Unix epoch (UTC)**.

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);       -- schema_version, workspace_name, registration_open, join_code, instance_id (random hex, created with the DB, kept by restore), last_backup_at

CREATE TABLE users(
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,                      -- stored lowercase, ^[a-z0-9._-]{3,32}$
  display_name TEXT NOT NULL,                                        -- 1..40 chars, normalised (§4.3)
  display_key TEXT NOT NULL UNIQUE,                                  -- NFKC+casefold of display_name: display names are unique (§4.3)
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin','member')),
  status_text TEXT NOT NULL DEFAULT '',                              -- "about", <=140 chars
  read_receipts INTEGER NOT NULL DEFAULT 1,
  show_last_seen INTEGER NOT NULL DEFAULT 1,
  disabled INTEGER NOT NULL DEFAULT 0,
  must_change_password INTEGER NOT NULL DEFAULT 0,                   -- set by admin.create_user / admin.reset_password (§4.3)
  created_at REAL NOT NULL, last_seen_at REAL, last_login_at REAL    -- last_login_at is set on registration and login; NULL = never logged in (User.activated=false)
);

CREATE TABLE sessions(
  token_hash TEXT PRIMARY KEY,                                       -- sha256 hex of the opaque token (its first 16 hex chars are the public session id, §4.2)
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL, last_used_at REAL NOT NULL, expires_at REAL NOT NULL,
  user_agent TEXT, ip TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE chats(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('direct','group')),
  title TEXT,                                                        -- groups only (1..60 chars)
  description TEXT NOT NULL DEFAULT '',                              -- <=500 chars
  direct_key TEXT UNIQUE,                                            -- "minUserId:maxUserId" (direct only; self-chat "5:5")
  is_default INTEGER NOT NULL DEFAULT 0,                             -- the auto-joined "Everyone" group
  only_admins_post INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id), created_at REAL NOT NULL,
  last_message_id INTEGER, last_activity_at REAL NOT NULL
);

CREATE TABLE chat_members(
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin','member')),
  joined_at REAL NOT NULL,
  history_from_id INTEGER NOT NULL DEFAULT 0,      -- member can't see messages with id <= this (joined later)
  cleared_before_id INTEGER NOT NULL DEFAULT 0,    -- "clear chat" (for me only)
  last_read_id INTEGER NOT NULL DEFAULT 0,         -- PRIVATE truth for unread counters
  read_receipt_id INTEGER NOT NULL DEFAULT 0,      -- PUBLIC read watermark; advances only while users.read_receipts=1
  delivered_id INTEGER NOT NULL DEFAULT 0,         -- PUBLIC delivered watermark
  muted_until REAL NOT NULL DEFAULT 0,             -- 0 = not muted; 4102444800 = forever
  pinned_at REAL,                                  -- pinned chat (NULL = not pinned)
  archived INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(chat_id,user_id)
);
CREATE INDEX chat_members_user ON chat_members(user_id);

CREATE TABLE attachments(
  id TEXT PRIMARY KEY,                                               -- 32 hex chars (uuid4().hex)
  uploader_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL, mime TEXT NOT NULL,                            -- name sanitised (§5.6); mime = SNIFFED type, never the client's (§5.5)
  kind TEXT NOT NULL CHECK(kind IN ('image','audio','video','file')),
  size INTEGER NOT NULL, path TEXT NOT NULL,                         -- relative to data/uploads
  width INTEGER, height INTEGER, duration REAL,
  created_at REAL NOT NULL
);

CREATE TABLE messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  sender_id INTEGER REFERENCES users(id),                            -- NULL for system messages
  client_id TEXT,                                                    -- idempotency key from the client
  kind TEXT NOT NULL CHECK(kind IN ('text','image','audio','video','file','system')),
  body TEXT NOT NULL DEFAULT '',
  reply_to_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
  forwarded INTEGER NOT NULL DEFAULT 0,
  attachment_id TEXT REFERENCES attachments(id),
  mentions TEXT NOT NULL DEFAULT '[]',                               -- JSON array of user ids: serialisation cache of message_mentions (never queried in SQL)
  system TEXT,                                                       -- JSON, only for kind='system'
  created_at REAL NOT NULL, edited_at REAL, deleted_at REAL
);
CREATE UNIQUE INDEX messages_client ON messages(sender_id, client_id) WHERE client_id IS NOT NULL;
CREATE INDEX messages_chat ON messages(chat_id, id);
CREATE INDEX messages_attachment ON messages(attachment_id) WHERE attachment_id IS NOT NULL;   -- /files access check, orphan sweep
CREATE INDEX messages_reply ON messages(reply_to_id) WHERE reply_to_id IS NOT NULL;            -- FK ON DELETE SET NULL
CREATE INDEX messages_sender ON messages(sender_id, id);                                       -- admin.users message_count
CREATE TABLE message_mentions(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY(message_id,user_id));   -- server-derived (§7.4)
CREATE INDEX message_mentions_user ON message_mentions(user_id, message_id);

CREATE TABLE receipts(
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,   -- the RECIPIENT
  delivered_at REAL, read_at REAL,                                   -- written only for chats with <= 50 members (§3.3); read_at only when the PUBLIC read watermark advances
  PRIMARY KEY(message_id,user_id)
);
CREATE TABLE reactions(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, emoji TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(message_id,user_id));                                  -- one reaction per user per message
CREATE TABLE stars(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, created_at REAL NOT NULL,
  PRIMARY KEY(user_id,message_id));
CREATE TABLE pins(chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, pinned_by INTEGER REFERENCES users(id),
  pinned_at REAL NOT NULL, PRIMARY KEY(chat_id,message_id));          -- max 5 per chat
CREATE TABLE hidden_messages(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, PRIMARY KEY(user_id,message_id)); -- "delete for me"
CREATE TABLE audit_log(id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL,
  target_id INTEGER, ip TEXT);                                       -- every admin.* mutation, create/reset via CLI (actor NULL), backups restores
```
Rules
* Direct chat: exactly two `chat_members` rows (or one for a self-chat "Saved messages"). Created lazily by `chat.open_direct`; `direct_key` guarantees no duplicates (handle the
  UNIQUE race by re-select). Lifecycle in §3.2.
* Default group "Everyone": created **in the transaction that registers the first admin** (`is_default=1`, title = workspace name or "Everyone", then the system message
  `created` with body "Welcome to <workspace>", inserted after the admin's member row so the admin sees it). Every later user is added automatically at registration
  (§3.2). Members **cannot leave or be removed** from it. Its title is independent of `workspace_name` after creation (`admin.settings` never renames it).
* **A newly added member cannot see history**: on joining set `history_from_id = chat.last_message_id` (or 0 for an empty chat), and set
  `last_read_id = read_receipt_id = delivered_id = history_from_id`. This is computed **before** the `added`/`joined`/`created` system message is inserted, so the new member
  sees their own notice. History queries use the visibility predicate (§3.1).
* **Delete for everyone**: set `deleted_at`, `body=''`, `attachment_id=NULL`, delete its `reactions`/`pins`/`stars`/`message_mentions` rows; keep the row (so replies show
  "message deleted"). After commit, delete the attachment's file and `attachments` row only if **no message at all** still references it (forwarded copies share the row, §7.4).
* Users are never deleted; they are `disabled`. Disabled users keep their messages, can't log in, are shown dimmed, stay chat members (and admins; see §3.2 promotion).
* System messages (`kind='system'`, `sender_id NULL`) carry `system` JSON:
  `{"event": "created|added|removed|left|renamed|promoted|demoted|pinned|joined", "actor_id": int|null, "target_ids": [int], "title": str|null, "message_id": int|null}`
  (`message_id` = the pinned message for `pinned`, else null) and a plain-English `body` fallback ("Ravi added Amit"). They never count as unread, never get receipts, and bump
  `chats.last_activity_at` except for `joined` (registration must not re-sort every user's list).

### 3.1 Visibility predicate (normative; applied identically everywhere)
`visible(viewer, m)` ≡ the viewer is a **current member** of `m.chat_id` AND `m.id > max(history_from_id, cleared_before_id)` (viewer's `chat_members` row) AND
`(viewer, m.id) ∉ hidden_messages`. `V(viewer, chat)` = the visible messages of that chat; **system messages and deleted-for-everyone placeholders are in V**.
The predicate is used by: `chat.history` (including `around_id`), `msg.search`, `msg.shared`, `msg.starred`, pinned lists, `Chat.last_message`, unread counts, `msg.info`,
forward sources, `reply_to_id` targets, `reply_to` previews, and the attachment access rule (§4.2). Pinned messages follow it too (a late joiner does not see older pins).
`remove_member` / `leave` delete that user's `stars` rows in the same transaction.

### 3.2 Group, membership and direct-chat rules
1. **Two kinds of "admin".** `users.role` is the workspace role; `chat_members.role` is the chat role. "Group admin" always means `chat_members.role='admin'`.
   In the default group `chat_members.role` **mirrors `users.role`**: `admin.update_user` role changes update both in one transaction and emit `ev.chat_members {updated}` for it.
   `chat.set_admin`/`remove_member`/`leave` on the default group ⇒ `invalid_state`. Workspace admins have **no implicit power in other groups** and cannot read chats they are not in.
2. **Direct chats** reject group operations (`chat.update`, `add_members`, `remove_member`, `set_admin`, `leave`) with `invalid_state`. In a direct chat only your own messages can be
   deleted for everyone; either member can pin.
3. **Dormant direct chats.** A direct chat with `last_message_id IS NULL` is *dormant*: `chat.open_direct` returns it to the creator (and emits `ev.chat_update` to the creator's connections) and
   it is part of the creator's `ev.ready`, but the **peer does not see it** (not in the peer's `ev.ready`, no event) until the first message exists: the first `msg.send` sends a full
   `ev.chat_update` to every member's connections immediately BEFORE the `ev.message` (§7.6). Opening a profile therefore never leaves an empty chat in anybody's list.
   `chat.open_direct` on an existing direct chat returns it unchanged and emits nothing. A **new** direct chat with a disabled user is `invalid_state`; an existing chat with a user who later
   becomes disabled stays listed, but `msg.send/forward/react/pin` into it return `invalid_state` and the client replaces the composer by "<name>'s account is disabled".
   **Self-chat**: `members` has one entry and `peer_id == me.id`; `ev.message` goes to all of the user's connections; `status` is always null; receipts and typing are no-ops; `unread` is always 0.
   `Chat.title` is always null for direct chats (the client derives the title; "You" for the self-chat).
4. **Registration / `admin.create_user`** (one transaction, `hub.user_created`, §6.1): create the user, join `Everyone` (history rule above), insert the system message
   `{event:'joined', actor_id:null, target_ids:[new]}`. Broadcast order: `ev.user_update` (new user) → `ev.chat_members {added}` to the other members → `ev.message`.
5. **`chat.add_members`**: silently skips users who are already members; unknown user ⇒ `not_found`; a disabled user, or a resulting total > 200 members ⇒ `invalid_state`; adding to the
   default group ⇒ `invalid_state`. Re-adding a previously removed user starts fresh (new `history_from_id`, prefs reset, new `joined_at`).
6. **Leaving and promotion.** When the last group admin leaves, or is disabled, the enabled member with the smallest `joined_at` (tie: lowest `user_id`) is promoted in the same transaction
   (system message `promoted`, `actor_id:null`). `chat.leave` by the last remaining member keeps the chat row but removes it from the user's list (no promotion; only the leaver's own connections get `ev.chat_removed`).
   Demoting the last admin ⇒ `invalid_state`.
7. **Prefs semantics.** `muted_until`, `archived` and `pinned_at` are client hints: the server never suppresses or delays events for muted/archived chats and unread counts include them.
   A new message from another user sets `archived=0` for non-muted members (they receive `ev.chat_update`); a muted archived chat stays archived. Archiving a pinned chat clears
   `pinned_at` in the same change. Pinned chats sort by `pinned_at` descending, then everything else by `last_activity_at` descending.

### 3.3 Receipt-row and watermark invariants
* After ANY update `db.py` enforces `delivered_id ≥ read_receipt_id` and `last_read_id ≥ read_receipt_id`; all three watermarks only move forward (`max()`).
* `receipts` rows are created lazily and **only for chats with ≤ 50 current members**: whenever a member's `delivered_id` advances `old → new`, in the same transaction upsert rows for every non-system
  message with `sender_id != member`, `old < id ≤ new`, `id > history_from_id` (`delivered_at = COALESCE(delivered_at, now)`); when the member's PUBLIC read watermark advances, also set
  `read_at = COALESCE(read_at, now)` for the advanced range. For larger chats no rows are written (§7.4 `msg.info`).
* `read_receipt_id` advances only while `users.read_receipts = 1` (toggle semantics in §8.1).
* A message mention (`message_mentions`) is a row per mentioned user; `unread_mentions` is a plain count of such rows with `message_id > last_read_id` that are visible (§3.1) and not deleted.

---------------------------------------------------------------------------------------------------

## 4. Auth, sessions, REST API

### 4.1 Passwords & tokens (`auth.py`)
* **Hash:** `hashlib.scrypt(n=2**16, r=8, p=1, dklen=32, maxmem=128*1024*1024)` if available else `pbkdf2_hmac('sha256', iterations=600_000)`. (`n ≥ 2**15` raises
  `ValueError: memory limit exceeded` unless `maxmem` is passed; measured ≈ 150–260 ms and 64 MiB per hash on the dev host, so the 2-thread hash executor peaks at 128 MiB.)
  Stored format `scrypt$65536$8$1$<salt_b64>$<hash_b64>` / `pbkdf2$600000$<salt_b64>$<hash_b64>`; the parameters are **read from the stored string**, and a successful login whose
  stored parameters differ from the current policy transparently re-hashes and `UPDATE`s. Verify with `hmac.compare_digest`. A dummy hash is verified for unknown usernames (no username
  oracle by timing). The cost `n` comes from config `scrypt_n` (tests only may lower it).
* **Password policy** (`weak_password`; identical for `/api/register`, `/api/password`, `admin.create_user`, `admin.reset_password` and the CLI): NFKC-normalise; `min_password_len ≤ length ≤ 128`
  characters (longer is rejected BEFORE hashing); not equal (casefold) to the username or display name; not in a built-in tuple of ≥ 200 common passwords (`auth.py`).
* **Session token:** `secrets.token_urlsafe(32)`; DB stores `sha256(token)` only. Sliding expiry `session_days`: `sessions.last_used_at` is updated at most every 10 minutes (and
  `expires_at = now + session_days`); the `Set-Cookie` is re-issued (same token, fresh `Max-Age`) when such an update happens and the previous `expires_at` was more than 1 day older
  than a fresh one (so the browser cookie keeps sliding).
* **Cookie:** `fc_session=<token>; Path=/; HttpOnly; SameSite=Strict; Max-Age=<session_days*86400>` (+`Secure` when TLS). The UI never reads or writes cookies (`document.cookie` is forbidden).
* **Login throttling** (in memory; all counters are incremented identically for known and unknown usernames; key = lowercased username if it matches `^[a-z0-9._-]{3,32}$`, else the fixed key `?`):
  (a) per `(ip, username)`: 5 failures / 5 min; (b) per `ip` across usernames: 20 failures / 10 min; (c) per `username` across IPs: 30 failures / 15 min (only delays, never locks).
  When exceeded: `429 rate_limited` with `retry_after = min(2^(n-5) · 5 s, 900 s)`. Counter dicts are capped at 10,000 entries with LRU eviction. A successful login clears (a) only.
  `disabled` (403) is returned only **after** the password verified; before that the answer is `bad_credentials`. Failed `POST /api/password` attempts count against (a) keyed by
  `(ip, the session's username)`. Wrong `setup_code`/`join_code` guesses count as failures per IP (5 / 10 min, then `429`).
* **Registration limiter:** 5 successful registrations / hour / IP, 60 / hour globally, and `max_users` (then `registration_closed`).

### 4.2 REST endpoints (all JSON unless noted; errors `{"error":{"code","msg","retry_after"?}}` + proper HTTP status)
| Method & path | Auth | Body → Response |
|---|---|---|
| `GET /healthz` | none | `200 text/plain "ok"` |
| `GET /api/info` | none | `{name, registration_open, needs_setup, tls}` (`needs_setup` = no users yet). No version number is exposed to unauthenticated callers. |
| `POST /api/register` | none | `{username, display_name, password, setup_code?, join_code?}` → `201 {me}` + cookie. While `needs_setup`: `setup_code` required (403 `setup_code_required` / `bad_setup_code`) and the account becomes **admin**. Otherwise 403 `registration_closed` unless `registration_open` and `users < max_users`; then `join_code` is required (403 `bad_join_code`). 409 `username_taken` (also for reserved names), 409 `name_taken`, 400 `weak_password`/`bad_request`, 429 `rate_limited`/`server_busy`. See §4.3. |
| `POST /api/login` | none | `{username, password}` → `200 {me}` + cookie. 401 `bad_credentials` (generic), 403 `disabled` (only after the password verified), 429 `rate_limited`/`server_busy`. |
| `POST /api/logout` | cookie | `204`, revokes this session, clears the cookie and closes that session's sockets (`hub.revoke`, §6.1). |
| `GET /api/me` | cookie | `{me}` or `401`. Allowed while `must_change_password`. |
| `POST /api/password` | cookie | `{old_password,new_password}` → `204`; revokes all *other* sessions (their sockets get `ev.kicked 'password_changed'` + 4001); clears `must_change_password`. |
| `GET /api/sessions` | cookie | `{sessions:[{id, created_at, last_used_at, ip, user_agent, current}]}`; `id` = first 16 hex chars of `token_hash`. |
| `POST /api/sessions/revoke` | cookie | `{id}` or `{all_others:true}` → `204`; revoked sessions' sockets get `ev.kicked 'revoked'` + 4001. |
| `POST /api/upload` | cookie + `X-Requested-With: freechat` | Raw body = file bytes. Headers: `Content-Length` (required, ≤ max), `Content-Type` (ignored, §5.5), `X-File-Name` (UTF-8, percent-encoded), optional `X-Meta` (JSON `{width,height,duration}`, hints only, §5.6). → `201 {attachment}`. 411 no length, 413 `too_large`, 413 `quota_exceeded`, 400 `blocked_type`, 507 `insufficient_storage`, 429 `rate_limited`. Quotas in §5.1. |
| `GET /files/<id>` | cookie | Streams the file. `?dl=1` forces download. Supports `Range`. 404 if the user may not access it (never 403 – no existence oracle); `<id>` must match `^[0-9a-f]{32}$` before any DB or filesystem access. |
| `GET /ws` | cookie | WebSocket upgrade (§6). |
| `GET /` and `/css/*` `/js/*` `/img/*` `/manifest.webmanifest` | none | Static files from `web/` (§5.3). |

REST error codes → status: `bad_request` 400, `weak_password` 400, `blocked_type` 400, `bad_credentials`/`unauthorized` 401, `forbidden`/`disabled`/`registration_closed`/`setup_code_required`/`bad_setup_code`/`bad_join_code`/
`password_change_required` 403, `not_found` 404, `host_not_allowed` 421, `username_taken`/`name_taken`/`conflict` 409, `too_large`/`quota_exceeded` 413, `rate_limited`/`server_busy` 429 (+`Retry-After`), `insufficient_storage` 507.

`me` object = `User` (§7.2) plus `show_last_seen`, `must_change_password`.
* **`must_change_password`:** while true every endpoint except `/api/me`, `/api/password`, `/api/logout`, `/healthz`, `/api/info` and static files returns 403 `password_change_required`,
  and the WebSocket handshake is refused with the same status; the client shows the forced change-password screen (§9.2).
* **CSRF / cross-site WebSocket / DNS rebinding:** every non-`GET` request and `/ws` pass `check_request_origin()` (§5.9). Non-`GET` requests additionally need `X-Requested-With: freechat`
  (all of the UI's own calls send it), and JSON bodies need `Content-Type: application/json`.
* **Attachment access rule:** allowed iff the requester is the uploader, or `visible(requester, m)` (§3.1) holds for some non-deleted message `m` that references the attachment (this also
  covers forwarded copies and pinned messages: a late joiner has no access to pre-join pins). A foreign or unknown id in `msg.send` is `not_found` (no existence oracle).
* **Cache headers:** static `Cache-Control: no-cache` + `ETag` (mtime-size) + `304`; `/files/*` → `Cache-Control: private, no-cache`, `ETag: "<id>"`, `Vary: Cookie`,
  `Cross-Origin-Resource-Policy: same-origin` (a `304` is answered only **after** the access check, so removed members and shared PCs cannot re-read cached files); `/api/*` → `Cache-Control: no-store`.

### 4.3 First run, registration, roles and identity
1. **Setup code.** Whenever `users` is empty (at startup and while running) `serve` has a one-time **setup code** (`secrets.token_urlsafe(6)`, 8 characters), writes it to `<data>/setup_code.txt`
   (restrictive permissions), prints it in the startup banner and log, and deletes the file once the first admin exists. `POST /api/register` while `needs_setup` requires it
   (`hmac.compare_digest`) — always, including from loopback — and the check plus the `INSERT` run inside one `BEGIN IMMEDIATE` db function (two racing callers can never both become admin).
   The installer and `doctor` print the code and the URL `http://127.0.0.1:<port>/` (https when TLS); `create-admin` (CLI) bypasses it. The setup screen shows a "Setup code" field while `needs_setup`.
2. **Registration is closed by default** once the first admin exists (`registration_open=false`). An admin may open it (Admin → Workspace; the UI warns "Anyone on this network who knows the join code
   can create an account"). A random **join code** (8 chars) is generated with the first admin, stored in `meta.join_code`, shown to admins, and regenerated by `admin.settings {rotate_join_code:true}`.
   While open, `POST /api/register` requires it. Admin-created users bypass the join code but not `max_users`. The user directory (`ev.ready.users`) is visible to **every** member.
3. **Reserved usernames** (`admin administrator root system support helpdesk it hr everyone freechat`) can only be created via `admin.create_user` / `create-admin`; registration answers `username_taken`.
4. **Text normalisation** for `display_name`, `status_text`, chat `title`, chat `description` and `workspace_name`: NFKC-normalise, remove Unicode categories `Cc, Cf, Cs, Co, Zl, Zp`
   (except `\n` in `description`), collapse runs of whitespace to one space, trim. Limits: display_name 1..40, status_text ≤ 140, title 1..60, description ≤ 500, workspace_name 1..40.
   **Display names are unique**: `display_key = casefold(NFKC(display_name))` is `UNIQUE`, and a display name must not casefold-equal any *other* user's username (own excepted) ⇒ REST `409 name_taken`,
   WS `conflict` with `err.reason:'name_taken'`. The UI additionally shows `@username` under sender names in groups and in every people picker.
5. **Forced password change.** `admin.create_user` and `admin.reset_password` set `users.must_change_password=1` (the admin hands over a temporary password); clearing happens in `POST /api/password`.
6. **Audit trail.** Every `admin.*` mutation (and CLI create/reset, restore) appends to `audit_log(ts, actor_id, action, target_id, ip)`; `admin.audit` reads it (§7.4).

---------------------------------------------------------------------------------------------------

## 5. HTTP server requirements (`http.py`, `files.py`, `tlsutil.py`)

5.1 Hand-written HTTP/1.1 on `asyncio.start_server(..., backlog=1024)`: request line + headers (≤ 16 KiB, ≤ 100 headers), keep-alive (idle 30 s), `Content-Length` bodies only
(reject `Transfer-Encoding` with 501/411), `HEAD` supported, `Connection: close` honoured, pipelining not required.
* **Strict framing (all 400 unless noted):** `Content-Length` must match `^[0-9]{1,12}$` (Python's `int()` accepts `1_0`, `+5`, ` 5` — do not use it unguarded); duplicate `Content-Length`
  headers (even if equal), header names with trailing whitespace, obs-fold, bare CR/LF or NUL in headers, absolute-form request targets and the HTTP/2 preface are rejected; an HTTP version other than 1.0/1.1 ⇒ 505.
* **Timeouts:** the header block must complete within 15 s **in total** from the first byte; a request body must make ≥ 8 KiB/s after its first 10 s and finish within `max(30 s, Content-Length / 32 KiB/s)`;
  idle keep-alive 30 s; TLS handshake `ssl_handshake_timeout=10`.
* **Body caps:** non-upload JSON bodies ≤ 64 KiB, rejected with `413` **before** reading when `Content-Length` exceeds it and before authentication. **Any response sent while the request body has not been fully
  consumed MUST carry `Connection: close` and close the socket afterwards** (otherwise the unread bytes are parsed as the next request); for `/api/upload` early errors (401/413/507) the server first drains up to 1 MiB
  or 2 s so browsers see the status instead of a connection reset.
* **Connection caps:** ≤ 1200 concurrent sockets in total (HTTP + WS; beyond that an immediate `503` + close), ≤ 64 per remote IP (WS limits in §6). On POSIX the server raises its soft `RLIMIT_NOFILE`
  to `min(hard, 8192)` at startup (macOS launchd and systemd default to 256/1024).
* **Downloads:** written in ≤ 64 KiB chunks with `await drain()` bounded by a 30 s write timeout; at most 4 concurrent downloads per user and 8 per IP.
* **Uploads:** streamed to `<data>/uploads/.tmp/<uuid>.part` and then `os.replace`d to `<data>/uploads/<aa>/<id>` (aa = first 2 hex chars; same filesystem, never the system temp dir); the temp file is removed in a
  `finally` on disconnect/oversize/any error. **Before reading the body** the server authenticates, checks quotas and free disk: `Content-Length ≤ max_upload_mb`; `shutil.disk_usage(data_dir).free ≥ Content-Length + max(2 GiB, 5 % of the volume)`
  else `507 insufficient_storage`; per-user *unattached* bytes ≤ 500 MB and per-user uploaded bytes ≤ 2 GiB per rolling 24 h else `413 quota_exceeded` (constants in `files.py`); concurrent uploads ≤ 3 per user and ≤ 6 per IP
  else `429 rate_limited`; blocked extension (§2.1 `blocked_extensions`, evaluated on the sanitised name's last suffix, case-insensitive) ⇒ `400 blocked_type`.
5.2 Router: `(method, path)` exact + prefix routes. 404/405/400/413/421/431/505 responses are JSON for `/api/*`, text elsewhere. `OPTIONS` ⇒ 405.
5.3 Static serving: root = `<app>/web`. **Positive allow-list:** percent-decode the path ONCE, split on `/`; every segment must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, must not end in `.`, and must not be a
Windows reserved device name (stem, case-insensitive: `CON PRN AUX NUL COM0-9 LPT0-9`); the final suffix must be in the explicit MIME table (so `.md .map .py .bak ~` give 404); then `os.lstat` must show a regular file that
is not a symlink or reparse point (`st_file_attributes & 0x400` on Windows) — do this **before** opening; `Path.resolve()` containment stays as a second check. (On Windows `a.css.`, `a.css `, `a.css::$DATA` and `A.CSS` all open
`a.css`, so name-based blocklists are bypassable.) Explicit `Content-Type` table (do NOT depend on the Windows registry): `.js` `text/javascript`, `.css`, `.html`, `.svg`, `.json`, `.png`, `.ico`, `.woff2`, `.webmanifest`.
`tests` request `/css/base.css.`, `/css/base.css::$DATA`, `/css/CON`, `/js/%5c..%5cchatd%5cdb.py` and `/.git/config` and expect 404.
5.4 Security headers on every response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Resource-Policy: same-origin`, `Cross-Origin-Opener-Policy: same-origin`,
`Permissions-Policy: camera=(), geolocation=(), payment=(), usb=(), microphone=(self)`; text types carry `; charset=utf-8`; no `Server` header; `/api/*` additionally `Cache-Control: no-store`. On HTML:
`Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws://<Host> wss://<Host>; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; require-trusted-types-for 'script'`
where `<Host>` is built only from the **validated** Host header (§5.9). (`ws:`/`wss:` without a host would allow exfiltration to any host.) Consequences for the UI: **no inline `<script>`, no inline `style=""` markup, no `setAttribute('style')`,
no `<style>` elements**; `el.style.<prop> = …`, `style.setProperty()` and `style.cssText = …` from JS ARE allowed. The UI lint (§12) also forbids `CSSStyleSheet`, `adoptedStyleSheets`, `insertRule`, `document.createElement('style')`
and every string-to-code sink (Trusted Types would block them anyway; Chromium enforces, other browsers ignore).
5.5 `/files/<id>` serving: `Content-Type` is the **sniffed** type stored in the DB (§5.6): inline only for `image/png|jpeg|gif|webp|bmp|avif`, `audio/mpeg|mp4|ogg|wav|flac`, `video/mp4|webm|ogg|quicktime`;
everything else (incl. SVG, HTML, XML, JS, unknown) → `application/octet-stream` + `Content-Disposition: attachment`. PDFs are download-only (no inline viewer under the sandbox CSP; the button says "Download").
Always add `Content-Security-Policy: sandbox; default-src 'none'`, `nosniff` and the cache headers of §4.2. `Content-Disposition` carries `filename="<ascii fallback>"` where the fallback keeps only `[A-Za-z0-9._-]`
(everything else becomes `_`; no quotes/CR/LF can ever appear) plus `filename*=UTF-8''<percent-encoding of everything outside RFC 5987 attr-char>`. Single `Range: bytes=a-b` support (206/416).
5.6 Upload processing (`files.py`):
* **Content sniffing.** The first 512 bytes are matched against an allow-list: PNG, JPEG, `GIF87a/GIF89a`, `RIFF….WEBP`, `BM` (bmp), ISO-BMFF `ftyp` (brand `avif`→`image/avif`, `qt  `→`video/quicktime`, `M4A `/`M4B `→`audio/mp4`,
  any other brand→`video/mp4`), EBML (`video/webm`), `OggS` (`audio/ogg`), `ID3`/MPEG frame sync (`audio/mpeg`), `RIFF….WAVE` (`audio/wav`), `fLaC` (`audio/flac`). `kind` (`image` png/jpeg/gif/webp/bmp/avif — **not svg**; `audio`;
  `video`; else `file`) and the stored/served `mime` come from the sniffed result **only**; the client's `Content-Type` is ignored. Unknown content ⇒ `kind=file`, `application/octet-stream`.
* **`X-Meta`** values are client hints: `width`/`height` integers 1..16384, `duration` finite float 0..86400, dropped (null) for `kind=file`; anything else is dropped silently (they are broadcast to every recipient's layout).
* **Name sanitising:** NFC-normalise; remove every character of Unicode categories `Cc, Cf, Zl, Zp` (kills bidi overrides such as U+202E); strip path components; replace `<>:"/\|?*` with `_`; strip trailing dots/spaces;
  Windows reserved device names (`CON PRN AUX NUL COM0-9 LPT0-9`, with or without extension) get a `_` prefix; ≤ 200 characters and ≤ 255 UTF-8 bytes; keep the extension; an empty result becomes `file`.
5.7 TLS (`--tls`): files `data/tls/{cert.pem,key.pem,meta.json}` (`meta.json` = `{san:[…], not_after, created}`). `serve --tls` (re)generates the leaf — keeping the old files as `*.old` and reusing the key — when cert/key are missing, when a
current LAN IP or the hostname is not in `san`, or when < 30 days remain; the installer also generates it at install time (the network is up then). **Generation** (works on OpenSSL 1.1.1/3.x and every LibreSSL, needs no `-addext`):
write `<data>/tls/openssl.cnf` =
```
[req]
distinguished_name=dn
x509_extensions=v3
prompt=no
[dn]
CN=<hostname>
[v3]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=@alt
[alt]
DNS.1=<hostname>
DNS.2=localhost
IP.1=127.0.0.1
IP.2=<lan ip>            ; one IP.n per non-loopback, non-169.254 address
```
run `[exe,'req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','825','-config',cnf,'-keyout',key,'-out',cert]` with `OPENSSL_CONF` removed from the child environment (a stray value breaks it), then delete the cnf.
(Without the `v3` section OpenSSL's default config yields `CA:TRUE` and no EKU, which Apple platforms reject. Use `-nodes`, not `-noenc`.) openssl search order: PATH `openssl`, `C:\Program Files\Git\mingw64\bin\openssl.exe`,
`C:\Program Files\Git\usr\bin\openssl.exe`, `/usr/bin/openssl`, `/opt/homebrew/bin/openssl`, `/usr/local/bin/openssl`. Validate with `ctx.load_cert_chain(cert, key)` right after generation; on any failure delete both files,
log a clear warning and continue **without** TLS (never crash). Key file mode 0600 (POSIX) / data-dir ACL (Windows, §10.2). Serve via `ssl.SSLContext(PROTOCOL_TLS_SERVER)`, min TLS 1.2.
With TLS: the cookie gets `Secure`; the UI derives `wss:` vs `ws:` from `location.protocol`; `/api/info.tls` is true; an optional `--redirect-port P` starts a tiny plain-HTTP listener that answers only
`301 https://<host>:<port>/` (never serves the app or sets cookies). `doctor` prints the certificate's SHA-256 fingerprint. A self-signed certificate causes a one-time browser warning; the README explains how to trust it on phones.
5.8 URL printing at startup: the **primary** URL is the UDP-connect address (`SOCK_DGRAM` `connect(('10.255.255.255', 1))` then `getsockname()`, in `try/except OSError`) printed first under "Share this link"; other non-loopback,
non-169.254 addresses from `socket.gethostbyname_ex(socket.gethostname())` and `getaddrinfo(…, AF_INET)` follow under "Other adapters (may not be reachable by phones)" (on a typical Windows PC the first entries are Hyper-V/hotspot
virtual adapters). Every discovery call is wrapped in `try/except (socket.gaierror, OSError)`; with no address yet the banner says "No network address yet" instead of failing. `app.py` re-checks every 60 s and logs the URLs again when the
primary address changes (DHCP after boot).
5.9 **`check_request_origin(request)`** — one shared function used for every non-`GET` request **and** for the `/ws` upgrade:
a. HTTP/1.1 requires exactly one `Host` header (else 400); it is lower-cased and must match `^([a-z0-9.-]{1,253}|\[[0-9a-f:.]+\])(:\d{1,5})?$`.
b. The host name must be on the allow-list, else `421 host_not_allowed`. Default list: IP literals, `localhost`, this machine's hostname/FQDN, single-label names, names ending `.local .lan .internal .home.arpa .corp`, plus `allowed_hosts` (`--allowed-host`).
   (This blocks DNS rebinding: an attacker domain that resolves to the server's LAN IP arrives with an unlisted Host.)
c. If `Sec-Fetch-Site` is present only `same-origin` passes (`none` is accepted on GET navigations); `same-site` and `cross-site` ⇒ 403.
d. Else if `Origin` is present it must equal `<scheme of this connection>://<Host>` (authority compared lower-cased **including the port**, default ports elided); `Origin: null` ⇒ 403. An absent `Origin` is accepted only for non-browser clients.
e. Every non-GET request must carry `X-Requested-With: freechat` (a custom header forces a CORS preflight that the server never grants). The WS upgrade is a GET that browsers cannot add headers to, so it relies on a–d (and the `SameSite=Strict` cookie).
f. The server never emits `Access-Control-*` headers; `OPTIONS` ⇒ 405.

---------------------------------------------------------------------------------------------------

## 6. WebSocket transport (`websocket.py`)
* Handshake per RFC 6455: require `Upgrade: websocket`, `Connection: upgrade`, `Sec-WebSocket-Version: 13`, valid `Sec-WebSocket-Key`; respond `101` with `Sec-WebSocket-Accept`. **Before upgrading**:
  `check_request_origin()` (§5.9); cookie session valid (401 otherwise); `must_change_password` ⇒ 403 `password_change_required`; connection caps: ≤ 800 WS in total (503), ≤ 30 concurrent WS per IP,
  ≤ 20 handshakes / min per IP and ≤ 30 / min per user (429 + `Retry-After`). The transport then calls `await hub.serve(ws, session)` (§6.1).
* Frames: text, binary (ignored/closed with 1003), continuation (reassemble), ping (reply pong), pong, close (echo code, then close). Client frames MUST be masked (else close 1002). Server frames unmasked.
  Reject RSV bits ≠ 0 (no extensions), invalid opcodes, fragmented control frames, payload > 125 for control frames, invalid UTF-8 (close 1007). **Inbound message limit 256 KiB** (reassembled; ⇒ close 1009).
  The declared payload length is validated **before reading the payload**: a 64-bit length with the MSB set, or one exceeding the remaining message budget, ⇒ close 1009 without reading; non-minimal length encodings ⇒ close 1002;
  the reassembled total is checked incrementally while fragments arrive.
* Server pings every 25 s; no inbound frame/pong for 60 s ⇒ close 1001 (checked every 10 s, §2.4).
* **Send path / back-pressure.** One writer task per connection is the only caller of `writer.write/drain`; `await asyncio.wait_for(writer.drain(), 10)` (timeout ⇒ close 1013) and
  `transport.set_write_buffer_limits(high=256 * 1024)`. Frames are **durable** (`res`, `ev.ready`, `ev.message*`, `ev.chat_*`, `ev.read_sync`, `ev.me`, `ev.user_update`, `ev.workspace`, `ev.kicked`) or **ephemeral**
  (`ev.typing`, `ev.presence`, `ev.receipt`). Ephemeral frames are coalesced by `key` (typing: chat+user, presence: user, receipt: chat+user) and are dropped first when more than 256 frames are queued; only
  > 512 queued durable frames or > 2 MiB of queued bytes closes the connection with 1013. Global budget: all queued bytes ≤ 128 MiB, else the connection with the largest queue is closed with 1013.
  Serialise (`json.dumps`) once per event *variant* (e.g. sender / non-sender) and hand the same `str` to every recipient. `send_text()` must never raise into the hub (swallow `ConnectionError`/`OSError`, mark closed).
* API used by the hub: `ws.recv() -> Optional[str]` (None on close), `ws.send_text(text, durable=True, key=None)` (non-blocking enqueue), `ws.close(code=1000, reason="")`, `ws.closed`, `ws.remote_addr`, `ws.user_agent`,
  `ws.session` (the dict below) and `ws.last_rx` (monotonic time of the last inbound frame/pong).
* Max **8 simultaneous connections per user**: when a 9th opens, the connection with the **oldest `last_rx`** is closed with `4003` and reason `"replaced"` (the client must not loop on this, §8.5); max 800 total.
* Application close codes: `4001` unauthorized/session revoked/disabled, `4003` too many connections, `4008` rate limited (repeat offender). Client behaviour per code: §8.5.

### 6.1 Hub API (transport ⇄ hub ⇄ REST)
`hub.py` exports (all `async` unless noted):
* `await hub.serve(ws, session)` — called by the WS transport after authentication with `session = {user_id:int, token_hash:str, ip:str, user_agent:str}`; registers the connection as pending, sends `ev.ready` first
  (§7.6), runs the read loop and returns when the socket has closed.
* `await hub.user_created(user_id)` — called by the REST register handler and by `admin.create_user`: one db function joins `Everyone` and inserts the `joined` system message, then the hub broadcasts
  `ev.user_update` → `ev.chat_members` → `ev.message` (§3.2(4)).
* `await hub.revoke(user_id, token_hash=None, except_token_hash=None, reason)` — called by `/api/logout` (that `token_hash`, reason `revoked`), `/api/password` (all except the current one, `password_changed`),
  `/api/sessions/revoke` (`revoked`), admin disable (`disabled`) and `admin.reset_password` (`revoked`). It sends `ev.kicked` and closes `4001` on the matching connections only — nothing waits for the 5-minute sweep.
* `await hub.shutdown()` and the flag `hub.stopping` (§2.4); `hub.online_user_ids()` (sync, for the heartbeat); `await hub.revalidate_all()` (control poll, §2.4: re-reads sessions/users and kicks revoked/disabled).
* `api.py` [hub] owns the REST handlers `/api/info /register /login /logout /me /password /sessions`; `http.py` owns routing, `/api/upload`, `/files` and static files; `app.py` wires them together.
  Registration including the first-admin check is a single `BEGIN IMMEDIATE` db function.

---------------------------------------------------------------------------------------------------

## 7. Realtime protocol (`hub.py` ⇄ `web/js/core/socket.js`)

### 7.1 Envelope
Every frame is one JSON object in one text frame.
```
client → server  request : {"t":"<type>","id":"<string>","d":{...}}      ("id" optional for fire-and-forget)
server → client  response: {"t":"res","id":"<same>","ok":true,"d":{...}}
                           {"t":"res","id":"<same>","ok":false,"err":{"code":"<code>","msg":"<human text>","reason":"<sub-code>"?,"retry_after":<seconds>?,"chat_id":<int>?,"message_id":<int>?}}
server → client  event   : {"t":"ev.<name>","d":{...}}
```
**Parsing and validation of every inbound frame**
* `json.loads(text, parse_constant=_reject, parse_int=_bounded_int, object_pairs_hook=_no_dupes)` where `_reject` raises on `NaN`/`Infinity`, `_bounded_int` rejects integer literals longer than 18 characters and `_no_dupes`
  rejects duplicate keys; catch `(ValueError, RecursionError)` (a deeply nested frame raises `RecursionError`, not `ValueError`); nesting depth > 8 is rejected; the frame must be a JSON object.
* `t`: string ≤ 32 chars. `id`: string of 1..64 chars; a non-string or out-of-range `id` is treated as **absent**. `d` missing ⇒ `{}`; `d` not an object ⇒ `bad_request`. Unknown fields in `d` are ignored (forward compatibility, §13).
* Ids are `int` (bool/str/float rejected) in `[1, 2^53-1]`; `up_to_id` is an `int` in `[0, 2^53-1]`; timestamps such as `muted_until` are finite numbers in `[0, 4102444800]` (bool rejected);
  strings are length-limited and lists size-limited per request.
* A request **without a usable `id` never gets a `res`** (not even for errors); it is only processed. This is how fire-and-forget `typing` works; "every request returns `res`" in §7.4 means "when an `id` was sent". `ping` requires an `id`.
* A **malformed frame** (not JSON, not an object, no `t`) is answered with `{"t":"res","id":null,"ok":false,"err":{"code":"bad_request",…}}` and counts toward the 20-consecutive-malformed rule (⇒ close 1008); it never kills
  the connection otherwise. An unknown `t` with a usable `id` is a normal `bad_request`.
* Requests are processed **concurrently** (one task per request) subject to the ordering rules of §7.6. Request frame ≤ 256 KiB; ≤ 64 requests in flight per connection (the 65th ⇒ `rate_limited`, `retry_after:1`, not executed).

### 7.1.1 Error code rules (normative; the tests assert them)
| code | when |
|---|---|
| `bad_request` | wrong type/range/length/enum; unknown `t`; `d` not an object; ids that are not an int in range; empty body without attachment; conflicting history cursors; `q` outside 2..64 chars; weak password on `admin.*` (`reason:'weak_password'`); a request with no updatable field |
| `unauthorized` | no / expired session |
| `not_member` | ANY `chat_id` the caller is not **currently** a member of — whether the chat does not exist or the caller left/was removed (no existence oracle). Applies to `chat.*`, `msg.send`, `msg.pin`, `receipt.*`, `chat.history`, `chat.get`, `msg.shared` and `msg.search {chat_id}` |
| `not_found` | a `message_id`, `reply_to_id`, `around_id`, `attachment_id`, forward source or `user_id` that does not exist, lives in a chat the caller is not in, or is not visible to the caller (§3.1) |
| `forbidden` | the caller can see the object but lacks the right: not a group admin; `only_admins_post`; editing/deleting/`msg.info` on someone else's message; any `admin.*` by a non-admin |
| `invalid_state` | the operation does not apply to this object's kind/state: group-only op on a direct chat; leaving/removing from the default group; demoting/disabling the last admin; pin limits (3 chats, 5 messages); react/pin/edit/forward/reply/star on a deleted or system message; editing a non-text or forwarded message; a disabled user or disabled direct peer as target; `max_users` reached (`reason:'max_users'`) |
| `window_expired` | edit or delete-for-everyone after its window |
| `conflict` | `client_id` reused for a different chat; username/display name taken in `admin.create_user`/`profile.update`/`admin.update_user` (`reason:'username_taken'`/`'name_taken'`) |
| `too_large` | body > `max_body_chars`; frame too large |
| `rate_limited` | carries `err.retry_after` (seconds, number) |
| `server_busy` | a time-boxed query was interrupted (§2.3); carries `retry_after` |
| `server_error` | generic message only; details in the log; also "restarting" during shutdown |
`reason` is an optional machine-readable sub-code (never required). `msg.forward` failures also carry `err.chat_id` / `err.message_id` of the offending target/source.
**Outbox retry taxonomy:** *retryable* (the message stays `sending`/queued, order preserved): socket loss before `res`, a request timeout (15 s), `rate_limited` (wait `retry_after`), `server_busy`, and `server_error`
(at most 3 tries with 1/2/4 s back-off). *Every other code is permanent* ⇒ the message becomes `failed` immediately, with no auto-retry. A timed-out mutation may still have committed, which is why every retried mutation is idempotent
(`msg.send`/`msg.forward` via `client_id`, `msg.react` via SET semantics).

### 7.2 Shared object shapes
**User** (directory entry): `{id, username, display_name, status_text, role, online: bool, last_seen: number|null, disabled: bool, activated: bool, read_receipts: bool}`.
`last_seen` is `last_seen_at`, or `null` when that user hides it (`show_last_seen=0`) or never connected — independent of `online`; `online` is always truthful. `activated` = has ever logged in (`last_login_at IS NOT NULL`);
`read_receipts` is public because clients compute ticks from it (§8.1).

**Chat** (serialised per *viewer*; built from ONE set-based read, never per-viewer queries):
```
{ id, kind:'direct'|'group', title:string|null, description, is_default, only_admins_post,
  created_by, created_at, last_activity_at,
  peer_id: int|null,                          // direct only: the other user (own id for self-chat); title is always null for direct chats
  members:[{user_id, role:'admin'|'member', delivered_up_to:int, read_up_to:int}],   // current members only; role = chat_members.role (§3.2)
  pinned_message_ids:[int],                   // ≤5, newest pin first, only pins visible to the viewer (§3.1)
  pinned_messages:[Message],                  // the same messages in the same order (banner / info drawer need bodies that may be older than any loaded page)
  last_message_id:int|null,                   // raw chats.last_message_id (the true newest id, even if not visible to the viewer)
  last_message: Message|null,                 // newest message in V(viewer) — system messages and deleted placeholders included, hidden ones excluded; null when V is empty
  me:{unread:int, unread_mentions:int, first_unread_mention_id:int|null, last_read_id:int, cleared_before_id:int,
      muted_until:number, pinned:bool, pinned_at:number|null, archived:bool} }
```
`members[].read_up_to` is the PUBLIC read watermark (frozen while that user has read receipts off). `Counters` (used by `receipt.read`, `ev.read_sync`, and equal to the `Chat.me` counter fields):
```
{ chat_id, last_read_id, last_message_id, unread, unread_mentions, first_unread_mention_id }
```
`unread` and `unread_mentions` are **capped at 1000** (the UI shows "999+"). A counters payload is valid *as of* `last_message_id` (the value of `chats.last_message_id` read in the same DB call), see §8.2.

**Message** (serialised per *viewer*):
```
{ id, chat_id, sender_id:int|null, client_id:string|null,   // client_id is non-null ONLY in the sender's own viewer (null for everybody else); clients reconcile their outbox only when sender_id == me.id
  kind:'text'|'image'|'audio'|'video'|'file'|'system',
  body:string, created_at, edited_at:number|null, deleted:bool, forwarded:bool, mentions:[int],
  reply_to: null | {id, sender_id:int|null, kind, body:string(<=200, "" if deleted or unavailable), attachment_name:string|null, deleted:bool, unavailable:bool},
  attachment: null | {id, name, mime, size, kind:'image'|'audio'|'video'|'file', url:'/files/<id>', width:int|null, height:int|null, duration:number|null},
  reactions:[{emoji, user_ids:[int]}],        // grouped, stable order = first-reaction time
  starred:bool, pinned:bool,
  status: 'sent'|'delivered'|'read'|null,     // only when sender_id == viewer (aggregate, see §8.1); null otherwise
  system: null | {event, actor_id, target_ids:[int], title, message_id:int|null} }
```
A deleted-for-everyone message has `deleted:true, body:'', attachment:null, reactions:[], pinned:false`. **`reply_to` is live**: it is serialised at send time from the quoted message as the viewer may see it, and clients patch held
quotes on `ev.message_update` (§7.6(9)). If the quoted message is not visible to the viewer (`id ≤ max(history_from_id, cleared_before_id)`, or hidden) the server sends
`{id, sender_id:null, kind:'text', body:'', attachment_name:null, deleted:false, unavailable:true}` — no content ever leaks from before a member joined; a deleted original is `{…, body:'', deleted:true, unavailable:false}`.

### 7.3 Server → client events
| Event | `d` | When / to whom |
|---|---|---|
| `ev.ready` | `{protocol:1, instance_id, me, workspace:{name, registration_open}, users:[User], chats:[Chat], server_time, limits:{max_upload_bytes, max_body_chars, edit_window_s, delete_window_s, max_pinned_chats:3, max_pinned_messages:5, max_group_members:200, max_forward_messages:20, max_forward_chats:5}}` | The very first frame of every connection (§7.6(7)). The client replaces ALL server-derived state with it (never UI state, §8.5). Other users' dormant direct chats are excluded (§3.2(3)). The client shows "reload the page" when `protocol` is higher than the one it was built for. |
| `ev.me` | `{me}` | All connections of the actor after `profile.update`, after `admin.update_user` on themselves, and after password-related changes. On `ev.user_update` for `me.id` clients merge only public fields; private ones come from `ev.me`. |
| `ev.message` | `{message}` | New message (incl. system) → **every connection of every current member whose `history_from_id < message.id`, including all connections of the sender** (clients upsert by `id` and reconcile their outbox by `client_id`). |
| `ev.message_update` | `{message}` | Edit, delete-for-everyone, reactions, pin/unpin → all members' connections (the requester included); star → only the actor's connections. Viewer-specific serialisation. |
| `ev.message_removed` | `{chat_id, message_ids:[int]}` | "Delete for me" → the actor's connections. |
| `ev.receipt` | `{chat_id, user_id, delivered_up_to?:int, read_up_to?:int}` | A member's PUBLIC watermark advanced: ABSOLUTE values for the changed fields only, routed per §8.2 (not to all members), coalesced per `(chat,user)`. Clients apply `max()` and recompute own-message `status` (§8.1). |
| `ev.read_sync` | `Counters` | Sent to **all** connections of every user whose counters changed (§8.2): receipt.read, own msg.send, delete (everyone/me), clear, mention edits, member removal. |
| `ev.typing` | `{chat_id, user_id, state:'typing'\|'recording'\|'stop'}` | Relayed to every connection of every other member; **never to any connection of the typist**. |
| `ev.presence` | `{user_id, online:bool, last_seen:number\|null}` | When a user's first connection opens / (after the 8 s grace) last closes → all live connections. `online:true` carries `last_seen:null`. |
| `ev.chat_update` | `{chat}` | Full viewer-specific Chat: new chat for a member (created/added/first message of a dormant direct chat), title/description/only_admins_post/pins changes, personal prefs/clear/delete-for-me. |
| `ev.chat_members` | `{chat_id, added:[member], removed:[user_id], updated:[member]}` | Compact, **identical JSON for every recipient (encoded once)**: members joined/left/changed role (`member` = the `members[]` entry shape). Sent to the members who remain; never to the added/removed user themselves. Used for high-fan-out membership changes (registration into `Everyone`, add/remove, `set_admin`, role mirror). |
| `ev.chat_removed` | `{chat_id}` | The viewer was removed / left → all of the viewer's connections. |
| `ev.user_update` | `{user}` | Profile edit, role/disabled/activated/read_receipts change, **new user registered** (also adds them to Everyone) → all connections. |
| `ev.workspace` | `{name, registration_open}` | Admin changed workspace settings → all. |
| `ev.kicked` | `{reason:'disabled'\|'revoked'\|'password_changed'}` | To the revoked session's connections only; followed by close `4001`. |

**Recipient matrix.** Default rule: the audience is **every connection of the target users, INCLUDING the requesting connection** (clients apply state from events and idempotently from `res`). Events are listed in enqueue order.
| Cause | Events (audience) |
|---|---|
| `msg.send` (and each message of `msg.forward`) | [first message of a dormant direct chat: `ev.chat_update` to every member] → `ev.message` (all members, sender's tabs included) → if the sender's watermarks changed: `ev.receipt` (§8.2) and `ev.read_sync` (all sender's connections) → archived reset `ev.chat_update` to members whose `archived` flipped (§3.2(7)) |
| `msg.edit` | `ev.message_update` (all members); if mentions changed: `ev.read_sync` to the affected users |
| `msg.delete` everyone | `ev.message_update` (all members) → if it was pinned: `ev.chat_update` (all members) → `ev.read_sync` to members whose counters changed |
| `msg.delete` me | `ev.message_removed` → `ev.chat_update` (if `last_message`/pins changed) → `ev.read_sync` (counters changed), all to the actor's connections |
| `msg.react` | `ev.message_update` (all members) |
| `msg.star` | `ev.message_update` (actor's connections only) |
| `msg.pin` | `ev.message_update` → `ev.chat_update` (pins) → (pin only) system `ev.message` `pinned`; all to all members |
| `chat.open_direct` (new) | `ev.chat_update` to the creator's connections (the peer learns from the first message) |
| `chat.create_group` | `ev.chat_update` to every member's connections → system `ev.message` `created` (all members) |
| `chat.update` | `ev.chat_update` (all members) → system `ev.message` `renamed` on a title change |
| `chat.add_members` | `ev.chat_update` to each new member → `ev.chat_members {added}` to the existing members → system `ev.message` `added` (all current members, new ones included) |
| `chat.remove_member` / `chat.leave` | `ev.chat_removed` to the removed/leaving user's connections → `ev.chat_members {removed}` to the remaining members → system `ev.message` `removed`/`left` (remaining members) |
| `chat.set_admin` | `ev.chat_members {updated}` (all members) → system `ev.message` `promoted`/`demoted` |
| `chat.prefs` | `ev.chat_update` to the **actor's connections only** (never to other members) |
| `chat.clear` | `ev.chat_update` and `ev.read_sync` to the actor's connections (also in `res`) |
| `receipt.delivered` / `receipt.read` | `ev.receipt` per §8.2; `ev.read_sync` to all of the actor's connections (read only) |
| `typing` | `ev.typing` to every member's connections except the typist's |
| `profile.update` | `ev.user_update` (all) + `ev.me` (actor's connections) |
| registration / `admin.create_user` | `ev.user_update` (all) → `ev.chat_members {added}` (Everyone's other members; the new user gets the full chat in `ev.ready`/`ev.chat_update`) → system `ev.message` `joined` |
| `admin.update_user` | `ev.user_update` (all) (+ `ev.me` if self); role change: `ev.chat_members {updated}` for Everyone; disable: `ev.kicked` + close 4001 on the target, promotions per §3.2(6) |
| `admin.reset_password` | `ev.kicked 'revoked'` + close 4001 on the target's connections |
| `admin.settings` | `ev.workspace` (all) |

### 7.4 Client → server requests (`d` fields; `?` = optional; every request returns `res` when an `id` was sent; error codes per §7.1.1; events per the §7.3 recipient matrix)
**Connection**
* `ping {}` → `{now}` (needs an `id`; the client sends one every 20 s; used for latency + liveness; exempt from all rate limits).

**Chats**
* `chat.get {chat_id}` → `{chat}` (`not_member` otherwise). Used by clients that receive an event for an unknown chat (§7.6(9)).
* `chat.open_direct {user_id}` → `{chat}`. `user_id` may be self; unknown ⇒ `not_found`; **idempotent**: an existing direct chat is returned unchanged and no event is emitted; a *new* chat with a disabled user ⇒ `invalid_state`;
  a new chat is dormant until its first message (§3.2(3)).
* `chat.create_group {title, member_ids:[int], description?}` → `{chat}`; title 1..60, description ≤ 500 (§4.3 normalisation); the creator is added as admin and is never listed in `member_ids` (the caller and duplicates are dropped silently);
  unknown user ⇒ `not_found`; disabled user, or more than 200 members in total ⇒ `invalid_state`; inserts the system message `created`. The first message of the chat is therefore the system message and every member gets `ev.chat_update` before it.
* `chat.update {chat_id, title?, description?, only_admins_post?}` (group admin; the default group included; ≥ 1 field else `bad_request`) → `{chat}`; system message `renamed` on a title change. A direct chat ⇒ `invalid_state`.
* `chat.add_members {chat_id, user_ids:[int]}` (group admin; ≤ 50 ids per call) → `{chat}`; rules in §3.2(5); system message `added`; each new member gets the full chat via `ev.chat_update` first.
* `chat.remove_member {chat_id, user_id}` (group admin, not self — use `chat.leave`; never the default group) → `{chat}`; target not a member ⇒ `not_found`; also deletes the target's `stars` of that chat.
* `chat.set_admin {chat_id, user_id, admin:bool}` (group admin; target must be a member else `not_found`; can't demote the last admin; never the default group) → `{chat}`; system message `promoted`/`demoted`.
* `chat.leave {chat_id}` (group, not default; promotion and last-member rules in §3.2(6)) → `{}`; also deletes the leaver's `stars` of that chat.
* `chat.prefs {chat_id, muted_until?:number, pinned?:bool, archived?:bool}` (per-user; at least one field else `bad_request`) → `{chat}`. `muted_until` ≤ server now is stored as `0`. At most 3 pinned chats (the 4th ⇒ `invalid_state`);
  pinning an already pinned chat is a no-op that keeps `pinned_at`; `archived:true` clears the pin (§3.2(7)). The client derives the absolute mute time from `server_time` (§8.5).
* `chat.clear {chat_id}` → `{chat}`. Sets `cleared_before_id = chats.last_message_id` and `last_read_id = last_message_id`; advances `delivered_id` but **not** `read_receipt_id`; the chat stays in the list with `last_message:null` and an unchanged
  `last_activity_at`; on a chat without messages it is a no-op. Clients drop cached messages with `id ≤ me.cleared_before_id`.
* `chat.history {chat_id, before_id?, after_id?, around_id?, limit?}` → `{messages:[Message], has_more_before:bool, has_more_after:bool}`, messages **ascending by id**. At most one of `before_id`/`after_id`/`around_id` (else `bad_request`);
  `limit` is clamped to [1,100], default 50. Let `V` = the viewer's visible messages (§3.1; empty ⇒ `[]` with both flags false).
  * no cursor: the newest `limit` of V; `has_more_after=false`.
  * `before_id=X`: the newest `limit` of `{v∈V: id<X}`; `has_more_after` = ∃ `v∈V` with `id ≥ X`.
  * `after_id=X`: the **oldest** `limit` of `{v∈V: id>X}`; `has_more_before` = ∃ `v∈V` with `id ≤ X`.
  * `around_id=X`: X must be in V else `not_found`; the page is up to `limit//2` messages before X, X itself and up to `limit-1-limit//2` after X (no compensation when one side is short).
  * `has_more_*` are computed with `LIMIT limit+1` on V, so hidden messages never produce phantom pages.

**Messages**
* `msg.send {chat_id, client_id, body?, attachment_id?, reply_to_id?, mentions?}` → `{message}`. ONE `Database.run` that performs, in this order:
  1. shape/ids ⇒ `bad_request` (`client_id` required, 8..64 chars `[A-Za-z0-9_-]`; `mentions` is a legacy hint and is **ignored**);
  2. membership ⇒ `not_member`; 3. permissions: `only_admins_post` and the caller is not a group admin ⇒ `forbidden`; direct chat with a disabled peer ⇒ `invalid_state`;
  4. **dedupe**: if `(sender_id, client_id)` exists: a different `chat_id` ⇒ `conflict`; otherwise return that message in its current serialisation (even if deleted since) with **no** fan-out/events, and it does **not** count toward the rate limit;
  5. `reply_to_id` must be `visible` (§3.1) to the sender in the same chat else `not_found`; a system or deleted message ⇒ `invalid_state`;
  6. `attachment_id` must exist, have `uploader_id == sender` and be unattached, else `not_found` (also when it belongs to someone else); it is attached in this same function (one attachment per message); the message `kind` is the attachment's `kind`, or `'text'` without one;
  7. body: strip surrounding whitespace, remove NUL and C0 controls except `\n` and `\t`, length ≤ `max_body_chars` (`too_large`); an empty body without attachment ⇒ `bad_request`.
  Then: INSERT the message, UPDATE `chats(last_message_id, last_activity_at)`, derive mentions (below), advance the **sender's own marks** and return `recipients=[(user_id, history_from_id)]` read in the same transaction. A `sqlite3.IntegrityError` on
  `messages_client` ⇒ ROLLBACK, re-select and return the existing row (step 4 semantics). **Sender marks** (same transaction): `last_read_id = max(last_read_id, new_id)`, `delivered_id = max(…, new_id)`, and `read_receipt_id = max(…, new_id)` **only if**
  `users.read_receipts=1`; `receipts` rows per §3.3. After `ev.message` is enqueued, if any watermark changed the server emits `ev.receipt` (§8.2) and `ev.read_sync` (counters) to all of the sender's connections.
  **Mentions** are derived by the SERVER from the body: the ids of current, non-disabled members other than the sender whose username appears as a token `@<username>` (case-insensitive; token grammar
  `(?<![A-Za-z0-9._-])@([A-Za-z0-9._-]{3,32})`, taking the greedy match and, when no member has exactly that username, trimming trailing `.`/`-` one character at a time), at most 20 distinct, and none in direct chats. They are stored in
  `message_mentions` (and mirrored in `messages.mentions`).
* `msg.edit {message_id, body}` → `{message}`: own `kind='text'`, non-forwarded, non-deleted message within `edit_window_s`; sets `edited_at`; the body follows rule 7 above and `mentions` are **recomputed** (rows added/removed; affected users get `ev.read_sync`;
  editing never triggers notifications because they fire on `ev.message` only). Errors: someone else's message ⇒ `forbidden`; non-text, forwarded, deleted or system ⇒ `invalid_state`; after the window ⇒ `window_expired`.
* `msg.delete {message_id, scope:'me'|'everyone'}` — `everyone`: the sender within `delete_window_s` (`window_expired`), or a **group admin** of that chat at any time (moderation; not in direct chats, where only your own messages qualify); someone else's by a non-admin ⇒ `forbidden`;
  a system message ⇒ `invalid_state`; deleting an already deleted message returns `{message}` (idempotent, no events) → `{message}`. `me`: inserts into `hidden_messages` → `{}`; an already hidden message returns `{}` (idempotent).
  Delete-for-everyone keeps `status` unchanged and keeps receipts rows. Side effects and events: §3 rules and the §7.3 matrix (pins removed, counters re-sent).
* `msg.react {message_id, emoji:string|null}` → `{message}`. **SET semantics**: it sets my single reaction; sending the same emoji again is a no-op; `null` removes it (the client sends `null` when its own reaction equals the emoji clicked), so a retry can never flip state.
  A deleted or system message ⇒ `invalid_state`. Emoji validation: ≤ 16 code points; every code point is of Unicode category `So` or `Sk`, or one of U+200D, U+FE0F, U+20E3, a regional indicator (U+1F1E6–1F1FF), a tag character (U+E0020–E007F), or a keycap base `[0-9#*]` that is
  immediately followed by `U+FE0F? U+20E3`; at least one code point must be `So`/`Sk` or a keycap sequence; no ASCII whitespace or letters/digits outside a keycap sequence; otherwise `bad_request`.
* `msg.star {message_id, starred:bool}` → `{message}` (a deleted or system message ⇒ `invalid_state`).
* `msg.pin {chat_id, message_id, pinned:bool}` (any member of a direct chat; any member of a group unless `only_admins_post`, then admins) → `{chat}`. At most 5 pinned messages (the 6th ⇒ `invalid_state`); a deleted or system message ⇒ `invalid_state`;
  pinning an already pinned message and unpinning a non-pinned one are idempotent no-ops with no events; unpin emits no system message; pinning emits the system message `pinned` with `system.message_id` = the pinned message.
* `msg.forward {message_ids:[int], chat_ids:[int], client_id}` (≤ 20 messages, ≤ 5 chats after de-duplication; `client_id` 8..64 `[A-Za-z0-9_-]`) → `{messages:[Message]}`. **All-or-nothing**: everything is validated first and any failure rejects the whole request with
  nothing created, naming the culprit in `err.chat_id`/`err.message_id`: a source that is unknown/not visible ⇒ `not_found`, deleted or system ⇒ `invalid_state`; a target the caller is not a member of ⇒ `not_member`, `only_admins_post` ⇒ `forbidden`, disabled direct peer ⇒ `invalid_state`.
  Creation order: for each chat in `chat_ids` order, for each source ascending by source id; `messages` is that flat list. A copy keeps `kind`, `body` and `attachment_id` (the same `attachments` row), sets `forwarded=1`, `reply_to_id=NULL`, no mentions, and
  `client_id = sha256("<client_id>|<chat_id>|<source_id>").hexdigest()[:32]`, so a retry after a lost response returns the existing messages and emits nothing. It counts `len(chat_ids)` tokens of the `msg.send` rate limit (§7.5). Locks: §7.6(2).
  Each target chat gets the normal `ev.message` and sender-marks rule. Forward is not queued in the outbox (it needs a live connection); a transport-loss retry reuses the same `client_id`.
* `msg.info {message_id}` → `{message, recipients:[{user_id, delivered:bool, read:bool, delivered_at:number|null, read_at:number|null}]}`: own, non-deleted, non-system message (someone else's ⇒ `forbidden`; deleted/system ⇒ `invalid_state`). `recipients` = current members except the
  sender and except members with `history_from_id ≥ message.id` (they can never receive it); disabled members are included. `delivered`/`read` come from the watermarks (`read` = public read watermark ≥ id); the timestamps come from `receipts` rows and are `null`
  when no row exists (e.g. chats with > 50 members, or `read_at` while that user has receipts off).
* `msg.search {q, chat_id?, before_id?, limit?}` → `{results:[{message, chat_id}], has_more, total?}`. `q` is stripped and must be 2..64 chars (`bad_request`); `limit` ≤ 50, default 30; a `chat_id` the caller is not in ⇒ `not_member`. Newest first by message id, only `V`
  (§3.1), non-deleted, non-system. SQL: `fold(body) LIKE ? ESCAPE '\'` with the pattern `%` + escaped `q.casefold()` + `%`; escape order: replace `\` with `\\`, then `%` with `\%`, then `_` with `\_`; `LIMIT limit+1`; at most one search in flight per user; time-boxed (§2.3).
  `total` (count, capped at 1000) is present only on the first page (no `before_id`) of a chat-scoped search. A hit is opened with `chat.history {around_id}`.
* `msg.starred {chat_id?, before_id?, limit?}` → `{messages:[Message], has_more}`: newest first **by message id** (`before_id` is a message id), only visible, non-deleted messages of chats the caller is still a member of; `limit` ≤ 50, default 30.
* `msg.shared {chat_id, kind:'media'|'files'|'links', before_id?, limit?}` → `{messages:[Message], has_more}`: newest first by message id; `media` = kind in (`image`,`video`); `files` = kind in (`file`,`audio`); `links` = `kind='text'` whose body contains `http://` or `https://`
  (case-insensitive); deleted and system messages excluded; `not_member` rule; `limit` clamped to [1,50], default 30.

**Receipts / typing / presence**
* `receipt.delivered {chat_id, up_to_id}` **or** `{items:[{chat_id, up_to_id}]}` (≤ 50 items, one transaction; a non-member item fails the whole request with `not_member`) → `{}`. `delivered_id = max(delivered_id, min(up_to_id, chat.last_message_id))`. Emits `ev.receipt` only if the
  value really increased.
* `receipt.read {chat_id, up_to_id}` → `Counters`. `up_to_id` is clamped to `min(up_to_id, chat.last_message_id)` (a client can never pre-read future messages; a non-int or negative value ⇒ `bad_request`). Advances `last_read_id` (**always**, private), `delivered_id`, and
  `read_receipt_id` (**only** if `users.read_receipts=1`). Emits `ev.receipt` only if a public watermark increased, and `ev.read_sync` to all of the actor's connections. The counters in the response are as of `last_message_id` (§8.2).
* `typing {chat_id, state:'typing'|'recording'|'stop'}` – fire-and-forget (no `id`, hence no `res`). Silently ignored for non-members (membership from the hub's in-memory index, no DB call per frame), for non-admins in `only_admins_post` chats, for a direct chat with a disabled
  peer and for the self-chat. Relay rules: §8.3.

**Profile & admin**
* `profile.update {display_name?, status_text?, read_receipts?:bool, show_last_seen?:bool}` → `{me}` (at least one field else `bad_request`; normalisation/uniqueness §4.3 ⇒ `conflict` `reason:'name_taken'`). Emits `ev.user_update` (all) and `ev.me` (actor's connections).
* `admin.users {}` → `{users:[User + {created_at, last_login_at, message_count, must_change_password}]}` (admin only; else `forbidden`).
* `admin.create_user {username, display_name, password, role?}` → `{user}` (works even when registration is closed; ignores the join code; reserved usernames allowed; password policy §4.1 ⇒ `bad_request` `reason:'weak_password'`; duplicate ⇒ `conflict`; `max_users` ⇒ `invalid_state`);
  sets `must_change_password=1`, calls `hub.user_created`.
* `admin.update_user {user_id, role?, disabled?, display_name?}` → `{user}`; can't demote/disable the last active admin (`invalid_state`) or yourself as last admin; a role change also updates the default-group role (§3.2(1)); disabling ⇒ `hub.revoke(reason 'disabled')`,
  group-admin promotions (§3.2(6)) and `ev.user_update`.
* `admin.reset_password {user_id, new_password}` → `{}`; revokes that user's sessions (`ev.kicked reason:'revoked'`), sets `must_change_password=1`.
* `admin.settings {workspace_name?, registration_open?, rotate_join_code?:bool}` → `{workspace:{name, registration_open, join_code}}` (`join_code` appears only in this admin-only response) + `ev.workspace`. Never renames the default group.
* `admin.stats {}` → `{users, online, chats, messages, attachments, storage_bytes (cached hourly), db_bytes, disk_free_bytes, last_backup_at, uptime_s, python, version, urls:[<LAN urls>]}`.
* `admin.audit {limit?}` → `{entries:[{id, ts, actor_id, action, target_id, ip}]}` newest first, `limit` ≤ 100.
Every `admin.*` mutation appends to `audit_log` (§4.3).

### 7.5 Limits (enforced server-side; token buckets per **user across all connections** unless noted; excess ⇒ `rate_limited` with `err.retry_after`)
| Scope | Limit |
|---|---|
| all requests except `ping`/`typing` | 100 / 10 s per user and 30 / 5 s per connection |
| `msg.send` | 20 / 10 s (dedupe hits do not count; `msg.forward` consumes `len(chat_ids)` tokens) |
| `msg.forward` | 5 / 10 s |
| `typing` | 5 / s per connection (silently dropped) |
| `receipt.*` | 10 / s per connection |
| `msg.search` | 5 / 10 s |
| `msg.info`, `msg.starred`, `msg.shared`, `chat.history`, `chat.get` | 20 / 10 s |
| `msg.react` | 30 / 10 s |
| `msg.pin` | 10 / min |
| `chat.create_group` | 5 / min |
| `chat.add_members` | 20 / min |
| `chat.open_direct` | 30 / min |
| `profile.update` | 5 / min |
| `admin.*` | exempt from the global bucket, but `admin.create_user` and `admin.reset_password` ≤ 30 / min each and every other `admin.*` ≤ 60 / min |
| in flight | ≤ 64 requests per connection (the 65th ⇒ `rate_limited`, `retry_after:1`, not executed) |
Request frame ≤ 256 KiB. A user who violates a limit in 3 separate windows within 60 s has the offending connection closed with `4008`. A deduplicated `msg.send` is never rate limited.

### 7.6 Delivery guarantees (ordering, locking, fan-out) — normative
1. **Per-connection order.** Frames reach a connection in the order the hub enqueued them (§6 send path; coalescing only drops/merges ephemeral frames). Enqueueing is synchronous: after `Database.run()` returns, the handler serialises the per-viewer objects and enqueues
   them with **no `await` in between**, so enqueue order equals commit order (the writer thread is single, so futures resolve in submission order).
2. **Per-chat lock.** Every handler that changes a chat's messages, members, pins or settings holds that chat's `asyncio.Lock` for the whole sequence "DB write → serialise per viewer → enqueue events": `msg.send/edit/delete(everyone)/react/pin/forward`,
   `chat.update/add_members/remove_member/set_admin/leave`, `hub.user_created` (for `Everyone`) and every system message they insert. `msg.forward` locks all its target chats in **ascending `chat_id`** order (two concurrent forwards `[1,2]` and `[2,1]` cannot deadlock).
   `chat.create_group` inserts a brand-new chat and needs no lock. Per-user changes (`msg.star`, delete-for-me, `chat.prefs`, `chat.clear`, `receipt.*`, `typing`) take no chat lock; their events carry absolute values that clients merge with `max()`.
   `asyncio.Lock` is not re-entrant: locks are taken only at request-handler level; internal helpers are named `*_locked` and never lock.
3. **`msg.send` FIFO.** `msg.send` acquires the chat lock as its FIRST `await` (shape validation and the rate limiter are synchronous), so lock order = frame arrival order = id order for one connection. The client additionally keeps at most one `msg.send` in flight per chat (§9.7).
4. **Events before `res`.** Every `ev.*` caused by a request is enqueued BEFORE that request's `res` (for the requesting connection too), so `res` is a barrier. Clients must still be idempotent (§7.6(9)); the test client queues events from the moment the socket opens.
5. **Cancel-safety.** Persist + fan-out of every mutating request runs as a detached task (`t = loop.create_task(op()); hub._inflight.add(t); t.add_done_callback(hub._inflight.discard)`); the request handler only awaits `asyncio.shield(t)`, so closing a connection cancels the waiter,
   never the operation. The fan-out loop wraps every connection in its own `try/except`. A timeout never turns a mutation into "did not happen": the server answers `server_error` and relies on idempotent retries.
6. **Recipients come from the committing transaction.** The write function returns `recipients=[(user_id, history_from_id)]` (or the member list for chat events) read in the SAME transaction; fan-out sends `ev.message` only to connections of users in that snapshot whose
   `history_from_id < message.id`. Membership changes take the chat lock and update the hub's in-memory membership index (used only for typing relays) before releasing it; a removed user gets `ev.chat_removed` and nothing chat-scoped afterwards. Role, disabled flag and membership
   are never cached on a connection.
7. **Pending → live (`ev.ready` first).** On WS open the hub registers the connection as `pending` — BEFORE it starts the snapshot read: events destined for it go to `conn.backlog` (cap 2000, else close 1013) and presence broadcasts skip it. The ready payload is built from ONE reader
   transaction; the hub sends `ev.ready` as the very FIRST frame, then flushes the backlog in order, then marks the connection `live`. Requests received before `ev.ready` was queued are held, not dispatched. No `ev.*` is ever sent before `ev.ready`. Replayed events may duplicate
   snapshot content — harmless, because every event is an idempotent upsert (item 9).
8. **Causal order.** `ev.user_update` (new user) < the `ev.chat_members`/`ev.chat_update` that mentions them < the first `ev.message` of that chat. `chat.create_group` sends `ev.chat_update` to every member before the system `created` message; a member's `ev.chat_update` for a chat
   always precedes its first `ev.message` for that member.
9. **Client apply rules.** Object events carry full viewer-specific state and replace the client's copy (last-writer-wins by arrival) but keep client-local fields (loaded messages, window flags, typing state); ids and watermarks merge with `max()`. `ev.message_update` patches **every
   held copy** of that message id: the loaded window, `Chat.last_message`, `Chat.pinned_messages`, the Starred list and every held `reply_to` whose `id` equals it (setting `deleted`, `body`, `unavailable`); for a message not held it is ignored. An event for an unknown `chat_id`
   triggers ONE `chat.get` and is re-applied afterwards. The client NEVER adjusts counters from `ev.message_update`/`ev.message_removed` (§8.2).

---------------------------------------------------------------------------------------------------

## 8. Delivery / seen / typing / presence semantics

### 8.1 Message status (✓, ✓✓, blue ✓✓)
For the sender's message `m` in chat `C`:
* `R` = members of `C` except the sender, except users with `disabled=true`, except users that were **never activated** (`User.activated=false`, they cannot have received anything). A member who joined after `m` has watermarks ≥ `m.id` by construction (§3) and never blocks.
* `R_read` = the members of `R` with `User.read_receipts=true`.
* `sent` (✓) — persisted (`status:'sent'`).
* `delivered` (✓✓ grey) — `R` is non-empty and **every** `r ∈ R` has `delivered_up_to ≥ m.id`.
* `read` (✓✓ blue) — delivered AND `R_read` is non-empty AND **every** `r ∈ R_read` has `read_up_to ≥ m.id`. Members with receipts off neither block nor contribute blue, so one privacy-minded member never mutes the ticks of a group; a direct chat with a
  receipts-off peer never turns blue (same as WhatsApp).
* Self-chat: `null` (no ticks). System messages: `null`. Any other chat with an empty `R`: `sent`.
The server serialises `status` per viewer as the *initial* value; clients **recompute** the status of their own messages with the same rule on `ev.receipt`, `ev.chat_update`, `ev.chat_members`, `ev.user_update` (disabled/activated/read_receipts changed), `ev.ready` and every history load.
Client-only extra states: `sending` (in outbox, clock icon), `failed` (tap to retry). A reader's `delivered_up_to` is always ≥ their `read_up_to` (§3.3).
**Toggling `read_receipts`:** `1→0` freezes `read_receipt_id` (nothing is retracted, no event is emitted). `0→1` publishes nothing by itself; the next `receipt.read` advances `read_receipt_id = max(read_receipt_id, min(up_to_id, last_message_id))` and sets `read_at` for the whole advanced
range, i.e. messages read while receipts were off are then revealed as read (a single watermark cannot hide individual messages — the Settings text says so). `receipt.read` always advances the private `last_read_id` regardless of the flag.

### 8.2 Who sends what, when
* **Delivered**: the client sends `receipt.delivered {chat_id, up_to_id: <highest message id it holds for that chat>}` as soon as it receives `ev.message`, batched with **`setTimeout(flush, 150)` — never `requestAnimationFrame`** (rAF does not fire in hidden tabs, and a backgrounded tab is exactly when the
  sender must see ✓✓) and **regardless of visibility/focus**. After `ev.ready` (every connect) it sends ONE batched `{items:[…]}` request (≤ 50 items, repeat if more) for every chat whose `last_message_id` > own `delivered_up_to`. Offline recipients therefore get ✓✓ for the sender the moment they come online.
* **Read**: the client sends `receipt.read` only when ALL hold: that chat is the active chat, `document.visibilityState === 'visible'`, `document.hasFocus()`, the window holds the newest message (`has_more_after === false`) and it is scrolled to within 40 px of the bottom — debounced 300 ms;
  messages that arrive while these hold are marked read immediately. `up_to_id` = the id of the newest server-acknowledged item in the window. When the user returns to the tab / focuses the window / scrolls to bottom, send `receipt.read` for the active chat. After every `ev.ready` and history merge the client
  re-evaluates both rules for all chats. This is the only receipt gated on visibility/focus. Sending a message needs nothing extra: the server advances the sender's own marks (§7.4 msg.send) and emits the matching `ev.receipt`/`ev.read_sync`.
* **Receipt routing (server).** A receipt event is emitted only when the stored value really increased. `ev.receipt` goes to (a) all connections of the actor and (b) all connections of the members who **authored a non-system message with id in `(old, new]`**
  (`SELECT DISTINCT sender_id FROM messages WHERE chat_id=? AND id>? AND id<=? AND kind!='system'`) — never to all members (a 200-member group would otherwise cost ~80,000 frames per message). Events are coalesced per `(chat, user)` over 100 ms and carry ABSOLUTE watermarks for the changed
  fields only; clients apply `max()`. Other clients' copies of a member's watermark may be stale and are refreshed by `ev.ready`, `ev.chat_update` and `msg.info`; only authors need them (to compute ticks).
* **Unread counters.** `unread` = messages with id > `last_read_id`, visible (§3.1), `sender ≠ me`, not hidden, `kind ≠ system`, not deleted-for-everyone, **capped at 1000** (`SELECT COUNT(*) FROM (SELECT 1 … LIMIT 1000)`). `unread_mentions` = the same set restricted to rows of `message_mentions` for me;
  `first_unread_mention_id` = the smallest such message id (or null). **As-of rule:** every counters payload (`res(receipt.read)`, `ev.read_sync`, the `Chat.me` fields) is valid *as of* its `last_message_id`. The client keeps, per chat, the countable messages it received via `ev.message` since its last payload
  (`id > last_read_id`, with a mention flag) and, on a payload, sets `unread = payload.unread + #{held countable messages with id > payload.last_message_id}` (same for mentions), then drops held entries with `id ≤ payload.last_message_id`. For an incoming countable `ev.message` it increments only if
  `message.id > chat.last_message_id` as currently known (a replayed/duplicate message is never counted twice) and updates `last_message_id`. The client **never** changes counters from `ev.message_update` or `ev.message_removed`; the server instead sends `ev.read_sync` to every affected user after:
  `receipt.read`, the user's own `msg.send`, delete-for-everyone (to members who had it unread), delete-for-me, `chat.clear`, mention changes by `msg.edit`, and member removal.
* Messages sent while a recipient is offline are persisted and appear in their chat list (with unread count) when they connect — **no polling needed**.

### 8.3 Typing indicator
* Client emits `typing {state:'typing'}` on `input` when the composer is non-empty, throttled to ≤ 1 per 2.5 s; emits `stop` when the composer is emptied, a message is sent, the chat is switched, or after 5 s without keystrokes. `recording` is emitted while recording a voice note (refresh every 2.5 s).
* Server state is keyed `typing[(chat_id, user_id, conn_id)] = (state, expires_at = monotonic()+7s)`; each `typing` refreshes its entry; a **1 s** sweeper expires entries; the effective state of `(chat, user)` is `recording` if any entry records, else `typing`, else stopped.
  `ev.typing {state:'stop'}` is emitted only when the **last** entry of that `(chat, user)` is gone (a `stop` from one tab, or the closing of one connection, drops only that connection's entries).
  A relay is sent only when the effective state changes or a refresh arrives ≥ 1 s after the last relay. `ev.typing` is **never** sent to any connection of the typist. Typing is never persisted. Ignore rules: §7.4.
* Receiving client also self-expires an indicator after 8 s without a refresh. Text: direct → "typing…" (header) / "typing…" (chat-list preview, green); group → "Ravi is typing…", "Ravi and Amit are typing…", "3 people are typing…"; recording → "recording audio…".

### 8.4 Presence
Online ⇔ the user has ≥ 1 open authenticated WS. **Grace period:** when a user's last connection closes the server records `last_seen = ws.last_rx` of that connection (the real last activity, not "now") and starts `loop.call_later(8, finalize)`; a new authenticated connection for that user before
`finalize` cancels it and broadcasts nothing (a page reload or Wi-Fi roam causes no offline/online noise); `finalize` broadcasts `ev.presence {online:false, last_seen}` and writes `users.last_seen_at`. The first connection of an offline user broadcasts `ev.presence {online:true, last_seen:null}`.
`last_seen_at` is also written on login, and every 60 s for all online users (crash-safe, §2.4). `ev.presence` is never sent to `pending` connections (they get the snapshot). Privacy: if `show_last_seen=0` the server sends `last_seen:null` to everybody else (online status is still shown — WhatsApp behaviour).
UI strings: "online", "last seen today at 10:42", "last seen yesterday at 18:05", "last seen 12 Mar at 09:00", or nothing when hidden — computed with the clock skew of §8.5.

### 8.5 Reconnect & offline robustness
* **Back-off:** `delay = random(0, min(10 s, 0.5·2ⁿ))` seconds (full jitter, `n` = consecutive failures). After a close with 1001/1013/1006 on a previously open socket the FIRST attempt waits `random(0, 5 s)` (a server restart makes ~300 clients reconnect at once). Immediate reconnect on `online` /
  `visibilitychange` only if the last attempt was > 3 s ago. Never more than 12 connection attempts per minute. A banner "Reconnecting…" is shown after 2 s.
* **Close-code table (client):** `1000/1001/1006/1012` ⇒ reconnect with the back-off above. `1013` ⇒ back-off starting at 5 s. `1002/1003/1007/1008/1009` ⇒ wait ≥ 10 s, then normal back-off. `4008` ⇒ wait ≥ 30 s, then jittered back-off. `4001` ⇒ **never reconnect**: discard in-memory state and show the login screen with text by the
  last `ev.kicked` reason (`disabled` "Your account has been disabled. Contact your admin.", `revoked` "You were signed out by an administrator.", `password_changed` "Password changed - please sign in again."). `4003` ⇒ **never auto-reconnect** (otherwise the tabs would evict each other in a cycle): full-screen
  "FreeChat is open in too many windows (max 8). Close another window, then [Use this window]" whose button reconnects.
* **Failure before `open`:** a handshake rejected with 401/403/429 is invisible to JS (the browser only reports 1006). After any close/error before `ev.ready` the client calls `GET /api/me` (3 s timeout): `401` ⇒ login screen ("Your session expired"); `403 password_change_required` ⇒ forced change-password screen;
  network error/5xx/429 ⇒ keep reconnecting with back-off; `200` ⇒ normal retry.
* **Half-open sockets:** after phone lock / laptop sleep `readyState` can stay OPEN while nothing arrives. On `visibilitychange`→visible, `online`, `pageshow` (incl. `persisted`) and whenever `Date.now()` jumps > 30 s between timer ticks the client sends `ping` and expects its `res` within 2.5 s, else `ws.close()` and reconnects
  immediately. Liveness is timestamp-based (hidden-tab timers are throttled).
* **On every `ev.ready`** the client (a) replaces server-derived state only — users, chats (members, watermarks, `me.*`), `me`, `limits`, `server_time` — and never UI state (route, scroll anchor, drafts, outbox, selection mode, open dialogs); (b) **drops ALL held messages of every chat** (edits, deletes, reactions and pins missed while offline cannot be replayed,
  so a merge could leave stale content or a hole; the store keeps only chat metadata); (c) for the open chat reloads the window: if it was stuck to bottom, fetch the newest page; otherwise `chat.history {around_id: A, limit: 100}` where `A` is the id of the message anchored at the top of the viewport, preserving the scroll anchor; older history reloads lazily by scrolling;
  (d) re-evaluates the delivered/read rules of §8.2 for all chats; (e) re-sends the outbox (below). Invariant: a cached message window is always **one contiguous id range** with `has_more_before/after` flags, never a union of disjoint pages.
* **Clock skew:** `skew = server_time − Date.now()/1000`, computed at every `ev.ready`; used for date separators, "last seen" text, optimistic timestamps, mute expiry (`muted_until` > now+skew) and enabling Edit / Delete-for-everyone (the server stays the authority via `window_expired`).
* **Outbox:** unsent messages live in an outbox (memory + namespaced `localStorage`, §9.7) with at most **one `msg.send` in flight per chat**; on reconnect the client re-sends the items of each chat in order with their original `client_id` (the server dedupes). Retry classification: §7.1.1.
* Draft text per chat is kept in memory (and namespaced `localStorage` best-effort) while switching chats.

---------------------------------------------------------------------------------------------------

## 9. Web client

### 9.1 Architecture
Vanilla ES modules, one `<script type="module" src="/js/main.js">`. No framework. A tiny reactive `store` (`core/store.js`) holds all state and emits
fine-grained events (`store.on('chat:<id>', fn)`, `store.on('messages:<chatId>', fn)`, `'chats'`, `'users'`, `'presence'`, `'typing:<chatId>'`, `'connection'`, `'ui'`);
views subscribe and patch the DOM (no full re-render of long message lists; message list is incrementally appended/updated by `message.id`).
`ui-core` documents the exported API of `store`, `socket`, `outbox`, `router`, `api`, `notify`, `ui` at the top of each file **and** in `docs/ui-core-api.md`
(function list with one-line semantics; it lives under `docs/` because everything under `web/` is served) — the other UI owners code against that.
**Safety rule (hard)**: user-controlled text is only ever inserted with `textContent` / `document.createTextNode` / DOM construction through `core/dom.js:h()`.
`innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, `setAttribute('style')`, inline event attributes, `DOMParser`, `createContextualFragment`, `template.innerHTML`, `srcdoc`, `javascript:` URLs,
`CSSStyleSheet`/`adoptedStyleSheets`/`insertRule`, `document.createElement('style')`, writing `document.cookie` and storing tokens in web storage are **forbidden** (the static UI lint in §12 enforces the list).
Links: only `http:`/`https:` URLs become anchors (`rel="noopener noreferrer" target="_blank"`).

### 9.2 Screens & features (MUST unless marked *)
**Auth**: first run → "Set up your workspace" (create admin; asks for the setup code, §4.3); otherwise Login / Register (if open; asks for the join code). Real `<form>` elements with `autocomplete=username|current-password|new-password` (password managers must offer to save).
Server errors shown inline. Enter submits. Remember session (cookie). Logout. A forced change-password screen when `me.must_change_password`. Login shows "Forgot password? Ask your admin to reset it." and, over plain HTTP to a non-loopback host, the "not encrypted" banner (§0).
**Layout** (WhatsApp-Web style): left sidebar (chat list) + conversation pane + optional right drawer (info). Dark & light theme (`prefers-color-scheme`, manual override), responsive:
≥ 900 px three-pane, 600–900 px two-pane, < 600 px single pane with back button and hash routing (§9.8; `viewport-fit`, 100dvh, safe-area insets, mobile rules). Keyboard accessible, ARIA roles/labels, visible focus, `prefers-reduced-motion`.
**Sidebar**: my avatar (opens settings), connection indicator, "New chat" (pick a person), "New group", filter tabs (All · Unread · Groups), search box (filters chats + people; typing ≥ 2 chars
also searches messages, debounced per §9.6), chat rows: avatar (initials + colour hashed from id), name, last-message preview with sender prefix in groups and delivery ticks for own messages,
time (HH:MM / Yesterday / date), unread badge (+ "@" if mentioned), muted/pinned icons, "typing…"/"recording…" in green replacing the preview, online dot for direct chats,
context menu (pin, mute 8h/1 week/always, archive, clear chat, leave group — with confirm dialogs); archived section. Sorted: pinned chats first (by `pinned_at` desc), then `last_activity_at` desc; a new message un-archives a non-muted archived chat (§3.2(7)); preview strings in §9.10.
**Conversation header**: avatar, name, status line (online / last seen / typing… / group member names), buttons: search-in-chat, info drawer, menu.
**Message list**: date separators ("Today", "Yesterday", date), grouped bubbles (own right, others left), sender name + colour in groups, "N unread messages" divider (§9.6),
"scroll to bottom" FAB with unread counter, infinite scroll upward (`chat.history before_id`), jump-to-quoted-message (`around_id`), highlight flash, pinned-message banner (from `Chat.pinned_messages`); window model and scroll rules in §9.6.
Bubble contents: rich text (§9.3), quoted reply block, "Forwarded" label, attachment (image thumbnail → lightbox; video player; audio player with duration; file card with icon/size/download),
reactions row (click to toggle), time, "edited" label, ticks (clock → ✓ → ✓✓ → blue ✓✓; red "!" on failed with retry), "message deleted" placeholder in italics, system messages centred.
Hover/long-press/right-click **message menu**: Reply, React (quick bar 👍❤️😂😮😢🙏 + more), Copy, Forward, Star/Unstar, Pin/Unpin, Info (own: delivered/read per member with times), Edit (own text, within window),
Delete (for me / for everyone). Multi-select mode* (select several → forward / delete / star).
**Composer**: auto-growing textarea (Enter = send on desktop, Shift+Enter = newline, touch rule in §9.8, IME-safe), emoji picker (categories + search*, built-in data per §9.8, recents in namespaced `localStorage`), attach button (any file; images show preview + caption; pipeline, paste, drag-and-drop, progress with cancel and
camera capture in §9.9), reply/edit preview bar with ✕, `@` mention autocomplete in groups (↑↓ Enter Esc; rules in §9.10),
voice note* (only when `navigator.mediaDevices` + `MediaRecorder` exist: hold/click to record, shows timer, emits `typing {state:'recording'}`), draft persistence per chat; the composer stays enabled while offline and is disabled only in the cases of §9.7.
**Typing/seen UI**: header + list preview typing text (§8.3); ticks as §8.1; unread dividers; "Seen by" sheet for groups via Info.
**Drawer / info**: contact info (avatar, name, @username, about, online status, "Message", mute, shared media/files/links tabs, starred); group info (title/description edit for admins, members list with admin badges,
add members, make/remove admin, remove, leave, mute, only-admins-post switch, pinned messages).
**Search**: sidebar global search (people + chats + messages), in-chat search bar with up/down navigation and highlight.
**Starred messages** view. **Settings** modal: profile (display name, about), privacy (read receipts — with the retroactive-reveal note of §8.1 —, last seen), notifications (sound on/off + volume + test sound, desktop-notification status/request button, show previews; §9.4),
appearance (theme, font size*), change password, sessions* (`GET /api/sessions`, revoke), logout (§9.7). **Admin** (admins only): users table (search; enable/disable; make admin; reset password; create user; "Add employees", §9.10), workspace name, registration toggle + join code (§4.3),
stats card (incl. disk free, last backup, LAN URLs).

### 9.3 Rich text (`lib/richtext.js`)
WhatsApp-style, produced as DOM nodes, never HTML strings: `*bold*`, `_italic_`, `~strike~`, `` `mono` ``, ```` ```block``` ````, `> quote` lines, `- ` / `1. ` lists*, auto-linked URLs (http/https, trailing punctuation
trimmed), `@mention` chips for tokens that resolve to a known current username (grammar §7.4; highlight if it's me), search-term highlighting, emoji-only messages rendered large. Linkify must be ReDoS-safe (linear time) and safe on 8000-char input.

### 9.4 Notifications — support matrix, mitigations, sound
**Support matrix on plain `http://<lan-ip>:8765` (an insecure context) — the UI and README must say this plainly:**
| Capability | Plain HTTP | Needs |
|---|---|---|
| In-page toast, tab-title and favicon badge, chime (after one user gesture) | Works on desktop while the tab exists and is not discarded; on phones only while the page is in the foreground | — |
| OS notification via the `Notification` API | **Not available in any browser** (Android Chrome never supports `new Notification()`; iOS tabs have no Notification API) | a secure context: `http://localhost` on the server PC itself, `https` with a trusted/accepted certificate (`--tls`), or an origin allow-listed by enterprise policy (below) |
| Alert while the browser is closed, the phone is locked or the app is in the background | **Impossible** (needs a push service or a native app) | documented limitation, §13 |
**Mitigations that MUST ship:**
1. README and the Admin welcome card document the Chrome/Edge policy `OverrideSecurityRestrictionsOnInsecureOrigin` (registry/GPO value `http://<server-ip>:8765`, browser restart), which makes the origin a secure context and unlocks `Notification`, clipboard, `crypto.randomUUID` and `mediaDevices` on every office PC.
2. `new Notification()` is wrapped in try/catch and never called when `!window.isSecureContext`.
3. Settings → Notifications shows a live status line from feature detection: "Desktop notifications: enabled" / "blocked in browser settings" / "not available on this connection (http) [Why?]"; on phones add "Alerts only work while FreeChat is open on screen."
4. On resume from background (`visibilitychange` → visible) show ONE summary toast "N new messages in M chats" (and `navigator.vibrate(200)` where defined; Android only).
5. README: pin the tab; Chrome "Always keep these sites active", Edge "Never put these sites to sleep" (Memory Saver / Sleeping Tabs can freeze a hidden tab).
**Acceptance test:** a desktop tab that is open but not focused (visible or hidden, not discarded) yields chime + title badge + favicon badge within 1 s of the message; an OS notification only on a secure-treated origin.

**Behaviour**
* **Badges.** Per-chat unread badge (`me.unread`, "999+" cap; "@" when `unread_mentions>0`, click jumps to `first_unread_mention_id`). Total `N` = Σ `unread` over chats that are not archived and not muted + Σ `unread_mentions` over muted, non-archived chats; shown capped at "99+".
  `document.title = '(N) <workspace name>'` when N > 0 else `'<workspace name>'`; the favicon is canvas-drawn (red dot/number; replace the `<link rel=icon>` href with a `data:` URL — allowed by CSP `img-src data:`).
* **Event handling is synchronous** inside the socket handler (§9.6 rule 8); the chime, toast, title and favicon never wait for `requestAnimationFrame`.
* **Multiple tabs of one user** elect a leader through `BroadcastChannel("freechat")` (feature-detected; without it every tab acts): only the most recently focused tab plays the chime and shows OS notifications; every tab maintains its own title/favicon. After `ev.read_sync` from another device, that device closes its notification for that chat (`Notification.close()` by `tag`).
* **In-app toast** for a message in another chat while the window is focused (click → open that chat).
* **Desktop notification** via `new Notification(...)` when `window.isSecureContext && Notification.permission==='granted'` and (tab hidden or window unfocused or message is for another chat); title = sender (+ group), body = preview (or "New message" when previews are off), `tag` = chat id (collapses), click → `window.focus()` + set `location.hash` to the chat.
  Never prompt for permission without a user click. Notifications fire on `ev.message` only (never on `ev.message_update`).
* Title blink ("💬 New message") while the tab is hidden and unread > 0.
* **Sound engine.** Create the `AudioContext` lazily; unlock it on the first `pointerdown`/`keydown`/`touchend` (`resume()` + play a 1-sample silent buffer). If it is `suspended` when a message arrives, show a persistent sidebar icon "Sound is off - click anywhere to enable" and do not queue sounds.
  On iOS (UA, or `maxTouchPoints>1` with a Mac platform) use an `<audio>` element fed by a WAV Blob generated in JS (`URL.createObjectURL`, allowed by `media-src blob:`) instead of Web Audio (Web Audio obeys the silent switch). Short two-note chime generated with WebAudio (no audio files); a softer "sent" tick*.
  Throttle: at most 1 chime per 2 s globally and 1 per chat per 5 s (mentions bypass the per-chat limit); never for own messages from another device; never for the chat that is open and focused; muted chats are silent except mentions; global sound off respected. Settings has "Play test sound", volume and the note "The phone silent switch may mute FreeChat".

### 9.5 Offline / resource rules
No network requests to any host other than same-origin (the WebSocket goes to the same host). `index.html` has `<meta name="color-scheme">`, `theme-color`, a data-URL favicon, `<link rel="manifest" href="/manifest.webmanifest">`, `<link rel="apple-touch-icon" href="/img/icon-180.png">` and the metas
`apple-mobile-web-app-capable`, `mobile-web-app-capable`, `apple-mobile-web-app-title` ("Add to Home Screen" gives a full-screen app without URL bar on iOS and Android even over plain HTTP; no service worker; the 180/192/512 px PNGs are generated with stdlib `zlib`). Total JS+CSS payload target ≤ 600 KB uncompressed.
All assets are served with correct MIME so `type="module"` works. Provide a `noscript` message.

### 9.6 Message list model & scroll rules
**Window.** Per chat the client holds ONE contiguous window `{lo, hi, has_more_before, has_more_after, items}` (invariant of §8.5).
* `ev.message`: if `has_more_after` is true the message is **not** inserted (only the preview, `last_message_id`, counters, FAB and notifications are updated); otherwise it is appended.
* **Opening a chat.** If `me.unread == 0`: fetch the newest page and scroll to the bottom. Otherwise let `L = chat.me.last_read_id` captured at open: if `me.unread ≤ 50` fetch the newest page (it reaches back to `L`), else fetch `chat.history {after_id: L, limit: 50}` (oldest unread first, `has_more_after:true`;
  scrolling down keeps paging with `after_id`). The **"N unread messages" divider** is placed before the first message with `id > L`, `sender ≠ me`, not system and not deleted, and the view scrolls so the divider sits ~25 % from the top. Its position is computed once at open and **never moves** when receipts or
  `ev.read_sync` arrive; it disappears when leaving the chat. If the tab is blurred/hidden while the chat is open and new messages arrive, a new divider is inserted before the first message that arrived after the blur (when none exists). The label counts non-own, non-system messages at/after the divider.
* **"Scroll to bottom" FAB:** with `has_more_after` the window is discarded and the newest page loaded, else smooth-scroll; its badge is `me.unread`.
* **Jump** (reply quote, pinned banner, search hit, starred item, the "@" button → `first_unread_mention_id`): `chat.history {around_id}` replaces the window, scrolls the target to the centre and flashes it for 1.5 s; on `not_found` toast "Original message is no longer available".
* **Window cap** 400 items: trim from the end opposite to the scroll direction and set the matching `has_more_*`. `receipt.read` gating: §8.2.
**Scroll rules**
1. `stickToBottom = (scrollHeight - scrollTop - clientHeight) <= 40`, recomputed only on user-initiated scroll events.
2. Prepending an older page: record `prevHeight`/`prevTop`, insert, then set `scrollTop = prevTop + (scrollHeight - prevHeight)` synchronously before paint; trigger loading at `scrollTop < 300 px`; ignore scroll events while compensating. Do not rely on CSS `overflow-anchor`.
3. Appending: if `stickToBottom` or the message is mine → scroll to bottom after layout; otherwise keep the position and bump the FAB counter.
4. One `ResizeObserver` on the list content: if `stickToBottom` was true before the resize re-pin to the bottom; else if the resized element is above the viewport anchor add the height delta to `scrollTop`.
5. Images/videos: before the bitmap loads set `style.aspectRatio` and width from `attachment.width/height` (clamped to max 320×360 CSS px); if null use a fixed 240×180 placeholder; `decoding="async"`; `loading="lazy"` except for the newest 20 messages.
6. Messages > 1200 chars or > 20 lines render collapsed with "Read more" (expands in place, rule 4 compensation applies).
7. Keyboard open/close (`visualViewport` resize) while stuck to the bottom re-pins.
8. Everything that updates the store, counters, `document.title`, the favicon, the chime and notifications runs **synchronously inside the socket message handler**; only DOM patching of the visible list may be deferred to `requestAnimationFrame`, and when the tab becomes visible a full reconcile render from the store runs.
**Search client rules.** Debounce 400 ms after the last keystroke; send only when `q.trim().length ≥ 2`; keep a sequence number and ignore stale responses; on `rate_limited` wait `retry_after` silently and keep the previous results. In-chat search shows "3 of 27" from `total` ("1000+" when capped), loads pages lazily (50) and navigates with
`around_id`; clicking a global result routes to `#/c/<chat>/m/<message>` (§9.8).
**Acceptance tests (manual checklist + automated where possible):** 120 messages arrive while offline, then reconnect → no holes when scrolling up; delete-for-everyone while a recipient is offline → the placeholder is shown after reconnect without reload; scroll up 5 pages with images → no visible jump; receive an image while at the bottom → the bottom stays pinned after it loads.

### 9.7 Outbox, drafts and local storage
* The composer stays **enabled** while disconnected: text goes to the outbox (clock icon) and a banner reads "Offline - messages will send when reconnected"; attach buttons are disabled with the tooltip "Reconnect to send files". The composer is **disabled with an explanation line only** when: `only_admins_post` and I am not a group admin; the direct peer is disabled ("<name>'s account is disabled"); I am no longer a member.
* Outbox item states: `queued` (clock) → `sending` → `sent` (replaced by the server message via `client_id`) | `failed`. FIFO per chat with **at most one `msg.send` in flight per chat**. Retry classification: §7.1.1. A failed bubble's menu offers Retry, Copy text, Discard. Attachment items are never persisted (File objects cannot be stored): after a reload they are dropped with a toast "N unsent attachments were discarded".
* **Storage keys** are namespaced `fc:v1:<instance_id>:<user_id>:outbox`, `…:draft:<chat_id>`, `…:emoji_recent` (`instance_id` from `ev.ready`; it changes when the DB is recreated). Every access is in try/catch with an in-memory fallback; keys of another user id are never read.
  **Wiped** (that user's keys): on explicit logout — after the confirm "N unsent messages will be lost" when the outbox is non-empty; Settings → Logout offers "also clear this device", default on —, on `ev.kicked` and on a 401 from the API. On connect, drop outbox items whose `chat_id` is not in `ev.ready.chats` or whose owner id ≠ `me.id`, and verify `me.id` equals the item's owner id before re-sending.
  Tokens are never stored; `document.cookie` is never touched.
* Drafts: text plus reply/edit target per chat, cleared on send; a chat that is not open shows "Draft: <text>" (red label) in the list instead of the last message.
* Optimistic bubbles are stamped with `Date.now() + skew` (§8.5).

### 9.8 Routing, mobile rules and accessibility
**Routing** (`core/router.js`, hash based): `#/` (list), `#/c/<chatId>`, `#/c/<chatId>/m/<messageId>` (jump), `#/starred`, `#/settings[/<tab>]`, `#/admin`. On widths < 900 px opening a chat/drawer/lightbox/settings does `history.pushState` (so the hardware Back pops it); on ≥ 900 px opening a chat uses `replaceState`.
Back order: lightbox > emoji picker/menu > drawer > in-chat search > chat > list; Back never exits the app from a sub-view. A cold load restores the route (F5 keeps the open chat); an unknown or forbidden chat id → list + toast; `history.scrollRestoration = 'manual'`; toast and notification clicks set `location.hash`;
auth state is never part of the hash. On `ev.chat_removed` for the open chat navigate to `#/` and toast "You were removed from <title>". Leave / remove member show confirm dialogs.
**Mobile rules.**
* `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">` (never `maximum-scale`/`user-scalable=no`). App root: `height:100vh; height:100dvh;` plus JS sets `--app-h = visualViewport.height` on `visualViewport` resize/scroll (iOS keeps the layout viewport when the keyboard opens); the root is `position:fixed`, a flex column; the composer is never fixed separately.
* Inputs/textarea `font-size ≥ 16px` on coarse pointers (iOS zooms otherwise); tap targets ≥ 44×44 px. `html,body{overscroll-behavior:none}` and the list `overscroll-behavior:contain` (Chrome Android pull-to-refresh would reload the page).
* On `(pointer:coarse)` devices Enter inserts a newline and the Send button sends (Settings: "Enter sends message", default on for desktop, off for touch).
* Long-press: a pointer-event timer of 450 ms cancelled by > 8 px movement (Safari iOS does not reliably fire `contextmenu`); bubble chrome gets `-webkit-touch-callout:none; user-select:none` and text is copied via the menu "Copy"; on touch the message menu is a bottom sheet and chat rows/messages always show a visible "…" button (no hover dependency).
* **Supported browsers:** Chrome/Edge ≥ 100, Firefox ≥ 100, Safari/iOS ≥ 15.4. `index.html` contains `<script nomodule src="/js/unsupported.js">` and `main.js` starts with a feature gate (WebSocket, fetch, `CSS.supports('height','1dvh')`, Unicode property escapes) that renders "Please update your browser" instead of a blank page.
**Accessibility.** The message list is `role="log" aria-live="polite" aria-relevant="additions" aria-label="Messages in <chat>"`; the typing line is `aria-live="polite"`; badges `aria-label="3 unread messages"`; ticks `role="img" aria-label="Sent|Delivered|Read"`; a failed message `aria-label="Failed to send, press Enter to retry"`; every icon-only button has an `aria-label`.
Dialogs/drawers/lightbox: `role="dialog" aria-modal="true"`, labelled, focus moves in on open, is trapped, and returns to the opener on close; Escape closes the topmost layer (lightbox > menu/emoji picker > reply/edit bar > drawer > search bar). Messages are focusable through a roving tabindex; Enter / Menu / Shift+F10 opens the message menu.
Shortcuts: Ctrl+K focus sidebar search, Alt+Up/Down previous/next chat, Up in an empty composer edits my last message (within the window). Sender/avatar colours come from a fixed 12-colour palette with ≥ 4.5:1 contrast on both bubble backgrounds in light and dark. `icons.js` builds SVG with `document.createElementNS` from path-data strings only.
**Text and emoji rendering.** `dir="auto"` (with `unicode-bidi:plaintext; text-align:start`) on every message body, the composer textarea, chat-list previews and reply quotes (Urdu/Arabic/Hebrew and mixed text). Font stack `system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif` (never a custom family that bypasses OS emoji/Indic fallbacks). Emoji picker data: only emoji up to Unicode Emoji 12.1 and **no Flags category**
(Windows draws flags as two letters); at picker build time each emoji is checked with a canvas test (its drawn width must differ from that of an unassigned code point) and the result is cached in `localStorage` keyed by `navigator.userAgent`.

### 9.9 Attachment pipeline
1. Pick / drop / paste / camera → an attachment tray (thumbnail, name, size, remove ✕); the composer text becomes the caption. ≤ 10 files per send (folders are rejected). N files ⇒ N messages in selection order; the caption goes on the first only (one attachment per message, §3).
2. Client pre-checks `0 < size ≤ limits.max_upload_bytes`.
3. Each file gets its own optimistic bubble: `uploading` (progress ring + cancel) → `sending` → `sent` | `failed` (Retry / Discard). `msg.send` is issued only after the upload succeeded and reuses the same `client_id` on retry. At most 2 parallel uploads on the client (server caps in §5.1; a 429 is retried after `Retry-After`).
4. Errors: `xhr.status 0`/timeout ⇒ "Upload failed - tap to retry" (restart from 0; no resume in v1); `413 too_large` ⇒ "File is larger than the limit (X MB)"; `413 quota_exceeded` ⇒ "Upload quota reached - try again later"; `507` ⇒ "Server storage is full - contact your admin"; `400 blocked_type` ⇒ "This file type is not allowed"; `401` ⇒ login. (Browsers may show an early `413` as status 0 — the server drains and closes cleanly, §5.1.)
5. Images (not GIF) larger than 1 MB or 1600 px are downscaled client-side with `canvas.toBlob` (JPEG q0.85, longest side 1600) unless the user chooses "Send as file"; the client sends `X-Meta` width/height of the result (there is no server thumbnailer).
6. If `<video>`/`<audio>` fires `error` (e.g. an unsupported codec) fall back to the file card with Download. Attach menu "Camera" = `<input type=file accept="image/*" capture="environment">`, "Video" = `accept="video/*" capture` (file inputs work in insecure contexts, `getUserMedia` does not). Pasting images (`paste` event `clipboardData.files`) and drag-and-drop also work on insecure origins — do not wait for `navigator.clipboard.read`.
7. The composer shows a character counter at 90 % of `max_body_chars` and blocks sending beyond it with "Message too long (N/8000)". PDFs are download-only (the button says "Download").

### 9.10 Copy deck, flows and implementation order
* **Empty states / strings:** desktop with no chat open: workspace name, "Select a chat to start messaging" and status chips "Sound: on/off", "Desktop notifications: <status>". Empty chat: "No messages yet. Say hello!". Search/new chat without match: `No chats or people match "<q>"`. Starred empty: "Long-press or right-click a message and choose Star to keep it here".
* **Chat-list preview text:** image "Photo", video "Video", audio "Audio (m:ss)", file `<filename>`, deleted "This message was deleted" (own: "You deleted this message"), forwarded prefix "Forwarded:", group prefix "`<Name>`: ", own prefix "You: ", and "Draft: `<text>`" wins over the last message for a chat that is not open.
* **Times:** today = `Intl.DateTimeFormat(locale,{hour:'2-digit',minute:'2-digit'})`, yesterday = "Yesterday", < 7 days = weekday name, else the date; a bubble's time has a `title` tooltip with the full date-time. Reactions: a chip click toggles my own reaction (§7.4 SET semantics); the chip tooltip (desktop) and a "Reactions" section of the message Info sheet (touch) list the names per emoji.
* **Forward picker:** a searchable list of chats AND people (a person without a DM → `chat.open_direct` first), chips for the selected targets, a 6th target is disabled with a hint, success toast "Forwarded to N chats" (tap opens it when N = 1). Forward is hidden for system, deleted and pending messages.
* **Mentions:** the autocomplete opens on a valid `@` boundary (token grammar §7.4: start of text or preceded by whitespace or `(`), matches display-name word prefix or username prefix, lists "Display Name  @username", inserts exactly `@username ` (with a trailing space), is anchored above the composer (not at the caret), Enter/Tab select while it is open, tap on touch. Chips render the directory display name; unknown usernames stay plain text.
* **Onboarding:** Admin → "Add employees": paste lines "Full Name, username" → loops `admin.create_user` with generated 10-character temporary passwords (CSPRNG, no ambiguous characters) → a result table shown once with Copy and Print buttons (those accounts have `must_change_password`). The Admin first-run card shows `admin.stats.urls` with "Copy invitation text" (URL + a line that the browser's "Not secure" label is expected on the office network). The server PC should use `http://localhost:8765` itself (a secure context). Login has "Forgot password? Ask your admin to reset it."
* **Implementation order** (NOT scope cuts — everything stays in v2; this is the build/QA priority). **P0** (solid before anything else): auth + admin basics; direct chats + Everyone + groups (create, add/remove, leave, rename); `msg.send` with outbox/idempotency; history paging/`around_id`/`after_id`; ticks + typing + presence; unread + `read_sync`; reconnect/`ev.ready` consistency;
  file/image upload/download/lightbox; reply, edit, delete-for-everyone, reactions; simple forward; basic search; in-page toast, chime, title/favicon badge; installer. **P1** (after P0 tests are green): star, delete-for-me, message pins + banner, chat pin/mute durations, info drawer + shared media/files/links, `msg.info`, mention autocomplete, privacy toggles, admin stats/"Add employees", group admin roles UI, Add-to-Home-Screen assets, camera capture.
  **P2:** voice notes and the `recording` state, archive, clear chat, multi-select, sessions list, font-size setting, emoji search/recents, title blink, `--tls` helpers, lists/quote syntax in rich text. P0 must pass the §12 suite before P1 work merges.

---------------------------------------------------------------------------------------------------

## 10. Service installer (`service/install_service.py`)

Single file, stdlib only, **Python ≥ 3.8**; the installer itself never imports `sqlite3`. Usage:
```
python service/install_service.py install   [--port N] [--host H] [--data-dir D] [--python PATH] [--name NAME] [--user USER]
                                            [--tls | --no-tls] [--redirect-port P] [--allowed-host H]... [--allow-sleep]
                                            [--no-firewall] [--allow-from localsubnet|CIDR[,CIDR]|any] [--firewall-profile private,domain|any]
                                            [--run-as-system] [--harden] [--admin USER [--password-stdin]] [--force]
                                            [--dry-run] [--target windows|linux|macos] [--elevate]
python service/install_service.py uninstall [--keep-firewall] [--dry-run] [--target windows|linux|macos]
python service/install_service.py start|stop|restart|status
python service/install_service.py logs [-n 100]
python service/install_service.py print-config [--target windows|linux|macos]    # show the generated unit/XML/plist
python service/install_service.py cli -- <chatd args>     # run `python -m chatd <args>` as the service identity with the installed options
```
`--target` is valid only together with `--dry-run`/`print-config` (used by tests). `--user` exists on Linux/macOS only (an error on Windows).

### 10.1 Common behaviour
* **Fixed names:** Windows task `FreeChat`, systemd unit `freechat`, launchd label `com.freechat.server`, firewall rule `FreeChat`. `--name` is the **workspace name** and is passed through as `serve --name NAME`.
* **Interpreter:** `--python` > the interpreter running the installer, **validated** with two subprocesses: `<py> -c "import sys,sqlite3,ssl,hashlib,asyncio;assert sys.version_info>=(3,8);assert sqlite3.sqlite_version_info>=(3,24);hashlib.pbkdf2_hmac"` and `<py> -c "import chatd"` with cwd = app root.
  If invalid, search and pick the newest valid one (machine-wide before per-user), else abort with a clear explanation. Store `os.path.realpath()`.
  *Windows discovery order:* (1) `--python`; (2) the PEP 514 registry via `winreg`: `HKLM\SOFTWARE\Python\PythonCore\*\InstallPath`, then `HKLM\SOFTWARE\WOW6432Node\…`, then `HKCU\SOFTWARE\Python\PythonCore\*\InstallPath` (read `ExecutablePath`, else `<default>\python.exe`); (3) `py -0p` only if `py` exists (catch `FileNotFoundError`); (4) `shutil.which('python')`.
  Reject any path containing `\WindowsApps\` (0-byte Store alias stubs), any 0-byte exe and any `pythonw.exe`; if a `pyvenv.cfg` sits next to or above the exe, resolve to its `home =` interpreter (venv launchers are redirector stubs that break PID tracking). *POSIX:* `python3.13 … python3.8`, `python3`, `/usr/bin/python3`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` (macOS rules in §10.4).
* **Command run by the service:** `<python> -X utf8 -I <app>/server.py serve --host H --port N --data-dir D --name NAME [--tls] [--redirect-port P] [--allowed-host H]… [--allow-sleep]`, working directory = app root, absolute paths everywhere. `-I` (isolated mode: no cwd, no `PYTHON*` environment, no user site on `sys.path`) is why `server.py` inserts its own directory (§1).
* **Paths and quoting:** every path goes through `os.path.abspath(os.path.normpath(p))` with trailing separators stripped except for drive roots. Windows `Arguments` are built with `subprocess.list2cmdline([...])` (it doubles trailing backslashes; a hand-written `--data-dir "C:\x\"` would swallow the following flags) and the Task XML is generated only with `xml.etree.ElementTree`/`xml.sax.saxutils.escape` (a literal `&` in a path breaks f-string XML).
  Linux: every `ExecStart`/`ReadWritePaths` token is double-quoted with `\\` and `"` escaped, `%` written `%%`, `$` written `$$`; paths containing a newline are rejected. launchd uses the `ProgramArguments` array (nothing to escape). Tests use a data dir named `A & B\Free Chat 100%\` for all three targets (`--dry-run --target`).
* **Data dir:** when `--data-dir` is absent use `<app>/data` only if `<app>/data/chat.db` already exists, otherwise `%ProgramData%\FreeChat\data` / `/var/lib/freechat` / `/Library/Application Support/FreeChat/data`. The installer creates it with restrictive permissions (Windows ACL §10.2; POSIX `install -d -o <user> -g <group> -m 0700`) before the first start and warns when it lies inside the app root.
* The installer never edits anything outside: the service definition, the firewall rule, the data dir (created if missing), a log dir and the state file. `--dry-run` prints every command/file instead of executing and must work **on any OS for any target OS** via `--target`.
* **State file** (`%ProgramData%\FreeChat\install.json` / `/etc/freechat/install.json` / `/Library/Application Support/FreeChat/install.json`): `{python, app_root, data_dir, host, port, tls, redirect_port, user, name, allowed_hosts, allow_sleep, firewall:{port, scope, profile}}`.
  `install` defaults every option that is not given from it and prints a diff of what changes; `uninstall` removes exactly the firewall rule recorded there; `status` reports "interpreter missing: <path>" when the saved python no longer exists.
* **Installer exit codes:** `0` ok, `1` error, `2` usage/privilege/refused check, `3` health check failed.
* **Health probe** (one helper used by `install`, `status` and `doctor`): host = `127.0.0.1` when `--host` is `0.0.0.0`, `::`, `localhost` or `127.*`, otherwise the `--host` value; scheme `https` when TLS is on, using `ssl.create_default_context()` with `check_hostname=False`, `verify_mode=CERT_NONE` (loopback health only); path `/healthz`.
  After install: start, wait ≤ 15 s polling it, then print the LAN URLs (https when TLS), the **setup code** while `needs_setup` (read from `<data>/setup_code.txt`) and "next step: open `http://127.0.0.1:<port>/` and create the admin account". If health never comes up print the last 30 lines of `freechat.log` and `boot.log` and exit 3.
* **First admin:** before the first start the installer asks "Create the admin account now? [Y/n]" (skipped when non-interactive; scripts pass `--admin USER` + `--password-stdin`) and runs `<py> -m chatd create-admin USER --password-stdin --data-dir <D>` (as the service user on POSIX) — `needs_setup` is then already false and nobody can claim the account.
* **TLS and exposure:** without `--tls`/`--no-tls` the installer prints a WARNING "Plain HTTP: anyone on this network can read passwords and messages (SPEC §0) — re-run with --tls" (TLS is recommended, plain HTTP stays the default so rollout needs no certificate step). With `--tls` the certificate is generated at install time (§5.7).
* **Idempotent:** re-running `install` updates the definition and restarts (on Windows the stop sequence of §10.2 runs first). `uninstall` stops and removes the service, the firewall rule (unless `--keep-firewall`) and the state file; **data is never deleted**; it prints the leftovers (data dir, logs, TLS key, the `freechat` user, Python) with copy-paste removal commands (`rmdir /s`, `rm -rf`, `userdel freechat`).
* **Upgrade** (printed by `install`, README): (1) `install_service.py stop`; (2) replace only `chatd/ web/ service/ docs/ server.py`, never `data/`; (3) `install_service.py install` (reuses the saved options, re-validates the interpreter, restarts); (4) `python -m chatd doctor`.
* **Privilege:** detect root/Administrator (`IsUserAnAdmin()` = elevated token). `status`, `logs`, `print-config` and `--dry-run` never need elevation (`status` falls back to `/healthz` when `schtasks` is denied). When privilege is missing print the exact steps — Windows: "Start menu → type PowerShell → right-click → Run as administrator, then paste: `cd "<app>"; & "<python>" service\install_service.py install`";
  Linux/macOS: `sudo "<sys.executable>" "<abs script>" install …` (sudo's `secure_path` can drop the user's PATH). Windows `--elevate`: `ShellExecuteExW` with `SHELLEXECUTEINFOW`, `fMask=SEE_MASK_NOCLOSEPROCESS|SEE_MASK_NOASYNC`, `lpVerb='runas'`, `lpFile=<python.exe>` (map `pythonw` to `python`), `lpParameters=subprocess.list2cmdline([abs_script, *argv_without_--elevate, '--elevated-child'])`,
  `lpDirectory=os.getcwd()`, `nShow=SW_SHOWNORMAL`; the parent calls `WaitForSingleObject`, reads `GetExitCodeProcess` and treats `GetLastError()==1223` as "UAC prompt declined" (exit 2). The elevated child tees its output to `%TEMP%\freechat-install-<pid>.log` and ends with `input('Press Enter to close')`; the parent prints that log's tail.
* **`cli --` passthrough:** reads the state file and runs `<python> -m chatd <args> --data-dir <D>` with cwd = app root as the service identity (Linux `runuser -u <user> --`, macOS `sudo -u <user>`, Windows: needs elevation, otherwise prints the PowerShell steps). The post-install message prints the exact `cli -- create-admin <user>` command.
* **`doctor` port check:** if binding fails, request `/healthz` (https if TLS) and `/api/info`: a FreeChat answer ⇒ PASS "running (version X, pid from server.lock)"; otherwise FAIL naming the owner via `netstat -ano -p tcp` (Windows), `ss -ltnp` (Linux) or `lsof -nP -iTCP:<port> -sTCP:LISTEN` (macOS). The probe socket mimics the server: no `SO_REUSEADDR` on Windows (asyncio sets it only on POSIX; on Windows it would allow port hijacking and report "free"), `SO_REUSEADDR` on POSIX.
* **Advisories** printed after install (the installer changes none of these settings): (1) keep the PC awake — `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0` are *shown, never run*; `serve` holds a keep-awake request (§2.2) but lid-close/manual sleep still stops the chat; (2) give the PC a static IP or a DHCP reservation, otherwise `http://<ip>:8765` changes;
  (3) on the server PC itself open `http://localhost:8765` (a secure context: notifications, clipboard); (4) tell staff the browser shows "Not secure" because there is no internet certificate — expected without `--tls`. `doctor` WARNs on Windows when the AC standby timeout is not 0 (parse `powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE`) and prints all LAN IPs with a note when several exist (VPN/virtual adapters are not reachable by staff).

### 10.2 Windows (no pywin32, no third-party tools)
* **Principal:** default `NT AUTHORITY\LOCAL SERVICE` (`<UserId>S-1-5-19</UserId>`, `RunLevel=LeastPrivilege`, no password); `--run-as-system` selects `S-1-5-18` with `RunLevel=HighestAvailable`. The interpreter must be machine-wide: a Python under `\Users\` (a per-user install) is refused for either principal (exit 2, explanation "install Python for all users").
* **ACL safety check (before the task is created):** for the app root, the interpreter's install directory and the data dir run `icacls <path> /save <tmp>` (SDDL, locale-independent) and inspect every allow ACE: if the trustee is `S-1-1-0` (Everyone), `S-1-5-11` (Authenticated Users), `S-1-5-32-545` (Users) or `S-1-5-4` (Interactive) and the access mask contains any of
  `FILE_WRITE_DATA/ADD_FILE 0x2`, `FILE_APPEND_DATA/ADD_SUBDIRECTORY 0x4`, `DELETE 0x10000`, `WRITE_DAC 0x40000`, `WRITE_OWNER 0x80000`, `GENERIC_WRITE 0x40000000`, `GENERIC_ALL 0x10000000` (or the symbolic equivalents), the installer refuses (exit 2), prints the hardening command and, only with `--harden`, applies it to every path that failed (the command is shown for `<app>`; the same form is used for the interpreter directory and, with `"*S-1-5-32-545:(OI)(CI)RX"`, never grants Users more than read/execute):
  `icacls "<app>" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-19:(OI)(CI)RX" "*S-1-5-32-545:(OI)(CI)RX"`. (Any local user could otherwise edit `chatd\*.py` or drop a shadowing module and get service-level code execution at the next boot; a default `E:\` grants Authenticated Users modify, so a dev checkout there fails until `--harden`.)
* **Data dir ACL:** `icacls "<data>" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-19:(OI)(CI)M"` (the chosen service account instead of `S-1-5-19` when `--run-as-system`; new files inherit it). `serve` applies the same ACL when it creates a missing data dir on Windows; `doctor` parses it and FAILS if Users/Everyone/Authenticated Users can read the data dir.
* **Task Scheduler task `FreeChat`** from an XML definition — generated with `ElementTree` (never f-strings) and written as bytes `codecs.BOM_UTF16_LE + ('<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding='unicode')).encode('utf-16-le')` (a UTF-8 file with that declaration is rejected). Tests parse it with `ET.fromstring(bytes)` and the namespace `{http://schemas.microsoft.com/windows/2004/02/mit/task}`.
```
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>FreeChat LAN chat server</Description></RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled><Delay>PT20S</Delay></BootTrigger>
    <TimeTrigger><StartBoundary>2020-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-19</UserId><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled><Hidden>false</Hidden><WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>4</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>C:\Python313\python.exe</Command>
    <Arguments>-X utf8 -I E:\pravin\applications\free-chat\server.py serve --host 0.0.0.0 --port 8765 --data-dir C:\ProgramData\FreeChat\data --name FreeChat</Arguments>
    <WorkingDirectory>E:\pravin\applications\free-chat</WorkingDirectory>
  </Exec></Actions>
</Task>
```
  `ExecutionTimeLimit=PT0S` is mandatory (the default is PT72H); `Priority=4` (omitting it gives 7 = below-normal CPU and low I/O priority, wrong for a DB server); `Hidden=false` (admins must find the task); the **`TimeTrigger` is a watchdog**: Task Scheduler's `RestartOnFailure` does not reliably fire for the exit code of a started process and its minimum interval is `PT1M`,
  so every 5 minutes the task is (re)started if it is not running (`IgnoreNew`), and a second instance that does start exits 73 on the instance lock. `S-1-5-18`/`HighestAvailable` only with `--run-as-system`. Register with `schtasks /Create /TN FreeChat /XML <file> /F` (no `/RU /RP`). The output states that the task starts at **boot, before anyone logs in**.
* **Start / stop sequence:** `schtasks /End` is a `TerminateProcess` (no signal reaches Python, so §2.4's handlers never run). Therefore `stop` = `schtasks /Change /TN FreeChat /DISABLE` (so the watchdog cannot revive it), write `<data>/control/stop.request`, wait ≤ 10 s until the port refuses connections, then `schtasks /End /TN FreeChat`. `start` = `/ENABLE` then `/Run`.
  `restart` = stop, wait until the port is closed, start. `install` over an existing task runs the stop sequence first. README states that a power cut or hard stop is recovered by SQLite WAL at the next open (no loss beyond the last uncommitted transaction). `status` reports `/healthz` as the ground truth plus a best-effort `schtasks /Query /TN FreeChat /FO CSV /NH /V` parsed by column index (output is localised).
* **Firewall:** fixed rule name `FreeChat` (no port in the name): `netsh advfirewall firewall delete rule name="FreeChat"` then `add rule name="FreeChat" dir=in action=allow protocol=TCP localport=<port>[,<redirect-port>] profile=<--firewall-profile> remoteip=<--allow-from>`; defaults `--firewall-profile private,domain` and `--allow-from localsubnet` (use `any`/CIDR lists for multi-VLAN offices; never open Public networks silently).
  Check elevation first (`netsh … delete rule` returns 1 both when the rule is absent and when not elevated; treat 1 as success only when elevated). `doctor` (works unelevated) reads the rule with `netsh advfirewall firewall show rule name="FreeChat"` by **exit code** (1 = absent) and `netsh advfirewall show allprofiles state` — never by English text — and warns when the active network category is Public
  (`Set-NetConnectionProfile -NetworkCategory Private`, or reinstall with `--firewall-profile any`).
* Logs: `<data>/logs/freechat.log` and `boot.log`; `logs` prints the tail of `freechat.log` followed by `boot.log`.

### 10.3 Linux
`/etc/systemd/system/freechat.service`:
```
[Unit]
Description=FreeChat LAN chat server
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=<user>
Group=<group>
WorkingDirectory=<app>
ExecStart="<python>" "-X" "utf8" "-I" "<app>/server.py" "serve" "--host" "0.0.0.0" "--port" "8765" "--data-dir" "<data>" "--name" "FreeChat"
Restart=always
RestartSec=3
RestartPreventExitStatus=78
TimeoutStopSec=20
LimitNOFILE=8192
UMask=0077
Environment=PYTHONUNBUFFERED=1 PYTHONUTF8=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
LockPersonality=true
CapabilityBoundingSet=
ReadWritePaths=<data>
[Install]
WantedBy=multi-user.target
```
* Unless `--allow-sleep`, and when `systemd-inhibit` exists, `ExecStart` is prefixed with the tokens `"systemd-inhibit" "--what=sleep:idle" "--who=FreeChat" "--why=chat server"`. For ports < 1024 add `AmbientCapabilities=CAP_NET_BIND_SERVICE` and `CapabilityBoundingSet=CAP_NET_BIND_SERVICE`. `ProtectHome=read-only` is always safe because `ReadWritePaths=` re-opens `<data>` even under `/home`;
  `ProtectSystem=full` only protects `/usr /boot /etc` (the `ReadWritePaths` line documents intent; the path must exist or the unit fails with 226/NAMESPACE, hence the installer creates it first). `RestartPreventExitStatus=78` stops the 3-second restart loop on a permanent configuration error (sqlite3 missing).
* **User:** `--user` > `SUDO_USER` when not root > a system user `freechat` created with `getent passwd freechat || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell <shutil.which('nologin') or /usr/sbin/nologin> freechat`. Never root (`--user root` is rejected). Before starting: `install -d -o <user> -g <group> -m 0700 <data>`, `chown -R <user>:<group> <data>`, then verify
  `runuser -u <user> -- <python> -c "import chatd"` (cwd = app root) and `runuser -u <user> -- test -w <data>`; on failure abort and print what to change (move the app to `/opt/free-chat`, or use `--user <your user>`; on RHEL/Fedora with SELinux enforcing the app must live outside `/home`). `uninstall` never deletes the user or data and prints `userdel freechat` as an optional manual step.
* `systemctl daemon-reload && systemctl enable --now freechat`. Firewall: if `ufw` is active (`ufw status | head -1` starts with `Status: active`, needs root) → `ufw allow <port>/tcp`; elif `firewall-cmd --state` prints `running` → `--add-port` permanent + reload; else print a note; `uninstall` removes the rule recorded in the state file. `logs` → `journalctl -u freechat -n N --no-pager`.
  Tests parse the unit with `configparser.RawConfigParser(strict=False)` (default interpolation chokes on `%`, strict mode rejects repeated keys).

### 10.4 macOS
* `/Library/LaunchDaemons/com.freechat.server.plist` written with `plistlib`: `Label`, `RunAtLoad=true`, `KeepAlive=true`, `ThrottleInterval=10` (≥ 10 avoids a hot crash loop on configuration errors), `WorkingDirectory`, `UserName`/`GroupName` = `--user`/`SUDO_USER` (never root), `StandardOutPath`/`StandardErrorPath` = `<data>/logs/launchd.log` (pre-created and chowned to the service user),
  `EnvironmentVariables{PYTHONUNBUFFERED=1, PYTHONUTF8=1, PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin}`, `SoftResourceLimits{NumberOfFiles=8192}` and `HardResourceLimits{NumberOfFiles=8192}` (the launchd default of 256 is below the WS cap), and `ProgramArguments` = `[/usr/bin/caffeinate, -i, <python>, -X, utf8, -I, <app>/server.py, serve, …]` (without `caffeinate` when `--allow-sleep`).
* The plist must be `root:wheel` mode `0644` (`chown`/`chmod` after writing) or `launchctl bootstrap` fails with "Bootstrap failed: 5: Input/output error". Sequence: `launchctl bootout system/com.freechat.server` (ignore rc), `launchctl enable system/com.freechat.server`, `launchctl bootstrap system /Library/LaunchDaemons/com.freechat.server.plist`. `start` = enable + bootstrap; `stop` = bootout; `restart` = `launchctl kickstart -k system/com.freechat.server`;
  `status` parses `launchctl print system/com.freechat.server` (`state = running`, `pid =`) plus `/healthz`; fall back to `load -w`/`unload -w` only when `bootstrap` is unknown (pre-10.10). `logs` = `tail -n N <data>/logs/launchd.log` plus `freechat.log`.
* **Interpreter:** never execute `/usr/bin/python3` unless `xcode-select -p` succeeds (the Command Line Tools shim pops an install dialog or exits 1). Candidate order: `--python`, `/Library/Frameworks/Python.framework/Versions/3.*/bin/python3`, `/opt/homebrew/bin/python3.*`, `/usr/local/bin/python3.*`, `/usr/bin/python3`; Apple's LibreSSL build has no `hashlib.scrypt` (the PBKDF2 fallback covers it).
* **TCC:** a daemon running as a normal user is denied `~/Desktop`, `~/Documents`, `~/Downloads`, iCloud Drive and removable/network volumes. The installer refuses (exit 2) unless `--force` when the app or data dir lies under those, or under `/Volumes/`, and recommends `/usr/local/freechat` or `/Users/Shared/FreeChat`.
* **Application Firewall:** run `/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate`; if enabled, as root `--add <real binary>` then `--unblockapp <real binary>` and verify with `--getappblocked`. The real binary is `os.path.realpath(sys.executable)` (for framework builds the Mach-O inside `Python.app/Contents/MacOS/Python`). Print that this step exists because a daemon cannot show the "Allow incoming connections" prompt.

---------------------------------------------------------------------------------------------------

## 11. Security checklist (must hold; the review stage verifies each)
1. No SQL string-building from user input (parameters only; `LIKE` escape with `ESCAPE '\'`).
2. Authorization on **every** request/event: membership + visibility (`history_from_id`, `cleared_before_id`, hidden) + role checks; `msg.*` ids from other chats are rejected (`not_found`, not `forbidden`, to avoid existence oracles).
3. Sessions: random 256-bit tokens, only hashes stored, HttpOnly + SameSite=Strict cookie, revoke on logout/password change/disable, WS re-validated on connect and every 5 min (disabled/revoked ⇒ kick).
4. WebSocket: Origin check, bounded frames/queues, per-user connection cap, no unbounded memory per connection.
5. Uploads: size cap enforced while streaming; filename sanitised; stored under random id without extension; served with forced download/sandbox CSP/nosniff; SVG/HTML never inline; per-user concurrent upload cap (3).
6. Static server: traversal-proof; no directory listing; no dotfiles; only files under `web/`.
7. XSS: UI never injects HTML from data; CSP forbids inline scripts/styles; links `rel=noopener`.
8. Brute force: login throttling; scrypt cost; generic errors; constant-time compares.
9. DoS: header/body limits & timeouts, max request in flight, regex linear-time, rate limits, bounded in-memory structures (typing, throttle maps are pruned).
10. Secrets: no tokens/passwords/bodies in logs; `data/` readable only by the service user (best effort `chmod 700` on POSIX).
11. Information disclosure: error messages generic for auth; stack traces only in logs.
12. Admin protections: last-admin invariants; admin actions authorised server-side regardless of UI.

---------------------------------------------------------------------------------------------------

## 12. Testing requirements (`tests/`, stdlib `unittest`, run with each of Python 3.11 / 3.12 / 3.13)
* `tests/wsclient.py`: minimal blocking-or-asyncio WebSocket client (masking, fragmentation, ping/pong, close) + helper `ChatSession` that logs in via HTTP (`urllib`/raw socket) and exposes `request(t,d)` / `wait_event(name, pred, timeout)`.
* Each suite boots the real server in a background thread/process on **port 0 / a free port** with a temp data dir (never touches `data/` or port 8765/9009).
* Required coverage: HTTP (static + MIME + traversal attempts + headers + Range + 304), auth (register/first-admin/login/throttle/logout/password change/cookie flags/CSRF/origin),
  upload/download (limits, names, access rule, forced-download types), WebSocket framing edge cases, protocol happy paths **and** error paths for every request in §7.4,
  delivery state machine (offline recipient → ✓ → connects → ✓✓ → reads → blue; group aggregation; read-receipts off), unread/mentions/read_sync, typing relay + expiry + disconnect stop, presence + last-seen privacy,
  idempotent `client_id`, history paging/`around_id`, edit/delete windows, reactions, forward, search escaping, permission matrix (non-member, removed member, disabled user, non-admin admin calls), last-admin invariants,
  rate limits, reconnect (new `ev.ready`), graceful shutdown, `doctor`, config precedence, installer dry-run output for `--target windows|linux|macos` (XML parses with `xml.etree`, plist with `plistlib`, unit with `configparser`),
  and a static UI lint (`tests/test_ui_static.py`: all relative imports resolve, no forbidden DOM sinks, no external URLs, no inline scripts/styles, every `socket.request('x.y'` type exists in `hub.py`, every `ev.*` handled in the store exists in §7.3).
* JS syntax is checked with `node --check` (copy `.js` → temp `.mjs` first) in a test that is skipped when `node` is absent.

---------------------------------------------------------------------------------------------------

## 13. Out of scope / future
Audio/video calls (WebRTC works on a LAN but needs a secure context), end-to-end encryption, web-push when the browser is closed (needs an internet push service), profile photos, message threads/channels,
SSO/LDAP, mobile native apps, federation. The protocol leaves room (`ev.*` names are namespaced; `d` is extensible — clients ignore unknown fields/events).

## Spec changelog
* v1 — initial.
