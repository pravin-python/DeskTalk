# DeskTalk 2 - LAN chat for an office, no internet needed

DeskTalk is an internal "WhatsApp + Teams" for a company network. One PC runs the server; everybody else opens
`http://<server-ip>:8765` in a browser - desktop or phone on the office Wi-Fi. There is nothing to install on the
clients, nothing talks to the internet, and the server is **Python standard library only** (no `pip install`).

```
  phone / laptop / desktop               the server PC
  +-------------------+   HTTP + WebSocket   +--------------------------------+
  |  any modern       | <------------------> |  python server.py serve        |
  |  browser          |       port 8765      |  chatd/  (asyncio, SQLite WAL) |
  +-------------------+                      |  web/    (served as-is)        |
                                             |  data/   (database, uploads)   |
                                             +--------------------------------+
```

* Direct chats, groups, an "Everyone" channel, replies, reactions, mentions, pins, stars, edit/delete, forwarding,
  delivery and read ticks, typing indicators, online/last seen, plain `LIKE` search, file/image/voice sharing.
* Accounts with passwords (scrypt), a one-time setup code for the first admin, optional self-registration with a
  join code, an admin screen (users, workspace name, audit log, "Add employees").
* Runs as a service: Windows Task Scheduler, Linux systemd or macOS launchd, started at boot by `service/install_service.py`.
* Optional HTTPS with a self-signed certificate (`--tls`). Backups, restore and a `doctor` command for diagnosis.
* Documentation of record is `docs/SPEC.md`; this file is the operator's guide.

The first generation of this project (a Tkinter client with a line-JSON protocol on ports 9009/9010) is kept, unchanged
and unused, in [`legacy/`](legacy/).

---------------------------------------------------------------------------------------------------------------

## 1. Requirements

| | |
|---|---|
| Server | Python **3.8 - 3.13** with a working `sqlite3` module (SQLite **3.24 or newer**) and `ssl`. Windows 10/11, Ubuntu/Debian (and other systemd distributions), macOS. Nothing else. |
| Clients | Chrome/Edge 108+, Firefox 101+, Safari/iOS 15.4+ (anything older shows "Please update your browser"). |
| Network | One PC with a fixed address (static IP or a DHCP reservation). Clients on the same LAN/Wi-Fi/VLAN. |

`python -m chatd doctor` (section 6) tells you in seconds whether a machine meets the requirements. Some Python builds
ship without `sqlite3` (a known case: a broken Windows install); `doctor` reports that and the server refuses to start
with an explanation instead of a traceback.

## 2. Quick start (try it on one PC)

```bash
python server.py serve            # or:  python -m chatd serve
```

The server prints where it listens and - until the first account exists - a **one-time setup code**:

```
DeskTalk 2.0.0  (Python 3.12.0, Windows)
Data dir: E:\desktalk\data
Share this link (open it in a browser on any device on this network):
    http://192.168.1.20:8765/
On this PC: http://localhost:8765/
SETUP CODE for the first admin account: Ab3dEf9k
Open http://127.0.0.1:8765/ and create the admin account.
Plain HTTP: anyone on this network can read passwords and messages. Consider --tls.
```

1. On the server PC open `http://localhost:8765/`, enter the setup code and create the **admin account**. The code is
   also saved in `<data>/setup_code.txt` and is deleted as soon as the first account exists.
   (Alternative that needs no code and no browser: `python -m chatd create-admin alice`.)
2. Give everybody else the "Share this link" URL. Colleagues are created in **Admin -> Users -> Add employees**
   (they get a temporary password and must change it at the first sign-in) or register themselves when the admin
   opened registration (**Admin -> Workspace**; self-registration needs the join code shown there and is **closed by default**).
3. Stop the server with Ctrl+C (graceful: connections are closed, the database is checkpointed).

Without `--data-dir` a hand-started server keeps its data in `<app>/data` (never committed to git). Installed
services keep it outside the application folder (section 5).

## 3. Phones, browsers and notifications - what really works

Everything needed for chatting works over plain HTTP: messages, receipts, typing, in-page toasts, tab-title and
favicon badge, sounds. **Operating-system notifications do not**, because browsers treat `http://<lan-ip>` as an
*insecure context*. This table is the honest support matrix (SPEC 9.4):

