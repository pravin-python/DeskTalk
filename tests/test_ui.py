import unittest

from desktalk.ui.common import DM_USAGE, parse_input


class ParseInputTests(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(parse_input(""))
        self.assertIsNone(parse_input("   "))

    def test_commands(self):
        self.assertEqual(parse_input("/quit").kind, "quit")
        self.assertEqual(parse_input("/exit").kind, "quit")
        self.assertEqual(parse_input("/who").kind, "who")
        self.assertEqual(parse_input("/users").kind, "who")

    def test_dm(self):
        cmd = parse_input("/dm bob hello there  ")
        self.assertEqual((cmd.kind, cmd.to, cmd.text), ("dm", "bob", "hello there"))

    def test_dm_usage_errors(self):
        for bad in ("/dm", "/dm ", "/dm bob", "/dm   bob   "):
            cmd = parse_input(bad)
            self.assertEqual((cmd.kind, cmd.error), ("error", DM_USAGE), msg=bad)

    def test_plain_text_and_unknown_slash(self):
        self.assertEqual(parse_input(" hi ").text, "hi")
        self.assertEqual(parse_input("/shrug").kind, "say")
        self.assertEqual(parse_input("/dmx foo").kind, "say")


try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None


@unittest.skipIf(tk is None, "tkinter not installed")
class GuiSmokeTests(unittest.TestCase):
    """Drive ChatWindow without a server: feed it events and make sure nothing raises."""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest("no display: {}".format(exc))
        from desktalk.ui.gui import ChatWindow
        self.win = ChatWindow(self.root, "127.0.0.1", 9009, "me", "pw")

    def tearDown(self):
        self.root.destroy()

    def text(self):
        return self.win.text.get("1.0", "end")

    def test_event_rendering_including_emoji(self):
        w = self.win
        w.client = type("C", (), {"host": "h", "port": 1, "close": lambda s: None})()
        for ev in [
            {"type": "welcome", "room": "r", "users": ["me"]},
            {"type": "msg", "user": "x", "text": "hi \U0001F600 there", "ts": 1.0},
            {"type": "dm", "user": "x", "to": "me", "text": "psst", "ts": 1.0},
            {"type": "system", "text": "x aaya"},
            {"type": "error", "text": "bad"},
            {"type": "disconnected", "reconnecting": True},
            {"type": "reconnecting", "attempt": 1, "delay": 1.0},
            {"type": "welcome", "room": "r", "users": ["me", "x"], "rejoined": True},
            {"type": "users", "users": None},                       # malformed on purpose
            {"type": "msg", "user": 5, "text": None, "ts": "junk"},  # malformed on purpose
        ]:
            w.handle(ev)
        out = self.text()
        self.assertIn("hi � there", out)   # emoji replaced, no TclError
        self.assertIn("[DM from x]", out)
        self.assertEqual(out.count("Online: me, x"), 1)  # the rejoin notice, wording-independent

    def test_stale_events_from_old_client_are_ignored(self):
        w = self.win
        old, current = object(), object()
        w.client = current
        w.events.put((old, {"type": "system", "text": "STALE"}))
        w.events.put((current, {"type": "system", "text": "FRESH"}))
        w._drain_events()
        self.assertNotIn("STALE", self.text())
        self.assertIn("FRESH", self.text())

    def test_one_bad_event_does_not_stop_the_pump(self):
        w = self.win
        w.client = object()
        w.events.put((w.client, {"type": "msg", "ts": object()}))   # blows up inside handle
        w.events.put((w.client, {"type": "system", "text": "after"}))
        w._drain_events()
        self.assertIn("after", self.text())

    def test_log_is_trimmed(self):
        from desktalk.ui import gui
        for i in range(gui.MAX_LINES + 50):
            self.win.sys_line("line {}".format(i))
        count = int(self.win.text.index("end-1c").split(".")[0])
        self.assertLessEqual(count, gui.MAX_LINES + 1)
        self.assertIn("line {}".format(gui.MAX_LINES + 49), self.text())

    def test_callback_errors_are_shown_not_raised(self):
        self.win._on_callback_error(RuntimeError, RuntimeError("kaboom"), None)
        self.assertIn("kaboom", self.text())

    def test_send_without_connection_is_reported(self):
        before = self.text()
        self.win.entry.insert(0, "hello")
        self.win.on_send()
        self.assertNotEqual(self.text(), before)       # the user is told something
        self.assertEqual(self.win.entry.get(), "hello")  # and the typed text is not lost

    def test_invalid_input_before_connect_shows_warning_not_crash(self):
        from unittest import mock
        with mock.patch("desktalk.ui.gui.messagebox") as mb:
            self.win.host_var.set("")
            self.win.toggle_connect()
            self.win.host_var.set("127.0.0.1")
            self.win.name_var.set("has space")
            self.win.toggle_connect()
            self.win.name_var.set("ok")
            self.win.port_var.set("99999")
            self.win.toggle_connect()
        self.assertEqual(mb.showwarning.call_count, 3)
        self.assertIsNone(self.win.client)


if __name__ == "__main__":
    unittest.main()
