#!/usr/bin/env python3
"""DeskTalk Tkinter GUI client - entry point.

    python chat_gui.py --help

The implementation lives in ``desktalk.ui.gui``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from desktalk.ui.gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