| Capability | Plain `http://<lan-ip>:8765` | What unlocks it |
|---|---|---|
| In-page toast, tab-title and favicon badge, chime (after one click/tap) | Works on desktop while the tab exists and is not discarded; on phones only while the page is in the foreground | - |
| OS notification (`Notification` API) | **Not available in any browser** (Android Chrome never supports `new Notification()`, iOS tabs have no such API) | a secure context: `http://localhost` on the server PC itself, `https` with a certificate the browser trusts or has been told to accept (`--tls`), or the enterprise policy below |
| Alert while the browser is closed, the phone is locked or the app is in the background | **Impossible** | needs a push service or a native app (out of scope) |

Practical advice:

* **Pin the tab** and stop the browser from freezing it: Chrome *Settings -> Performance -> Always keep these sites active*;
  Edge *Settings -> System and performance -> Never put these sites to sleep*. Memory Saver / Sleeping Tabs otherwise freeze a hidden tab.
* **On the server PC itself** use `http://localhost:8765` - that is a secure context, so notifications, clipboard and the
  microphone work there without HTTPS.
* **Office PCs: Chrome/Edge policy.** The enterprise policy `OverrideSecurityRestrictionsOnInsecureOrigin` (a list; one entry
  per origin) makes `http://<server-ip>:8765` a secure context and unlocks notifications, clipboard, `crypto.randomUUID` and
  the voice-note recorder. Per PC (elevated prompt), then restart the browser:
  ```
  reg add "HKLM\SOFTWARE\Policies\Google\Chrome\OverrideSecurityRestrictionsOnInsecureOrigin" /v 1 /t REG_SZ /d "http://192.168.1.20:8765" /f
  reg add "HKLM\SOFTWARE\Policies\Microsoft\Edge\OverrideSecurityRestrictionsOnInsecureOrigin" /v 1 /t REG_SZ /d "http://192.168.1.20:8765" /f
  ```
  In a domain use a Group Policy. Chrome shows "managed by your organization" afterwards; check `chrome://policy`.
* **Phones**: use *Add to Home Screen* (iOS Share menu, Android Chrome menu) for a full-screen app without the address bar. Keep the
  screen on and the app in the foreground to receive messages live; reconnecting after the phone slept is automatic and
  shows one summary toast ("N new messages in M chats").
* A phone on a guest Wi-Fi or another VLAN can only connect if that network is allowed to reach the server PC on the chat port.

## 4. HTTPS (`--tls`) and trusting the certificate

`python server.py serve --tls` (or `tls: true` in `config.json`, or `DESKTALK_TLS=1`) serves HTTPS with a **self-signed**
certificate created with the `openssl` command line (OpenSSL 1.1.1+/3.x/LibreSSL; Git for Windows ships one). Files
live in `<data>/tls/`: `cert.pem`, `key.pem` (mode 0600 / data-dir ACL), `meta.json`. The certificate covers the host
name, `localhost`, `127.0.0.1` and every LAN address; it is **recreated automatically when the PC gets a new address**
or has fewer than 30 days left (the key is reused, the previous files stay as `*.old`). If `openssl` is missing the
server logs a warning and keeps serving plain HTTP - `python -m chatd doctor` shows which.

```bash
python -m chatd tls-init            # create/refresh the certificate now, print names and SHA-256 fingerprint
python -m chatd tls-init --force    # regenerate even if the current one is fine
```

A self-signed certificate makes every browser show a one-time warning per device and per address. Either click through
it ("Advanced -> Proceed") or install `cert.pem` as trusted; copy it from the server by USB stick or a shared folder (the
web server deliberately never serves it). Compare the fingerprint shown by the browser with `tls-init`/`doctor` first.

* **Windows PC:** `certutil -addstore -f Root cert.pem` in an elevated prompt (Trusted Root Certification Authorities), restart the browser.
* **macOS:** open the file in Keychain Access, set *Always Trust* (or `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain cert.pem`).
* **iOS/iPadOS:** send the file (rename to `cert.crt`), tap it, *Install Profile*, then *Settings -> General -> About -> Certificate Trust Settings* and enable full trust.
* **Android:** *Settings -> Security -> Encryption & credentials -> Install a certificate*; some Android/Chrome versions do not accept a non-CA
  leaf certificate this way - then accepting the browser warning is the dependable path.

