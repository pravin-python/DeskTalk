#!/usr/bin/env python3
"""FreeChat - Tkinter GUI client.

    python chat_gui.py
    python chat_gui.py --host 172.31.1.143 --name pravin

Tkinter Python ke saath hi aata hai (Ubuntu pe: sudo apt install python3-tk).
"""

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from freechat import discovery  # noqa: E402
from freechat.client import ChatClient  # noqa: E402
from freechat.protocol import DEFAULT_PORT, fmt_time  # noqa: E402

BG = "#11141a"
PANEL = "#1a1f28"
FG = "#e6e9ef"
MUTED = "#8b93a7"
ACCENT = "#5aa9ff"
SELF = "#7ee787"
DMC = "#d98cff"
ERR = "#ff7b72"


class ChatWindow:
    def __init__(self, root, host, port, name):
        self.root = root
        self.client = None
        self.events = queue.Queue()
        self.me = ""

        root.title("FreeChat - LAN")
        root.geometry("860x600")
        root.minsize(620, 420)
        root.configure(bg=BG)

        self._build_connect_bar(host, port, name)
        self._build_chat_area()
        self._build_input_bar()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(60, self._drain_events)

    # ---------- layout ----------

    def _build_connect_bar(self, host, port, name):
        bar = tk.Frame(self.root, bg=PANEL, padx=10, pady=8)
        bar.pack(fill="x")

        def label(text):
            tk.Label(bar, text=text, bg=PANEL, fg=MUTED).pack(side="left", padx=(0, 4))

        label("Server IP")
        self.host_var = tk.StringVar(value=host)
        tk.Entry(bar, textvariable=self.host_var, width=16, bg=BG, fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left", padx=(0, 10))

        label("Port")
        self.port_var = tk.StringVar(value=str(port))
        tk.Entry(bar, textvariable=self.port_var, width=6, bg=BG, fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left", padx=(0, 10))

        label("Naam")
        self.name_var = tk.StringVar(value=name)
        tk.Entry(bar, textvariable=self.name_var, width=14, bg=BG, fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left", padx=(0, 10))

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

    def _build_chat_area(self):
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

    def _build_input_bar(self):
        bar = tk.Frame(self.root, bg=BG, padx=10, pady=10)
        bar.pack(fill="x")
        self.entry = tk.Entry(bar, bg=PANEL, fg=FG, insertbackground=FG, relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.entry.bind("<Return>", self.on_send)
        tk.Button(bar, text="Send", command=self.on_send, bg=ACCENT, fg="#06131f",
                  relief="flat", padx=18, activebackground="#7fbcff").pack(side="left")

    # ---------- output ----------

    def write(self, parts):
        """parts = [(text, tag), ...]"""
        self.text.configure(state="normal")
        for chunk, tag in parts:
            self.text.insert("end", chunk, tag)
        self.text.insert("end", "\n")
        self.text.configure(state="disabled")
        self.text.see("end")

    def sys_line(self, text):
        self.write([(text, "sys")])

    # ---------- actions ----------

    def toggle_connect(self):
        if self.client:
            self.disconnect("Tumne disconnect kiya.")
            return

        host = self.host_var.get().strip()
        name = self.name_var.get().strip()
        if not host or not name:
            messagebox.showwarning("FreeChat", "Server IP aur naam dono bharo.")
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showwarning("FreeChat", "Port number hona chahiye.")
            return

        self.status.configure(text="connecting...", fg=MUTED)
        client = ChatClient(host, port, name, self.events.put)
        try:
            client.connect()
        except OSError as e:
            self.status.configure(text="offline", fg=MUTED)
            messagebox.showerror("Connect nahi hua", "{}:{} se connect nahi hua.\n\n{}".format(host, port, e))
            return

        self.client = client
        self.me = name
        self.connect_btn.configure(text="Disconnect", bg="#6b7280")
        self.status.configure(text="connected to {}:{}".format(host, port), fg=SELF)
        self.entry.focus_set()

    def disconnect(self, reason):
        if self.client:
            self.client.close()
            self.client = None
        self.connect_btn.configure(text="Connect", bg=ACCENT)
        self.status.configure(text="offline", fg=MUTED)
        self.userlist.delete(0, "end")
        self.sys_line("-- " + reason)

    def scan_lan(self):
        self.scan_btn.configure(state="disabled", text="scanning...")
        self.sys_line("-- LAN pe servers dhoond raha hoon...")

        def work():
            servers = discovery.scan(timeout=1.5)
            self.events.put({"type": "_scan_result", "servers": servers})

        threading.Thread(target=work, daemon=True).start()

    def on_user_double_click(self, _event):
        sel = self.userlist.curselection()
        if not sel:
            return
        user = self.userlist.get(sel[0])
        self.entry.delete(0, "end")
        self.entry.insert(0, "/dm {} ".format(user))
        self.entry.focus_set()

    def on_send(self, _event=None):
        text = self.entry.get().strip()
        if not text:
            return
        if not self.client:
            self.sys_line("-- pehle connect karo.")
            return
        self.entry.delete(0, "end")

        if text.startswith("/dm "):
            rest = text[4:].strip()
            if " " not in rest:
                self.sys_line("-- use: /dm <naam> <message>")
                return
            to, body = rest.split(" ", 1)
            self.client.dm(to, body)
        elif text in ("/who", "/users"):
            self.client.who()
        elif text == "/quit":
            self.disconnect("Tumne disconnect kiya.")
        else:
            self.client.say(text)

    # ---------- event pump (background thread -> Tk main thread) ----------

    def _drain_events(self):
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(60, self._drain_events)

    def handle(self, ev):
        kind = ev.get("type")
        stamp = fmt_time(ev["ts"]) if ev.get("ts") else ""

        if kind == "welcome":
            self.sys_line("-- '{}' me aa gaye. Online: {}".format(
                ev.get("room", "room"), ", ".join(ev.get("users", [])) or "sirf tum"))
            self.set_users(ev.get("users", []))

        elif kind == "msg":
            user = ev.get("user", "?")
            mine = user == self.me
            self.write([
                (stamp + " ", "time"),
                (user + ": ", "me" if mine else "other"),
                (ev.get("text", ""), "body"),
            ])

        elif kind == "dm":
            sender, to = ev.get("user", "?"), ev.get("to", "?")
            label = "[DM -> {}]".format(to) if sender == self.me else "[DM from {}]".format(sender)
            self.write([(stamp + " ", "time"), (label + " ", "dm"), (ev.get("text", ""), "body")])

        elif kind == "system":
            self.sys_line("-- " + ev.get("text", ""))

        elif kind == "users":
            self.set_users(ev.get("users", []))

        elif kind == "error":
            self.write([("!! " + ev.get("text", ""), "err")])

        elif kind == "disconnected":
            self.disconnect("Server se connection toot gaya.")

        elif kind == "_scan_result":
            self.scan_btn.configure(state="normal", text="LAN scan")
            servers = ev.get("servers", [])
            if not servers:
                self.sys_line("-- koi server nahi mila. Manual IP daalo.")
                return
            for s in servers:
                self.sys_line("   mila: {}:{}  ({}, {} online)".format(
                    s["host"], s["port"], s["room"], s["users"]))
            first = servers[0]
            self.host_var.set(first["host"])
            self.port_var.set(str(first["port"]))
            self.sys_line("-- pehla server bhar diya, ab Connect dabao.")

    def set_users(self, users):
        self.userlist.delete(0, "end")
        for u in users:
            self.userlist.insert("end", u)

    def on_close(self):
        if self.client:
            self.client.close()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description="FreeChat GUI client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default=os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    args = ap.parse_args()

    root = tk.Tk()
    ChatWindow(root, args.host, args.port, args.name)
    root.mainloop()


if __name__ == "__main__":
    main()
