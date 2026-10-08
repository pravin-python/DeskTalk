#!/usr/bin/env python3
"""DeskTalk terminal client - entry point.

    python chat_cli.py --help

The implementation lives in ``desktalk.ui.cli``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from desktalk.ui.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