Browsers differ in what they allow after you click through a warning (notifications may stay blocked); installing the
certificate as trusted is the reliable way to get a secure context. `--redirect-port 80` (ports below 1024 need privileges) starts a tiny plain-HTTP listener that only
answers `301 https://...` so people can type the bare address. A local certificate authority (one trust step per device
instead of one warning per address) is not part of version 2.

## 5. Running DeskTalk as a service (installer)

`service/install_service.py` is one file, standard library only, for Windows (Task Scheduler), Linux (systemd) and macOS
(launchd). Run it with the Python you want the service to use, from an **elevated** shell
(Windows: *Start -> PowerShell -> right-click -> Run as administrator*; Linux/macOS: `sudo`).

```text
python service/install_service.py install   [--port N] [--host H] [--data-dir D] [--python PATH] [--name NAME] [--user USER]
                                            [--tls | --no-tls] [--redirect-port P] [--allowed-host H]... [--allow-sleep]
                                            [--no-firewall] [--allow-from localsubnet|CIDR[,CIDR]|any] [--firewall-profile private,domain|any]
                                            [--run-as-system] [--harden] [--admin USER [--password-stdin]] [--force]
                                            [--dry-run] [--target windows|linux|macos] [--elevate]
python service/install_service.py uninstall [--keep-firewall] [--dry-run]
python service/install_service.py start | stop | restart | status
python service/install_service.py logs [-n 100]
python service/install_service.py print-config [--target windows|linux|macos]
python service/install_service.py cli -- <chatd arguments>
```

What `install` does, in order: validates the Python (it needs `sqlite3` >= 3.24 and must be able to `import chatd`; it searches for a
suitable one when yours is not), creates the data directory with restrictive permissions, with `--tls` runs
`python -m chatd tls-init` as the service account, optionally creates the first admin (it asks "Create the admin account now? [Y/n]", or use `--admin alice --password-stdin`
in scripts), writes the service definition, opens the firewall, starts the service and waits up to 15 s for `/healthz`.
It then prints the URLs and - if no admin exists yet - the setup code. Exit codes: `0` ok, `1` error, `2` usage/privilege/refused
check, `3` the server did not become healthy (the last log lines are printed). `--dry-run` prints every command and file
and changes nothing (add `--target` to preview another operating system).

| | Windows | Linux | macOS |
|---|---|---|---|
| Service | Task Scheduler task `DeskTalk`, **starts at boot, before anyone logs in** | `/etc/systemd/system/desktalk.service` | `/Library/LaunchDaemons/com.desktalk.server.plist` |
| Runs as | `NT AUTHORITY\LOCAL SERVICE` (`--run-as-system` for SYSTEM) | `--user`, `SUDO_USER`, or a new system user `desktalk` (never root) | `--user` / `SUDO_USER` (never root) |
| Default data dir | `%ProgramData%\DeskTalk\data` | `/var/lib/desktalk` | `/Library/Application Support/DeskTalk/data` |
| Firewall | rule `DeskTalk`, profiles `private,domain`, local subnet only | `ufw allow` or `firewall-cmd` when active | Application Firewall entry for the interpreter |
| Keep awake | the server holds a keep-awake request | `systemd-inhibit` wrapper | `caffeinate -i` wrapper |

* **`status`** prints the service manager's view plus the real health check; **`logs`** prints the tail of `desktalk.log` and `boot.log`
  (journal on Linux). Both work without elevation where the OS allows it.
* **`cli -- ...`** runs `python -m chatd ...` as the *service identity* with the installed options, which is what you need when the
  data directory belongs to the service account: `python service/install_service.py cli -- create-admin alice`,
  `cli -- reset-password alice`, `cli -- backup`, `cli -- doctor`.
* **`--elevate`** (Windows) re-launches the command through a UAC prompt in a separate window and shows its output afterwards.
  It cannot be combined with `--password-stdin`.
