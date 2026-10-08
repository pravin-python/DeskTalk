# DeskTalk — LAN Real-Time Chat

A modern, internal **WhatsApp / Teams-style chat** for office local networks (LAN). Works **100% offline with no internet connection**.

Written in pure Python using **standard library only** (no `pip install` required for the server). One machine runs the server, and everyone else connects directly using any modern web browser on desktop or mobile.

```
┌─────────────────┐        HTTP / WebSocket (8765)        ┌─────────────────┐
│ Browser Client  │ ◄───────────────────────────────────► │    server.py    │
│ (Desktop/Phone) │                                       │ (chatd engine)  │
└─────────────────┘                                       └─────────────────┘
```

---

## Key Features

- 🌐 **Web-Based Client**: Pure HTML5/CSS/JS ES modules — no build step, no npm, no external CDNs.
- 🔒 **Zero Internet / Standalone**: Runs completely offline inside your office LAN/VLAN.
- ⚡ **Pure Standard Library Server**: Built with Python 3.8+ `asyncio` and `sqlite3` (WAL mode).
- 💬 **Rich Messaging**: Direct messages, group chats ("Everyone" & custom groups), mentions, replies, reactions, and pinned messages.
- 📁 **File & Media Sharing**: Upload images, audio notes, video, and documents with built-in preview and storage quotas.
- 🔑 **Account & Admin Roles**: Username/password accounts, one-time setup code, workspace administration, and audit logs.
- 🛠️ **Cross-Platform Service**: Native installer for Windows Task Scheduler, Linux systemd, and macOS launchd.
- 🔐 **Security & TLS**: Hashed passwords (scrypt), HttpOnly session cookies, CSRF/CSWSH protection, path traversal defenses, and optional self-signed TLS (`--tls`).

---

## Quick Start

### 1. Start the Server

On the host machine:

```bash
python server.py
# or
python -m chatd serve
```

On first run, the server prints a **one-time setup code** (also saved in `data/setup_code.txt`):

```
  DeskTalk server running on port 8765
  Setup Code: XXXXXXXX
  Open in browser: http://<your-lan-ip>:8765
```

### 2. Connect from Any Browser

Open `http://<server-ip>:8765` on your PC, laptop, or phone connected to the office Wi-Fi.

- **First user**: Enter the setup code printed by the server to create the **Admin account**.
- **Team members**: Register using the join code (if enabled by admin) or log in to start chatting.

---

## CLI Commands (`python -m chatd <command>`)

DeskTalk provides a suite of administrative and maintenance CLI commands:

| Command | Description |
|---|---|
| `serve` | Run the chat server (default port `8765`, host `0.0.0.0`) |
| `create-admin <username>` | Create or promote a user to admin (rescue path / CLI admin setup) |
| `reset-password <username>` | Reset a user's password and revoke active sessions |
| `backup [--out DIR] [--with-uploads]` | Take a consistent snapshot of the SQLite database and uploads |
| `restore <path>` | Restore database and uploads from a backup snapshot |
| `doctor` | Run system diagnostics (Python 3.8+, SQLite >= 3.24, ports, WAL, permissions) |
| `--version` | Print app and schema version |

### Examples:

```bash
# Run server on custom port with TLS
python -m chatd serve --port 8765 --name "Engineering Team" --tls

# Run system diagnostic doctor
python -m chatd doctor

# Reset admin password
python -m chatd reset-password admin
```

---

## Service Installation (Run on Boot)

To run DeskTalk as a background system service that starts automatically on boot:

```bash
# Run as administrator / root
python service/install_service.py install
```

Supported platforms:
- **Windows**: Windows Task Scheduler (`DeskTalk` task)
- **Linux**: systemd unit (`desktalk.service`)
- **macOS**: launchd daemon (`com.desktalk.server.plist`)

To check status or uninstall:
```bash
python service/install_service.py status
python service/install_service.py uninstall
```

---

## Configuration Options

Configuration resolution order: **CLI Flags > Environment Variables (`DESKTALK_*`) > `data/config.json` > Defaults**.

