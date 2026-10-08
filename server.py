#!/usr/bin/env python3
"""DeskTalk LAN server - entry point.

    python server.py                       # all interfaces, port 9009
    python server.py --password secret     # require a shared password
    python server.py --help

The implementation lives in the ``desktalk.server`` package.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from desktalk.server.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