* **`--harden`** (Windows): the installer refuses to run a service whose code any local user could replace - if `Users`, `Everyone`
  or `Authenticated Users` can write to the application folder, the Python folder or the data folder it stops (exit 2) and
  prints the `icacls` command that fixes it. `--harden` applies that command for you. A checkout on a plain `E:\` drive
  fails the check until hardened (default ACLs on non-system drives grant Authenticated Users *modify*).
* **State file**: `%ProgramData%\DeskTalk\install.json` (Linux `/etc/desktalk/install.json`, macOS
  `/Library/Application Support/DeskTalk/install.json`) records the chosen options. Re-running `install` reuses every option you do not
  repeat and prints what changes; `uninstall` removes exactly the firewall rule recorded there. **`uninstall` never deletes your data**;
  it lists what is left (data folder, logs, TLS key, the `desktalk` user, Python) with copy-paste removal commands.
* Re-running `install` is safe (idempotent): it updates the definition and restarts the service.

**Upgrading**

1. `python service/install_service.py stop`
2. Replace only `chatd/`, `web/`, `service/`, `docs/` and `server.py` - never `data/`.
3. `python service/install_service.py install` (reuses the saved options, re-validates the interpreter, restarts). The server writes
   `backups/pre-migrate-v<old>-<time>.db` before it migrates the database schema.
4. `python -m chatd doctor` (or `... service/install_service.py cli -- doctor`).

## 6. Command line reference

`python -m chatd <command>` (also `python server.py <command>`; with no command it means `serve`).

| Command | What it does |
|---|---|
| `serve` | Run the server. One instance per data dir (a second one prints "already running (pid N, port P)" and exits 73). |
| `create-admin USER [--password-stdin]` | Create an admin account or promote an existing user. The rescue path for a forgotten admin password. Prompts for the password (hidden) unless `--password-stdin`. |
| `reset-password USER [--password-stdin] [--must-change]` | Set a new password and sign the user out everywhere. |
| `backup [--out DIR] [--with-uploads]` | Consistent snapshot of the database (safe while the server runs) into `<backup dir>/chat-YYYYmmdd-HHMMSS.db`; with `--with-uploads` a folder that also holds `uploads/`. Prints the path. |
| `restore PATH` | Replace the database by a snapshot (or a `--with-uploads` folder). Refuses while the server runs; parks the old `chat.db`/`-wal`/`-shm` in `backups/pre-restore-<time>/` and checks integrity. **Never copy a snapshot over `chat.db` by hand** while an old `-wal`/`-shm` exists - they would be replayed onto it. |
| `tls-init [--force]` | Create or refresh the self-signed certificate (section 4). |
| `doctor` | Diagnose the installation (below). |
| `--version` | Version and supported database schema. |

All commands take the configuration flags of section 8 (`--data-dir`, `--port`, ...). They print a message and a non-zero exit code,
never a traceback, for the expected failures. **Exit codes:** `0` ok, `1` runtime error, `2` usage / invalid configuration value,
`73` another instance holds the data dir, `78` environment or configuration problem (sqlite3 missing or too old, data dir
unusable, WAL not possible, database newer than this program). Every failure is also appended to `<data>/logs/boot.log`.

**`doctor`** prints a table of `PASS`/`WARN`/`FAIL`/`INFO` rows and exits `0` only when the server can run (`78` for environment failures,
`1` for other failures such as a port taken by another program). It works even when `sqlite3` is missing - that is one of the things it reports. It checks:

* Python version and path, `sqlite3` (FAIL below 3.24), `scrypt`, `ssl`, the `openssl` binary;
* the data dir: writable, `chat.db`/`-wal`/`-shm` writable *by the current account* (otherwise it names the owner and tells you to run
  from an elevated shell, as the service account, or via `install_service.py cli -- doctor`), free disk space, and **locations SQLite WAL cannot
  use** (UNC shares, network drives, OneDrive/Dropbox/iCloud folders = FAIL);
* Windows: the data folder must not be readable by Users/Everyone/Authenticated Users (FAIL); the app and Python folders must not be writable by them (WARN);
* open-file limit (POSIX), the port (free, or "running (version, pid)" read from `control/server.lock`, or which program owns it),
  the LAN addresses and the link to share, firewall state, the Windows sleep timeouts, the TLS certificate (fingerprint, names, expiry);
* application and schema version, journal mode, integrity, backups, the setup code while the first admin is missing, and the **effective
  configuration with the source of every value** (`flag`, `env`, `file`, `default`, or `db` when an admin changed it in the UI).

## 7. Updating, backups and crash safety

* The server writes an automatic backup about every 20 hours of uptime (`backups/auto-*.db`, newest 7 kept; your manual `chat-*` backups are never
  pruned). A PC that is switched off at night still gets a backup the next day. Copy `backups/` somewhere else - a disk failure takes both.
* The database uses SQLite WAL with `synchronous=FULL`. A power cut, a crash or a hard stop is recovered automatically at the next start; nothing
  acknowledged to a user is lost. On Windows the supported stop is `install_service.py stop` (it asks the server to stop gracefully first).
* To move DeskTalk to another PC: stop it, copy the whole data directory (or `backup --with-uploads`), install on the new PC with the same `--data-dir`
  content, start, then `doctor`.

## 8. Configuration

Precedence: **command-line flag > `DESKTALK_*` environment variable > `<data-dir>/config.json` > default.** The data dir itself is decided first
(flag, then `DESKTALK_DATA_DIR`, then `<app>/data`) because that is where `config.json` lives. Booleans accept `1|true|yes|on` / `0|false|no|off`.
Boolean flags come in pairs (`--registration/--no-registration`, `--tls/--no-tls`) so a flag can override a file in either direction.
`workspace_name` and `registration_open` (and the join code) are editable by an admin in the UI and then stored in the database, which wins over file/env;
`serve` logs a warning and `doctor` shows source `db` in that case. Unknown keys in `config.json` are warned about, not fatal.

| Key (`config.json`) | Flag | Environment | Default |
|---|---|---|---|
| `host` | `--host` | `DESKTALK_HOST` | `0.0.0.0` |
| `port` | `--port` | `DESKTALK_PORT` | `8765` (`0` = any free port) |
| (data dir) | `--data-dir` | `DESKTALK_DATA_DIR` | `<app>/data` (the installer picks an OS location) |
| `workspace_name` | `--name` | `DESKTALK_NAME` | `DeskTalk` |
| `registration_open` | `--registration` / `--no-registration` | `DESKTALK_REGISTRATION` | `false` |
| `max_users` | - | - | `2000` |
| `max_upload_mb` | `--max-upload-mb` | `DESKTALK_MAX_UPLOAD_MB` | `100` |
| `blocked_extensions` | - | `DESKTALK_BLOCKED_EXT` | `exe scr com pif bat cmd msi msp vbs vbe wsf wsh hta lnk reg cpl dll jar` |
| `tls` | `--tls` / `--no-tls` | `DESKTALK_TLS` | `false` |
| `redirect_port` | `--redirect-port` | `DESKTALK_REDIRECT_PORT` | `0` (off) |
| `allowed_hosts` | `--allowed-host` (repeatable) | `DESKTALK_ALLOWED_HOSTS` (comma list) | `[]` |
| `allow_sleep` | `--allow-sleep` | `DESKTALK_ALLOW_SLEEP` | `false` (the server keeps the PC awake) |
| `backup_dir` | `--backup-dir` | `DESKTALK_BACKUP_DIR` | `<data>/backups` |
| `edit_window_s` / `delete_window_s` | - | - | `900` (15 min) / `172800` (48 h) |
| `max_body_chars` | - | - | `8000` |
| `session_days` | - | - | `30` |
| `min_password_len` | - | - | `8` |
| `log_level` | `--log-level` | `DESKTALK_LOG` | `INFO` |

`allowed_hosts` extends the Host-name allow-list that blocks DNS-rebinding: IP addresses, `localhost`, this PC's name and names ending in
`.local .lan .internal .home.arpa .corp` are accepted by default; add the DNS name you use for the server (a wrong Host gives `421 host_not_allowed`).
`scrypt_n`, `test_scale` and `test_limits` exist for the test suite only and are ignored unless `DESKTALK_TEST=1`.

```json
{ "workspace_name": "Acme Office", "max_upload_mb": 50, "allowed_hosts": ["chat.acme.corp"], "tls": true }
```

## 9. What is stored where (data directory)

```
<data>/
  chat.db  chat.db-wal  chat.db-shm   the SQLite database (WAL mode)
  config.json                          optional settings (section 8)
  setup_code.txt                       one-time admin code; deleted once the first account exists
  uploads/<aa>/<id>                    uploaded files under random ids (uploads/.tmp = in-flight)
  backups/                             auto-*.db (newest 7), chat-*.db (manual), pre-migrate-*, pre-restore-*
  logs/                                desktalk.log (rotating), boot.log (startup failures), launchd.log (macOS)
  tls/                                 cert.pem  key.pem  meta.json  (+ *.old)
  control/                             server.lock  stop.request  reload  pending-delete.txt