| Key | Flag | Environment Variable | Default | Description |
|---|---|---|---|---|
| `host` | `--host` | `DESKTALK_HOST` | `0.0.0.0` | Bind address |
| `port` | `--port` | `DESKTALK_PORT` | `8765` | TCP port |
| `data_dir` | `--data-dir` | `DESKTALK_DATA_DIR` | `<app>/data` | Directory for DB, uploads, logs & TLS |
| `workspace_name` | `--name` | `DESKTALK_NAME` | `DeskTalk` | Workspace title |
| `registration_open` | `--registration` / `--no-registration` | `DESKTALK_REGISTRATION` | `false` | Allow open user registration |
| `max_upload_mb` | `--max-upload-mb` | `DESKTALK_MAX_UPLOAD_MB` | `100` | Max file upload size (MB) |
| `tls` | `--tls` / `--no-tls` | `DESKTALK_TLS` | `false` | Enable HTTPS / WSS |
| `log_level` | `--log-level` | `DESKTALK_LOG` | `INFO` | Logging level (`DEBUG`, `INFO`, etc.) |

---

## Firewall Rules

Ensure port **8765** is allowed inbound on the server host machine.

**Windows (PowerShell as Administrator):**
```powershell
New-NetFirewallRule -DisplayName "DeskTalk TCP" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
```

**Linux (Ubuntu / ufw):**
```bash
sudo ufw allow 8765/tcp
```

---

## Repository Structure

```
desktalk/
├── README.md                      # Project documentation
├── server.py                      # Entry point wrapper
├── pyproject.toml                 # Package configuration & ruff settings
├── requirements.txt               # Dependencies declaration (stdlib only)
├── docs/
│   ├── SPEC.md                    # Core specification & contract
│   ├── DB_API.md                  # Database API (core, users, auth, admin)
│   ├── DB_API_chat.md             # Database API (chats, messages, receipts)
│   └── ui-core-api.md             # Frontend core API documentation
├── chatd/                         # Python server engine
│   ├── __main__.py                # CLI dispatch & boot wrapper
│   ├── config.py                  # Configuration loader
│   ├── db.py                      # Database facade & connection manager
│   ├── db_users.py                # Users, sessions, admin & audit log
│   ├── db_chats.py                # Chats, members & direct/group rules
│   ├── db_messages.py             # Messages, reactions, stars, pins & search
│   ├── db_receipts.py             # Delivery & read receipts, watermarks
│   ├── auth.py                    # Password hashing & session tokens
│   ├── http.py                    # HTTP/1.1 web server & static router
│   ├── websocket.py               # RFC6455 WebSocket engine
│   ├── files.py                   # File storage, content sniffing & sanitizing
│   ├── tlsutil.py                 # Self-signed TLS certificate manager
│   ├── app.py                     # Server wiring & background tasks
│   ├── hub.py                     # Real-time WebSocket event dispatcher
│   ├── maintenance.py             # Admin CLI commands implementation
│   └── doctor.py                  # Diagnostic & environment checker
├── web/                           # Client web app (HTML/CSS/JS ES modules)
│   ├── index.html                 # Main web application UI
│   ├── css/                       # Modular CSS stylesheets
│   └── js/                        # ES module JavaScript app logic
├── service/
│   └── install_service.py         # Cross-platform background service installer
├── tests/                         # Automated unit & integration tests
└── legacy/                        # Legacy v1 terminal & Tkinter client code
```

---

## Requirements

- **Python 3.8+** (supports 3.8 through 3.13)
- **SQLite 3.24+** (included in standard Python builds)
- No external `pip` dependencies needed for the server.

---

## Running Tests

To run the full unit and integration test suite:

```bash
python -m unittest discover -s tests -v
```

---

## Security & Scope Note

DeskTalk is engineered for local network (LAN) environments:
- **Authentication**: Passwords are saved using `scrypt` / `PBKDF2-HMAC-SHA256`.
- **Session Tokens**: Stored as SHA256 hashes in DB and passed via HttpOnly `SameSite=Strict` cookies.
- **Traffic Encryption**: Plain HTTP transmits network traffic unencrypted over LAN. For networks where traffic privacy is required, start the server with `--tls` to enable TLS (HTTPS/WSS).
- **Internet Exposure**: DeskTalk is designed for LAN use. Do not expose the port directly to the public internet without proper firewall controls or VPN access.


