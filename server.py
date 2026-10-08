#!/usr/bin/env python3
"""DeskTalk server entry point (thin wrapper, SPEC §1).

    python server.py serve --port 8765
    python server.py doctor
    python server.py --help

Services run `python -X utf8 -I server.py serve ...` (isolated mode), so the app
directory is put on sys.path explicitly instead of relying on the script dir.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chatd.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