```

It is created with mode `0700` (POSIX, umask 077) or an ACL that admits only SYSTEM, Administrators and the service account (Windows).
Anyone who can read it can read every message: **messages are not end-to-end encrypted** and an administrator of the server PC can always read them.

## 10. Security notes

The LAN is semi-trusted: any employee phone, guest on the Wi-Fi or compromised PC can reach the port.

* **Plain HTTP is unencrypted.** Anyone on the same network segment can read passwords, messages and the session cookie, or rewrite pages in
  flight. That is acceptable on a wired, trusted VLAN only. **Use `--tls` and trust the certificate** (section 4). The login screen shows a warning over plain HTTP to a non-local address.
* Defended: unauthenticated hosts (setup code, join code, login throttling per address/user), cross-site and DNS-rebinding pages in an employee's
  browser (Host allow-list, `Origin`/`Sec-Fetch-Site` checks, `X-Requested-With`), malicious members (every request is authorised against membership and role
  inside one transaction), uploads that try to run in the browser (sniffed types, forced download/sandbox CSP, blocked executable extensions), path traversal, resource exhaustion (size, rate and connection limits).
* Passwords are hashed with scrypt (`n=2^16`; PBKDF2 where scrypt is unavailable); sessions are random 256-bit tokens, only their hashes are stored; sessions
  and sockets are revoked immediately on logout, password change or disabling a user.
* **Not** defended: someone with OS-level access to the server PC or its data folder; passive sniffing while running plain HTTP.
* The Windows service runs as `LocalService`, and the installer refuses a layout where ordinary users could edit the code that runs at boot (section 5).

## 11. Windows notes

* **Sleep.** A sleeping PC is a stopped chat. The server asks Windows to stay awake while it runs, but lid-close and manual sleep still stop it.
  Set the power plan yourself (the installer only *shows* these, `doctor` warns when they are not set):
  `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`.
* **Task Scheduler.** The task has no time limit, normal priority, restarts on failure, and a 5-minute *watchdog* trigger that restarts it if it is not running
  (a second instance exits at once with 73 on the instance lock). `Stop` disables the task so the watchdog cannot revive it, asks the server to stop through
  `control/stop.request`, waits up to 10 s for the port to close and only then ends the task.
* **Python.** Use a machine-wide Python (e.g. *Install for all users*); a per-user Python under `C:\Users\...` and the Microsoft Store alias are refused.
  Always `python.exe`, never `pythonw.exe` (the server logs to a file; a console is not needed).
* **Permissions.** See `--harden` above. The data folder grants access to SYSTEM, Administrators and `LocalService` only, so `python -m chatd ...` from a normal prompt
  cannot open it: use `install_service.py cli -- ...` from an elevated shell.
* **Firewall.** The rule covers *private* and *domain* networks and the local subnet. If Windows classifies your office network as **Public**, phones cannot connect:
  `Set-NetConnectionProfile -NetworkCategory Private`, or reinstall with `--firewall-profile any` (or `--allow-from` a CIDR list for several VLANs).
* **Antivirus and file locks.** Deleting or replacing files that Defender or Explorer is scanning is retried automatically; leftovers are swept hourly.

## 12. Troubleshooting

Start with `python -m chatd doctor` (or `python service/install_service.py cli -- doctor` for an installed service) and read the FAIL/WARN rows.

| Symptom | Likely cause and fix |
|---|---|
| Phones/PCs cannot open the link | Wrong address (use "Share this link", not `localhost`); Windows firewall rule missing or the network is *Public* (section 11); guest Wi-Fi isolating clients; VPN/virtual adapter picked - `doctor` lists every address. |
| The address changed after a reboot | Give the server PC a static IP or a DHCP reservation. With `--tls` the certificate is renewed automatically; devices must trust it again. |
| "Please update your browser" | The browser is older than the floors in section 1 (no `dvh` units / WebSocket / Unicode property escapes). |
| "This connection is not encrypted" banner | Expected on plain HTTP. Use `--tls`, or an office-wide rule that the chat is wired-only. |
| Browser says "Not secure" / certificate warning | Expected without `--tls` / with the self-signed certificate (section 4). |
| No desktop notifications | Plain HTTP cannot show them (section 3). Use `localhost`, `--tls` with a trusted certificate, or the Chrome/Edge policy. Also check the browser's site settings. |
| Alerts stop when the phone is locked or the tab is in the background | A browser limit, not a bug; there is no push service offline. |
| `already running on this data dir (pid N, port P)` (exit 73) | The service or another copy runs. `install_service.py status`, or `stop` it first. |
| Exit 78 / "sqlite3 ... too old or missing" | Install a full Python 3.8+ build (python.org) with SQLite >= 3.24; `doctor` shows the version found. systemd does not restart-loop on 78; the Windows watchdog retries every 5 minutes, so read the log. |
| Port already in use | `doctor` names the program using it (via `netstat`/`ss`/`lsof`); stop it or choose `--port`. Ports below 1024 need root/`CAP_NET_BIND_SERVICE` on Linux. |
| Windows service "Ready" but nothing listens | `install_service.py logs`; open `<data>\logs\boot.log`; check the Python path (`status` says "interpreter missing") and the ACL check (`--harden`). |
| Service stays down after `stop` | `stop` disables the task on purpose; `start` (or `install`) re-enables it. |
| Data dir "not writable" / `PermissionError` from the CLI | It belongs to the service account: run from an elevated shell or use `install_service.py cli -- ...` (the message names the owner). |
| `421 host_not_allowed` | You reached the server through a DNS name the allow-list does not know: `--allowed-host chat.acme.corp`. |
| Upload fails | Over `max_upload_mb`, blocked extension (`exe`, `bat`, ...), per-user quota (500 MB unattached, 2 GiB per 24 h) or less than max(2 GiB, 5%) free disk. |
| Lost the admin password | `python -m chatd create-admin <that user>` (service: `install_service.py cli -- create-admin <user>`) sets a new password and signs them out everywhere. |
| Lost the setup code | `<data>/setup_code.txt` (only while no account exists), `doctor`, or skip it with `create-admin`. |
| Messages missing after restore | A restore returns to the snapshot's state; `backups/pre-restore-<time>/` holds what it replaced. |
| `doctor` says WAL FAIL | The data folder is on a network share or in OneDrive/Dropbox. Move it to a local disk and `--data-dir` there (copy the folder while stopped). |

## 13. Project layout

```
server.py                    thin entry point (puts the app folder on sys.path, runs chatd)
chatd/                       the server package (python -m chatd)
  __main__.py config.py      command line, configuration loading
  app.py http.py websocket.py   lifecycle and background tasks, HTTP/1.1 server, RFC 6455
  hub.py api.py auth.py      realtime protocol, REST handlers, passwords/sessions/throttles
  db*.py maintenance.py      SQLite layer (writer thread + readers), CLI business logic and its SQL
  files.py tlsutil.py util.py  uploads, self-signed certificate, shared helpers
  doctor.py                  the `doctor` command
