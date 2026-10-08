# DeskTalk — LAN Real-Time Chat

Real-time chat over local network (LAN). Written in pure Python using **standard library only** — no `pip install` required.
Runs seamlessly across Windows, macOS, and Linux (Ubuntu).

```
┌─────────────┐        TCP 9009         ┌─────────────┐
│   Your PC   │ ◄─────────────────────► │  server.py  │  ← on any one machine
│ 172.31.1.143│                         │ (hub)       │
└─────────────┘                         └─────────────┘
┌─────────────┐                               ▲
│ Peer's PC   │ ──────────────────────────────┘
└─────────────┘        UDP 9010 = auto-discovery
```

---

## Setup (3 Steps)

### 1. Run the server on one machine

On your PC (e.g., IP `172.31.1.143`):

```bash
python server.py
```

The output will display your IP address — share this address with your peers:

```
  DeskTalk server running: 'DESKTOP-XYZ room'
  TCP port: 9009
  Share this address with others:
      172.31.1.143:9009
```

> The server runs on **one** machine only. All other users connect as clients.
> You can also run a client on the same machine hosting the server.

### 2. Run the client on all machines

GUI (recommended):

```bash
python chat_gui.py
```

Click **LAN scan** in the window — the server will be detected automatically — then click **Connect**.
Or directly specify the server IP:

```bash
python chat_gui.py --host 172.31.1.143 --name pravin
```

Terminal client:

```bash
python chat_cli.py --host 172.31.1.143 --name pravin
```

```bash
python chat_cli.py --scan
```

### 3. Allow through Firewall (Server machine)

If connection fails, it is usually due to firewall settings blocking the ports.

**Windows** (PowerShell, as Administrator):

```powershell
New-NetFirewallRule -DisplayName "DeskTalk TCP" -Direction Inbound -Protocol TCP -LocalPort 9009 -Action Allow
```

```powershell
New-NetFirewallRule -DisplayName "DeskTalk UDP discovery" -Direction Inbound -Protocol UDP -LocalPort 9010 -Action Allow
```

**Ubuntu**:

```bash
sudo ufw allow 9009/tcp && sudo ufw allow 9010/udp
```

**macOS**: When Python requests incoming connections for the first time, click **Allow**.

---

## In-Chat Commands

| Command | Description |
|---|---|
| `/dm <name> <message>` | Send a private message to a user |
| `/who` | List online users |
| `/quit` | Exit chat |

In GUI, **double-click** any username in the right panel user list to pre-fill `/dm`.

---

## Project Structure

```
server.py  chat_gui.py  chat_cli.py     thin entry-point wrappers (python server.py ...)
pyproject.toml                          packaging, console scripts, ruff config
desktalk/
  protocol.py     wire format (newline-delimited JSON), limits, input validation
  errors.py       DeskTalkError / ProtocolError / StoreError
  log.py          logging setup (--verbose, --log-file)
  store.py        chat history: thread-safe SQLite with RAM fallback
  discovery.py    LAN scan + probe/reply format (malformed replies are ignored)
  client.py       ChatClient: background thread, auto-reconnect, heartbeat
  argtypes.py     shared argparse helpers
  server/
    hub.py          ChatServer: join/auth, routing, history, presence
    connection.py   per-client send queue (a slow client cannot block others)
    responder.py    UDP discovery responder
    app.py          argument parsing, startup, graceful shutdown (python -m desktalk.server)
  ui/
    gui.py          Tkinter GUI
    cli.py          terminal client
    common.py       command parsing shared by both
tests/              run from the repo root: python -m unittest discover -s tests -v
```

---

## Requirements

- Python 3.8+
- On Ubuntu (for GUI): `sudo apt install python3-tk`
- All machines must be on the **same LAN / subnet**

---

## Password, History, Reconnect

**Shared Password** (optional) — requires users to enter password before joining:

```bash
python server.py --password secret          # or: set DESKTALK_PASSWORD=secret
python chat_gui.py --password secret        # GUI has a "Password" entry box
python chat_cli.py --host 172.31.1.143 --password secret
```

LAN scan shows `[password required]` for password-protected servers. Incorrect password denies connection attempts.

**History** — public messages are saved to SQLite database file (`desktalk.db`, next to `server.py`).
Newly joined users receive the last 30 messages even after a server restart. To change database path: `--db path.db`.
`--db :memory:` = disable persistent storage (RAM only). If Python lacks the `sqlite3` module, server automatically degrades to RAM history with a warning.

**Auto-reconnect** — Clients automatically reconnect upon WiFi or network drops (exponential backoff: 1s, 2s, 4s … up to 10s) and fetch missed messages without duplication. Heartbeat runs every 15s; if no response within 45s, connection is considered dead. Fatal errors like incorrect password or username already taken will not attempt auto-reconnect.

---

## Error Handling & Logs

- Server and clients use `logging`: `-v/--verbose` enables debug logs, `--log-file chat.log` also writes to a file.
- Hostile or malformed input (bad JSON, deeply nested JSON, lone-surrogate emoji, wrong field types,
  messages over 4000 characters) only gets that client an error — the connection and other users are unaffected.
- A bug in a message handler is logged, the client receives an "internal error" reply, and the server keeps running.
- Port already in use: a clear message and exit code 1 instead of a traceback. On Ctrl+C every client is told
  the server is shutting down (and their clients reconnect automatically when it is back).
- GUI: connecting happens in the background (no frozen window), Tk callback errors are shown in the chat window,
  emoji are handled safely, and the chat log is trimmed to 2000 lines.

---

## Command-Line Options

```bash
python server.py --port 9009 --name "Dev team room"
python server.py --db D:/chat/history.db
python server.py --no-discovery          # disable UDP broadcast discovery
python server.py -v --log-file chat.log  # debug logs + log file
python server.py --host 172.31.1.143     # bind to specific network interface
```

---

## Troubleshooting

**"Failed to connect"**
1. Check if `server.py` is running on the server host machine.
2. Ping the server host from client PC: `ping 172.31.1.143`
3. Ensure firewall rules are added (see step 3).
4. Verify both machines are on the same subnet (`172.31.1.x`).

**"LAN scan finds no servers"**
Some office networks block UDP broadcast packets. Type the server IP directly to connect via TCP.

**"Name already in use"**
Another connected user is using that name. Change name using `--name`.

---

## Security & Scope Note

DeskTalk is built specifically for trusted local network (LAN) environments.
The shared password serves as an entry gate — **traffic is not encrypted**:
Payload data (including password and messages) is transmitted as plaintext JSON.
Do not expose the server port directly to the internet (avoid router port forwarding).
If required, TLS support (`ssl` module) and per-user authentication can be added.

