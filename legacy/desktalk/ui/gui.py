"""DeskTalk Tkinter GUI client.

    python chat_gui.py
    python chat_gui.py --host 172.31.1.143 --name pravin --password secret

Tkinter ships with Python (Ubuntu: ``sudo apt install python3-tk``).

Threading model: network threads never touch widgets. They put ``(source, event)``
tuples on a queue; the Tk main loop drains it every POLL_MS. ``source`` is the
ChatClient that produced the event (None for internal events), which lets us
ignore leftovers from a client the user has already disconnected.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Optional, Sequence, Tuple

from .. import __version__, discovery
from ..argtypes import port_number
from ..client import ChatClient
from ..errors import ProtocolError
from ..log import setup_logging
from ..protocol import (
    DEFAULT_PORT,
    EV_DISCONNECTED,
    EV_RECONNECTING,
    T_DM,
    T_ERROR,
    T_MSG,
    T_SYSTEM,
    T_USERS,
    T_WELCOME,
    Message,
    clean_name,
    fmt_time,
)
from .common import default_username, parse_input

log = logging.getLogger(__name__)

BG = "#11141a"
PANEL = "#1a1f28"
FG = "#e6e9ef"
MUTED = "#8b93a7"
ACCENT = "#5aa9ff"
SELF = "#7ee787"
DMC = "#d98cff"
ERR = "#ff7b72"

POLL_MS = 60
MAX_EVENTS_PER_TICK = 200   # keep the UI responsive even if a flood arrives
MAX_LINES = 2000            # chat log is trimmed to this many lines
SCAN_TIMEOUT = 1.5

# internal (non-network) events
_EV_CONNECTED = "_connected"
_EV_CONNECT_FAILED = "_connect_failed"
_EV_SCAN_RESULT = "_scan_result"

QueueItem = Tuple[Optional[ChatClient], Message]


def tk_safe(text: str) -> str:
    """Tcl 8.6 (Python <= 3.12) raises TclError on characters above U+FFFF (emoji)."""
    if all(ord(ch) <= 0xFFFF for ch in text):
        return text
    return "".join(ch if ord(ch) <= 0xFFFF else "�" for ch in text)


class ChatWindow:
    def __init__(self, root: tk.Tk, host: str, port: int, name: str, password: str = "") -> None:
        self.root = root
        self.client: Optional[ChatClient] = None
        self.me = ""
        self._connecting = False
        self.events: "queue.Queue[QueueItem]" = queue.Queue()

        root.title("DeskTalk - LAN")
        root.geometry("860x600")
        root.minsize(620, 420)
        root.configure(bg=BG)
        root.report_callback_exception = self._on_callback_error  # Tk swallows these otherwise

        self._build_connect_bar(host, port, name, password)
        self._build_chat_area()
        self._build_input_bar()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(POLL_MS, self._drain_events)

    # ---------- layout ----------

    def _build_connect_bar(self, host: str, port: int, name: str, password: str) -> None:
        bar = tk.Frame(self.root, bg=PANEL, padx=10, pady=8)
        bar.pack(fill="x")

        def label(text: str) -> None:
            tk.Label(bar, text=text, bg=PANEL, fg=MUTED).pack(side="left", padx=(0, 4))

        def entry(var: tk.StringVar, width: int, **kw: Any) -> None:
            tk.Entry(bar, textvariable=var, width=width, bg=BG, fg=FG, insertbackground=FG,
                     relief="flat", **kw).pack(side="left", padx=(0, 10))

        label("Server IP")
        self.host_var = tk.StringVar(value=host)
        entry(self.host_var, 16)

        label("Port")
        self.port_var = tk.StringVar(value=str(port))
        entry(self.port_var, 6)

        label("Name")
        self.name_var = tk.StringVar(value=name)
        entry(self.name_var, 14)

        label("Password")
        self.pass_var = tk.StringVar(value=password)
        entry(self.pass_var, 10, show="*")

        self.connect_btn = tk.Button(bar, text="Connect", command=self.toggle_connect,
                                     bg=ACCENT, fg="#06131f", relief="flat",
                                     activebackground="#7fbcff", padx=14)
        self.connect_btn.pack(side="left")

        self.scan_btn = tk.Button(bar, text="LAN scan", command=self.scan_lan,
                                  bg="#2b3242", fg=FG, relief="flat",
                                  activebackground="#3a4357", padx=10)
        self.scan_btn.pack(side="left", padx=6)

        self.status = tk.Label(bar, text="offline", bg=PANEL, fg=MUTED)
        self.status.pack(side="right")

    def _build_chat_area(self) -> None:
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(8, 0))

        self.text = tk.Text(body, bg=PANEL, fg=FG, wrap="word", relief="flat",
                            state="disabled", padx=10, pady=8,
                            font=("Consolas" if sys.platform == "win32" else "Menlo", 11))
        self.text.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(body, command=self.text.yview)
        scroll.pack(side="left", fill="y")
        self.text.configure(yscrollcommand=scroll.set)

        side = tk.Frame(body, bg=PANEL, width=160)
        side.pack(side="left", fill="y", padx=(8, 0))
        side.pack_propagate(False)
        tk.Label(side, text="  Online", bg=PANEL, fg=MUTED, anchor="w").pack(fill="x", pady=(8, 2))
        self.userlist = tk.Listbox(side, bg=PANEL, fg=FG, relief="flat",
                                   highlightthickness=0, selectbackground="#2b3242")
        self.userlist.pack(fill="both", expand=True, padx=6, pady=6)
        self.userlist.bind("<Double-Button-1>", self.on_user_double_click)

        for tag, color in (("sys", MUTED), ("me", SELF), ("other", ACCENT),
                           ("dm", DMC), ("err", ERR), ("body", FG), ("time", MUTED)):
            self.text.tag_configure(tag, foreground=color)

    def _build_input_bar(self) -> None:
        bar = tk.Frame(self.root, bg=BG, padx=10, pady=10)
        bar.pack(fill="x")
        self.entry = tk.Entry(bar, bg=PANEL, fg=FG, insertbackground=FG, relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.entry.bind("<Return>", self.on_send)
        tk.Button(bar, text="Send", command=self.on_send, bg=ACCENT, fg="#06131f",
                  relief="flat", padx=18, activebackground="#7fbcff").pack(side="left")

    # ---------- output ----------

    def write(self, parts: Sequence[Tuple[str, str]]) -> None:
        """Append one line made of (text, tag) chunks, trimming old history."""
        self.text.configure(state="normal")
        try:
            for chunk, tag in parts:
                self.text.insert("end", tk_safe(chunk), tag)
            self.text.insert("end", "\n")
            lines = int(self.text.index("end-1c").split(".")[0])
            if lines > MAX_LINES:
                self.text.delete("1.0", "{}.0".format(lines - MAX_LINES))
        finally:
            self.text.configure(state="disabled")
        self.text.see("end")

    def sys_line(self, text: str) -> None:
        self.write([(text, "sys")])

    def set_users(self, users: Any) -> None:
        self.userlist.delete(0, "end")
        if isinstance(users, list):
            for u in users:
                self.userlist.insert("end", tk_safe(str(u)))

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status.configure(text=text, fg=color)

    def _reset_connect_ui(self) -> None:
        self._connecting = False
        self.connect_btn.configure(text="Connect", bg=ACCENT, state="normal")
        self._set_status("offline")
        self.userlist.delete(0, "end")

    # ---------- actions ----------

    def toggle_connect(self) -> None:
        if self._connecting:
            return
        if self.client:
            self.disconnect("Disconnected by user.")
            return

        host = self.host_var.get().strip()
        if not host:
            messagebox.showwarning("DeskTalk", "Please enter Server IP.")
            return
        try:
            name = clean_name(self.name_var.get())
        except ProtocolError as exc:
            messagebox.showwarning("DeskTalk", str(exc))
            return
        try:
            port = port_number(self.port_var.get().strip())
        except argparse.ArgumentTypeError as exc:
            messagebox.showwarning("DeskTalk", "Invalid port: {}".format(exc))
            return

        client = ChatClient(host, port, name, lambda ev: self.events.put((client, ev)),
                            password=self.pass_var.get())
        self.client = client
        self.me = name
        self._connecting = True
        self.connect_btn.configure(text="Connecting...", state="disabled")
        self._set_status("connecting...")
        threading.Thread(target=self._connect_worker, args=(client,), name="gui-connect", daemon=True).start()

    def _connect_worker(self, client: ChatClient) -> None:
        """Connect off the UI thread so the window never freezes."""
        try:
            client.connect()
        except OSError as exc:
            self.events.put((None, {"type": _EV_CONNECT_FAILED, "client": client, "error": str(exc)}))
        except Exception as exc:  # noqa: BLE001
            log.exception("connect crashed")
            self.events.put((None, {"type": _EV_CONNECT_FAILED, "client": client, "error": str(exc)}))
        else:
            self.events.put((None, {"type": _EV_CONNECTED, "client": client}))

    def disconnect(self, reason: str) -> None:
        client, self.client = self.client, None
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                log.exception("error while closing client")
        self._reset_connect_ui()
        self.sys_line("-- " + reason)

    def scan_lan(self) -> None:
        self.scan_btn.configure(state="disabled", text="scanning...")
        self.sys_line("-- Searching for servers on LAN...")

        def work() -> None:
            try:
                servers = discovery.scan(timeout=SCAN_TIMEOUT)
            except Exception:  # noqa: BLE001 - scan() should not raise, but never leave the button stuck
                log.exception("LAN scan crashed")
                servers = []
            self.events.put((None, {"type": _EV_SCAN_RESULT, "servers": servers}))

        threading.Thread(target=work, name="gui-scan", daemon=True).start()

    def on_user_double_click(self, _event: Any = None) -> None:
        sel = self.userlist.curselection()
        if not sel:
            return
        self.entry.delete(0, "end")
        self.entry.insert(0, "/dm {} ".format(self.userlist.get(sel[0])))
        self.entry.focus_set()

    def on_send(self, _event: Any = None) -> None:
        cmd = parse_input(self.entry.get())
        if cmd is None:
            return
        if not self.client or self._connecting:
            self.sys_line("-- Connect first." if not self.client else "-- Connecting, please wait.")
            return
        self.entry.delete(0, "end")

        if cmd.kind == "error":
            self.sys_line("-- " + cmd.error)
            return
        if cmd.kind == "quit":
            self.disconnect("Disconnected by user.")
            return
        if cmd.kind == "who":
            ok = self.client.who()
        elif cmd.kind == "dm":
            ok = self.client.dm(cmd.to, cmd.text)
        else:
            ok = self.client.say(cmd.text)
        if not ok:
            self.sys_line("-- Not connected currently (reconnecting); message not sent.")

    # ---------- event pump (background threads -> Tk main thread) ----------

    def _drain_events(self) -> None:
        try:
            for _ in range(MAX_EVENTS_PER_TICK):
                try:
                    source, ev = self.events.get_nowait()
                except queue.Empty:
                    break
                if source is not None and source is not self.client:
                    continue  # leftover from a client that has been disconnected
                try:
                    self.handle(ev)
                except Exception:  # noqa: BLE001 - one bad event must not stop the pump
                    log.exception("failed to handle event %r", ev.get("type"))
        finally:
            try:
                self.root.after(POLL_MS, self._drain_events)
            except tk.TclError:
                pass  # window already destroyed

    def handle(self, ev: Message) -> None:
        kind = ev.get("type")
        stamp = fmt_time(ev["ts"]) if ev.get("ts") else ""
        text = str(ev.get("text", ""))

        if kind == T_WELCOME:
            users = ev.get("users")
            online = ", ".join(map(str, users)) if isinstance(users, list) and users else "only you"
            if ev.get("rejoined"):
                self.sys_line("-- Reconnected. Online: " + online)
                if self.client:
                    self._set_status("connected to {}:{}".format(self.client.host, self.client.port), SELF)
            else:
                self.sys_line("-- Joined '{}'. Online: {}".format(ev.get("room", "room"), online))
            self.set_users(users)

        elif kind == T_MSG:
            user = str(ev.get("user", "?"))
            self.write([
                (stamp + " ", "time"),
                (user + ": ", "me" if user == self.me else "other"),
                (text, "body"),
            ])

        elif kind == T_DM:
            sender, to = str(ev.get("user", "?")), str(ev.get("to", "?"))
            label = "[DM -> {}]".format(to) if sender == self.me else "[DM from {}]".format(sender)
            self.write([(stamp + " ", "time"), (label + " ", "dm"), (text, "body")])

        elif kind == T_SYSTEM:
            self.sys_line("-- " + text)

        elif kind == T_USERS:
            self.set_users(ev.get("users"))

        elif kind == T_ERROR:
            self.write([("!! " + text, "err")])

        elif kind == EV_RECONNECTING:
            self._set_status("reconnecting (try {}, in {:g}s)...".format(
                ev.get("attempt", 0), ev.get("delay", 0)), ERR)

        elif kind == EV_DISCONNECTED:
            if ev.get("reconnecting"):
                self.sys_line("-- Connection lost, trying to reconnect...")
                self._set_status("reconnecting...", ERR)
                self.userlist.delete(0, "end")
            else:
                self.disconnect(str(ev.get("reason") or "Connection to server lost."))

        elif kind == _EV_CONNECTED:
            if ev.get("client") is self.client:
                self._connecting = False
                self.connect_btn.configure(text="Disconnect", bg="#6b7280", state="normal")
                self._set_status("connected to {}:{}".format(self.client.host, self.client.port), SELF)
                self.entry.focus_set()

        elif kind == _EV_CONNECT_FAILED:
            if ev.get("client") is self.client:
                client, self.client = self.client, None
                self._reset_connect_ui()
                messagebox.showerror("Connection Failed", "Could not connect to {}:{}.\n\n{}".format(
                    client.host, client.port, ev.get("error", "")))

        elif kind == _EV_SCAN_RESULT:
            self._on_scan_result(ev.get("servers") or [])

    def _on_scan_result(self, servers: Sequence[discovery.ServerInfo]) -> None:
        self.scan_btn.configure(state="normal", text="LAN scan")
        if not servers:
            self.sys_line("-- No servers found. Enter manual IP.")
            return
        for s in servers:
            self.sys_line("   found: {}:{}  ({}, {} online){}".format(
                s.host, s.port, s.room, s.users, "  [password required]" if s.locked else ""))
        first = servers[0]
        self.host_var.set(first.host)
        self.port_var.set(str(first.port))
        self.sys_line("-- Selected first server. Click Connect.")

    # ---------- misc ----------

    def _on_callback_error(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Called by Tk for exceptions in widget callbacks: log, and tell the user in-window."""
        log.error("UI callback failed", exc_info=(exc_type, exc, tb))
        try:
            self.write([("!! Internal error: {}".format(exc), "err")])
        except Exception:  # noqa: BLE001 - the window itself may be what is broken
            log.debug("could not show the error in the window", exc_info=True)

    def on_close(self) -> None:
        if self.client:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                log.exception("error while closing client")
        self.root.destroy()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console entry point. Returns the process exit code."""
    ap = argparse.ArgumentParser(prog="chat_gui.py", description="DeskTalk GUI client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=port_number, default=DEFAULT_PORT)
    ap.add_argument("--name", default=default_username())
    ap.add_argument("--password", default=os.environ.get("DESKTALK_PASSWORD", ""),
                    help="server password (or env DESKTALK_PASSWORD)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--log-file", default=None, help="write logs to this file")
    ap.add_argument("--version", action="version", version="DeskTalk " + __version__)
    args = ap.parse_args(argv)

    setup_logging(verbose=args.verbose, log_file=args.log_file)
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display / broken Tk install
        print("Failed to start GUI: {}\nTry terminal client: python chat_cli.py".format(exc), file=sys.stderr)
        return 1
    ChatWindow(root, args.host, args.port, args.name, args.password)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