web/                         the browser client: plain HTML/CSS/JS modules, no build step, no CDN
service/install_service.py   service installer (Windows / Linux / macOS)
tests/                       unittest suites (see below)
docs/SPEC.md                 the contract every module is written against
docs/DB_API*.md  docs/ui-core-api.md   cross-module function tables
legacy/                      the first generation (Tkinter client, line-JSON on ports 9009/9010) - frozen, unused
```

## 14. Development and tests

Everything is standard library, so there is nothing to set up. The suites boot the real server on **port 0** with a temp data dir and a lowered
scrypt cost (`DESKTALK_TEST=1`); they never touch `data/` or port 8765. Use Python 3.11, 3.12 or 3.13 (the suite runs on all three):

```bash
python -m unittest discover -s tests -t . -v                 # everything (takes a few minutes)
python -m unittest tests.test_doctor_checks tests.test_doctor_probe     # one area
python -m unittest discover -s tests -t . -k installer       # filter by name
python -m ruff check chatd service tests                     # lint (config in pyproject.toml)
```

Rules the code follows (SPEC section 0, enforced by the suite): valid Python 3.8 syntax, standard library only, `encoding="utf-8"` on every `open()`, no blocking calls on the
event loop, no SQL string-building from user input, no external URLs in the client. The service installer is tested without installing anything
(`--dry-run`, fake registries and scripted command output); the only way to exercise a real installation is to run it on a test machine.

## 15. Out of scope

Audio/video calls, end-to-end encryption, web-push or any alert while the browser is closed, profile photos, threads/channels, SSO/LDAP, native mobile apps and
federation (SPEC section 13).
